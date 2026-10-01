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

class PDFDeleteTests(unittest.TestCase):
    def test_pdf_reviews_knowledge_and_rollback(self):
        import json
        from hosting.document_management import delete_pdf, pdf_listing
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ['docs','source-documents','document-review']: (root/name).mkdir()
            pdf=root/'source-documents/guide.pdf';pdf.write_bytes(b'%PDF-original')
            md=root/'docs/pdf-abcd.md';md.write_text('Shower guide')
            review=root/'document-review/converted.md';review.write_text('Shower guide')
            meta=review.with_suffix('.json');meta.write_text(json.dumps({'source':'guide.pdf','source_id':'abcd'}))
            value={'confirmed':True,'revision':pdf_listing(root)[0]['revision']}
            def fail(*args):raise RuntimeError('failed')
            with self.assertRaises(RuntimeError):delete_pdf(root,'guide.pdf',value,fail)
            self.assertTrue(all(p.exists() for p in [pdf,md,review,meta]))
            self.assertEqual(delete_pdf(root,'guide.pdf',value,lambda *args:None)['chunks'],0)
            self.assertFalse(any(p.exists() for p in [pdf,md,review,meta]))
            self.assertEqual(pdf_listing(root),[])
    def test_bad_filename_and_stale_revision(self):
        from hosting.document_management import pdf_file,delete_pdf
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'source-documents').mkdir();(root/'source-documents/g.pdf').write_bytes(b'pdf')
            with self.assertRaises(ValueError):pdf_file(root,'../g.pdf')
            with self.assertRaises(ValueError):delete_pdf(root,'g.pdf',{'confirmed':True,'revision':'stale'})

class SupersededTests(unittest.TestCase):
    def test_approved_new_conversion_hides_old_but_not_new_pending(self):
        from hosting.document_management import mark_superseded
        rows=[dict(source='guide.pdf',filename='old.md',conversion_version=1,approved=False),dict(source='guide.pdf',filename='current.md',conversion_version=2,approved=True),dict(source='guide.pdf',filename='next.md',conversion_version=3,approved=False)]
        mark_superseded(rows)
        self.assertTrue(rows[0]['superseded']);self.assertFalse(rows[1]['superseded']);self.assertFalse(rows[2]['superseded'])
    def test_pending_replacement_does_not_hide_current_approval(self):
        from hosting.document_management import mark_superseded
        rows=[dict(source='guide.pdf',filename='old.md',conversion_version=1,approved=True),dict(source='guide.pdf',filename='new.md',conversion_version=2,approved=False)]
        self.assertFalse(any(r['superseded'] for r in mark_superseded(rows)))
