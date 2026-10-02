"""Certificate persistence, validation and HTTPS-only administration."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hosting import tls_settings as tls


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pair(self, root=None):
        root = root or self.root
        tls.generate(root, '192.168.1.172,localhost')
        value, cert, key = tls.active(root)
        return value, cert.read_text(), key.read_text()

    def test_first_start_persists_private_pair_and_ip_san(self):
        with patch.dict(os.environ, {'TLS_HOSTS': '192.168.1.172,localhost'}):
            first = tls.ensure(self.root)
            second = tls.ensure(self.root)
        self.assertEqual(first, second)
        value, cert, key = tls.active(self.root)
        self.assertIn('192.168.1.172', value['addresses'])
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
        self.assertEqual(tls.validate_pair(cert.read_text(), key.read_text(), ['192.168.1.172'])['fingerprint'], value['fingerprint'])

    def test_upload_and_regenerate_preserve_previous_versions(self):
        old, cert, key = self.pair()
        uploaded = tls.activate(self.root, cert, key, ['192.168.1.172'], 'uploaded')
        self.assertEqual(uploaded['source'], 'uploaded')
        self.assertNotEqual(old['id'], uploaded['id'])
        replacement = tls.generate(self.root, '192.168.1.172')
        self.assertNotEqual(old['fingerprint'], replacement['fingerprint'])
        self.assertTrue((self.root / 'state/tls' / old['id'] / 'private-key.pem').exists())

    def test_bad_key_and_missing_san_leave_active_pair_unchanged(self):
        old, cert, key = self.pair()
        with tempfile.TemporaryDirectory() as other:
            _, _, wrong = self.pair(Path(other))
        for invalid_key, expected in [(wrong, ['192.168.1.172']), (key, ['192.168.1.173'])]:
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                tls.activate(self.root, cert, invalid_key, expected, 'uploaded')
            self.assertEqual(tls.active(self.root)[0]['id'], old['id'])

    def test_invalid_addresses_and_pem_rejected(self):
        for addresses in ('', 'https://example.com', 'example.com/path', '../secret', 'bad..name'):
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                tls.names(addresses)
        with self.assertRaises(ValueError):
            tls.validate_pair('not a certificate', 'not a key', ['localhost'])

    def test_admin_endpoints_require_auth_and_https_and_never_return_key(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.pair()
        app = FastAPI()
        with patch.dict(os.environ, {'TOOLKIT_ADMIN_SECRET': 'tls-test-secret'}):
            tls.install(app, self.root)
            https = TestClient(app, base_url='https://localhost')
            auth = {'Authorization': 'Bearer tls-test-secret'}
            self.assertEqual(https.get('/admin/settings/tls').status_code, 401)
            http = TestClient(app, base_url='http://localhost')
            self.assertEqual(http.get('/admin/settings/tls', headers=auth).status_code, 426)
            response = https.get('/admin/settings/tls', headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('PRIVATE KEY', response.text)
            download = https.get('/admin/settings/tls/certificate', headers=auth)
            self.assertIn('BEGIN CERTIFICATE', download.text)
            self.assertNotIn('PRIVATE KEY', download.text)
            self.assertEqual(https.post('/admin/settings/tls', headers={**auth, 'Origin': 'https://evil.example'}, json={'operation': 'generate', 'addresses': 'localhost'}).status_code, 403)
            before = tls.active(self.root)[0]['id']
            failed = https.post('/admin/settings/tls', headers=auth, json={'operation': 'upload', 'addresses': 'localhost', 'certificate': 'bad', 'private_key': 'bad'})
            self.assertEqual(failed.status_code, 400)
            self.assertEqual(tls.active(self.root)[0]['id'], before)
            self.assertFalse((self.root / 'state/restart-request.json').exists())
            result = https.post('/admin/settings/tls', headers=auth, json={'operation': 'generate', 'addresses': 'localhost'})
            self.assertEqual(result.status_code, 200)
            self.assertTrue((self.root / 'state/restart-request.json').exists())


if __name__ == '__main__': unittest.main()
