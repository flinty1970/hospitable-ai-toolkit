import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from hosting import ai_service as ai
from tests import test_admin_ui as admin_test

class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def test_private_persistence_and_public_redaction(self):
        ai.save_settings(self.root, 'owner', 'openai', 'gpt-test', 'secret-key')
        path = self.root / 'state/ai-settings.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        public = ai.public_settings(self.root, 'owner', {'properties': {}})
        self.assertNotIn('secret-key', json.dumps(public))
        self.assertTrue(next(p for p in public['providers'] if p['id']=='openai')['key_present'])
        ai.save_settings(self.root, 'owner', 'openai', 'gpt-next')
        self.assertEqual(ai.read_settings(self.root, 'owner')['keys']['openai'], 'secret-key')
        with self.assertRaises(ValueError): ai.read_settings(self.root, 'different')
        path.chmod(0o644)
        with self.assertRaises(ValueError): ai.read_settings(self.root, 'owner')

    def test_selection_does_not_reuse_other_provider_key(self):
        ai.save_settings(self.root, 'owner', 'anthropic', 'claude-test', 'claude-key')
        with self.assertRaises(ValueError): ai.save_settings(self.root, 'owner', 'google', 'gemini-test')
        with self.assertRaises(ValueError): ai.save_settings(self.root, 'owner', 'arbitrary', 'test', 'key')
        with self.assertRaises(ValueError): ai.save_settings(self.root, 'owner', 'openai', 'test', 'bad\nkey')

    def test_compatible_provider_endpoints_and_payloads(self):
        response = Mock(status_code=200)
        response.json.return_value={'choices':[{'message':{'content':'{"ok":true}'}}]}
        with patch.object(ai.requests, 'post', return_value=response) as post:
            for provider in ['openai','google','xai']:
                self.assertEqual(ai.test_connection(provider, 'text-model', 'secret-key'), {'ok':True})
                args,kw=post.call_args
                self.assertEqual(args[0], ai.PROVIDERS[provider]['base']+'/chat/completions')
                self.assertFalse(kw['allow_redirects'])
                self.assertEqual(kw['headers']['Authorization'],'Bearer secret-key')
                self.assertEqual(kw['json']['messages'][0]['role'],'system')
                self.assertNotIn('secret-key',json.dumps(kw['json']))
        response.status_code=401
        with patch.object(ai.requests,'post',return_value=response):
            with self.assertRaisesRegex(ValueError,'AI request failed'): ai.test_connection('openai','test','key')

    def test_live_selection_read_for_each_draft(self):
        with patch.dict(os.environ, {'TOOLKIT_DATA_DIR':str(self.root),'TOOLKIT_ACCOUNT_ID':'owner'}):
            with patch.object(ai,'generate',return_value='{}') as generate:
                ai.save_settings(self.root,'owner','google','gemini-test','gemini-key')
                ai.draft_text({},'rules','message')
                self.assertEqual(generate.call_args.args[:3],('google','gemini-test','gemini-key'))
                ai.save_settings(self.root,'owner','xai','grok-test','grok-key')
                ai.draft_text({},'rules','message')
                self.assertEqual(generate.call_args.args[:3],('xai','grok-test','grok-key'))

    def test_paginated_model_listing_and_google_filter(self):
        first=Mock(status_code=200);first.json.return_value={'data':[{'id':'claude-a'}],'has_more':True,'last_id':'claude-a'}
        second=Mock(status_code=200);second.json.return_value={'data':[{'id':'claude-b'}],'has_more':False}
        with patch.object(ai.requests,'get',side_effect=[first,second]) as get:
            self.assertEqual(ai.list_models('anthropic','key'),['claude-a','claude-b'])
            self.assertEqual(get.call_args.kwargs['params']['after_id'],'claude-a')
        first.json.return_value={'models':[{'name':'models/gemini-text','supportedGenerationMethods':['generateContent']},{'name':'models/embedding','supportedGenerationMethods':['embedContent']}]}
        with patch.object(ai.requests,'get',return_value=first) as get:
            self.assertEqual(ai.list_models('google','key'),['gemini-text'])
            self.assertEqual(get.call_args.kwargs['headers'],{'x-goog-api-key':'key'})

class AIAdminTests(unittest.TestCase):
    setUp = admin_test.AdminUITests.setUp
    make_client = admin_test.AdminUITests.make_client
    post = admin_test.AdminUITests.post
    def test_api_key_never_returned_and_auth_required(self):
        value={'provider':'openai','model':'gpt-test','api_key':'private-ai-key'}
        self.assertEqual(self.client.post('/admin/settings/ai/save',json=value).status_code,401)
        self.assertEqual(self.client.post('/admin/settings/ai/save',headers={**self.headers,'Origin':'https://evil.example'},json=value).status_code,403)
        self.assertEqual(self.post('ai/save',value).status_code,200)
        snapshot=self.client.get('/admin/settings',headers=self.headers)
        self.assertNotIn('private-ai-key',snapshot.text)
        self.assertEqual(snapshot.json()['ai']['provider'],'openai')
        with patch('hosting.ai_service.test_connection',return_value={'ok':True}) as test:
            self.assertEqual(self.post('ai/test',{'provider':'openai','model':'gpt-test'}).status_code,200)
            self.assertEqual(test.call_args.args,('openai','gpt-test','private-ai-key'))
        self.assertEqual(self.post('ai/save',{'provider':'xai','model':'grok-test'}).status_code,400)
