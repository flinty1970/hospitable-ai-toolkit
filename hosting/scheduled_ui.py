"""Authenticated browser scheduling interface."""
import asyncio
import json
import os
from pathlib import Path
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from hosting.document_ui import authorize
from hosting.scheduled_messages import Queue, reservations
from hosting.account_details import read


def install(app,accounts,data_root=None):
    aid,account=next(iter(accounts.items()));root=Path(data_root or os.environ['TOOLKIT_DATA_DIR']);queue=Queue(root,aid,account)
    async def task(fn):
        try:return await asyncio.to_thread(fn)
        except (ValueError,KeyError) as error:
            if str(error).startswith('This time occurs twice when clocks change'):
                raise HTTPException(409, {'code':'ambiguous_time','message':'The clocks move back at this time, so it occurs twice. Choose the earlier or later occurrence, then schedule again.'})
            if str(error).startswith('This local time does not exist because clocks change'):
                raise HTTPException(400, 'The clocks move forward past this time. Choose a different send time.')
            if str(error).startswith('For a Hospitable message, choose a send time outside'):
                raise HTTPException(400, 'For Hospitable messages, choose a time outside the repeated clock-change hour.')
            if 'MCP' in str(error) or str(error).startswith(('Native message','Unexpected scheduled message')):
                raise HTTPException(400, 'Unable to load or edit Hospitable messages. Check the fallback MCP connection, account and booking; refresh before retrying an edit.')
            raise HTTPException(400,'Unable to schedule/cancel: check reservation ownership, future property-local time and tested owner email. Delivery may already be in progress.')
        except Exception:raise HTTPException(502,'Reservation or scheduling service unavailable. Check status before trying again.')
    async def body(request):
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>16000:raise HTTPException(413,'Request too large')
        try:value=json.loads(data)
        except (ValueError,UnicodeError):raise HTTPException(400,'Invalid JSON')
        if not isinstance(value,dict):raise HTTPException(400,'Expected scheduling object')
        return value
    def prop(pid):
        if pid not in account['properties']:raise HTTPException(404,'Property not found')
    @app.middleware('http')
    async def private(request,call_next):
        response=await call_next(request)
        if request.url.path.startswith('/admin/scheduled'):response.headers['Cache-Control']='no-store'
        return response
    @app.get('/scheduled',response_class=HTMLResponse)
    async def page():
        return HTMLResponse(Path(__file__).with_name('scheduled_ui.html').read_text(),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
    @app.get('/admin/scheduled/properties')
    async def properties(request:Request):
        authorize(request)
        def snapshot():
            display=read(root,aid)
            return {'properties':[{'id':pid,'name':display.get('properties',{}).get(pid,{}).get('nickname') or p['name'],'timezone':p['timezone']} for pid,p in account['properties'].items()]}
        return await task(snapshot)
    @app.get('/admin/scheduled/connection')
    async def connection(request:Request):
        authorize(request)
        from hosting.hospitable_schedules import settings
        def status():
            value=settings(root,aid)
            return {'configured':bool(value),'verified_at':value.get('verified_at') if value else None}
        return await task(status)
    @app.post('/admin/scheduled/connection')
    async def connect_native(request:Request):
        authorize(request);value=await body(request)
        from hosting.hospitable_schedules import save,disconnect,test_connection
        if value.get('disconnect') is True:return await task(lambda:disconnect(root))
        if value.get('test') is True:return await task(lambda:test_connection(root,aid,account))
        return await task(lambda:save(root,aid,account,value.get('token')))
    @app.get('/admin/scheduled/{pid}/reservations')
    async def listing(pid:str,request:Request,page:int=1):
        authorize(request);prop(pid);return await task(lambda:reservations(account,pid,page))
    @app.get('/admin/scheduled/{pid}')
    async def schedules(pid:str,request:Request):
        authorize(request);prop(pid);return await task(lambda:{'messages':queue.list(pid)})
    @app.post('/admin/scheduled/{pid}')
    async def create(pid:str,request:Request):
        authorize(request);prop(pid);value=await body(request)
        if value.get('confirm_send') is not True:raise HTTPException(400,'Confirm this scheduled guest send')
        return await task(lambda:queue.create(pid,value.get('reservation_id'),value.get('local_time'),value.get('message'),'admin-ui',value.get('fold')))
    @app.post('/admin/scheduled/{pid}/{identity}/cancel')
    async def cancel(pid:str,identity:str,request:Request):
        authorize(request);prop(pid);return await task(lambda:queue.cancel(pid,identity,'admin-ui'))
    @app.post('/admin/scheduled/{pid}/{identity}/edit')
    async def edit(pid:str,identity:str,request:Request):
        authorize(request);prop(pid);value=await body(request)
        return await task(lambda:queue.update(pid,identity,value,'admin-ui'))
    @app.get('/admin/scheduled/{pid}/native')
    async def native(pid:str,request:Request,reservation_id:str):
        authorize(request);prop(pid)
        from hosting.hospitable_schedules import listing
        return await task(lambda:{'messages':listing(root,aid,account,pid,reservation_id)})
    @app.post('/admin/scheduled/{pid}/native/{identity}/edit')
    async def edit_native(pid:str,identity:str,request:Request):
        authorize(request);prop(pid);value=await body(request)
        from hosting.hospitable_schedules import update
        return await task(lambda:update(root,aid,account,pid,value.get('reservation_id'),identity,value))
