"""Optional native schedules via Hospitable's documented MCP tools.

Fixed upstream, independent credentials, verified account/property ownership.
Native messages remain with Hospitable and are never queued for local delivery.
"""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
import httpx
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from hosting.config import secret
from hosting.account_details import picture_url
from urllib.parse import urlsplit
from hosting.gateway import API_BASE, object_id
from hosting.pdf_ingestion import write_atomic
from hosting.indexing import index_lock
from hosting.scheduled_messages import property_id, reservation, local_due

URL='https://mcp.hospitable.com/mcp'
ALLOWED={'get-user','get-reservation','get-reservation-scheduled-messages','update-scheduled-message'}

def payload(result):
    if result.isError:raise ValueError('Hospitable MCP operation failed')
    value=result.structuredContent
    if not isinstance(value,dict):
        blocks=[c.text for c in result.content if getattr(c,'type',None)=='text']
        if len(blocks)!=1:raise ValueError('Unexpected Hospitable MCP response')
        value=json.loads(blocks[0])
    return value.get('data',value)

async def _call(token,name,args):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=30,follow_redirects=False) as client:
        async with streamable_http_client(URL,http_client=client) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                schema=payload(await session.call_tool('get-tool-schema',{'tools':[name]}))
                if not isinstance(schema,list) or len(schema)!=1 or schema[0].get('status')!='ok':raise ValueError('Native tool schema unavailable')
                contract=schema[0].get('inputSchema',{})
                if set(args)-set(contract.get('properties',{})) or set(contract.get('required',[]))-set(args):raise ValueError('Native tool schema changed')
                return payload(await session.call_tool(name,args))

def call(token,name,args):
    if name not in ALLOWED:raise ValueError('Unsupported native schedule operation')
    try:return asyncio.run(asyncio.wait_for(_call(token,name,args),45))
    except Exception:raise ValueError('Hospitable MCP request failed; refresh messages before retrying an edit') from None

def settings(root,aid):
    path=Path(root)/'state/hospitable-mcp.json'
    if path.is_symlink():raise ValueError('Invalid MCP credential path')
    if not path.exists():return None
    if path.stat().st_mode & 0o077:raise ValueError('MCP credential file must be private')
    value=json.loads(path.read_text())
    if value.get('account_id')!=aid or not isinstance(value.get('token'),str):raise ValueError('Invalid MCP account configuration')
    return value

