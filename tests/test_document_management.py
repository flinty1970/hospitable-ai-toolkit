import tempfile
import unittest
from pathlib import Path
from hosting.document_management import change, listing, document
class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'docs').mkdir();self.path=self.root/'docs/guide.md';self.path.write_text('Old knowledge')
    def value(self,action):return dict(action=action,revision=listing(self.root)[0]['revision'],confirmed=True,text='New knowledge',guest_safe=True)
    def test_replace_delete_index_and_archives(self):
        records=[]
        def build(path,chunks):records.extend(chunks)
        self.assertTrue(change(self.root,'guide.md',self.value('replace'),build)['index_updated'])
        self.assertEqual(self.path.read_text(),'New knowledge\n')
        self.assertIn('New knowledge',str(records))
        self.assertEqual(change(self.root,'guide.md',self.value('delete'),build)['chunks'],0)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list((self.root/'document-archive').glob('*/document.md'))),2)
    def test_failed_index_restores_previous_document(self):
        def fail(*args):raise RuntimeError('index failed')
        with self.assertRaises(RuntimeError):change(self.root,'guide.md',self.value('delete'),fail)
        self.assertEqual(self.path.read_text(),'Old knowledge')
    def test_revision_and_traversal_and_symlink(self):
        value=self.value('replace');value['revision']='stale'
        with self.assertRaises(ValueError):change(self.root,'guide.md',value,lambda *a:None)
        for name in ['../guide.md','/etc/test.md']:
            with self.assertRaises(ValueError):document(self.root,name)
        (self.root/'docs/link.md').symlink_to(self.path)
        with self.assertRaises(ValueError):document(self.root,'link.md')

class PreviewTests(unittest.TestCase):
    def test_authenticated_preview_and_sources_without_sending(self):
        import os
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from hosting.document_ui import install
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'TOOLKIT_ADMIN_SECRET':'admin'}):
            app=FastAPI();install(app,{'a':{'properties':{'p':{'runtime_dir':root}}}});client=TestClient(app)
            self.assertEqual(client.post('/admin/documents/p/test-question',json={'question':'Shower?'}).status_code,401)
            with patch('hosting.indexing.retrieve',return_value=[{'text':'Press power','source':{'source':'shower.md'}}]),patch('hosting.worker.prepare_draft',return_value={'action':'draft','answer':'Press power','reason':'Guide'}) as draft,patch('hosting.guest_sending.send') as send:
                r=client.post('/admin/documents/p/test-question',headers={'Authorization':'Bearer admin'},json={'question':'Shower?'})
                self.assertEqual(r.status_code,200);self.assertFalse(r.json()['sent']);self.assertEqual(r.json()['sources'][0]['source']['source'],'shower.md');draft.assert_called_once();send.assert_not_called()
