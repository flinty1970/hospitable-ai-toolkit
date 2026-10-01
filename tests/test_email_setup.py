import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from hosting import email_setup as email
from hosting import account_details
from tests import test_admin_ui as admin_test

class EmailSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.value=dict(host='smtp.example.com',port=587,security='starttls',username='owner',password='private-password',sender='owner@example.com',recipient='review@example.com')
    def test_save_test_redaction_and_changed_config_requires_retest(self):
        email.save_settings(self.root,'owner',self.value)
        self.assertFalse(email.ready(self.root,'owner'))
        with patch.object(email,'send') as send:
            self.assertTrue(email.test_email(self.root,'owner',self.value)['sent'])
            self.assertEqual(send.call_args.args[1]['To'],'review@example.com')
        self.assertTrue(email.ready(self.root,'owner'))
        self.assertNotIn('private-password',json.dumps(email.public_settings(self.root,'owner')))
        self.assertEqual((self.root/'state/smtp-settings.json').stat().st_mode&0o777,0o600)
        email.save_settings(self.root,'owner',{**self.value,'password':'','recipient':'other@example.com'})
        self.assertFalse(email.ready(self.root,'owner'))
        with self.assertRaises(ValueError): email.save_settings(self.root,'owner',{**self.value,'password':'','host':'new.example.com'})
    def test_msmtp_tls_private_file_and_cleanup_on_failure(self):
        from email.message import EmailMessage
        message=EmailMessage();message['To']='review@example.com';message.set_content('test')
        paths=[]
        def run(args,**kwargs):
            path=Path(args[1].removeprefix('--file='));paths.append(path)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            text=path.read_text();self.assertIn('tls_starttls on',text);self.assertIn('tls_trust_file',text)
            self.assertTrue(kwargs['capture_output']);raise RuntimeError('private smtp error')
        with patch.object(email.subprocess,'run',side_effect=run):
            with self.assertRaises(RuntimeError): email.send(self.value,message)
        self.assertFalse(paths[0].exists())
        for value in [{**self.value,'host':'smtp.example.com\npassword injected'},{**self.value,'security':'off'},{**self.value,'sender':'bad\n@example.com'}]:
            with self.assertRaises(ValueError): email.validate(value)
    def test_live_notification_config_uses_saved_settings(self):
        email.save_settings(self.root,'owner',self.value)
        from hosting.notifications import validate_channel, Outbox
        with patch.dict(os.environ,{'TOOLKIT_INSTANCE_MODE':'container','TOOLKIT_DATA_DIR':str(self.root)}):
            config=validate_channel({'id':'owner','properties':{}},None,'email')
            self.assertEqual(config['recipients'],['review@example.com'])
            outbox=Outbox(self.root/'events.db');outbox.enqueue('event','owner',None,{})
            with outbox.connect() as db:self.assertEqual([r[0] for r in db.execute('SELECT channel FROM notification_outbox')],['email'])
            from hosting.controls import Controls
            controls=Controls(self.root/'controls.db');controls.set('test','owner',email_enabled=True)
            with patch.object(email,'send') as send:
                outbox.drain({'owner':{'id':'owner','properties':{}}},controls)
                self.assertEqual(send.call_count,1)
                self.assertEqual(send.call_args.args[1]['To'],'review@example.com')
            with outbox.connect() as db:self.assertEqual(db.execute('SELECT state FROM notification_outbox').fetchone()[0],'sent')

class AccountDetailTests(unittest.TestCase):
    def test_name_nickname_channel_markup_and_no_fabrication(self):
        with tempfile.TemporaryDirectory() as tmp:
            response=Mock(status_code=200);response.json.return_value={'data':[{'id':'p','name':'Nickname','public_name':'Public title','user':{'name':'Host account'},'listings':[{'platform':'airbnb','markup':{'percentage':12}},{'platform':'vrbo'}]}],'meta':{'last_page':1}}
            with patch.dict(os.environ,{'HOSPITABLE_PAT':'secret'}),patch.object(account_details.requests,'get',return_value=response):
                self.assertTrue(account_details.refresh(tmp,'owner',{'api_key_env':'HOSPITABLE_PAT'})['name_available'])
            data=account_details.read(tmp,'owner')
            self.assertEqual(data['name'],'Host account');self.assertEqual(data['properties']['p']['nickname'],'Nickname')
            self.assertEqual(data['properties']['p']['channels'][0]['markup'],'12% (API)')
            self.assertIn('Not exposed',data['properties']['p']['channels'][1]['markup'])
            account_details.save_name(tmp,'owner','Business name')
            self.assertEqual(account_details.read(tmp,'owner')['name'],'Business name')

class EmailAdminTests(unittest.TestCase):
    setUp=admin_test.AdminUITests.setUp
    make_client=admin_test.AdminUITests.make_client
    post=admin_test.AdminUITests.post
    def test_auth_error_redaction_required_email_and_enable(self):
        value=dict(host='smtp.example.com',port=587,security='starttls',username='owner',password='private-password',sender='owner@example.com',recipient='review@example.com')
        self.assertEqual(self.client.post('/admin/settings/smtp/test',json=value).status_code,401)
        with patch.dict(os.environ,{'TOOLKIT_INSTANCE_MODE':'container','TOOLKIT_DATA_DIR':str(self.root)}):
            self.assertEqual(self.post('controls',{'response_mode':'draft'}).status_code,409)
            self.assertEqual(self.post('smtp/save',value).status_code,200)
            with patch.object(email,'send',side_effect=RuntimeError('private-password')):
                result=self.post('smtp/test',value);self.assertEqual(result.status_code,502);self.assertNotIn('private-password',result.text)
            with patch.object(email,'send'):
                self.assertEqual(self.post('smtp/test',value).status_code,200)
            self.assertEqual(self.post('controls',{'property_id':'alpha','response_mode':'draft'}).status_code,200)
            self.assertEqual(self.post('controls',{'email_enabled':False}).status_code,409)
            state=self.client.get('/admin/settings',headers=self.headers)
            self.assertNotIn('private-password',state.text)
            self.assertTrue(state.json()['smtp']['tested'])