def pat_user(account):
    response=requests.get(API_BASE+'/user',headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
    if response.status_code!=200:raise ValueError('Unable to verify Hospitable account')
    row=response.json().get('data',{})
    identity=object_id(row.get('id')) if isinstance(row,dict) else ''
    if not identity:raise ValueError('Hospitable account identity unavailable')
    return identity

def save(root,aid,account,token):
    if not isinstance(token,str) or not 16<=len(token.strip())<=4096 or any(ord(c)<=32 for c in token.strip()):raise ValueError('Enter a Hospitable fallback MCP token')
    token=token.strip();user=call(token,'get-user',{})
    if not isinstance(user,dict) or object_id(user.get('id'))!=pat_user(account):raise ValueError('MCP token belongs to a different Hospitable account')
    path=Path(root)/'state/hospitable-mcp.json';path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with index_lock(root,True,'hospitable-mcp.lock'):
        write_atomic(path,json.dumps({'account_id':aid,'token':token,'verified_at':time.time()}))
    return {'configured':True}

def test_connection(root,aid,account):
    value=settings(root,aid)
    if not value:raise ValueError('Connect Hospitable MCP first')
    user=call(value['token'],'get-user',{})
    if not isinstance(user,dict) or object_id(user.get('id'))!=pat_user(account):raise ValueError('MCP token belongs to a different Hospitable account')
    with index_lock(root,True,'hospitable-mcp.lock'):
        current=settings(root,aid)
        if not current or current['token']!=value['token']:raise ValueError('MCP connection changed; test it again')
        current['verified_at']=time.time()
        write_atomic(Path(root)/'state/hospitable-mcp.json',json.dumps(current))
    return {'tested':True,'verified_at':current['verified_at']}

def disconnect(root):
    path=Path(root)/'state/hospitable-mcp.json'
    with index_lock(root,True,'hospitable-mcp.lock'):
        if path.is_symlink():raise ValueError('Invalid credential path')
        path.unlink(missing_ok=True)
    return {'configured':False}

def revision(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()

def image_attachments(row):
    """Only expose explicit image attachments, never URLs inferred from message text."""
    rows=row.get('attachments',[])
    if isinstance(rows,dict): rows=rows.get('data',[])
    if not isinstance(rows,list): return []
    result=[];seen=set()
    for item in rows[:50]:
        if isinstance(item,str): item={'url':item}
        if not isinstance(item,dict): continue
        url=picture_url(item.get('url'))
        if not url or url in seen: continue
        mime=item.get('mime_type') or item.get('content_type') or ''
        kind=item.get('type')
        extension=urlsplit(url).path.lower().endswith(('.jpg','.jpeg','.png','.gif','.webp','.avif'))
        if not ((isinstance(mime,str) and mime.startswith('image/')) or kind=='image' or extension): continue
        seen.add(url);result.append({'url':url,'name':str(item.get('filename') or item.get('name') or 'Message image')[:200]})
    return result

def listing(root,aid,account,pid,rid):
    if pid not in account['properties']:raise ValueError('Unknown property')
    reservation(account,pid,rid)
    value=settings(root,aid)
    if not value:raise ValueError('Connect Hospitable MCP first')
    token=value['token']
    current=call(token,'get-reservation',{'identifier':rid,'include':'properties'})
    if not isinstance(current,dict) or property_id(current)!=pid:raise ValueError('MCP reservation belongs to another property/account')
    rows=call(token,'get-reservation-scheduled-messages',{'identifier':rid})
    if not isinstance(rows,list) or len(rows)>500:raise ValueError('Unexpected scheduled message list')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or (row.get('message') is not None and not isinstance(row.get('message'),str)):raise ValueError('Unexpected scheduled message format')
        state='sent' if row.get('sent_at') else 'cancelled' if row.get('cancelled_at') else 'failed' if row.get('failed') else 'pending'
        result.append({'id':row['id'],'source':'hospitable','reservation':rid,'title':row.get('title') or 'Hospitable message','body':row.get('message') or '', 'body_available':isinstance(row.get('message'),str),'images':image_attachments(row),'local_time':row.get('scheduled_for'),'timezone':row.get('timezone'),'state':state,'revision':revision(row)})
    return result

def update(root,aid,account,pid,rid,identity,value):
    body=value.get('message')
    if value.get('confirm_send') is not True or not isinstance(body,str) or not body.strip() or len(body)>5000:raise ValueError('Confirm edited message')
    row=next((r for r in listing(root,aid,account,pid,rid) if r['id']==identity),None)
    if not row or row['state']!='pending' or row['revision']!=value.get('revision'):raise ValueError('Native message changed or is no longer pending; refresh before editing')
    zone=account['properties'][pid]['timezone']
    if row['timezone']!=zone:raise ValueError('Hospitable message timezone differs from property settings; edit in Hospitable')
    try:local_due(value.get('local_time'),zone)
    except ValueError as error:
        if str(error).startswith('This time occurs twice'):raise ValueError('For a Hospitable message, choose a send time outside the repeated clock-change hour')
        raise
    formatted=datetime.fromisoformat(value['local_time']).strftime('%Y-%m-%d %H:%M')
    # Never use send_now, and never retry a mutation: the first attempt may succeed.
    call(settings(root,aid)['token'],'update-scheduled-message',{'id':identity,'message':body.strip(),'scheduled_for':formatted,'send_now':False})
    return {'updated':True}
