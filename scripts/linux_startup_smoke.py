"""Clean Linux startup of the real supervisor, with dummy credentials.

No Docker daemon required. Does not validate Docker mounts/image build or models.
Run using a virtualenv with requirements-runtime.txt installed.
"""
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
CERTIFICATE=None

def request(path,token=None,payload=None):
    headers={'Authorization':'Bearer '+token} if token else {}
    data=json.dumps(payload).encode() if payload is not None else None
    if data is not None:headers['Content-Type']='application/json'
    owner = path in {'/review','/settings','/documents','/scheduled'} or path.startswith('/admin/')
    base = 'https://127.0.0.1:9443' if owner else 'http://127.0.0.1:8790'
    context = ssl.create_default_context(cafile=str(CERTIFICATE)) if owner else None
    with urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=headers),timeout=3,context=context) as response:
        raw=response.read()
        return json.loads(raw) if 'application/json' in response.headers.get('content-type','') else raw.decode()

def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.kill();process.wait()

def start(env):
    global CERTIFICATE
    process=subprocess.Popen([sys.executable,'-m','hosting.container_runtime'],cwd=REPO,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            if process.poll() is not None:raise RuntimeError('Supervisor exited')
            try:
                if request('/ready')['ok']:
                    state=Path(env['TOOLKIT_DATA_DIR'])/'state/tls'
                    identity=json.loads((state/'active.json').read_text())['id']
                    CERTIFICATE=state/identity/'certificate.pem'
                    request('/admin/operations','dummy-admin')
                    return process
            except (urllib.error.URLError,TimeoutError,ConnectionError):pass
            time.sleep(.25)
        raise RuntimeError('Startup timeout')
    except Exception:stop(process);raise

def main():
    if sys.platform!='linux':raise SystemExit('Linux startup test only; Windows is untested.')
    for port in (8790,9000,9443):
        with socket.socket() as probe:
            try:probe.bind(('127.0.0.1',port))
            except OSError:raise SystemExit('Test ports 8790/9000/9443 must be free. Do not stop another instance.')
    with tempfile.TemporaryDirectory(prefix='toolkit-clean-') as folder:
        root=Path(folder);data=root/'data';secret_dir=root/'secrets'
        (data/'config').mkdir(parents=True);secret_dir.mkdir()
        config={'account':{'id':'clean-smoke','enabled':False,'shadow':True,'notifications':{'email':{'enabled':False}},
            'properties':{'test-property':{'name':'Test House','timezone':'UTC','enabled':False}}},'mcp':{'enabled':False}}
        (data/'config/account.json').write_text(json.dumps(config))
        credentials=secret_dir/'credentials.env';credentials.write_text('HOSPITABLE_PAT=dummy-pat\nHOSPITABLE_WEBHOOK_SECRET=dummy-hook\nTOOLKIT_ADMIN_SECRET=dummy-admin\n');credentials.chmod(0o600)
        env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','SYSTEMROOT'}}
        env.update(TOOLKIT_DATA_DIR=str(data),TOOLKIT_SECRETS_DIR=str(secret_dir),PYTHONPATH=str(REPO),PYTHONDONTWRITEBYTECODE='1')
        process=start(env)
        try:
            try:
                urllib.request.urlopen('http://127.0.0.1:8790/settings')
                raise AssertionError('HTTP owner page accepted')
            except urllib.error.HTTPError as error: assert error.code == 426
            assert 'Review and webhooks' in request('/review')
            assert request('/admin/operations/probe','dummy-admin',{})['ok']
            event={'action':'message.created','data':{'id':'clean-event','body':'No external calls; paused','sender_type':'guest'}}
            assert request('/webhook/hospitable/clean-smoke?token=dummy-hook',payload=event)['action']=='queued'
            status=request('/admin/operations','dummy-admin');assert status['inbox'][0]['count']==1
        finally:stop(process)
        process=start(env)
        try:
            assert request('/admin/operations','dummy-admin')['inbox'][0]['count']==1
            assert request('/admin/settings','dummy-admin')['account']['mode']=='disabled'
            assert request('/admin/operations/reviews','dummy-admin')['items']==[]
        finally:stop(process)
    print('PASS: clean Linux supervisor startup without AI key; owner routes, local webhook probe, inbox persistence and paused restart. No Docker/image/model/live-provider verification.')

if __name__=='__main__':main()

