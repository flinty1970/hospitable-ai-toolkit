"""Durable account webhook inbox with API-verified property dispatch.

Authentication uses the configured URL token, not an invented Hospitable
signature format. Only authenticated message.created events enter the inbox.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, Request

from hosting.config import load_registry, secret
from hosting.controls import Controls, control_path
from hosting.notifications import Outbox

MAX_BODY = 1024 * 1024
API_BASE = "https://public.api.hospitable.com/v2"


class Unresolved(Exception):
    """Keep event for operator review without forwarding to any property."""


def object_id(value):
    if isinstance(value, dict):
        value = value.get("id") or value.get("uuid")
    return str(value).strip() if value is not None else ""


def resolve_property(account, payload, get=requests.get):
    """Use this account's API as authority; never trust a payload property alone."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise Unresolved("Missing event data")
    reservation = object_id(data.get("reservation_id") or data.get("reservation"))
    inquiry = object_id(data.get("conversation_id") or data.get("conversation"))
    if reservation:
        path = "/reservations/" + quote(reservation, safe="")
    elif inquiry:
        path = "/inquiries/" + quote(inquiry, safe="")
    else:
        raise Unresolved("No reservation or inquiry identity")
    response = get(API_BASE + path, headers={
        "Authorization": "Bearer " + secret(account["api_key_env"]),
        "Accept": "application/json",
    }, timeout=20, allow_redirects=False)
    if response.status_code == 404:
        raise Unresolved("Reservation or inquiry not found in this account")
    response.raise_for_status()
    record = response.json().get("data")
    if not isinstance(record, dict):
        raise Unresolved("API returned no reservation or inquiry object")
    property_id = object_id(record.get("property_id") or record.get("property"))
    # Some API responses expose a properties array. Accept exactly one only.
    if not property_id:
        properties = record.get("properties")
        if isinstance(properties, list) and len(properties) == 1:
            property_id = object_id(properties[0])
    if property_id not in account["properties"]:
        raise Unresolved("Unresolved or unmanaged property")
    claimed = object_id(data.get("property_id") or data.get("property"))
    if claimed and claimed != property_id:
        raise Unresolved("Payload property conflicts with account API")
    return property_id


class Inbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, account TEXT NOT NULL,
                event_key TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', property_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL DEFAULT 0,
                received REAL NOT NULL, reason TEXT,
                UNIQUE(account, event_key))""")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, account, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        message_id = object_id(payload.get("data", {}).get("id"))
        key = "message:" + message_id if message_id else "payload:" + hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO inbox(account,event_key,payload,received) VALUES(?,?,?,?)",
                       (account, key, encoded, time.time()))

    def pending(self, account):
        with self.connect() as db:
            return db.execute("SELECT * FROM inbox WHERE account=? AND state='pending' AND next_attempt<=? ORDER BY id LIMIT 8",
                              (account, time.time())).fetchall()

    def update(self, event_id, state, property_id=None, reason=None, retry=False):
        with self.connect() as db:
            row = db.execute("SELECT attempts FROM inbox WHERE id=?", (event_id,)).fetchone()
            attempts = row["attempts"] + int(retry)
            delay = min(3600, 5 * 2 ** min(attempts, 10)) if retry else 0
            db.execute("UPDATE inbox SET state=?,property_id=?,reason=?,attempts=?,next_attempt=? WHERE id=?",
                       (state, property_id, reason, attempts, time.time() + delay, event_id))

    def counts(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT account,state,COUNT(*) AS count FROM inbox GROUP BY account,state")]


def deliver(inbox, account, row, expected_property=None, controls=None):
    property_id = None
    try:
        payload = json.loads(row["payload"])
        property_id = resolve_property(account, payload)
        if expected_property and property_id != expected_property:
            raise Unresolved("Property changed during dispatch; operator review required")
        if controls and controls.effective(row["account"], account, property_id)["mode"] not in {"shadow", "automatic"}:
            inbox.update(row["id"], "pending", property_id, "Processing paused by account/property controls", retry=True)
            return
        prop = account["properties"][property_id]
        response = requests.post(prop["worker_url"].rstrip("/") + "/webhook/hospitable",
                                 params={"token": secret(prop["worker_secret_env"])},
                                 json=payload, timeout=180, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError("Worker did not acknowledge with HTTP 200")
        result = response.json()
        if not result.get("ok"):
            # The existing worker can claim dedupe before processing fails.
            # Never automatically replay a reported processing failure as success.
            raise Unresolved("Worker reported processing failure; inspect property audit")
        if result.get("action") == "review":
            inbox.update(row["id"], "review", property_id, "Property worker requires operator review")
            return
        inbox.update(row["id"], "delivered", property_id)
    except Unresolved as exc:
        review_event(inbox, row, property_id, str(exc))
    except Exception:
        # Exception messages can contain URL tokens. Store only a fixed reason.
        inbox.update(row["id"], "pending", property_id, "API or worker unavailable; retry scheduled", retry=True)


def review_event(inbox, row, property_id, reason):
    outbox = Outbox(inbox.path)
    payload = json.loads(row["payload"])
    data = payload.get("data", {})
    with inbox.connect() as db:
        db.execute("UPDATE inbox SET state='review',property_id=?,reason=? WHERE id=?", (property_id, reason, row["id"]))
        outbox.enqueue("gateway:" + str(row["id"]), row["account"], property_id,
                       {"account_id": row["account"], "property_id": property_id,
                        "reason": reason, "guest_message": str(data.get("body") or "")[:1000]}, db=db)


def create_app(registry_path=None, inbox_path=None):
    accounts = load_registry(registry_path or os.environ["TOOLKIT_ACCOUNTS_FILE"])
    inbox = Inbox(inbox_path or os.environ["TOOLKIT_INBOX_DB"])
    controls = Controls(control_path(inbox.path.parent / "controls.sqlite3"))
    outbox = Outbox(inbox.path)
    semaphore = asyncio.Semaphore(8)
    property_locks = {}

    async def process(account, row):
        # Concurrent properties and accounts have independent processing queues.
        # Resolving is cheap relative to a model call; preserve property order.
        async with semaphore:
            try:
                pid = await asyncio.to_thread(resolve_property, account, json.loads(row["payload"]))
            except Unresolved as exc:
                review_event(inbox, row, None, str(exc))
                return
            except Exception:
                inbox.update(row["id"], "pending", reason="Account API unavailable", retry=True)
                return
            lock = property_locks.setdefault((row["account"], pid), asyncio.Lock())
            async with lock:
                await asyncio.to_thread(deliver, inbox, account, row, pid, controls)

    async def consume(account_id, account):
        account_gate = asyncio.Semaphore(2)
        async def limited(row):
            async with account_gate:
                await process(account, row)
        while True:
            if controls.effective(account_id, account)["mode"] == "disabled":
                await asyncio.sleep(1)
                continue
            rows = inbox.pending(account_id)
            if rows:
                await asyncio.gather(*(limited(row) for row in rows))
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        async def send_alerts():
            while True:
                await asyncio.to_thread(outbox.drain, accounts, controls)
                await asyncio.sleep(1)
        tasks = [asyncio.create_task(consume(aid, account)) for aid, account in accounts.items()]
        tasks.append(asyncio.create_task(send_alerts()))
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhook/hospitable/{account_id}")
    async def receive(account_id: str, request: Request):
        account = accounts.get(account_id)
        supplied = request.query_params.get("token", "")
        if not account or not hmac.compare_digest(supplied.encode(), secret(account["webhook_secret_env"]).encode()):
            raise HTTPException(401, "Unauthorized")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Payload too large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise HTTPException(400, "Expected event object with data")
        event = payload.get("action") or payload.get("event") or payload.get("type") or payload.get("event_type")
        if event != "message.created":
            return {"ok": True, "action": "ignored", "reason": "Only message.created is supported"}
        try:
            inbox.add(account_id, payload)
        except sqlite3.Error:
            raise HTTPException(503, "Durable inbox unavailable")
        return {"ok": True, "action": "queued"}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/admin/inbox")
    async def inbox_status(request: Request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret("TOOLKIT_ADMIN_SECRET")).encode()):
            raise HTTPException(401, "Unauthorized")
        return {"counts": inbox.counts()}

    return app
