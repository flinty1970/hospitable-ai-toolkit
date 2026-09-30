"""Owner-configured optional HA/msmtp alerts with a durable per-channel outbox."""
import json
import os
import re
import sqlite3
import subprocess
import time
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlparse

import requests
from hosting.config import secret


def channel_config(account, property_id, channel):
    parent = account.get("notifications", {}).get(channel, {})
    child = account.get("properties", {}).get(property_id, {}).get("notifications", {}).get(channel, {}) if property_id else {}
    return {**parent, **child}


def validate_channel(account, property_id, channel):
    config = channel_config(account, property_id, channel)
    if channel == "ha":
        url = urlparse(secret(config["webhook_url_env"]))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("HA webhook must be an operator-configured HTTP(S) URL")
    elif channel == "email":
        path = Path(config["msmtp_config_file"])
        if not path.is_absolute():
            raise ValueError("msmtp_config_file must be absolute")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", config["msmtp_account"]):
            raise ValueError("Invalid msmtp account name")
        if not config["recipients"] or not isinstance(config["recipients"], list):
            raise ValueError("Configure this owner's email recipients")
        addresses = [config["sender"], *config["recipients"]]
        for address in addresses:
            if not isinstance(address, str) or "\n" in address or "\r" in address or parseaddr(address)[1] != address or "@" not in address:
                raise ValueError("Invalid email address")
    return config


class Outbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
                event_key TEXT, channel TEXT, account TEXT, property TEXT,
                payload TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                next_attempt REAL DEFAULT 0, reason TEXT,
                PRIMARY KEY(event_key,channel))""")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, event_key, account_id, property_id, payload, db=None):
        values = [(event_key, channel, account_id, property_id or "", json.dumps(payload)) for channel in ("ha", "email")]
        sql = "INSERT OR IGNORE INTO notification_outbox(event_key,channel,account,property,payload) VALUES(?,?,?,?,?)"
        if db is not None:
            db.executemany(sql, values)
        else:
            with self.connect() as connection:
                connection.executemany(sql, values)

    def pending_rows(self):
        # Keyset pages keep memory bounded without letting paused channels hide
        # deliverable alerts behind the first page. Close reads before writes.
        last_rowid = 0
        cutoff = time.time()
        while True:
            with self.connect() as db:
                rows = db.execute("SELECT rowid AS queue_rowid,* FROM notification_outbox WHERE state='pending' AND next_attempt<=? AND rowid>? ORDER BY rowid LIMIT 100", (cutoff, last_rowid)).fetchall()
            if not rows:
                return
            last_rowid = rows[-1]["queue_rowid"]
            yield from rows

    def drain(self, accounts, controls):
        attempted = 0
        for row in self.pending_rows():
            account = accounts.get(row["account"])
            property_id = row["property"] or None
            if account is None or (property_id and property_id not in account["properties"]):
                continue
            effective = controls.effective(row["account"], account, property_id)
            channel = row["channel"]
            if not effective["enabled"] or not effective[channel + "_enabled"]:
                continue  # Paused alerts remain queued for explicit re-enabling.
            if channel == "ha" and not effective["ha_alerts_enabled"]:
                continue
            attempted += 1
            if attempted > 20:
                break
            try:
                config = validate_channel(account, property_id, channel)
                payload = json.loads(row["payload"])
                if channel == "ha":
                    from hosting.controls import require_ha
                    require_ha(controls.effective(row["account"], account, property_id))
                    response = requests.post(secret(config["webhook_url_env"]), json=payload, timeout=10, allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError("HA did not accept the alert")
                else:
                    message = EmailMessage()
                    message["From"] = config["sender"]
                    message["To"] = ", ".join(config["recipients"])
                    message["Subject"] = "Guest support requires review"
                    import hashlib
                    identity = hashlib.sha256((row["account"] + ":" + row["event_key"]).encode()).hexdigest()
                    message["Message-ID"] = f"<{identity}@hospitable-ai-toolkit.local>"
                    message.set_content(json.dumps(payload, ensure_ascii=False, indent=2))
                    subprocess.run(["/usr/bin/msmtp", "--file=" + config["msmtp_config_file"],
                                    "--account=" + config["msmtp_account"], "-f", config["sender"], "-t"],
                                   input=message.as_bytes(), capture_output=True, timeout=20, check=True)
                with self.connect() as db:
                    db.execute("UPDATE notification_outbox SET state='sent',reason=NULL WHERE event_key=? AND channel=?", (row["event_key"], channel))
            except Exception:
                # Errors can contain private webhook URLs or SMTP details.
                attempts = row["attempts"] + 1
                with self.connect() as db:
                    db.execute("UPDATE notification_outbox SET attempts=?,next_attempt=?,reason=? WHERE event_key=? AND channel=?",
                               (attempts, time.time() + min(3600, 5 * 2 ** min(attempts, 10)),
                                "Alert delivery failed; retry scheduled", row["event_key"], channel))
