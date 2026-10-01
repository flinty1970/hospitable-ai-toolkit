import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hosting import scheduled_messages as scheduling
from hosting.controls import Controls
from hosting.scheduled_ui import install


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'TOOLKIT_INSTANCE_MODE':'container', 'TOOLKIT_DATA_DIR':str(self.root), 'TOOLKIT_ADMIN_SECRET':'admin-token', 'PAT':'private-key'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.account = {'id':'owner', 'api_key_env':'PAT', 'enabled':True, 'shadow':True, 'notifications':{'email':{'enabled':True}}, 'properties':{'property':{'name':'House', 'timezone':'Europe/London', 'enabled':True, 'shadow':True, 'notifications':{'email':{'enabled':True}}}}}
        self.queue = scheduling.Queue(self.root, 'owner', self.account)
        self.ready = patch.object(scheduling, 'ready', return_value=True)
        self.ready.start(); self.addCleanup(self.ready.stop)
        self.record = patch.object(scheduling, 'reservation', return_value={'properties':[{'id':'property'}], 'status':{'current':{'category':'accepted'}}})
        self.record.start(); self.addCleanup(self.record.stop)
        self.drain = patch.object(scheduling.Outbox, 'drain')
        self.drain.start(); self.addCleanup(self.drain.stop)
        self.controls = Controls(self.root/'state/controls.sqlite3')

    def create(self):
        return self.queue.create('property','reservation','2099-06-01T12:00','Hello guest','owner')

    def due(self, identity, seconds=1):
        with self.queue.connect() as db:
            db.execute('UPDATE scheduled SET due=? WHERE id=?',(time.time()-seconds,identity))

    def test_persistent_cancel_and_duplicate_create(self):
        first = self.create()
        self.assertEqual(first, self.create())
        queue = scheduling.Queue(self.root,'owner',self.account)
        self.assertEqual(len(queue.list('property')),1)
        with self.assertRaises(ValueError): queue.cancel('other',first['id'],'owner')
        self.assertTrue(queue.cancel('property',first['id'],'owner')['cancelled'])
        self.assertTrue(queue.cancel('property',first['id'],'owner')['cancelled'])
        self.due(first['id'])
        with patch.object(scheduling.requests,'post') as post:
            queue.deliver_due(); post.assert_not_called()
        self.assertEqual(queue.list('property')[0]['state'],'cancelled')

    def test_due_send_once_in_shadow_and_pause_holds(self):
        identity = self.create()['id']; self.due(identity)
        self.controls.set('owner','owner','property',enabled=False)
        with patch.object(scheduling.requests,'post',return_value=Mock(status_code=201)) as post:
            self.queue.deliver_due(); post.assert_not_called()
            self.controls.set('owner','owner','property',enabled=True)
            self.queue.deliver_due(); self.queue.deliver_due()
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.kwargs['json'],{'body':'Hello guest'})
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(self.queue.list('property')[0]['state'],'sent')
        with self.assertRaises(ValueError):self.queue.cancel('property',identity,'owner')

    def test_uncertain_send_never_retried_and_restart_review(self):
        identity = self.create()['id']; self.due(identity)
        with patch.object(scheduling.requests,'post',side_effect=TimeoutError('private-key')) as post:
            self.queue.deliver_due(); self.queue.deliver_due()
            self.assertEqual(post.call_count,1)
        self.assertEqual(self.queue.list('property')[0]['state'],'review')
        with self.queue.connect() as db:
            db.execute("UPDATE scheduled SET state='sending' WHERE id=?",(identity,))
        self.queue.recover()
        self.assertEqual(self.queue.list('property')[0]['state'],'review')
        with self.queue.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],1)

    def test_mcp_schedule_grant_is_separate_and_confirmation_required(self):
        import json
        from hosting.access import Denied
        from hosting.mcp_tools import HostedTools
        self.account['webhook_secret_env']='HOOK'
        self.account['properties']['property']['worker_secret_env']='WORKER'
        clients=self.root/'clients.json'
        grant={'permissions':['read'],'properties':{'property':['read']}}
        clients.write_text(json.dumps({'clients':{'owner':{'token_env':'MCP_TOKEN','accounts':{'owner':grant}}}}))
        with patch.dict(os.environ,{'HOOK':'hook-only','WORKER':'worker-only','MCP_TOKEN':'mcp-only'}):
            tools=HostedTools({'owner':self.account},clients,self.root/'state/controls.sqlite3',self.root/'inbox.sqlite3')
            with self.assertRaises(Denied):tools.schedule_message('mcp-only','owner','property','reservation','2099-06-01T12:00','Hi',True)
            grant['properties']['property'].append('schedule')
            clients.write_text(json.dumps({'clients':{'owner':{'token_env':'MCP_TOKEN','accounts':{'owner':grant}}}}))
            with self.assertRaises(ValueError):tools.schedule_message('mcp-only','owner','property','reservation','2099-06-01T12:00','Hi')
            with self.assertRaises(Denied):tools.schedule_message('mcp-only','owner','other','reservation','2099-06-01T12:00','Hi',True)
            result=tools.schedule_message('mcp-only','owner','property','reservation','2099-06-01T12:00','Hi',True)
            self.assertEqual(len(tools.scheduled_messages('mcp-only','owner','property')),1)
            self.assertTrue(tools.cancel_scheduled_message('mcp-only','owner','property',result['id'])['cancelled'])

    def test_late_cancelled_and_changed_reservation_do_not_send(self):
        identity = self.create()['id']; self.due(identity,901)
        with patch.object(scheduling.requests,'post') as post:
            self.queue.deliver_due(); post.assert_not_called()
        self.assertEqual(self.queue.list('property')[0]['state'],'review')
        with self.queue.connect() as db:db.execute("UPDATE scheduled SET state='pending',due=? WHERE id=?",(time.time()-1,identity))
        with patch.object(scheduling,'reservation',side_effect=ValueError('Property changed')),patch.object(scheduling.requests,'post') as post:
            self.queue.deliver_due(); post.assert_not_called()
        self.assertEqual(self.queue.list('property')[0]['state'],'review')

    def test_timezone_clock_changes_and_invalid_input(self):
        with patch.object(scheduling.time,'time',return_value=0):
            with self.assertRaises(ValueError):scheduling.local_due('2026-03-29T01:30','Europe/London')
            with self.assertRaises(ValueError):scheduling.local_due('2026-10-25T01:30','Europe/London')
            a=scheduling.local_due('2026-10-25T01:30','Europe/London',0)
            b=scheduling.local_due('2026-10-25T01:30','Europe/London',1)
            self.assertEqual(b-a,3600)
            self.assertEqual(datetime.fromtimestamp(a,timezone.utc).hour,0)
        with self.assertRaises(ValueError):scheduling.local_due('2000-01-01T12:00','Europe/London')
        with patch.object(scheduling,'ready',return_value=False):
            with self.assertRaises(ValueError):self.create()
        with self.assertRaises(ValueError):self.queue.create('other','reservation','2099-01-01T12:00','Hi','owner')

    def test_page_auth_confirmation_and_cancel(self):
        app=FastAPI(); install(app,{'owner':self.account},self.root)
        client=TestClient(app)
        headers={'Authorization':'Bearer admin-token'}
        value={'reservation_id':'reservation','local_time':'2099-06-01T12:00','message':'Hello guest'}
        self.assertEqual(client.get('/scheduled').status_code,200)
        self.assertEqual(client.get('/admin/scheduled/property').status_code,401)
        self.assertEqual(client.post('/admin/scheduled/property',headers=headers,json=value).status_code,400)
        value['confirm_send']=True
        response=client.post('/admin/scheduled/property',headers=headers,json=value)
        self.assertEqual(response.status_code,200)
        identity=response.json()['id']
        response=client.get('/admin/scheduled/property',headers=headers)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(len(response.json()['messages']),1)
        self.assertEqual(client.post('/admin/scheduled/property/'+identity+'/cancel',headers={**headers,'Origin':'https://untrusted.example'}).status_code,403)
        self.assertEqual(client.post('/admin/scheduled/property/'+identity+'/cancel',headers=headers).status_code,200)
        self.assertEqual(client.get('/admin/scheduled/unknown',headers=headers).status_code,404)

    def test_reservation_api_scopes_and_pagination(self):
        self.record.stop()
        response=Mock(status_code=200)
        response.json.return_value={'data':[{'id':'reservation','properties':[{'id':'property'}],'guest':{'first_name':'Test','last_name':'Guest'},'check_in':'2099-06-01','check_out':'2099-06-02'},{'id':'other','properties':[{'id':'other'}]}],'meta':{'last_page':2}}
        with patch.object(scheduling.requests,'get',return_value=response) as get:
            result=scheduling.reservations(self.account,'property')
            self.assertEqual(len(result['reservations']),1)
            self.assertEqual(result['next_page'],2)
            self.assertIsNone(result['reservations'][0]['platform'])
            response.json.return_value['data'][0]['platform']={'name':'airbnb'}
            self.assertEqual(scheduling.reservations(self.account,'property')['reservations'][0]['platform'],'airbnb')
            self.assertEqual(get.call_args.kwargs['params']['properties[]'],'property')
            response.json.return_value={'data':{'properties':{'data':[{'id':'other'}]}}}
            with self.assertRaises(ValueError):scheduling.reservation(self.account,'property','reservation')
            response.json.return_value={'data':{'properties':{'data':[{'id':'property'}]}}}
            self.assertTrue(scheduling.reservation(self.account,'property','reservation'))

    def test_page_requests_clock_choice_only_for_ambiguous_time(self):
        app=FastAPI();install(app,{'owner':self.account},self.root)
        client=TestClient(app);headers={'Authorization':'Bearer admin-token'}
        value={'reservation_id':'reservation','local_time':'2026-10-25T01:30','message':'Hi','confirm_send':True}
        with patch.object(scheduling.time,'time',return_value=0):
            response=client.post('/admin/scheduled/property',headers=headers,json=value)
            self.assertEqual(response.status_code,409)
            self.assertEqual(response.json()['detail']['code'],'ambiguous_time')
            self.assertEqual(self.queue.list('property'),[])
            value['fold']=1
            self.assertEqual(client.post('/admin/scheduled/property',headers=headers,json=value).status_code,200)
            value['local_time']='2026-03-29T01:30'
            response=client.post('/admin/scheduled/property',headers=headers,json=value)
            self.assertEqual(response.status_code,400)
            self.assertIn('clocks move forward',response.json()['detail'])

    def test_edit_pending_revision_and_worker_uses_updated_message(self):
        identity=self.create()['id'];row=self.queue.list('property')[0]
        value={'confirm_send':True,'message':'Updated greeting','local_time':'2099-06-02T13:00','revision':row['revision']}
        with self.assertRaises(ValueError):self.queue.update('other',identity,value,'owner')
        self.assertTrue(self.queue.update('property',identity,value,'owner')['updated'])
        with self.assertRaises(ValueError):self.queue.update('property',identity,value,'owner')
        row=self.queue.list('property')[0];self.assertEqual(row['body'],'Updated greeting')
        self.due(identity)
        with patch.object(scheduling.requests,'post',return_value=Mock(status_code=201)) as post:
            self.queue.deliver_due()
            self.assertEqual(post.call_args.kwargs['json'],{'body':'Updated greeting'})
        value['revision']=self.queue.list('property')[0]['revision']
        with self.assertRaises(ValueError):self.queue.update('property',identity,value,'owner')

    def test_edit_during_delivery_validation_invalidates_old_send_snapshot(self):
        identity=self.create()['id'];self.due(identity)
        def changed(*args):
            with self.queue.connect() as db:db.execute('UPDATE scheduled SET due=?,body=? WHERE id=?',(time.time()+3600,'Edited before delivery',identity))
            return {'status':'accepted'}
        with patch.object(scheduling,'reservation',side_effect=changed),patch.object(scheduling.requests,'post') as post:
            self.queue.deliver_due();post.assert_not_called()
        self.assertEqual(self.queue.list('property')[0]['state'],'pending')


if __name__=='__main__':unittest.main()
