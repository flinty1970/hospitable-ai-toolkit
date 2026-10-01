"""Private SMTP setup and msmtp delivery for owner alerts."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from email.message import EmailMessage
from email.utils import parseaddr
from hosting.indexing import index_lock
from hosting.pdf_ingestion import write_atomic

FIELDS = {'host','port','security','username','password','sender','recipient'}

def read_settings(root, aid):
    path=Path(root)/'state/smtp-settings.json'
    if path.is_symlink(): raise ValueError('Invalid SMTP settings path')
    if not path.exists(): return None
    if path.stat().st_mode & 0o077: raise ValueError('SMTP settings must be private')
    value=json.loads(path.read_text())
    if value.get('schema')!=1 or value.get('account_id')!=aid: raise ValueError('Invalid SMTP account')
    return value['settings']

def validate(value):
    if set(value)!=FIELDS: raise ValueError('Complete all SMTP fields')
    if not isinstance(value['host'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',value['host']): raise ValueError('Invalid SMTP host')
    if type(value['port']) is not int or not 1<=value['port']<=65535: raise ValueError('Invalid SMTP port')
    if value['security'] not in {'starttls','tls'}: raise ValueError('SMTP requires TLS')
    for field in ('username','password'):
        v=value[field]
        if not isinstance(v,str) or not v or len(v)>4096 or any(ord(c)<32 or ord(c)==127 for c in v): raise ValueError('Invalid SMTP credential')
    for field in ('sender','recipient'):
        v=value[field]
        if not isinstance(v,str) or len(v)>254 or any(ord(c)<=32 for c in v) or parseaddr(v)[1]!=v or '@' not in v: raise ValueError('Invalid email address')
    return value

def resolve(root, aid, value):
    if set(value)-FIELDS: raise ValueError('Unsupported SMTP field')
    previous=read_settings(root,aid)
    value=dict(value)
    if not value.get('password') and previous:
        if value.get('host')!=previous['host'] or value.get('username')!=previous['username']:
            raise ValueError('Enter password again after changing host or username')
        value['password']=previous['password']
    return validate(value)

def public_settings(root, aid):
    value=read_settings(root,aid)
    return {'configured':bool(value),'settings':{k:v for k,v in (value or {}).items() if k!='password'},'password_present':bool(value and value.get('password')), 'tested':ready(root,aid)}

def save_settings(root, aid, value):
    root=Path(root);(root/'state').mkdir(parents=True,exist_ok=True,mode=0o700)
    with index_lock(root,True,'smtp-settings.lock'):
        value=resolve(root,aid,value)
        write_atomic(root/'state/smtp-settings.json',json.dumps({'schema':1,'account_id':aid,'settings':value}))
    return {'saved':True}

def send(value, message):
    validate(value)
    def quoted(text): return '"'+text.replace('\\','\\\\').replace('"','\\"')+'"'
    text='\n'.join(['defaults','auth on','tls on','tls_trust_file /etc/ssl/certs/ca-certificates.crt',
        'tls_starttls '+('on' if value['security']=='starttls' else 'off'),'account owner',
        'host '+value['host'],'port '+str(value['port']),'user '+quoted(value['username']),
        'password '+quoted(value['password']),'from '+quoted(value['sender'])])+'\n'
    fd,path=tempfile.mkstemp(prefix='toolkit-smtp-')
    try:
        with os.fdopen(fd,'w') as file: file.write(text)
        subprocess.run(['/usr/bin/msmtp','--file='+path,'--account=owner','-f',value['sender'],'-t'],
            input=message.as_bytes(),capture_output=True,timeout=20,check=True)
    finally:
        os.unlink(path)

def test_email(root, aid, value):
    value=resolve(root,aid,value)
    message=EmailMessage();message['From']=value['sender'];message['To']=value['recipient']
    message['Subject']='Hospitable AI Toolkit: email test'
    message.set_content('Your owner email alert connection is working. This test contains no guest or property information.')
    send(value,message)
    root=Path(root);(root/'state').mkdir(parents=True,exist_ok=True,mode=0o700)
    with index_lock(root,True,'smtp-settings.lock'):
        write_atomic(root/'state/smtp-tested.json',json.dumps({'account_id':aid,'fingerprint':fingerprint(value)}))
    return {'sent':True}

def fingerprint(value):
    import hashlib
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def ready(root, aid):
    saved=read_settings(root,aid)
    path=Path(root)/'state/smtp-tested.json'
    if not saved or not path.exists() or path.is_symlink(): return False
    value=json.loads(path.read_text())
    return value.get('account_id')==aid and value.get('fingerprint')==fingerprint(saved)
