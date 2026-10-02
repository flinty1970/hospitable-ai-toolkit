import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
from hosting.controls import Controls
from hosting import guest_sending as sending

class GuestSendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'TOOLKIT_INSTANCE_MODE':'container','HOSPITABLE_PAT':'private-key'},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.account={'id':'owner','api_key_env':'HOSPITABLE_PAT','enabled':True,'shadow':True,'notifications':{'email':{'enabled':True}},'properties':{'property':{'enabled':True,'shadow':True,'notifications':{'email':{'enabled':True}}}}}
        self.controls=Controls(self.root/'controls.db')
        self.controls.set('owner','owner',shadow=False)
        self.controls.set('owner','owner','property',shadow=False)
        self.payload={'data':{'id':'incoming','reservation_id':'reservation','sender_type':'guest','body':'Where are towels?','created_at':datetime.now(timezone.utc).isoformat()}}
        self.ready=patch('hosting.email_setup.ready',return_value=True);self.ready.start();self.addCleanup(self.ready.stop)
    def get(self,url,**kwargs):
        response=Mock(status_code=200)
        response.json.return_value={'data':[self.payload['data']]} if url.endswith('/messages') else {'data':{'property_id':'property'}}
        return response
    def send(self):return sending.send(self.root,'owner','property',self.account,self.payload,'Towels are in the blue cupboard.',self.controls)
    def test_account_and_property_switches_both_required(self):
        self.assertEqual(self.controls.effective('owner',self.account,'property')['mode'],'automatic')
        self.controls.set('owner','owner',shadow=True)
        with patch.object(sending.requests,'post') as post:
            with self.assertRaises(sending.SendReview):self.send()
            post.assert_not_called()
        self.controls.set('owner','owner',shadow=False)
        self.controls.set('owner','owner','property',shadow=True)
        self.assertEqual(self.controls.effective('owner',self.account,'property')['mode'],'shadow')
    def test_send_exactly_one_post_and_duplicate_never_retried(self):
        with patch.object(sending.requests,'get',side_effect=self.get),patch.object(sending.requests,'post',return_value=Mock(status_code=201)) as post:
            self.assertEqual(self.send()['action'],'sent')
            self.assertEqual(post.call_args.args[0],sending.API_BASE+'/reservations/reservation/messages')
            self.assertEqual(post.call_args.kwargs['json'],{'body':'Towels are in the blue cupboard.'})
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            with self.assertRaises(sending.SendReview):self.send()
            self.assertEqual(post.call_count,1)
    def test_timeout_and_rejection_never_retried(self):
        with patch.object(sending.requests,'get',side_effect=self.get),patch.object(sending.requests,'post',side_effect=TimeoutError('private-key')) as post:
            with self.assertRaises(sending.SendReview) as error:self.send()
            self.assertNotIn('private-key',str(error.exception))
            with self.assertRaises(sending.SendReview):self.send()
            self.assertEqual(post.call_count,1)
        with sqlite3.connect(self.root/'state/guest-sends.sqlite3') as db:self.assertEqual(db.execute('SELECT state FROM sends').fetchone()[0],'unknown')
    def test_old_backlog_missing_time_host_reply_and_cross_property_blocked(self):
        with patch.object(sending.requests,'get',side_effect=self.get),patch.object(sending.requests,'post') as post:
            self.payload['data']['created_at']='2020-01-01T00:00:00Z'
            with self.assertRaises(sending.SendReview):self.send()
            self.payload['data'].pop('created_at')
            with self.assertRaises(sending.SendReview):self.send()
            self.payload['data']['created_at']=datetime.now(timezone.utc).isoformat()
            self.payload['data']['sender_type']='host'
            with self.assertRaises(sending.SendReview):self.send()
            post.assert_not_called()
        response=Mock(status_code=200);response.json.return_value={'data':{'property_id':'other'}}
        with patch.object(sending.requests,'get',return_value=response),patch.object(sending.requests,'post') as post:
            with self.assertRaises(Exception):self.send()
            post.assert_not_called()
    def test_switch_off_during_preflight_blocks_post(self):
        def get(url,**kw):
            if url.endswith('/messages'):self.controls.set('owner','owner',shadow=True)
            return self.get(url,**kw)
        with patch.object(sending.requests,'get',side_effect=get),patch.object(sending.requests,'post') as post:
            with self.assertRaises(sending.SendReview):self.send()
            post.assert_not_called()
    def test_rate_limits_and_inquiry_target(self):
        with patch.object(sending.requests,'get',side_effect=self.get),patch.object(sending.requests,'post',return_value=Mock(status_code=201)) as post:
            self.send();self.payload['data']['id']='second';self.send();self.payload['data']['id']='third'
            with self.assertRaises(sending.SendReview):self.send()
            self.assertEqual(post.call_count,2)
        self.payload['data'].pop('reservation_id');self.payload['data']['conversation_id']='inquiry'
        with patch.object(sending.requests,'get',side_effect=self.get),patch.object(sending.requests,'post',return_value=Mock(status_code=201)) as post:
            self.send();self.assertIn('/inquiries/inquiry/messages',post.call_args.args[0])

