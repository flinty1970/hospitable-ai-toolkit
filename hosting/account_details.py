"""Read-only Hospitable display metadata; no channel pricing changes."""
import json
import math
import re
from pathlib import Path
import requests
from hosting.config import secret
from hosting.pdf_ingestion import write_atomic


def read(root, aid):
    path=Path(root)/'state/account-display.json'
    if path.is_symlink(): raise ValueError('Invalid account display path')
    if not path.exists(): return {'name':None,'properties':{}}
    value=json.loads(path.read_text())
    if value.get('account_id')!=aid: raise ValueError('Invalid display account')
    return value

def enrich_channels(root, account, properties):
    headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'}
    connected=set()
    try:
        response=requests.get('https://public.api.hospitable.com/v2/channels',headers=headers,timeout=20,allow_redirects=False)
        rows=response.json().get('data',[]) if response.status_code==200 else []
        if isinstance(rows,list):
            for row in rows:
                if isinstance(row,dict) and isinstance(row.get('platform'),str) and row.get('user_id') is not None:
                    connected.add((row['platform'],str(row['user_id'])))
    except (requests.RequestException,ValueError):
        pass
    for pid in account.get('properties',{}):
        if pid not in properties or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',pid): continue
        try:
            response=requests.get('https://public.api.hospitable.com/v2/properties/'+pid,
                params={'include':'listings,bookings'},headers=headers,timeout=20,allow_redirects=False)
            if response.status_code!=200: continue
            data=response.json().get('data',{})
            if not isinstance(data,dict): continue
            listings=data.get('listings',[])
            if isinstance(listings,dict): listings=listings.get('data',[])
            bookings=data.get('bookings',{})
            markups=bookings.get('listing_markups',[]) if isinstance(bookings,dict) else []
            labels={}
            if isinstance(markups,list):
                for entry in markups:
                    if not isinstance(entry,dict): continue
                    value=entry.get('markup');platform=entry.get('platform')
                    if isinstance(platform,str) and type(value) in (int,float) and math.isfinite(value):
                        labels[platform]=f'{value:g}% (API)' if entry.get('type')=='percent' else f'{value:g} (API value; unit not supplied)'
            channels=[]
            if isinstance(listings,list):
                for listing in listings:
                    if not isinstance(listing,dict) or not isinstance(listing.get('platform'),str): continue
                    platform=listing['platform'];uid=listing.get('platform_user_id')
                    status='Connected account matched' if uid is not None and (platform,str(uid)) in connected else 'Not confirmed by API'
                    if listing.get('connected') is False: status='Disconnected'
                    channels.append({'channel':platform[:100],'markup':labels.get(platform,'Markup not supplied by API'),'connection_status':status})
                properties[pid].update(channels=channels,available=True)
        except (requests.RequestException,ValueError):
            continue


def refresh(root, aid, account):
    name=None;properties={}
    profile=requests.get('https://public.api.hospitable.com/v2/user',
        headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
    if profile.status_code == 200:
        owner=profile.json().get('data', {})
        if isinstance(owner,dict):
            candidate=owner.get('company') or owner.get('name')
            if isinstance(candidate,str) and candidate.strip(): name=candidate[:200]
    for page in range(1,101):
        response=requests.get('https://public.api.hospitable.com/v2/properties',
            params={'page':page,'per_page':50,'include':'listings'},
            headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
        if response.status_code in {400,403,422}:
            response=requests.get('https://public.api.hospitable.com/v2/properties',
                params={'page':page,'per_page':50},headers={'Authorization':'Bearer '+secret(account['api_key_env']),'Accept':'application/json'},timeout=20,allow_redirects=False)
        if response.status_code!=200: raise ValueError('Property/listing access unavailable')
        value=response.json();rows=value.get('data')
        if not isinstance(rows,list): raise ValueError('Unexpected property details')
        for row in rows:
            pid=row.get('id') or row.get('uuid')
            if not isinstance(pid,str): continue
            user=row.get('user')
            if isinstance(user,dict) and isinstance(user.get('data'),dict): user=user['data']
            if not name and isinstance(user,dict) and isinstance(user.get('name'),str) and user['name'].strip(): name=user['name'][:200]
            listings=row.get('listings')
            if isinstance(listings,dict): listings=listings.get('data')
            channels=[]
            if isinstance(listings,list):
                for listing in listings:
                    platform=listing.get('platform') or listing.get('channel')
                    if isinstance(platform,dict): platform=platform.get('name') or platform.get('id')
                    if not isinstance(platform,str): continue
                    markup=listing.get('markup')
                    label='Not exposed by Hospitable API'
                    if isinstance(markup,dict):
                        percentage=markup.get('percentage',markup.get('percent'))
                        if type(percentage) in (int,float): label=str(percentage)+'% (API)'
                    elif type(markup) in (int,float):
                        label=str(markup)+' (API value; unit not supplied)'
                    channels.append({'channel':platform[:100],'markup':label,'connection_status': 'Connected' if listing.get('connected') is True else 'Disconnected' if listing.get('connected') is False else 'Not supplied by API'})
            nickname=row.get('name') or row.get('public_name')
            properties[pid]={'channels':channels,'available':isinstance(listings,list), 'nickname':nickname[:500] if isinstance(nickname,str) else None}
        meta=value.get('meta') or {};last=meta.get('last_page')
        if type(last) is int:
            if last<1 or last>100: raise ValueError('Invalid pagination')
            more=page<last
        else: more=bool((value.get('links') or {}).get('next'))
        if not more: break
        if not rows: raise ValueError('Pagination did not advance')
    else: raise ValueError('Too many property pages')
    if account.get("properties"):
        enrich_channels(root,account,properties)
    previous=read(root,aid)
    display={'account_id':aid,'name':name or previous.get('name'),'properties':properties}
    root=Path(root);(root/'state').mkdir(parents=True,exist_ok=True,mode=0o700)
    write_atomic(root/'state/account-display.json',json.dumps(display))
    return {'refreshed':True,'name_available':bool(name)}

def save_name(root, aid, name):
    if not isinstance(name,str) or not name.strip() or len(name)>200: raise ValueError('Enter an account display name')
    value=read(root,aid);value.update(account_id=aid,name=name.strip())
    root=Path(root);(root/'state').mkdir(parents=True,exist_ok=True,mode=0o700)
    write_atomic(root/'state/account-display.json',json.dumps(value))
    return {'saved':True}
