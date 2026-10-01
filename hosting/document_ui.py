"""Authenticated, property-scoped document review for account operators."""
import asyncio
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from hosting.config import secret
from hosting.indexing import index_lock, rebuild
from hosting.pdf_ingestion import approve, confined_folder, convert_all, write_atomic

MAX_UPLOAD = 50 * 1024 * 1024
MAX_EDIT = 8 * 1024 * 1024


def authorize(request):
    expected = 'Bearer ' + secret('TOOLKIT_ADMIN_SECRET')
    if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), expected.encode()):
        raise HTTPException(401, 'Admin token required')
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403, 'Cross-origin requests are not allowed')


def install(app, accounts):
    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/admin/documents'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    properties = {pid: prop for account in accounts.values() for pid, prop in account['properties'].items()}

    def root_for(pid):
        prop = properties.get(pid)
        if not prop:
            raise HTTPException(404, 'Selected property not found')
        return Path(prop['runtime_dir'])

    def review_file(root, filename):
        if Path(filename).name != filename or not re.fullmatch(r'[A-Za-z0-9_-]+\.md', filename):
            raise HTTPException(400, 'Select a converted Markdown file')
        folder = confined_folder(root, 'document-review')
        path = folder / filename
        metadata = path.with_suffix('.json')
        if path.is_symlink() or metadata.is_symlink() or not path.is_file() or not metadata.is_file():
            raise HTTPException(404, 'Review document not found')
        return path, metadata

    async def body(request, limit):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise HTTPException(413, 'File or request exceeds the size limit')
            data.extend(chunk)
        return bytes(data)

    async def operation(root, function):
        def run():
            with index_lock(root, True, 'document-ui.lock'):
                try:
                    return function()
                except HTTPException:
                    raise
                except (ValueError, FileNotFoundError):
                    raise HTTPException(400, 'Document operation failed. Check source version, extracted pages and replacement options.')
                except Exception:
                    raise HTTPException(500, 'Operation failed; previous index is preserved. Inspect the source and retry.')
        return await asyncio.to_thread(run)

    @app.get('/documents', response_class=HTMLResponse)
    async def page():
        return HTMLResponse(Path(__file__).with_name('document_ui.html').read_text(), headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            'Referrer-Policy': 'no-referrer'})

    @app.get('/admin/documents/properties')
    async def selected(request: Request):
        authorize(request)
        from hosting.account_details import read as display_details
        import os
        names = {}
        if os.environ.get('TOOLKIT_DATA_DIR'):
            for aid in accounts:
                names.update(display_details(os.environ['TOOLKIT_DATA_DIR'], aid).get('properties', {}))
        return {'properties': [{'id': pid, 'name': names.get(pid, {}).get('nickname') or prop.get('name', pid)} for pid, prop in properties.items()]}

    @app.get('/admin/documents/{pid}')
    async def documents(pid: str, request: Request):
        authorize(request)
        root = root_for(pid)
        def listing():
            rows = []
            for path in confined_folder(root, 'document-review').glob('*.md'):
                _, meta = review_file(root, path.name)
                record = json.loads(meta.read_text())
                rows.append({'filename': path.name, 'source': record['source'], 'source_id':record.get('source_id'), 'conversion_version':record.get('conversion_version',1), 'converted_at':record.get('converted_at',0), 'blank_pages': record.get('blank_pages', []),
                             'approved': __import__('hosting.document_management',fromlist=['is_approved']).is_approved(root,record,path.read_text())})
            from hosting.document_management import mark_superseded
            mark_superseded(rows)
            report = confined_folder(root, 'document-review') / 'conversion-report.json'
            errors = [row for row in json.loads(report.read_text()) if row.get('status') == 'error'] if report.exists() and not report.is_symlink() else []
            return {'documents': rows, 'errors': errors}
        return await operation(root, listing)

    @app.post('/admin/documents/{pid}/upload')
    async def upload(pid: str, request: Request):
        authorize(request)
        root = root_for(pid)
        name = request.query_params.get('filename', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,119}\.pdf', name, re.I) or Path(name).name != name:
            raise HTTPException(400, 'Use a simple PDF filename (letters, numbers, spaces, dots, dashes).')
        data = await body(request, MAX_UPLOAD)
        if not data.startswith(b'%PDF-'):
            raise HTTPException(400, 'Upload a PDF file')
        def save():
            target = confined_folder(root, 'source-documents') / name
            # Exclusive creation protects archived originals from replacement.
            try:
                with target.open('xb') as file:
                    file.write(data)
            except FileExistsError:
                raise HTTPException(409, 'Filename already exists; rename the updated PDF before uploading.')
            return {'results': convert_all(root)}
        return await operation(root, save)

    @app.get('/admin/documents/{pid}/review/{filename}')
    async def read_review(pid: str, filename: str, request: Request):
        authorize(request)
        root = root_for(pid)
        def read():
            path, metadata = review_file(root, filename)
            text = path.read_text()
            return {'text': text, 'revision': hashlib.sha256(text.encode()).hexdigest(), 'metadata': json.loads(metadata.read_text())}
        return await operation(root, read)

    @app.post('/admin/documents/{pid}/review/{filename}')
    async def save_review(pid: str, filename: str, request: Request):
        authorize(request)
        root = root_for(pid)
        try:
            value = json.loads(await body(request, MAX_EDIT))
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid JSON')
        if not isinstance(value, dict) or not isinstance(value.get('text'), str) or not value['text'].strip():
            raise HTTPException(400, 'Markdown cannot be empty')
        def save():
            path, _ = review_file(root, filename)
            if value.get('revision') != hashlib.sha256(path.read_text().encode()).hexdigest():
                raise HTTPException(409, 'Document changed; reopen it before saving.')
            write_atomic(path, value['text'])
            return {'revision': hashlib.sha256(value['text'].encode()).hexdigest()}
        return await operation(root, save)

    @app.post('/admin/documents/{pid}/approve/{filename}')
    async def approve_review(pid: str, filename: str, request: Request):
        authorize(request)
        root = root_for(pid)
        try:
            value = json.loads(await body(request, 4096))
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid JSON')
        if not isinstance(value, dict) or value.get('confirmed_guest_safe') is not True:
            raise HTTPException(400, 'Confirm that the document contains approved guest information')
        def publish():
            path, _ = review_file(root, filename)
            if value.get('revision') != hashlib.sha256(path.read_text().encode()).hexdigest():
                raise HTTPException(409, 'Document changed; review it again before approval.')
            destination = approve(root, filename, value.get('replace') is True, value.get('allow_incomplete') is True)
            return {'approved_document': destination.name, 'index_update_required': True}
        return await operation(root, publish)

    @app.post('/admin/documents/{pid}/reindex')
    async def update_index(pid: str, request: Request):
        authorize(request)
        return await operation(root_for(pid), lambda: rebuild(root_for(pid)))

    @app.get('/admin/documents/{pid}/approved')
    async def approved_documents(pid: str, request: Request):
        authorize(request)
        from hosting.document_management import listing, document, revision
        root=root_for(pid)
        def read():
            name=request.query_params.get('filename')
            if name:
                path=document(root,name)
                return {'text':path.read_text(),'revision':revision(path)}
            return {'documents':listing(root)}
        return await operation(root,read)

    @app.post('/admin/documents/{pid}/approved')
    async def change_approved(pid: str, request: Request):
        authorize(request)
        from hosting.document_management import change
        try: value=json.loads(await body(request,MAX_EDIT))
        except (ValueError,UnicodeError): raise HTTPException(400,'Invalid JSON')
        if not isinstance(value,dict): raise HTTPException(400,'Invalid request')
        root=root_for(pid)
        return await operation(root,lambda:change(root,request.query_params.get('filename'),value))

    @app.post('/admin/documents/{pid}/test-question')
    async def test_question(pid: str, request: Request):
        authorize(request)
        root=root_for(pid)
        try: value=json.loads(await body(request,16384))
        except (ValueError,UnicodeError): raise HTTPException(400,'Invalid JSON')
        question=value.get('question') if isinstance(value,dict) else None
        if not isinstance(question,str) or not question.strip() or len(question)>4000:
            raise HTTPException(400,'Enter a question up to 4000 characters')
        def preview():
            from hosting.indexing import retrieve
            from hosting.worker import prepare_draft
            return {'sources':retrieve(root,question.strip()),'result':prepare_draft(properties[pid],question.strip()),'sent':False}
        return await operation(root,preview)

    @app.get('/admin/documents/{pid}/pdfs')
    async def pdfs(pid: str, request: Request):
        authorize(request)
        from hosting.document_management import pdf_listing
        root=root_for(pid)
        return await operation(root,lambda:{'documents':pdf_listing(root)})

    @app.post('/admin/documents/{pid}/delete-pdf')
    async def remove_pdf(pid: str, request: Request):
        authorize(request)
        from hosting.document_management import delete_pdf
        try: value=json.loads(await body(request,4096))
        except (ValueError,UnicodeError): raise HTTPException(400,'Invalid JSON')
        if not isinstance(value,dict): raise HTTPException(400,'Invalid request')
        root=root_for(pid)
        return await operation(root,lambda:delete_pdf(root,request.query_params.get('filename'),value))
