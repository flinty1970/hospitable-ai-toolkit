"""Persistent owner HTTPS certificates; keys never leave private instance storage."""
import ipaddress
import json
import os
import re
import ssl
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def names(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('Enter IP addresses or DNS names separated by commas')
    result = []
    for name in value.split(','):
        name = name.strip()
        if not name: continue
        try: ipaddress.ip_address(name)
        except ValueError:
            if len(name) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in name.split('.')):
                raise ValueError('Invalid certificate IP address or DNS name')
        if name not in result: result.append(name)
    if not result or len(result) > 32:
        raise ValueError('Enter between 1 and 32 certificate addresses')
    return result


def tls_root(root):
    directory = Path(root) / 'state/tls'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink(): raise ValueError('Invalid TLS directory')
    directory.chmod(0o700)
    return directory


def certificate_info(cert):
    try: san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound: raise ValueError('Certificate must have subject alternative names')
    addresses = san.get_values_for_type(x509.DNSName) + [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    return {'addresses': addresses, 'expires': cert.not_valid_after_utc.isoformat(),
            'fingerprint': cert.fingerprint(hashes.SHA256()).hex()}


def validate_pair(certificate, private_key, expected):
    if not isinstance(certificate, str) or not isinstance(private_key, str) or max(len(certificate), len(private_key)) > 64000:
        raise ValueError('Certificate and key must be PEM text, at most 64 KB each')
    try:
        certs = x509.load_pem_x509_certificates(certificate.encode())
        if not certs: raise ValueError()
        cert = certs[0]
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except Exception: raise ValueError('Invalid PEM certificate or unencrypted private key') from None
    if key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError('Certificate and private key do not match')
    now = datetime.now(timezone.utc)
    if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
        raise ValueError('Certificate is expired or not yet valid')
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.SERVER_AUTH not in eku: raise ValueError('Certificate is not valid for HTTPS server use')
    except x509.ExtensionNotFound: pass
    info = certificate_info(cert)
    for name in expected:
        if name not in info['addresses']:
            raise ValueError('Certificate does not contain every configured address in its SANs')
    return info


def active(root):
    directory = tls_root(root)
    value = json.loads((directory / 'active.json').read_text())
    identity = value.get('id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', identity): raise ValueError('Invalid TLS state')
    folder = directory / identity
    return value, folder / 'certificate.pem', folder / 'private-key.pem'


def activate(root, certificate, private_key, expected, source):
    info = validate_pair(certificate, private_key, expected)
    directory = tls_root(root)
    identity = uuid.uuid4().hex
    folder = directory / identity
    folder.mkdir(mode=0o700)
    for name, text in [('certificate.pem', certificate), ('private-key.pem', private_key)]:
        path = folder / name
        with path.open('x') as handle: handle.write(text)
        path.chmod(0o600)
    # OpenSSL also validates supported key algorithms, security level and chain loading.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(folder / 'certificate.pem', folder / 'private-key.pem')
    value = {'id': identity, 'source': source, 'expected_addresses': expected, **info}
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='active-')
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, directory / 'active.json')
    return value


def generate(root, addresses):
    expected = names(addresses)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Toolkit owner HTTPS')])
    sans = []
    for name in expected:
        try: sans.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError: sans.append(x509.DNSName(name))
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    return activate(root, cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode(), expected, 'generated')


def ensure(root):
    if not (tls_root(root) / 'active.json').exists():
        generate(root, os.environ.get('TLS_HOSTS', 'localhost,127.0.0.1'))
    value, certificate, key = active(root)
    validate_pair(certificate.read_text(), key.read_text(), value['expected_addresses'])
    return str(certificate), str(key)


def install(app, root):
    import asyncio
    from fastapi import Request, HTTPException
    from fastapi.responses import Response
    from hosting.document_ui import authorize

    def authorized(request):
        authorize(request)
        if request.url.scheme != 'https':
            raise HTTPException(426, 'Use HTTPS for certificate settings')

    @app.get('/admin/settings/tls')
    async def status(request: Request):
        authorized(request)
        value, _, _ = active(root)
        return {**value, 'restart_required': (Path(root) / 'state/restart-request.json').exists()}

    @app.get('/admin/settings/tls/certificate')
    async def download(request: Request):
        authorized(request)
        _, certificate, _ = active(root)
        return Response(certificate.read_bytes(), media_type='application/x-pem-file',
                        headers={'Content-Disposition': 'attachment; filename=toolkit-certificate.pem', 'Cache-Control': 'no-store'})

    @app.post('/admin/settings/tls')
    async def save(request: Request):
        authorized(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 140000: raise HTTPException(413, 'Certificate upload too large')
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or value.get('operation') not in {'upload', 'generate'}:
                raise ValueError('Choose upload or generate')
            expected = names(value.get('addresses', ''))
            def apply():
                if value['operation'] == 'generate':
                    result = generate(root, ','.join(expected))
                else:
                    result = activate(root, value.get('certificate'), value.get('private_key'), expected, 'uploaded')
                # The supervisor restarts gracefully after the HTTP response is returned.
                (Path(root) / 'state/restart-request.json').write_text('{}')
                return {'saved': True, 'restart_required': True, 'expires': result['expires']}
            return await asyncio.to_thread(apply)
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid certificate settings: check PEM files, matching unencrypted key, validity and SAN addresses') from None
        except Exception:
            raise HTTPException(503, 'Certificate activation failed; existing HTTPS configuration retained') from None
