import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from hosting.gateway import Inbox, create_app
from hosting.operations import Operations
from hosting.operations_ui import install


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        prop = self.root / 'properties/p'; (prop / 'state').mkdir(parents=True)
        self.account = {'id': 'owner', 'api_key_env': 'PAT', 'webhook_secret_env': 'HOOK',
                        'properties': {'p': {'name': 'House', 'timezone': 'UTC', 'runtime_dir': str(prop),
                                             'worker_url': 'http://127.0.0.1:9000', 'worker_secret_env': 'WORKER', 'model_key_env': 'MODEL'}}}
        self.env = patch.dict(os.environ, {'PAT': 'private-pat', 'HOOK': 'private-hook', 'WORKER': 'private-worker',
            'MODEL': 'private-model', 'TOOLKIT_ADMIN_SECRET': 'admin', 'TOOLKIT_CONTROLS_DB': str(self.root/'state/controls.sqlite3')}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.registry = self.root/'registry.json'; self.registry.write_text(json.dumps({'accounts': {'owner': self.account}}))
        self.inbox = Inbox(self.root/'state/inbox.sqlite3')
        self.app = create_app(self.registry, self.inbox.path); install(self.app, {'owner': self.account}, self.root)
        self.client = TestClient(self.app); self.auth = {'Authorization': 'Bearer admin'}
        self.ops = Operations(self.root, 'owner', self.account)
        self.event_path = prop/'state/events.sqlite3'
        with sqlite3.connect(self.event_path) as db:
            db.execute('CREATE TABLE events(event_key TEXT PRIMARY KEY,payload TEXT,result TEXT,received REAL)')
            db.execute('INSERT INTO events VALUES(?,?,?,?)', ('msg', json.dumps({'data': {'id': 'msg', 'body': '<script>guest</script>', 'conversation_id': 'inquiry'}}),
                json.dumps({'action': 'draft', 'answer': 'Towels in cupboard', 'reason': 'Guide', 'sources': [{'text': 'Towels in cupboard', 'source': {'source': 'guide.md'}}]}), 1))

    def test_auth_origin_private_response_and_source_snapshot(self):
        self.assertEqual(self.client.get('/admin/operations').status_code, 401)
        response = self.client.get('/admin/operations/reviews', headers=self.auth)
        self.assertEqual(response.status_code, 200); self.assertEqual(response.headers['cache-control'], 'no-store')
        item = response.json()['items'][0]; self.assertEqual(item['sources'][0]['source']['source'], 'guide.md')
        self.assertEqual(item['conversation'], {'kind': 'inquiries', 'id': 'inquiry'})
        request = {'revision': item['revision'], 'handled': True, 'confirm': True}
        self.assertEqual(self.client.post('/admin/operations/reviews/'+item['id'], headers={**self.auth, 'Origin': 'https://evil.example'}, json=request).status_code, 403)
        self.assertEqual(self.client.get('/admin/operations/reviews?property_id=other', headers=self.auth).status_code, 400)

    def test_handled_persists_audits_reopens_and_stale_revision_rejected(self):
        item = self.ops.items()[0]
        self.ops.mark(item['id'], item['revision'], True, 'Answered in Hospitable')
        reopened = Operations(self.root, 'owner', self.account)
        self.assertTrue(reopened.items()[0]['handled'])
        self.assertEqual(self.client.get('/admin/operations/reviews', headers=self.auth).json()['items'], [])
        with reopened.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM review_audit').fetchone()[0], 1)
        with sqlite3.connect(self.event_path) as db: db.execute('UPDATE events SET result=?', (json.dumps({'action': 'review', 'reason': 'Changed'}),))
        self.assertFalse(reopened.items()[0]['handled'])
        with self.assertRaises(ValueError): reopened.mark(item['id'], item['revision'], True, '')

    def test_probe_does_not_queue_or_claim_real_message_receipt(self):
        response = self.client.post('/admin/operations/probe', headers=self.auth)
        self.assertEqual(response.status_code, 200); self.assertFalse(response.json()['external_delivery_verified'])
        status = self.ops.status(); self.assertIsNone(status['last_message_received'])
        self.assertIsNotNone(status['last_local_probe']); self.assertEqual(self.inbox.counts(), [])
        event = {'action': 'message.created', 'data': {'id': 'real', 'sender_type': 'guest'}}
        self.client.post('/webhook/hospitable/owner?token=private-hook', json=event)
        self.assertIsNotNone(self.ops.status()['last_guest_message_received'])
        self.client.post('/webhook/hospitable/owner?token=private-hook', json=event)
        self.assertEqual(self.ops.status()['received_count'], 2)
        self.assertEqual(sum(r['count'] for r in self.inbox.counts()), 1)

    def test_webhook_url_formats_without_network_and_requires_https(self):
        with patch('requests.get') as get, patch('requests.post') as post:
            response = self.client.post('/admin/operations/webhook-url', headers=self.auth, json={'base_url': 'https://toolkit.example.com'})
            self.assertEqual(response.json()['url'], 'https://toolkit.example.com/webhook/hospitable/owner?token=private-hook')
            get.assert_not_called(); post.assert_not_called()
        for url in ['http://example.com', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com?token=x']:
            self.assertEqual(self.client.post('/admin/operations/webhook-url', headers=self.auth, json={'base_url': url}).status_code, 400)
        self.assertNotIn('private-hook', self.client.get('/admin/operations', headers=self.auth).text)

    def test_gateway_review_is_retained_and_other_account_is_hidden(self):
        for account in ['owner', 'other']:
            self.inbox.add(account, {'data': {'id': account, 'body': 'Unknown property'}})
        with self.inbox.connect() as db: db.execute("UPDATE inbox SET state='review',reason='Unmanaged'")
        items = self.ops.items()
        self.assertEqual(len([r for r in items if r['source']=='gateway']), 1)

    def test_inflight_cannot_be_marked_handled(self):
        with sqlite3.connect(self.event_path) as db: db.execute('UPDATE events SET result=?', (json.dumps({'action':'send_pending'}),))
        item = self.ops.items()[0]
        with self.assertRaises(ValueError): self.ops.mark(item['id'], item['revision'], True, '')


if __name__ == '__main__': unittest.main()
