"""At-most-once guest sends; ambiguous outcomes require human review."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from hosting.config import secret
from hosting.gateway import API_BASE, object_id, resolve_property

class SendReview(ValueError): pass

def timestamp(value):
    if not isinstance(value,str): raise SendReview('Message time not confirmed')
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed.timestamp()
    except (ValueError,TypeError): raise SendReview('Message time not confirmed')

def target(data):
    identity=object_id(data.get('reservation_id') or data.get('reservation'))
    kind='reservations'
    if not identity:
        identity=object_id(data.get('conversation_id') or data.get('conversation'))
        kind='inquiries'
    if not identity: raise SendReview('Conversation identity not confirmed')
    return kind,identity

def verify_current(account, property_id, payload, effective):
    data=payload['data'];created=timestamp(data.get('created_at'))
    if created < effective['auto_enabled_at'] or time.time()-created>600 or created>time.time()+60:
        raise SendReview('Old or queued message requires human review; automatic replies apply to new messages only')
    if resolve_property(account,payload,get=requests.get)!=property_id: raise SendReview('Property identity changed')
    kind,identity=target(data)
    response=requests.get(API_BASE+'/'+kind+'/'+quote(identity,safe='')+'/messages',
        params={'per_page':100},headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
    if response.status_code!=200: raise SendReview('Current conversation unavailable')
    value=response.json();rows=value.get('data')
    if not isinstance(rows,list) or not rows: raise SendReview('Current conversation not confirmed')
    meta=value.get('meta') or {}
    if (value.get('links') or {}).get('next') or (type(meta.get('last_page')) is int and meta['last_page']>1):
        raise SendReview('Full conversation cannot be confirmed within the automatic reply limit')
    if any(not isinstance(row,dict) for row in rows): raise SendReview('Unexpected conversation response')
    latest=max(rows,key=lambda row:timestamp(row.get('created_at')))
    sender=latest.get('sender')
    role=str(latest.get('sender_type') or latest.get('sender_role') or (sender.get('type') if isinstance(sender,dict) else '') or '').lower()
    if object_id(latest.get('id'))!=object_id(data.get('id')) or role!='guest':
        raise SendReview('A newer message or host response exists; human review required')
    return kind,identity

def send(root, aid, property_id, account, payload, answer, controls):
    effective=controls.effective(aid,account,property_id)
    if effective['mode']!='automatic' or not effective['email_enabled']: raise SendReview('Automatic replies switched off before sending')
    from hosting.email_setup import ready
    if not ready(root,aid): raise SendReview('Owner email is not ready')
    if not isinstance(answer,str) or not answer.strip() or len(answer)>5000: raise SendReview('Reply text unavailable or too long')
    kind,identity=verify_current(account,property_id,payload,effective)
    path=Path(root)/'state/guest-sends.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with sqlite3.connect(path,timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sends (event_key TEXT PRIMARY KEY, account TEXT, property TEXT, thread TEXT, attempted REAL, state TEXT)')
        db.execute('BEGIN IMMEDIATE')
        key=aid+':'+property_id+':'+payload['data']['id']
        if db.execute('SELECT 1 FROM sends WHERE event_key=?',(key,)).fetchone(): raise SendReview('A send was already attempted; inspect the conversation before replying')
        now=time.time()
        if db.execute('SELECT count(*) FROM sends WHERE account=? AND attempted>?',(aid,now-300)).fetchone()[0]>=50 or db.execute('SELECT count(*) FROM sends WHERE account=? AND thread=? AND attempted>?',(aid,kind+':'+identity,now-60)).fetchone()[0]>=2:
            raise SendReview('Automatic reply rate limit reached; human review required')
        db.execute('INSERT INTO sends VALUES (?,?,?,?,?,?)',(key,aid,property_id,kind+':'+identity,now,'attempting'))
    path.chmod(0o600)
    # No retries: a timeout or server error may mean the message was accepted.
    state='unknown'
    try:
        if controls.effective(aid,account,property_id)['mode']!='automatic': raise SendReview('Automatic replies switched off before sending')
        response=requests.post(API_BASE+'/'+kind+'/'+quote(identity,safe='')+'/messages',
            headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},json={'body':answer},timeout=30,allow_redirects=False)
        if response.status_code not in {200,201,202}: raise SendReview('Guest send was not confirmed; inspect the conversation before replying')
        state='sent'
    except SendReview:
        raise
    except Exception:
        raise SendReview('Guest send outcome is uncertain; inspect the conversation before replying')
    finally:
        with sqlite3.connect(path,timeout=10) as db: db.execute('UPDATE sends SET state=? WHERE event_key=?',(state,key))
    return {'action':'sent','answer':answer,'reason':'Automatic reply accepted by Hospitable'}
