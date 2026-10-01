"""Gateway -> actual property worker -> draft/alert/send with mocked egress."""
import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
import test_hosting
from hosting import gateway, worker
from hosting.controls import Controls
from hosting.notifications import Outbox
from hosting.operations import Operations


class MessageFlowTests(unittest.TestCase):
    def setUp(self):
        self.base=test_hosting.HostingTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.accounts.pop('b'); self.base.accounts['a']['id']='a'
        account=self.base.accounts['a']; account['notifications']={'email':{'enabled':True}}
        account['properties']['property-a']['notifications']={'email':{'enabled':True}}
        self.base.save(); self.root=self.base.root; self.account=account
        self.env=patch.dict(os.environ,{'TOOLKIT_INSTANCE_MODE':'container','TOOLKIT_DATA_DIR':str(self.root),
            'TOOLKIT_ACCOUNT_ID':'a','HOSPITABLE_PROPERTY_UUID':'property-a','TOOLKIT_ACCOUNTS_FILE':str(self.base.registry)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.client=TestClient(worker.create_app()); self.controls=Controls(self.root/'controls.sqlite3')
        self.ready=patch('hosting.email_setup.ready',return_value=True); self.ready.start(); self.addCleanup(self.ready.stop)
        self.inbox=gateway.Inbox(self.root/'state/inbox.sqlite3')
        self.api=TestClient(gateway.create_app(self.base.registry,self.inbox.path))
        self.event=json.loads(json.dumps(self.base.payload))
        self.sources=[{'text':'Towels are in the blue cupboard.','source':{'source':'guide.md','guest_safe':True}}]

    def transport(self,url,**kwargs):
        if url.startswith('http://127.0.0.1:'):
            return self.client.post('/webhook/hospitable?token=worker-a-secret',json=kwargs['json'])
        return Mock(status_code=201)

    def get(self,url,**kwargs):
        response=Mock(status_code=200)
        response.json.return_value={'data':[self.event['data']]} if url.endswith('/messages') else {'data':{'property_id':'property-a'}}
        return response

    def dispatch(self):
        response=self.api.post('/webhook/hospitable/a?token=account-a-hook',json=self.event)
        self.assertEqual(response.status_code,200)
        resolver=gateway.resolve_property
        resolve=lambda account,payload,**kw: resolver(account,payload,get=self.get)
        with patch('hosting.gateway.resolve_property',side_effect=resolve),patch('hosting.worker.resolve_property',side_effect=resolve), \
             patch('hosting.worker.retrieve_references',return_value=self.sources),patch('hosting.ai_service.draft_text',return_value=json.dumps({'action':'draft','answer':'In the blue cupboard.','reason':'Guide'})), \
             patch('requests.get',side_effect=self.get),patch('requests.post',side_effect=self.transport) as post:
            gateway.deliver(self.inbox,self.account,self.inbox.pending('a')[0],controls=self.controls)
            return [call for call in post.call_args_list if call.args[0].startswith(gateway.API_BASE)]

    def test_booking_draft_sources_owner_email_and_duplicate(self):
        self.assertEqual(self.dispatch(),[])
        ops=Operations(self.root,'a',self.account); item=ops.items()[0]
        self.assertEqual(item['sources'],self.sources); self.assertEqual(item['state'],'draft')
        outbox=Outbox(self.root/'a/state/events.sqlite3')
        config={'sender':'host@example.com','recipients':['owner@example.com'],'browser_smtp':{}}
        with patch('hosting.notifications.validate_channel',return_value=config),patch('hosting.email_setup.send') as send:
            outbox.drain({'a':self.account},self.controls); outbox.drain({'a':self.account},self.controls)
            self.assertEqual(send.call_count,1); self.assertIn('In the blue cupboard',send.call_args.args[1].get_content())
        self.api.post('/webhook/hospitable/a?token=account-a-hook',json=self.event)
        self.assertEqual(self.inbox.pending('a'),[])

    def test_inquiry_automatic_sends_once_to_inquiry_thread(self):
        self.controls.set('test','a',shadow=False); self.controls.set('test','a','property-a',shadow=False)
        self.event['data'].pop('reservation_id'); self.event['data']['conversation_id']='inquiry'
        self.event['data']['created_at']=datetime.now(timezone.utc).isoformat()
        calls=self.dispatch(); self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].args[0].endswith('/inquiries/inquiry/messages'))
        self.assertEqual(calls[0].kwargs['json'],{'body':'In the blue cupboard.'})
        self.assertEqual(Operations(self.root,'a',self.account).items(),[])

    def test_incident_alerts_without_model_or_guest_send(self):
        self.event['data']['body']='There is a gas leak'
        self.assertEqual(self.dispatch(),[])
        item=Operations(self.root,'a',self.account).items()[0]
        self.assertEqual(item['state'],'review'); self.assertEqual(item['answer'],'')

    def test_paused_gateway_keeps_event_pending(self):
        self.controls.set('test','a',enabled=False)
        self.assertEqual(self.dispatch(),[])
        with self.inbox.connect() as db:self.assertEqual(db.execute('SELECT state FROM inbox').fetchone()[0],'pending')


if __name__=='__main__':unittest.main()
