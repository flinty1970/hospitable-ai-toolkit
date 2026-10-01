"""Admin-authenticated review and webhook setup; no guest sends."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit
import httpx
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from hosting.document_ui import authorize
from hosting.config import secret
from hosting.operations import Operations


def install(app, accounts, data_root=None):
    if len(accounts) != 1:
        raise ValueError('One account per operations page')
    aid, account = next(iter(accounts.items()))
    root = Path(data_root or os.environ['TOOLKIT_DATA_DIR'])
    ops = Operations(root, aid, account)

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16000:
                raise HTTPException(413, 'Request too large')
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid request')

    async def task(fn):
        def run():
            try:
                return fn()
            except ValueError:
                raise HTTPException(409, 'Record changed or unavailable; refresh before trying again')
            except Exception:
                raise HTTPException(503, 'Operational records unavailable; check container storage')
        return await asyncio.to_thread(run)

    @app.middleware('http')
    async def private(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/admin/operations'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.get('/review')
    async def page():
        return HTMLResponse(Path(__file__).with_name('operations_ui.html').read_text(), headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    @app.get('/admin/operations')
    async def status(request: Request):
        authorize(request)
        return await task(ops.status)

    @app.get('/admin/operations/reviews')
    async def reviews(request: Request, property_id: str = '', handled: bool = False, offset: int = 0):
        authorize(request)
        if property_id and property_id not in account['properties'] or not 0 <= offset <= 20000:
            raise HTTPException(400, 'Invalid selection')
        def snapshot():
            rows = [r for r in ops.items() if (not property_id or r['property_id'] == property_id) and (handled or not r['handled'])]
            return {'items': rows[offset:offset+50], 'total': len(rows), 'next_offset': offset+50 if offset+50 < len(rows) else None,
                    'properties': [{'id': pid, 'name': prop['name']} for pid, prop in account['properties'].items()]}
        return await task(snapshot)

    @app.post('/admin/operations/reviews/{identity}')
    async def mark(identity: str, request: Request):
        authorize(request)
        value = await body(request)
        if value.get('confirm') is not True:
            raise HTTPException(400, 'Confirm the review decision')
        return await task(lambda: ops.mark(identity, value.get('revision'), value.get('handled'), value.get('note', '')))

    @app.post('/admin/operations/webhook-url')
    async def webhook_url(request: Request):
        authorize(request)
        value = await body(request)
        base = value.get('base_url')
        if not isinstance(base, str) or len(base) > 2048:
            raise HTTPException(400, 'Enter the public HTTPS origin')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            raise HTTPException(400, 'Use an HTTPS origin such as https://toolkit.example.com')
        # Only formats a URL. Never connects to a browser-supplied host.
        return {'url': base.rstrip('/') + '/webhook/hospitable/' + quote(aid, safe='') + '?token=' + quote(secret(account['webhook_secret_env']), safe='')}

    @app.post('/admin/operations/probe')
    async def probe(request: Request):
        authorize(request)
        # Runs the real handler in-process; no external network or guest event.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://toolkit-local') as client:
            path = '/webhook/hospitable/' + quote(aid, safe='')
            denied = await client.post(path, params={'token': 'invalid-probe'}, json={'action': 'toolkit.connection_test', 'data': {}})
            accepted = await client.post(path, params={'token': secret(account['webhook_secret_env'])}, json={'action': 'toolkit.connection_test', 'data': {}})
        if denied.status_code != 401 or accepted.status_code != 200 or accepted.json().get('action') != 'ignored':
            raise HTTPException(503, 'Local webhook authentication test failed')
        return {'ok': True, 'external_delivery_verified': False, 'message': 'Local authentication and handler test passed. This does not test DNS, HTTPS, firewall or delivery from Hospitable.'}
