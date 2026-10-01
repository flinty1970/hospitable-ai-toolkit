import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hosting.admin_ui import install
from hosting.controls import Controls
from hosting.property_setup import discovery, read_selection, apply_selection, save_selection


class AdminUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'TOOLKIT_ADMIN_SECRET': 'admin', 'HOSPITABLE_PAT': 'private-pat'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.account = {'id': 'owner', 'enabled': True, 'shadow': True, 'api_key_env': 'HOSPITABLE_PAT',
            'notifications': {'email': {'enabled': False}, 'ha': {'enabled': False}},
            'properties': {'alpha': {'name': 'Alpha', 'timezone': 'UTC', 'enabled': True, 'shadow': True}}}
        self.headers = {'Authorization': 'Bearer admin'}
        self.make_client()

    def make_client(self):
        app = FastAPI()
        install(app, {'owner': self.account}, self.root)
        self.client = TestClient(app)

    def post(self, path, value):
        return self.client.post('/admin/settings/' + path, headers=self.headers, json=value)

    def test_auth_and_origin_required_for_mutations(self):
        self.assertEqual(self.client.get('/settings').status_code, 200)
        self.assertEqual(self.client.get('/admin/settings').status_code, 401)
        self.assertEqual(self.client.post('/admin/settings/controls', headers={**self.headers, 'Origin': 'https://bad.example'}, json={'enabled': False}).status_code, 403)

    def test_controls_pause_resume_and_account_gate_persist(self):
        self.assertEqual(self.post('controls', {'property_id': 'alpha', 'response_mode': 'paused'}).json()['mode'], 'disabled')
        self.make_client()
        state = self.client.get('/admin/settings', headers=self.headers).json()
        self.assertEqual(state['properties'][0]['settings']['mode'], 'disabled')
        self.assertEqual(self.post('controls', {'response_mode': 'paused'}).status_code, 200)
        self.assertEqual(self.post('controls', {'property_id': 'alpha', 'response_mode': 'draft'}).json()['mode'], 'disabled')
        self.assertEqual(self.post('controls', {'response_mode': 'draft'}).json()['mode'], 'shadow')
        with Controls(self.root / 'state/controls.sqlite3').connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_audit').fetchone()[0], 4)

    def test_live_unknown_property_and_bad_settings_rejected(self):
        self.assertEqual(self.post('controls', {'property_id': 'alpha', 'response_mode': 'automatic'}).status_code, 409)
        self.assertEqual(self.post('controls', {'property_id': 'other', 'enabled': True}).status_code, 404)
        self.assertEqual(self.post('controls', {'enabled': 'true'}).status_code, 400)
        self.assertEqual(self.post('controls', {'shadow': False}).status_code, 400)
        self.assertEqual(self.post('controls', {'email_enabled': True}).status_code, 400)
        self.assertEqual(self.post('controls', {'enabled': False, 'response_mode': 'draft'}).status_code, 400)

    def test_verified_import_pending_restart_and_no_runtime_path_input(self):
        found = {'alpha': {'name': 'Alpha', 'timezone': 'UTC'}, 'beta': {'name': 'Beta', 'timezone': 'Europe/London'}}
        with patch('hosting.admin_ui.discovery', return_value=found):
            self.assertEqual(self.post('import', {'property_ids': ['other']}).status_code, 400)
            self.assertEqual(self.post('import', {'property_ids': ['beta']}).status_code, 200)
        selected = read_selection(self.root, 'owner')
        self.assertEqual(set(selected['beta']), {'name', 'timezone'})
        applied = apply_selection(self.root, self.account)
        self.assertFalse(applied['properties']['beta']['enabled'])
        self.assertTrue(applied['properties']['beta']['shadow'])
        self.assertEqual(self.client.get('/admin/settings', headers=self.headers).json()['pending_properties'][0]['id'], 'beta')
        self.assertEqual(self.post('restart', {}).status_code, 400)
        self.assertEqual(self.post('restart', {'confirm_restart': True}).status_code, 200)
        self.assertTrue((self.root / 'state/restart-request.json').exists())
        with self.assertRaises(ValueError):
            read_selection(self.root, 'other-account')

    def test_bootstrap_empty_account_reports_setup_required(self):
        self.account['properties'] = {}
        self.make_client()
        self.assertTrue(self.client.get('/admin/settings', headers=self.headers).json()['setup_required'])

    def test_discovery_paginates_without_following_external_links(self):
        first = Mock(status_code=200)
        first.json.return_value = {'data': [{'id': 'alpha', 'name': 'A', 'timezone': 'UTC'}], 'links': {'next': 'https://evil.example/steal'}}
        second = Mock(status_code=200)
        second.json.return_value = {'data': [{'id': 'beta', 'name': 'B', 'timezone': 'Europe/London'}], 'links': {'next': None}}
        get = Mock(side_effect=[first, second])
        self.assertEqual(set(discovery(self.account, get)), {'alpha', 'beta'})
        for call in get.call_args_list:
            self.assertEqual(call.args[0], 'https://public.api.hospitable.com/v2/properties')
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(get.call_args_list[1].kwargs['params']['page'], 2)

    def test_discovery_errors_do_not_expose_credentials(self):
        get = Mock(return_value=Mock(status_code=401))
        with self.assertRaises(ValueError) as error:
            discovery(self.account, get)
        self.assertNotIn('private-pat', str(error.exception))
        response = Mock(status_code=200)
        response.json.return_value = {'data': [{'id': '../escape', 'name': 'x'}]}
        with self.assertRaises(ValueError):
            discovery(self.account, Mock(return_value=response))

    def test_invalid_timezone_has_specific_safe_error(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data':[{'id':'unimported','name':'Private property','timezone':'unsupported-timezone'}], 'meta':{'last_page':1}}
        with patch('hosting.property_setup.requests.get', return_value=response):
            result = self.post('discover', {})
        self.assertEqual(result.status_code, 400)
        self.assertIn('timezone', result.json()['detail'])
        self.assertNotIn('Private property', result.text)
        self.assertNotIn('private-pat', result.text)

    def test_hospitable_offsets_preserve_existing_timezone_or_require_choice(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data':[{'id':'alpha','name':'Alpha','timezone':'+0100'}, {'id':'beta','name':'Beta','timezone':'-0400'}], 'meta':{'last_page':1}}
        with patch('hosting.property_setup.requests.get', return_value=response):
            result = self.post('discover', {}).json()['properties']
            self.assertEqual(result[0]['timezone'], 'UTC')
            self.assertTrue(result[1]['timezone_required'])
            self.assertEqual(self.post('import', {'property_ids':['beta']}).status_code, 400)
            self.assertEqual(self.post('import', {'property_ids':['beta'],'timezones':{'beta':'America/New_York'}}).status_code, 200)
        self.assertEqual(read_selection(self.root, 'owner')['beta']['timezone'], 'America/New_York')

    def test_community_admin_rejects_home_assistant_controls(self):
        self.assertEqual(self.post('controls', {'ha_enabled':True}).status_code, 400)
        self.assertEqual(self.post('controls', {'ha_alerts_enabled':True}).status_code, 400)
        self.assertNotIn('Home Assistant integration', self.client.get('/settings').text)
        self.account['home_assistant'] = {'enabled':True, 'heating_enabled':True}
        self.account['notifications']['ha']['enabled'] = True
        with patch.dict(os.environ, {'TOOLKIT_INSTANCE_MODE':'container'}):
            effective = Controls(self.root / 'state/controls.sqlite3').effective('owner', self.account)
            self.assertFalse(effective['ha_enabled'])
            self.assertFalse(effective['ha_alerts_enabled'])
            self.assertFalse(effective['heating_enabled'])
