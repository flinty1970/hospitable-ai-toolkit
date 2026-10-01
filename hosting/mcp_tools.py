"""Hosted MCP operations. Every public operation rechecks its scope and role."""
import json
import sqlite3
from pathlib import Path
from hosting.access import Access
from hosting.controls import Controls
from hosting.worker import prepare_draft, retrieve_references


class HostedTools:
    def __init__(self, accounts, clients_path, controls_path, inbox_path):
        self.accounts = accounts
        self.access = Access(clients_path, accounts)
        self.controls = Controls(controls_path)
        self.inbox_path = Path(inbox_path)

    def list_access(self, token):
        _, client = self.access.identify(token)
        return [{"account_id": aid, "account_permissions": grant.get("permissions", []),
                 "properties": [{"property_id": pid, "name": self.accounts[aid]["properties"][pid]["name"],
                                 "permissions": permissions} for pid, permissions in grant.get("properties", {}).items()]}
                for aid, grant in client["accounts"].items()]

    def get_settings(self, token, account_id, property_id=None):
        self.access.require(token, account_id, property_id, "read")
        return self.controls.effective(account_id, self.accounts[account_id], property_id)

    def set_settings(self, token, account_id, property_id=None, enabled=None, shadow=None,
                     ha_enabled=None, email_enabled=None, heating_enabled=None, ha_alerts_enabled=None):
        actor = self.access.require(token, account_id, property_id, "settings")
        import os
        if os.environ.get('TOOLKIT_INSTANCE_MODE') == 'container' and any(v is not None for v in (ha_enabled, heating_enabled, ha_alerts_enabled)):
            raise ValueError('Home Assistant is not part of the community toolkit')
        from hosting.notifications import validate_channel
        account = self.accounts[account_id]
        for channel, value in (("ha", ha_alerts_enabled), ("email", email_enabled)):
            if value is True:
                validate_channel(account, property_id, channel)
        self.controls.set(actor, account_id, property_id, enabled, shadow, ha_enabled, email_enabled, heating_enabled, ha_alerts_enabled)
        # Wake retained deliveries immediately after a scope's settings change.
        if self.inbox_path.exists():
            with sqlite3.connect(self.inbox_path, timeout=10) as db:
                if property_id:
                    db.execute("UPDATE inbox SET next_attempt=0 WHERE account=? AND property_id=? AND state='pending'", (account_id, property_id))
                else:
                    db.execute("UPDATE inbox SET next_attempt=0 WHERE account=? AND state='pending'", (account_id,))
        # Wake queued alerts in the gateway and authorized property's own outbox.
        paths = [self.inbox_path]
        if property_id:
            paths.append(Path(account["properties"][property_id]["runtime_dir"]) / "state/events.sqlite3")
        else:
            paths.extend(Path(prop["runtime_dir"]) / "state/events.sqlite3" for prop in account["properties"].values())
        for path in paths:
            if path.exists():
                with sqlite3.connect(path, timeout=10) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_outbox'").fetchone():
                        if property_id:
                            db.execute("UPDATE notification_outbox SET next_attempt=0 WHERE account=? AND property=? AND state='pending'", (account_id, property_id))
                        else:
                            db.execute("UPDATE notification_outbox SET next_attempt=0 WHERE account=? AND state='pending'", (account_id,))
        return self.controls.effective(account_id, self.accounts[account_id], property_id)

    def search(self, token, account_id, property_id, query):
        self.access.require(token, account_id, property_id, "read")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query must be 1-4000 characters")
        return retrieve_references(self.accounts[account_id]["properties"][property_id], query)

    def preview(self, token, account_id, property_id, message):
        self.access.require(token, account_id, property_id, "preview")
        if not isinstance(message, str) or not message.strip() or len(message) > 10000:
            raise ValueError("message must be 1-10000 characters")
        # Explicit manual preview remains available while automatic processing is paused.
        return {**prepare_draft(self.accounts[account_id]["properties"][property_id], message), "sent": False}

    def recent_drafts(self, token, account_id, property_id, limit=20):
        self.access.require(token, account_id, property_id, "read")
        limit = max(1, min(int(limit), 100))
        path = Path(self.accounts[account_id]["properties"][property_id]["runtime_dir"]) / "state/events.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return [{"message_id": mid, "result": json.loads(result), "received": received}
                    for mid, result, received in db.execute("SELECT event_key,result,received FROM events ORDER BY received DESC LIMIT ?", (limit,))]

    def inbox_status(self, token, account_id, property_id=None):
        self.access.require(token, account_id, property_id, "read")
        if not self.inbox_path.exists():
            return []
        with sqlite3.connect(f"file:{self.inbox_path}?mode=ro", uri=True) as db:
            if property_id:
                rows = db.execute("SELECT state,COUNT(*) FROM inbox WHERE account=? AND property_id=? GROUP BY state", (account_id, property_id))
            else:
                rows = db.execute("SELECT state,COUNT(*) FROM inbox WHERE account=? GROUP BY state", (account_id,))
            return [{"state": state, "count": count} for state, count in rows]

    def notification_status(self, token, account_id, property_id=None):
        self.access.require(token, account_id, property_id, "read")
        paths = [self.inbox_path]
        if property_id:
            paths.append(Path(self.accounts[account_id]["properties"][property_id]["runtime_dir"]) / "state/events.sqlite3")
        result = []
        for path in paths:
            if not path.exists():
                continue
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_outbox'").fetchone():
                    continue
                if property_id:
                    rows = db.execute("SELECT channel,state,COUNT(*) FROM notification_outbox WHERE account=? AND property=? GROUP BY channel,state", (account_id, property_id))
                else:
                    rows = db.execute("SELECT channel,state,COUNT(*) FROM notification_outbox WHERE account=? GROUP BY channel,state", (account_id,))
                result.extend({"channel": channel, "state": state, "count": count} for channel, state, count in rows)
        return result

    def scheduled_queue(self, account_id):
        import os
        if os.environ.get('TOOLKIT_INSTANCE_MODE') != 'container':
            raise ValueError('Scheduled messages require the community container')
        from hosting.scheduled_messages import Queue
        return Queue(os.environ['TOOLKIT_DATA_DIR'], account_id, self.accounts[account_id])

    def list_reservations(self, token, account_id, property_id, page=1):
        self.access.require(token, account_id, property_id, 'read')
        from hosting.scheduled_messages import reservations
        return reservations(self.accounts[account_id], property_id, page)

    def scheduled_messages(self, token, account_id, property_id):
        self.access.require(token, account_id, property_id, 'read')
        return self.scheduled_queue(account_id).list(property_id)

    def schedule_message(self, token, account_id, property_id, reservation_id, local_time, message, confirm_send=False, fold=None):
        actor = self.access.require(token, account_id, property_id, 'schedule')
        if confirm_send is not True:
            raise ValueError('Confirm this future guest-facing send')
        return self.scheduled_queue(account_id).create(property_id, reservation_id, local_time, message, actor, fold)

    def cancel_scheduled_message(self, token, account_id, property_id, scheduled_message_id):
        actor = self.access.require(token, account_id, property_id, 'schedule')
        return self.scheduled_queue(account_id).cancel(property_id, scheduled_message_id, actor)
