"""Account-owner property setup and persistent processing controls."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from hosting.controls import Controls, control_path
from hosting.ai_service import public_settings
from hosting.document_ui import authorize
from hosting.indexing import index_lock
from hosting.pdf_ingestion import write_atomic
from hosting.property_setup import DiscoveryError, discovery, read_selection, save_selection


def install(app, accounts, data_root=None):
    if len(accounts) != 1:
        raise ValueError('Admin setup supports one account per container')
    aid, account = next(iter(accounts.items()))
    root = Path(data_root or os.environ['TOOLKIT_DATA_DIR'])
    controls = Controls(control_path(root / 'state/controls.sqlite3'))

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 16000:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid JSON')
        if not isinstance(value, dict):
            raise HTTPException(400, 'Expected settings object')
        return value

    async def task(function):
        def run():
            try:
                return function()
            except HTTPException:
                raise
            except DiscoveryError as error:
                raise HTTPException(400, str(error))
            except (ValueError, KeyError):
                raise HTTPException(400, 'Unable to apply settings; check provider, model, API key/billing, selection or notification configuration.')
            except Exception:
                raise HTTPException(502, 'Hospitable or settings unavailable; retry later. No credentials displayed.')
        return await asyncio.to_thread(run)

    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/admin/settings'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/settings', response_class=HTMLResponse)
    async def page():
        return HTMLResponse(Path(__file__).with_name('admin_ui.html').read_text(), headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    @app.get('/admin/settings')
    async def status(request: Request):
        authorize(request)
        def snapshot():
            pending = read_selection(root, aid)
            return {'account_id': aid, 'account': controls.effective(aid, account),
                'properties': [{'id': pid, 'name': prop['name'], 'timezone': prop['timezone'],
                    'settings': controls.effective(aid, account, pid)} for pid, prop in account['properties'].items()],
                'pending_properties': [{'id': pid, **prop} for pid, prop in pending.items() if pid not in account['properties']],
                'setup_required': not bool(account['properties']), 'auto_responses_available': False,
                'heating_available': False, 'ai': public_settings(root, aid, account)}
        return await task(snapshot)

    @app.post('/admin/settings/discover')
    async def discover(request: Request):
        authorize(request)
        def fetch():
            found = discovery(account)
            selected = set(account['properties']) | set(read_selection(root, aid))
            return {'properties': [{'id': pid, **prop, 'imported': pid in selected} for pid, prop in found.items()]}
        return await task(fetch)

    @app.post('/admin/settings/import')
    async def import_properties(request: Request):
        authorize(request)
        value = await body(request)
        ids = value.get('property_ids')
        timezones = value.get('timezones', {})
        if not isinstance(timezones, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in timezones.items()):
            raise HTTPException(400, 'Invalid property timezone choices')
        if not isinstance(ids, list) or not ids or len(ids) > 64 or any(not isinstance(pid, str) for pid in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, 'Select 1–64 distinct properties')
        def save():
            found = discovery(account)  # Verify again with this account's PAT; do not trust the browser.
            if not set(ids) <= set(found):
                raise HTTPException(400, 'A selected property is not in this Hospitable account')
            if set(timezones) - set(ids):
                raise HTTPException(400, 'Timezone choices must belong to selected properties')
            from zoneinfo import ZoneInfo
            for pid in ids:
                timezone = timezones.get(pid) or found[pid].get('timezone')
                if not timezone:
                    raise HTTPException(400, 'Choose an IANA timezone for each selected property, such as Europe/London')
                try:
                    ZoneInfo(timezone)
                except (ValueError, KeyError):
                    raise HTTPException(400, 'Unrecognised timezone; use an IANA name such as Europe/London')
                found[pid] = {'name': found[pid]['name'], 'timezone': timezone}
            with index_lock(root, True, 'property-selection.lock'):
                selected = read_selection(root, aid)
                folders = {prop.get('folder', pid): pid for pid, prop in account['properties'].items()}
                if any(pid in folders and folders[pid] != pid for pid in ids):
                    raise HTTPException(400, 'A selected property conflicts with an existing data folder')
                selected.update({pid: found[pid] for pid in ids if pid not in account['properties']})
                if len(set(selected) | set(account['properties'])) > 64:
                    raise HTTPException(400, 'This container supports up to 64 properties')
                save_selection(root, aid, selected)
            return {'saved': True, 'restart_required': bool(set(selected) - set(account['properties'])), 'new_properties_start_paused': True}
        return await task(save)

    @app.post('/admin/settings/restart')
    async def restart(request: Request):
        authorize(request)
        value = await body(request)
        if value.get('confirm_restart') is not True:
            raise HTTPException(400, 'Confirm the account container restart')
        def request_restart():
            path = root / 'state/restart-request.json'
            if path.is_symlink():
                raise ValueError('Invalid restart request path')
            write_atomic(path, json.dumps({'actor': 'admin-ui', 'account_id': aid}))
            return {'restarting': True}
        return await task(request_restart)

    @app.post('/admin/settings/controls')
    async def update_controls(request: Request):
        authorize(request)
        value = await body(request)
        allowed = {'property_id', 'enabled', 'response_mode', 'email_enabled'}
        if set(value) - allowed:
            raise HTTPException(400, 'Unsupported setting')
        pid = value.get('property_id')
        if pid is not None and (not isinstance(pid, str) or pid not in account['properties']):
            raise HTTPException(404, 'Imported property not found')
        mode = value.get('response_mode')
        if mode is not None and mode not in {'draft', 'paused'}:
            raise HTTPException(409, 'Automatic guest responses are not implemented')
        options = {key: value[key] for key in ('enabled', 'email_enabled') if key in value}
        if mode is not None:
            if 'enabled' in options and options['enabled'] != (mode == 'draft'):
                raise HTTPException(400, 'Conflicting enabled and response mode settings')
            options.update(enabled=mode == 'draft', shadow=True)
        if not options:
            raise HTTPException(400, 'Choose a setting to change')
        def save():
            from hosting.notifications import validate_channel
            for channel, key in (('email', 'email_enabled'),):
                if options.get(key) is True:
                    validate_channel(account, pid, channel)
            controls.set('admin-ui', aid, pid, **options)
            inbox = root / 'state/inbox.sqlite3'
            if inbox.exists():
                with sqlite3.connect(inbox, timeout=10) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='inbox'").fetchone():
                        if pid:
                            db.execute("UPDATE inbox SET next_attempt=0 WHERE account=? AND property_id=? AND state='pending'", (aid, pid))
                        else:
                            db.execute("UPDATE inbox SET next_attempt=0 WHERE account=? AND state='pending'", (aid,))
            return controls.effective(aid, account, pid)
        return await task(save)

    @app.post('/admin/settings/ai/{operation}')
    async def ai_settings(operation: str, request: Request):
        authorize(request)
        if operation not in {'models', 'test', 'save'}:
            raise HTTPException(404, 'Unknown AI operation')
        value = await body(request)
        if set(value) - {'provider', 'model', 'api_key'}:
            raise HTTPException(400, 'Unsupported AI setting')
        from hosting.ai_service import validate, read_settings, key_for, list_models, test_connection, save_settings
        provider = value.get('provider')
        model = value.get('model')
        supplied = value.get('api_key') or None
        def apply():
            validate(provider, model, supplied)
            if operation != 'models' and model is None:
                raise HTTPException(400, 'Choose a model')
            if operation == 'save':
                return save_settings(root, aid, provider, model, supplied)
            key = key_for(read_settings(root, aid), provider, supplied)
            if operation == 'models':
                return {'models': list_models(provider, key)}
            return test_connection(provider, model, key)
        return await task(apply)
