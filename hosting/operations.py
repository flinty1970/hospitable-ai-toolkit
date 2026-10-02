"""Owner-only operational status and review of existing durable records.

Review dispositions never send, retry or modify a guest message.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path


def read_rows(path, sql, args=()):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Invalid state path')
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Operations:
    def __init__(self, root, aid, account):
        self.root, self.aid, self.account = Path(root), aid, account
        self.path = self.root / 'state/review-dispositions.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Invalid review path')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS dispositions (id TEXT PRIMARY KEY, account TEXT, revision TEXT, handled INTEGER, note TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS review_audit (id TEXT, account TEXT, revision TEXT, handled INTEGER, note TEXT, at REAL, actor TEXT)')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def status(self):
        from hosting.controls import Controls, control_path
        from hosting.email_setup import ready
        effective = Controls(control_path(self.root / 'state/controls.sqlite3')).effective(self.aid, self.account)
        inbox = self.root / 'state/inbox.sqlite3'
        rows = read_rows(inbox, 'SELECT state,COUNT(*) AS count,MAX(received) AS latest FROM inbox WHERE account=? GROUP BY state', (self.aid,))
        receipts = []
        tables = read_rows(inbox, "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_receipts'")
        if tables:
            receipts = read_rows(inbox, 'SELECT last_message,last_guest,last_probe,received_count FROM webhook_receipts WHERE account=?', (self.aid,))
        latest = max((r['latest'] for r in rows), default=None)
        receipt = receipts[0] if receipts else {}
        alerts = []
        for path in [inbox, self.root / 'state/scheduled-messages.sqlite3', *[Path(p['runtime_dir']) / 'state/events.sqlite3' for p in self.account['properties'].values()]]:
            if read_rows(path, "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_outbox'"):
                alerts.extend(read_rows(path, "SELECT state,COUNT(*) AS count FROM notification_outbox WHERE account=? AND channel='email' GROUP BY state", (self.aid,)))
        return {'account_id': self.aid, 'webhook_path': '/webhook/hospitable/' + self.aid,
                'last_message_received': receipt.get('last_message') or latest,
                'last_guest_message_received': receipt.get('last_guest'),
                'last_local_probe': receipt.get('last_probe'), 'received_count': receipt.get('received_count', 0),
                'inbox': rows, 'owner_alerts': alerts, 'processing_mode': effective['mode'],
                'owner_email_tested': ready(self.root, self.aid),
                'has_received_messages': bool(receipt.get('last_message') or latest)}

    def items(self):
        result = []
        def add(source, pid, identity, raw, received, message, answer, reason, state, sources=None, conversation=None):
            key = digest([self.aid, source, pid, identity])
            result.append({'id': key, 'revision': digest(raw), 'source': source, 'property_id': pid,
                           'property_name': self.account['properties'].get(pid, {}).get('name', 'Property not verified'),
                           'received': received, 'guest_message': message, 'answer': answer, 'reason': reason,
                           'state': state, 'sources': sources or [], 'conversation': conversation or {}})
        worker_ids = set()
        for pid, prop in self.account['properties'].items():
            path = Path(prop['runtime_dir']) / 'state/events.sqlite3'
            for row in read_rows(path, 'SELECT event_key,payload,result,received FROM events ORDER BY received DESC LIMIT 200'):
                payload, outcome = json.loads(row['payload']), json.loads(row['result'])
                worker_ids.add((pid, row['event_key']))
                if outcome.get('action') not in {'draft', 'review', 'send_pending'}:
                    continue
                data = payload.get('data', {})
                from hosting.guest_sending import target, SendReview
                try:
                    kind, identity = target(data)
                    conversation = {'kind': kind, 'id': identity}
                except SendReview:
                    conversation = {}
                add('property', pid, row['event_key'], row, row['received'], data.get('body') or '',
                    outcome.get('answer') or '', outcome.get('reason') or '', outcome.get('action'), outcome.get('sources'), conversation)
        for row in read_rows(self.root / 'state/inbox.sqlite3', "SELECT * FROM inbox WHERE account=? AND state='review' ORDER BY received DESC LIMIT 200", (self.aid,)):
            data = json.loads(row['payload']).get('data', {})
            if (row['property_id'], data.get('id')) in worker_ids:
                continue
            add('gateway', row['property_id'], str(row['id']), row, row['received'], data.get('body') or '', '', row['reason'] or 'Property could not be verified', 'review')
        path = self.root / 'state/scheduled-messages.sqlite3'
        for row in read_rows(path, "SELECT * FROM scheduled WHERE account=? AND state IN ('review','sending') ORDER BY created DESC LIMIT 200", (self.aid,)):
            if row['property'] not in self.account['properties']:
                continue
            add('schedule', row['property'], row['id'], row, row['created'], '', row['body'], row['reason'] or 'Delivery in progress or uncertain; inspect Hospitable before replying', row['state'], conversation={'kind': 'reservations', 'id': row['reservation']})
        with self.connect() as db:
            saved = {r[0]: r[1:] for r in db.execute('SELECT id,revision,handled,note,at FROM dispositions WHERE account=?', (self.aid,))}
        for item in result:
            value = saved.get(item['id'])
            item.update(handled=bool(value and value[0] == item['revision'] and value[1]), note=value[2] if value else '', handled_at=value[3] if value else None)
        return sorted(result, key=lambda item: item['received'], reverse=True)

    def mark(self, identity, revision, handled, note):
        if type(handled) is not bool or not isinstance(note, str) or len(note) > 1000:
            raise ValueError('Invalid review decision')
        item = next((r for r in self.items() if r['id'] == identity), None)
        if not item or item['revision'] != revision or item['state'] in {'sending', 'send_pending'}:
            raise ValueError('Record changed or delivery is in progress; refresh first')
        at = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO dispositions VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,handled=excluded.handled,note=excluded.note,at=excluded.at', (identity, self.aid, revision, int(handled), note, at))
            db.execute('INSERT INTO review_audit VALUES(?,?,?,?,?,?,?)', (identity, self.aid, revision, int(handled), note, at, 'admin-ui'))
        return {'saved': True, 'sent': False}
