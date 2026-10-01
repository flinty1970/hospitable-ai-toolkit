import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hosting.document_ui import install
from test_pdf_ingestion import fixture_pdf


class DocumentUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.dict('os.environ', {'TOOLKIT_ADMIN_SECRET': 'test-admin'})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app = FastAPI()
        install(app, {'owner': {'properties': {'alpha': {'name': 'Alpha', 'runtime_dir': str(self.root / 'alpha')}, 'beta': {'runtime_dir': str(self.root / 'beta')}}}})
        self.client = TestClient(app)
        self.headers = {'Authorization': 'Bearer test-admin'}
        source = self.root / 'fixture.pdf'
        fixture_pdf(source)
        self.pdf = source.read_bytes()

    def upload(self):
        r = self.client.post('/admin/documents/alpha/upload?filename=guide.pdf', content=self.pdf, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        return r.json()['results'][0]['markdown']

    def test_authentication_and_origin(self):
        self.assertEqual(self.client.get('/admin/documents/properties').status_code, 401)
        self.assertEqual(self.client.post('/admin/documents/alpha/reindex', headers={**self.headers, 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/documents').status_code, 200)
        self.assertEqual(self.client.get('/admin/documents/properties', headers=self.headers).status_code, 200)

    def test_review_edit_approve_and_reindex(self):
        filename = self.upload()
        endpoint = '/admin/documents/alpha/review/' + filename
        review = self.client.get(endpoint, headers=self.headers).json()
        self.assertFalse(list((self.root / 'alpha/docs').glob('*.md')))
        saved = self.client.post(endpoint, headers=self.headers, json={'text': '# Safe facts\nTowels provided.', 'revision': review['revision']})
        self.assertEqual(saved.status_code, 200)
        approval = '/admin/documents/alpha/approve/' + filename
        payload = {'revision': saved.json()['revision'], 'confirmed_guest_safe': True}
        self.assertEqual(self.client.post(approval, headers=self.headers, json={}).status_code, 400)
        self.assertEqual(self.client.post(approval, headers=self.headers, json={**payload, 'revision': review['revision']}).status_code, 409)
        self.assertEqual(self.client.post(approval, headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(len(list((self.root / 'alpha/docs').glob('*.md'))), 1)
        with patch('hosting.document_ui.rebuild', return_value={'chunks': 3}) as build:
            r = self.client.post('/admin/documents/alpha/reindex', headers=self.headers)
            self.assertEqual(r.json()['chunks'], 3)
            build.assert_called_once_with(self.root / 'alpha')

    def test_isolation_traversal_and_duplicate_upload(self):
        filename = self.upload()
        self.assertEqual(self.client.get('/admin/documents/beta/review/' + filename, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get('/admin/documents/unknown', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/admin/documents/alpha/upload?filename=../bad.pdf', content=self.pdf, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/admin/documents/alpha/upload?filename=guide.pdf', content=self.pdf, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post('/admin/documents/alpha/upload?filename=bad.pdf', content=b'not pdf', headers=self.headers).status_code, 400)

    def test_upload_limit_and_failed_index_preserve_existing_data(self):
        with patch('hosting.document_ui.MAX_UPLOAD', 4):
            self.assertEqual(self.client.post('/admin/documents/alpha/upload?filename=large.pdf', content=self.pdf, headers=self.headers).status_code, 413)
        root = self.root / 'alpha'
        (root / 'index').mkdir(parents=True)
        manifest = root / 'index/current.json'
        manifest.write_text('{"previous": true}')
        with patch('hosting.document_ui.rebuild', side_effect=RuntimeError('private exception')):
            response = self.client.post('/admin/documents/alpha/reindex', headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private exception', response.text)
        self.assertEqual(manifest.read_text(), '{"previous": true}')

    def test_scanned_pdf_reports_ocr_and_stale_save_fails(self):
        filename = self.upload()
        endpoint = '/admin/documents/alpha/review/' + filename
        review = self.client.get(endpoint, headers=self.headers).json()
        data = {'revision': review['revision'], 'text': 'updated'}
        self.assertEqual(self.client.post(endpoint, headers=self.headers, json=data).status_code, 200)
        self.assertEqual(self.client.post(endpoint, headers=self.headers, json=data).status_code, 409)
        source = self.root / 'scan.pdf'
        fixture_pdf(source, blank=True)
        r = self.client.post('/admin/documents/alpha/upload?filename=scan.pdf', content=source.read_bytes(), headers=self.headers)
        self.assertIn('OCR', str(r.json()))
