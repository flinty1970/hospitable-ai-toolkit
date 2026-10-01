"""Persistent manually authored reservation messages; no uncertain-send retries."""
import json
import hashlib
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
from hosting.config import secret
from hosting.controls import Controls, control_path
from hosting.gateway import API_BASE, object_id
from hosting.notifications import Outbox
from hosting.email_setup import ready


def property_id(record):
    pid=object_id(record.get('property_id') or record.get('property'))
    properties=record.get('properties')
    if isinstance(properties,dict):properties=properties.get('data')
    if not pid and isinstance(properties,list) and len(properties)==1:pid=object_id(properties[0])
    return pid

def reservation(account,pid,rid):
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',rid):raise ValueError('Invalid reservation ID')
    response=requests.get(API_BASE+'/reservations/'+quote(rid,safe=''),params={'include':'properties'},headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
    if response.status_code!=200:raise ValueError('Reservation unavailable')
    row=response.json().get('data')
    if not isinstance(row,dict) or property_id(row)!=pid:raise ValueError('Reservation does not belong to the selected property')
    return row

def reservations(account,pid,page=1):
    if pid not in account['properties'] or type(page) is not int or not 1<=page<=100:raise ValueError('Invalid property or page')
    from datetime import timedelta
    today=datetime.now(timezone.utc).date()
    response=requests.get(API_BASE+'/reservations',params={'properties[]':pid,'page':page,'per_page':50,'start_date':str(today-timedelta(days=30)),'end_date':str(today+timedelta(days=365)),'include':'guest,properties'},headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
    if response.status_code!=200:raise ValueError('Reservation list unavailable; check API permissions')
    value=response.json();rows=value.get('data')
    if not isinstance(rows,list):raise ValueError('Unexpected reservation list')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or property_id(row)!=pid:continue
        rid=object_id(row.get('id') or row.get('uuid'))
        if not rid:continue
        guest=row.get('guest') or {};guest=guest.get('data',guest) if isinstance(guest,dict) else {}
        name=guest.get('name') or ' '.join(str(guest.get(k) or '') for k in ('first_name','last_name')).strip() or 'Guest'
        platform=row.get('platform')
        if isinstance(platform,dict):platform=platform.get('name') or platform.get('code') or platform.get('id')
        result.append({'id':rid,'guest':name[:200],'platform':platform[:100] if isinstance(platform,str) else None,'code':str(row.get('code') or row.get('reservation_code') or '')[:100],'check_in':str(row.get('check_in') or row.get('checkin') or '')[:100],'check_out':str(row.get('check_out') or row.get('checkout') or '')[:100]})
    last=(value.get('meta') or {}).get('last_page')
    more=page<last if type(last) is int else bool((value.get('links') or {}).get('next'))
    return {'reservations':result,'next_page':page+1 if more and page<100 else None}

def local_due(value,zone,fold=None):
    if not isinstance(value,str):raise ValueError('Choose a date and time')
    local=datetime.fromisoformat(value)
    if local.tzinfo is not None:raise ValueError('Use property-local date and time')
    if fold is not None and (type(fold) is not int or fold not in {0,1}):raise ValueError('Invalid daylight-saving choice')
    tz=ZoneInfo(zone);choices=[]
    for f in (0,1):
        aware=local.replace(tzinfo=tz,fold=f);utc=aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None)==local:choices.append((f,utc.timestamp()))
    if not choices:raise ValueError('This local time does not exist because clocks change; choose another time')
    if len({v for _,v in choices})>1 and fold is None:raise ValueError('This time occurs twice when clocks change; choose first or second occurrence')
    matching=[v for f,v in choices if fold is None or f==fold]
    if not matching:raise ValueError('Invalid local time choice')
    if matching[0]<=time.time():raise ValueError('Scheduled time must be in the future')
    return matching[0]

def message_revision(row):
    return hashlib.sha256(json.dumps({k:row[k] for k in ('id','due','body','state')},sort_keys=True).encode()).hexdigest()

class Queue:
    def __init__(self,root,aid,account):
        self.root=Path(root);self.aid=aid;self.account=account
        self.path=self.root/'state/scheduled-messages.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scheduled (id TEXT PRIMARY KEY, account TEXT, property TEXT, reservation TEXT, due REAL, timezone TEXT, body TEXT, state TEXT, actor TEXT, created REAL, reason TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS schedule_audit (id TEXT, action TEXT, actor TEXT, at REAL)')
        self.path.chmod(0o600)
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row;return db
    def list(self,pid):
        if pid not in self.account['properties']:raise ValueError('Unknown property')
        with self.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM scheduled WHERE account=? AND property=? ORDER BY due DESC LIMIT 200',(self.aid,pid))]
        for row in rows:
            row['local_time']=datetime.fromtimestamp(row['due'],ZoneInfo(row['timezone'])).isoformat()
            row['revision']=message_revision(row)
        return rows
    def create(self,pid,rid,local_time,body,actor,fold=None):
        if pid not in self.account['properties']:raise ValueError('Unknown property')
        if not isinstance(body,str) or not body.strip() or len(body)>5000:raise ValueError('Enter a message of 1–5000 characters')
        if not ready(self.root,self.aid):raise ValueError('Save and test owner email before scheduling messages')
        prop=self.account['properties'][pid];due=local_due(local_time,prop['timezone'],fold)
        reservation(self.account,pid,rid)
        identity=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id,state FROM scheduled WHERE account=? AND property=? AND reservation=? AND due=? AND body=? AND state!=?',(self.aid,pid,rid,due,body.strip(),'cancelled')).fetchone()
            if existing:return {'id':existing['id'],'state':existing['state']}
            db.execute('INSERT INTO scheduled VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,self.aid,pid,rid,due,prop['timezone'],body.strip(),'pending',actor,time.time(),None))
            db.execute('INSERT INTO schedule_audit VALUES(?,?,?,?)',(identity,'created',actor,time.time()))
        return {'id':identity,'state':'pending'}
    def update(self,pid,identity,value,actor):
        if pid not in self.account['properties']:raise ValueError('Unknown property')
        body=value.get('message')
        if value.get('confirm_send') is not True or not isinstance(body,str) or not body.strip() or len(body)>5000:raise ValueError('Confirm replacement message')
        if not ready(self.root,self.aid):raise ValueError('Save and test owner email')
        due=local_due(value.get('local_time'),self.account['properties'][pid]['timezone'],value.get('fold'))
        with self.connect() as db:row=db.execute('SELECT * FROM scheduled WHERE id=? AND account=? AND property=?',(identity,self.aid,pid)).fetchone()
        if not row:raise ValueError('Scheduled message not found')
        reservation(self.account,pid,row['reservation'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM scheduled WHERE id=? AND account=? AND property=?',(identity,self.aid,pid)).fetchone()
            if row['state']!='pending' or message_revision(row)!=value.get('revision'):raise ValueError('Message changed or delivery is already in progress; refresh before editing')
            db.execute('UPDATE scheduled SET due=?,body=? WHERE id=?',(due,body.strip(),identity))
            db.execute('INSERT INTO schedule_audit VALUES(?,?,?,?)',(identity,'edited',actor,time.time()))
        return {'updated':True}
    def cancel(self,pid,identity,actor):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state FROM scheduled WHERE id=? AND account=? AND property=?',(identity,self.aid,pid)).fetchone()
            if not row:raise ValueError('Scheduled message not found')
            if row['state']=='cancelled':return {'cancelled':True}
            if row['state']!='pending':raise ValueError('Only pending messages can be cancelled; delivery may already be in progress')
            db.execute("UPDATE scheduled SET state='cancelled' WHERE id=?",(identity,));db.execute('INSERT INTO schedule_audit VALUES(?,?,?,?)',(identity,'cancelled',actor,time.time()))
        return {'cancelled':True}
    def review(self,row,reason):
        outbox=Outbox(self.path)
        with self.connect() as db:
            if not db.execute("UPDATE scheduled SET state='review',reason=? WHERE id=? AND state IN ('pending','sending')",(reason,row['id'])).rowcount:return
            outbox.enqueue('scheduled:'+row['id'],self.aid,row['property'],{'account_id':self.aid,'property_id':row['property'],'reservation_id':row['reservation'],'scheduled_message_id':row['id'],'reason':reason},db=db)
    def recover(self):
        with self.connect() as db:rows=list(db.execute("SELECT * FROM scheduled WHERE account=? AND state='sending'",(self.aid,)))
        for row in rows:self.review(row,'Scheduled send outcome is uncertain after restart; inspect conversation before replying')
    def deliver_due(self):
        controls=Controls(control_path(self.root/'state/controls.sqlite3'))
        with self.connect() as db:rows=list(db.execute("SELECT * FROM scheduled WHERE account=? AND state='pending' AND due<=? ORDER BY due LIMIT 20",(self.aid,time.time())))
        for row in rows:
            pid=row['property']
            if pid not in self.account['properties']:self.review(row,'Property no longer configured');continue
            effective=controls.effective(self.aid,self.account,pid)
            if not effective['enabled'] or not effective['email_enabled'] or not ready(self.root,self.aid):continue
            if time.time()-row['due']>900:self.review(row,'Scheduled message missed its send window; review instead of sending late');continue
            try:
                record=reservation(self.account,pid,row['reservation'])
                status=record.get('status')
                if isinstance(status,dict):status=status.get('current',status.get('category'))
                if isinstance(status,dict):status=status.get('category') or status.get('code')
                if not isinstance(status,str) or status.lower() not in {'accepted','confirmed'}:raise ValueError('Reservation is not confirmed for sending')
            except Exception:self.review(row,'Reservation ownership/status could not be verified; review scheduled message');continue
            with self.connect() as db:
                changed=db.execute("UPDATE scheduled SET state='sending' WHERE id=? AND state='pending' AND due=? AND body=?",(row['id'],row['due'],row['body'])).rowcount
            if not changed:continue
            try:
                from hosting.guest_sending import claim_attempt
                key='scheduled:'+self.aid+':'+row['id']
                ledger=claim_attempt(self.root,self.aid,pid,key,'reservations:'+row['reservation'])
                current=controls.effective(self.aid,self.account,pid)
                if not current['enabled'] or not current['email_enabled'] or not ready(self.root,self.aid):raise ValueError('Processing or owner alerts paused before scheduled send')
                response=requests.post(API_BASE+'/reservations/'+quote(row['reservation'],safe='')+'/messages',headers={'Authorization':'Bearer '+secret(self.account['api_key_env']),'Accept':'application/json'},json={'body':row['body']},timeout=30,allow_redirects=False)
                if response.status_code not in {200,201,202}:raise ValueError('Send not accepted')
            except Exception:self.review(row,'Scheduled send outcome not confirmed; inspect conversation before replying');continue
            with sqlite3.connect(ledger,timeout=10) as db:db.execute("UPDATE sends SET state='sent' WHERE event_key=?",(key,))
            with self.connect() as db:
                db.execute("UPDATE scheduled SET state='sent',reason=NULL WHERE id=?",(row['id'],));db.execute('INSERT INTO schedule_audit VALUES(?,?,?,?)',(row['id'],'sent','scheduler',time.time()))
        Outbox(self.path).drain({self.aid:self.account},controls)

def main():
    from hosting.config import load_registry
    accounts=load_registry(os.environ['TOOLKIT_ACCOUNTS_FILE']);aid,account=next(iter(accounts.items()))
    queue=Queue(os.environ['TOOLKIT_DATA_DIR'],aid,account);queue.recover()
    while True:
        try:queue.deliver_due()
        except Exception:pass  # Private failures must not expose credentials/payloads.
        time.sleep(5)

if __name__=='__main__':main()
