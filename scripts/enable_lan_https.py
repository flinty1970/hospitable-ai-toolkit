"""Migrate this toolkit's LAN owner port and its existing Caddy webhook route."""
import argparse
import ipaddress
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', required=True)
    parser.add_argument('--address', required=True)
    parser.add_argument('--project', default='account-one')
    args = parser.parse_args()
    if os.geteuid() != 0: raise SystemExit('Run with sudo; no Docker permission changes are needed.')
    ipaddress.ip_address(args.address)
    repo = Path(__file__).resolve().parents[1]
    instance = Path(args.instance).resolve()
    env = instance / 'instance.env'
    caddy = Path('/etc/caddy/Caddyfile')
    compose = repo / 'compose.yaml'
    if '"127.0.0.1:${HOST_PORT:-8790}:8790"' not in compose.read_text():
        raise SystemExit('Compose HTTP port must use loopback. Preserve your old Compose override in a backup/stash and use the updated tracked Compose file.')
    if not env.is_file() or not caddy.is_file(): raise SystemExit('Instance environment or Caddy configuration missing.')
    original_env = env.read_text()
    original_caddy = caddy.read_text()
    pattern = r'(handle @toolkit_webhook\s*\{\s*reverse_proxy\s+)127\.0\.0\.1:(?:8790|8791)(\s*\})'
    candidate, count = re.subn(pattern, r'\g<1>127.0.0.1:8791\2', original_caddy)
    if count != 1: raise SystemExit('Expected exactly one existing toolkit webhook handle; no changes made.')
    values = {'HOST_PORT': '8791', 'HTTPS_PORT': '8790', 'HTTPS_BIND': '0.0.0.0',
              'TLS_HOSTS': args.address + ',localhost,127.0.0.1'}
    remaining = [line for line in original_env.splitlines() if line.partition('=')[0].strip() not in values]
    updated_env = '\n'.join(remaining + [key + '=' + value for key, value in values.items()]) + '\n'
    command = ['/usr/bin/docker', 'compose', '--project-name', args.project, '--env-file', str(env), '-f', str(compose)]
    # Validate Caddy and build before switching any live port configuration.
    temporary = caddy.with_name('Caddyfile.https-candidate')
    temporary.write_text(candidate)
    try:
        subprocess.run(['caddy', 'validate', '--config', str(temporary), '--adapter', 'caddyfile'], check=True)
        subprocess.run(command + ['build', 'toolkit'], check=True)
    finally:
        temporary.unlink(missing_ok=True)
    suffix = '.before-https-' + time.strftime('%Y%m%d-%H%M%S')
    for path in (env, caddy): shutil.copy2(path, str(path) + suffix)
    try:
        env.write_text(updated_env)
        subprocess.run(command + ['up', '-d', '--no-build', 'toolkit'], check=True)
        # Verify the new TLS listener with its certificate, without disabling verification.
        for attempt in range(90):
            try:
                directory = instance / 'data/state/tls'
                identity = json.loads((directory / 'active.json').read_text())['id']
                context = ssl.create_default_context(cafile=str(directory / identity / 'certificate.pem'))
                with urllib.request.urlopen('https://127.0.0.1:8790/ready', context=context, timeout=2) as response:
                    if response.status == 200: break
            except Exception:
                if attempt == 89: raise RuntimeError('New HTTPS listener did not become ready')
                time.sleep(1)
        caddy.write_text(candidate)
        subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
    except Exception:
        env.write_text(original_env)
        caddy.write_text(original_caddy)
        subprocess.run(command + ['up', '-d', '--no-build', 'toolkit'], check=False)
        subprocess.run(['systemctl', 'reload', 'caddy'], check=False)
        raise SystemExit('HTTPS migration failed. Configuration files restored; check container status. The newly built image remains available.')
    print('Owner UI: https://' + args.address + ':8790/settings')
    print('Generated certificates require explicit device/browser trust. Verify the fingerprint in Settings.')
    print('Caddy toolkit webhook upstream moved to loopback port 8791. Other routes preserved.')
    print('Configuration backups use suffix ' + suffix)


if __name__ == '__main__': main()
