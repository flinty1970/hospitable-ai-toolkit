import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from hosting.backup import backup, restore, verify, stopped
from hosting.controls import Controls


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.instance = self.root/'original'
        (self.instance/'data/config').mkdir(parents=True); (self.instance/'secrets').mkdir()
        (self.instance/'instance.env').write_text('INSTANCE_DIR='+str(self.instance))
        (self.instance/'data/config/account.json').write_text(json.dumps({'account':{'id':'owner','enabled':True,'shadow':False,'properties':{}}}))
        (self.instance/'secrets/credentials.env').write_text('TOKEN=private')
        state=self.instance/'data/state'; state.mkdir()
        with sqlite3.connect(state/'ledger.sqlite3') as db:
            db.execute('CREATE TABLE sent(id TEXT)'); db.execute("INSERT INTO sent VALUES('already-sent')")
        Controls(state/'controls.sqlite3').set('owner','owner',enabled=True,shadow=False)
        (self.instance/'data/model-cache').mkdir(); (self.instance/'data/model-cache/model').write_bytes(b'cache')
        self.archive=self.root/'backups/instance.tar.gz'

    def create(self):
        with patch('hosting.backup.stopped'): return backup(self.instance,self.archive,'owner')

    def test_roundtrip_preserves_secrets_ledger_and_pauses_account(self):
        self.create(); self.assertEqual(self.archive.stat().st_mode & 0o777,0o600)
        self.assertTrue(verify(self.archive)['verified'])
        target=self.root/'restored'; result=restore(self.archive,target); self.assertTrue(result['account_paused'])
        self.assertEqual((target/'secrets/credentials.env').read_text(),'TOKEN=private')
        self.assertFalse((target/'data/model-cache').exists())
        with sqlite3.connect(target/'data/state/ledger.sqlite3') as db: self.assertEqual(db.execute('SELECT id FROM sent').fetchone()[0],'already-sent')
        config=json.loads((target/'data/config/account.json').read_text()); self.assertFalse(config['account']['enabled'])
        self.assertFalse(Controls(target/'data/state/controls.sqlite3').get('owner',None,{})['enabled'])
        with self.assertRaises(ValueError): restore(self.archive,target)

    def test_backup_refuses_symlink_and_existing_archive(self):
        (self.instance/'data/link').symlink_to(self.instance/'secrets/credentials.env')
        with self.assertRaises(ValueError): self.create()
        (self.instance/'data/link').unlink(); self.create()
        with self.assertRaises(FileExistsError): self.create()

    def test_archive_traversal_and_corruption_rejected_without_destination(self):
        self.create()
        bad=self.root/'bad.tar.gz'
        with tarfile.open(bad,'w:gz') as tar:
            info=tarfile.TarInfo('../escape'); info.size=1; tar.addfile(info,io.BytesIO(b'x'))
        target=self.root/'bad-restore'
        with self.assertRaises(ValueError): restore(bad,target)
        self.assertFalse(target.exists())
        with tarfile.open(self.archive) as source, tarfile.open(bad,'w:gz') as tar:
            for member in source.getmembers():
                data=source.extractfile(member).read()
                if member.name=='secrets/credentials.env': data=b'changed'; member.size=len(data)
                tar.addfile(member,io.BytesIO(data))
        with self.assertRaises(ValueError): verify(bad)

    def test_stopped_guard_checks_project_and_other_container_mounts(self):
        with patch('hosting.backup.subprocess.run',return_value=Mock(stdout='container')):
            with self.assertRaises(ValueError): stopped(self.instance,'owner')
        results=[Mock(stdout=''),Mock(stdout='container'),Mock(stdout=json.dumps([{'Mounts':[{'Source':str(self.instance/'data')}]}]))]
        with patch('hosting.backup.subprocess.run',side_effect=results):
            with self.assertRaises(ValueError): stopped(self.instance,'owner')


if __name__ == '__main__': unittest.main()
