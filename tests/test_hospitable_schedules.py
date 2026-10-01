import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hosting import hospitable_schedules as native
from hosting.scheduled_ui import install

class NativeScheduleTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        env=patch.dict(os.environ,{'PAT':'pat-only','TOOLKIT_ADMIN_SECRET':'admin-token'},clear=True);env.start();self.addCleanup(env.stop)
        self.account={'id':'owner','api_key_env':'PAT','properties':{'property':{'name':'House','timezone':'Europe/London'}}}
        self.row={'id':'opaque-id','reservation_code':'ABC','message':'Welcome','title':'Check in','scheduled_for':'2099-06-01T12:00:00+01:00','sent_at':None,'cancelled_at':None,'failed':False,'timezone':'Europe/London'}
        (self.root/'state').mkdir()
        self.path=self.root/'state/hospitable-mcp.json'
        self.path.write_text(json.dumps({'account_id':'owner','token':'native-token-123456789'}));self.path.chmod(0o600)
        reservation=patch.object(native,'reservation',return_value={'property_id':'property'})
        reservation.start();self.addCleanup(reservation.stop)
    def upstream(self,token,name,args):
        if name=='get-reservation':return {'properties':[{'id':'property'}]}
        if name=='get-reservation-scheduled-messages':return [dict(self.row)]
        if name=='get-user':return {'id':'account-id'}
        return {'updated':True}
    def test_native_listing_and_edit_use_correct_tool_and_never_send_now(self):
        with patch.object(native,'call',side_effect=self.upstream) as call:
            row=native.listing(self.root,'owner',self.account,'property','reservation')[0]
            self.assertEqual(row['source'],'hospitable');self.assertEqual(row['state'],'pending')
            value={'confirm_send':True,'message':'New welcome','local_time':'2099-06-02T13:00','revision':row['revision']}
            self.assertTrue(native.update(self.root,'owner',self.account,'property','reservation','opaque-id',value)['updated'])
            self.assertEqual(call.call_args.args[1],'update-scheduled-message')
            self.assertEqual(call.call_args.args[2],{'id':'opaque-id','message':'New welcome','scheduled_for':'2099-06-02 13:00','send_now':False})
    def test_other_property_and_changed_cancelled_or_sent_message_rejected(self):
        with patch.object(native,'call',side_effect=self.upstream) as call:
            row=native.listing(self.root,'owner',self.account,'property','reservation')[0]
            value={'confirm_send':True,'message':'New welcome','local_time':'2099-06-02T13:00','revision':row['revision']}
            for field,item in [('message','Edited elsewhere'),('sent_at','2099-06-01T12:01:00+01:00'),('cancelled_at','2099-06-01T11:00:00+01:00')]:
                old=self.row[field];self.row[field]=item
                with self.assertRaises(ValueError):native.update(self.root,'owner',self.account,'property','reservation','opaque-id',value)
                self.row[field]=old
            self.assertFalse(any(c.args[1]=='update-scheduled-message' for c in call.call_args_list))
        with patch.object(native,'call',return_value={'property_id':'other'}):
            with self.assertRaises(ValueError):native.listing(self.root,'owner',self.account,'property','reservation')
    def test_credentials_private_account_matches_and_disconnect(self):
        with patch.object(native,'call',side_effect=self.upstream),patch.object(native,'pat_user',return_value='wrong-account'):
            with self.assertRaises(ValueError):native.save(self.root,'owner',self.account,'new-token-123456789')
        self.assertEqual(native.settings(self.root,'owner')['token'],'native-token-123456789')
        with patch.object(native,'call',side_effect=self.upstream),patch.object(native,'pat_user',return_value='account-id'):
            self.assertTrue(native.save(self.root,'owner',self.account,'new-token-123456789')['configured'])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):native.settings(self.root,'owner')
        self.path.chmod(0o600);native.disconnect(self.root);self.assertIsNone(native.settings(self.root,'owner'))
    def test_native_edit_timeout_attempted_once_and_result_errors_sanitized(self):
        with patch.object(native,'call',side_effect=self.upstream):row=native.listing(self.root,'owner',self.account,'property','reservation')[0]
        value={'confirm_send':True,'message':'New','local_time':'2099-06-02T13:00','revision':row['revision']}
        count=[]
        def error(token,name,args):
            if name=='update-scheduled-message':count.append(name);raise ValueError('Uncertain response')
            return self.upstream(token,name,args)
        with patch.object(native,'call',side_effect=error):
            with self.assertRaises(ValueError):native.update(self.root,'owner',self.account,'property','reservation','opaque-id',value)
        self.assertEqual(len(count),1)
        async def fail(*args):raise RuntimeError('secret-value')
        with patch.object(native,'_call',side_effect=fail):
            with self.assertRaises(ValueError) as result:native.call('secret-value','get-user',{})
        self.assertNotIn('secret-value',str(result.exception))
    def test_page_native_auth_no_token_echo_and_explicit_edit_confirmation(self):
        app=FastAPI();install(app,{'owner':self.account},self.root);client=TestClient(app)
        headers={'Authorization':'Bearer admin-token'}
        self.assertEqual(client.get('/admin/scheduled/connection').status_code,401)
        response=client.get('/admin/scheduled/connection',headers=headers)
        self.assertEqual(response.json(),{'configured':True,'verified_at':None});self.assertNotIn('native-token',response.text)
        with patch.object(native,'call',side_effect=self.upstream):
            response=client.get('/admin/scheduled/property/native?reservation_id=reservation',headers=headers)
            self.assertEqual(response.status_code,200)
            value={'reservation_id':'reservation','message':'New','local_time':'2099-06-02T13:00','revision':response.json()['messages'][0]['revision']}
            self.assertEqual(client.post('/admin/scheduled/property/native/opaque-id/edit',headers=headers,json=value).status_code,400)
            value['confirm_send']=True
            self.assertEqual(client.post('/admin/scheduled/property/native/opaque-id/edit',headers=headers,json=value).status_code,200)
        self.assertEqual(client.get('/admin/scheduled/unknown/native?reservation_id=reservation',headers=headers).status_code,404)
    def test_native_repeated_clock_hour_not_guessed(self):
        with patch.object(native,'call',side_effect=self.upstream):row=native.listing(self.root,'owner',self.account,'property','reservation')[0]
        value={'confirm_send':True,'message':'New','local_time':'2026-10-25T01:30','revision':row['revision'],'fold':1}
        with patch.object(native,'call',side_effect=self.upstream),patch('hosting.scheduled_messages.time.time',return_value=0):
            with self.assertRaisesRegex(ValueError,'outside the repeated'):native.update(self.root,'owner',self.account,'property','reservation','opaque-id',value)
    def test_payload_json_and_structured_formats(self):
        self.assertEqual(native.payload(SimpleNamespace(isError=False,structuredContent={'data':[self.row]},content=[])),[self.row])
        self.assertEqual(native.payload(SimpleNamespace(isError=False,structuredContent=None,content=[SimpleNamespace(type='text',text=json.dumps({'data':[self.row]}))])),[self.row])
        with self.assertRaises(ValueError):native.payload(SimpleNamespace(isError=True))

    def test_cancelled_null_text_does_not_hide_pending_messages(self):
        cancelled={**self.row,'id':'cancelled-id','message':None,'cancelled_at':'2026-07-27T21:11:16+01:00'}
        def upstream(token,name,args):
            if name=='get-reservation-scheduled-messages':return [cancelled,self.row]
            return self.upstream(token,name,args)
        with patch.object(native,'call',side_effect=upstream):
            rows=native.listing(self.root,'owner',self.account,'property','reservation')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['state'],'cancelled');self.assertEqual(rows[0]['body'],'')
        self.assertFalse(rows[0]['body_available']);self.assertEqual(rows[1]['body'],'Welcome')

    def test_saved_connection_can_be_retested_without_reentering_token(self):
        app=FastAPI();install(app,{'owner':self.account},self.root);client=TestClient(app)
        headers={'Authorization':'Bearer admin-token'}
        with patch.object(native,'call',side_effect=self.upstream) as call,patch.object(native,'pat_user',return_value='account-id'):
            response=client.post('/admin/scheduled/connection',headers=headers,json={'test':True})
        self.assertEqual(response.status_code,200);self.assertTrue(response.json()['tested'])
        self.assertEqual(call.call_args.args[0],'native-token-123456789')
        self.assertGreater(client.get('/admin/scheduled/connection',headers=headers).json()['verified_at'],0)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        with patch.object(native,'call',side_effect=self.upstream),patch.object(native,'pat_user',return_value='another-account'):
            self.assertEqual(client.post('/admin/scheduled/connection',headers=headers,json={'test':True}).status_code,400)

    def test_image_attachments_are_explicit_and_https_only(self):
        url='https://images.example.com/photo.jpg'
        self.assertEqual(native.image_attachments({'attachments':[{'url':url,'filename':'Photo'}, {'url':url}, {'url':'javascript:alert(1)','type':'image'}, {'url':'https://example.com/file.pdf'}, {'url':'http://example.com/a.png'}]}),[{'url':url,'name':'Photo'}])
        self.assertEqual(native.image_attachments({'message':url}),[])
        self.assertEqual(native.image_attachments({'attachments':{'data':[{'url':'https://example.com/signed?id=1','mime_type':'image/png'}]}})[0]['url'],'https://example.com/signed?id=1')


if __name__=='__main__':unittest.main()
