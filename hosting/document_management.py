"""Revision checked approved knowledge edits with index publication and rollback."""
import hashlib
import json
import uuid
from pathlib import Path
from hosting.pdf_ingestion import confined_folder, write_atomic
from hosting.indexing import rebuild

def document(root, name):
    if not isinstance(name,str) or not name or '\\' in name:
        raise ValueError('Invalid document path')
    relative=Path(name)
    if relative.is_absolute() or any(p in {'.','..'} for p in relative.parts) or relative.suffix.lower()!='.md':
        raise ValueError('Invalid document path')
    folder=confined_folder(root,'docs');path=folder
    for part in relative.parts:
        path=path/part
        if path.is_symlink(): raise ValueError('Symlinks are not supported')
    if not path.is_file() or folder.resolve() not in path.resolve().parents:
        raise ValueError('Document not found')
    return path

def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def listing(root):
    folder=confined_folder(root,'docs')
    rows=[]
    for path in sorted(folder.rglob('*.md')):
        name=path.relative_to(folder).as_posix()
        try: safe=document(root,name)
        except ValueError: continue
        rows.append({'filename':name,'revision':revision(safe)})
    return rows

def change(root, name, value, builder=None):
    path=document(root,name)
    if value.get('revision')!=revision(path):
        raise ValueError('Document changed; reopen before making changes')
    action=value.get('action')
    if action not in {'delete','replace'} or value.get('confirmed') is not True:
        raise ValueError('Confirm deletion or replacement')
    text=value.get('text')
    if action=='replace' and (not isinstance(text,str) or not text.strip() or value.get('guest_safe') is not True):
        raise ValueError('Approve replacement text as guest safe')
    original=path.read_bytes()
    archive=confined_folder(root,'document-archive')/uuid.uuid4().hex
    archive.mkdir(mode=0o700)
    (archive/'document.md').write_bytes(original)
    write_atomic(archive/'record.json',json.dumps({'filename':name,'action':action}))
    if action=='delete': path.unlink()
    else: write_atomic(path,text.strip()+'\n')
    try:
        result=rebuild(root,builder=builder,allow_empty=True)
    except Exception:
        path.write_bytes(original)
        raise
    return {'action':action,'chunks':result['chunks'],'index_updated':True}

def is_approved(root, record, text):
    try:
        path=document(root,record.get('approved_document'))
        digest=record.get('approved_sha256')
        return digest==hashlib.sha256(text.strip().encode()).hexdigest() and digest==hashlib.sha256(path.read_text().strip().encode()).hexdigest()
    except ValueError:
        return False

def pdf_file(root,name):
    if not isinstance(name,str) or Path(name).name!=name or not name.lower().endswith('.pdf') or '\\' in name:
        raise ValueError('Invalid PDF filename')
    path=confined_folder(root,'source-documents')/name
    if path.is_symlink() or not path.is_file(): raise ValueError('PDF not found')
    return path

def pdf_listing(root):
    rows=[]
    for path in sorted(confined_folder(root,'source-documents').iterdir()):
        if path.suffix.lower()=='.pdf' and path.is_file() and not path.is_symlink():
            rows.append({'filename':path.name,'revision':revision(path)})
    return rows

def delete_pdf(root,name,value,builder=None):
    path=pdf_file(root,name)
    if value.get('confirmed') is not True or value.get('revision')!=revision(path):
        raise ValueError('Confirm the current PDF version before deleting')
    targets=[path]
    review=confined_folder(root,'document-review')
    for metadata in review.glob('*.json'):
        if metadata.is_symlink() or metadata.name=='conversion-report.json': continue
        record=json.loads(metadata.read_text())
        if not isinstance(record,dict) or record.get('source')!=name: continue
        targets.append(metadata)
        markdown=metadata.with_suffix('.md')
        if markdown.is_symlink(): raise ValueError('Review symlink is not supported')
        if markdown.exists(): targets.append(markdown)
        source_id=record.get('source_id')
        import re
        if not isinstance(source_id,str) or not re.fullmatch(r'[a-f0-9]+',source_id): raise ValueError('Invalid PDF identity')
        approved=confined_folder(root,'docs')/('pdf-'+source_id+'.md')
        if approved.is_symlink(): raise ValueError('Approved symlink is not supported')
        if approved.exists(): targets.append(approved)
    originals={target:target.read_bytes() for target in set(targets)}
    archive=confined_folder(root,'document-archive')/uuid.uuid4().hex
    archive.mkdir(mode=0o700)
    for i,(target,content) in enumerate(originals.items()):
        (archive/str(i)).write_bytes(content)
    write_atomic(archive/'record.json',json.dumps({'action':'delete_pdf','filename':name,'files':[str(p.relative_to(root)) for p in originals]}))
    try:
        for target in originals: target.unlink()
        result=rebuild(root,builder=builder,allow_empty=True)
    except Exception:
        for target,content in originals.items(): target.write_bytes(content)
        raise
    # The conversion report is auxiliary; deleted PDFs must not leave error rows.
    report=review/'conversion-report.json'
    if report.exists() and not report.is_symlink():
        try:
            rows=json.loads(report.read_text())
            write_atomic(report,json.dumps([r for r in rows if r.get('source')!=name]))
        except (ValueError,OSError): pass
    return {'deleted_pdf':name,'index_updated':True,'chunks':result['chunks']}