class WorkerSendTests(unittest.TestCase):
    def setUp(self):
        import json
        from tests import test_hosting
        self.base=test_hosting.HostingTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.accounts.pop('b');self.base.save()
        self.root=self.base.root;self.accounts=self.base.accounts
        self.controls=Controls(self.root/'controls.sqlite3')
        self.controls.set('test','a',shadow=False,email_enabled=True)
        self.controls.set('test','a','property-a',shadow=False,email_enabled=True)
        self.env=patch.dict(os.environ,{'TOOLKIT_INSTANCE_MODE':'container','TOOLKIT_DATA_DIR':str(self.root),'TOOLKIT_ACCOUNT_ID':'a','HOSPITABLE_PROPERTY_UUID':'property-a','TOOLKIT_ACCOUNTS_FILE':str(self.base.registry)})
        self.env.start();self.addCleanup(self.env.stop)
        from fastapi.testclient import TestClient
        from hosting.worker import create_app
        self.client=TestClient(create_app())
        self.resolve=patch('hosting.worker.resolve_property',return_value='property-a');self.resolve.start();self.addCleanup(self.resolve.stop)
        self.ready=patch('hosting.email_setup.ready',return_value=True);self.ready.start();self.addCleanup(self.ready.stop)
    def post(self):return self.client.post('/webhook/hospitable?token=worker-a-secret',json=self.base.payload)
    def test_worker_sent_duplicate_and_crash_recovery(self):
        import json
        with patch('hosting.worker.prepare_draft',return_value={'action':'draft','answer':'In the cupboard','reason':'Guide'}),patch('hosting.guest_sending.send',return_value={'action':'sent','answer':'In the cupboard','reason':'Accepted'}) as send:
            self.assertTrue(self.post().json()['sent']);self.assertEqual(self.post().json()['action'],'duplicate');self.assertEqual(send.call_count,1)
            path=self.root/'a/state/events.sqlite3'
            with sqlite3.connect(path) as db:db.execute('UPDATE events SET result=?',(json.dumps({'action':'send_pending'}),))
            self.assertEqual(self.post().json()['action'],'review');self.assertEqual(send.call_count,1)
            with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],1)
    def test_worker_review_and_property_draft_never_send(self):
        with patch('hosting.worker.prepare_draft',return_value={'action':'review','answer':'','reason':'Host needed'}),patch('hosting.guest_sending.send') as send:
            self.assertEqual(self.post().json()['action'],'review');send.assert_not_called()
        self.base.payload['data']['id']='other'
        self.controls.set('test','a','property-a',shadow=True)
        with patch('hosting.worker.prepare_draft',return_value={'action':'draft','answer':'In cupboard','reason':'Guide'}),patch('hosting.guest_sending.send') as send:
            self.assertFalse(self.post().json()['sent']);send.assert_not_called()
