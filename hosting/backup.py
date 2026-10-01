"""Offline Linux instance backup/restore, standard library only.

Backups contain credentials. Restore requires a new directory and pauses sends.
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def stopped(instance, project):
    if not project or not all(c.isalnum() or c in '_-' for c in project):
        raise ValueError('Provide the Compose project name')
    result = subprocess.run(['docker', 'compose', '--env-file', str(instance / 'instance.env'), '-p', project,
                             'ps', '--status', 'running', '-q'], capture_output=True, text=True, check=True, timeout=30)
    if result.stdout.strip():
        raise ValueError('Stop this Compose project before backing up')
    # Also reject an instance mounted by a differently named running container.
    names = subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True, check=True, timeout=30).stdout.split()
    for identity in names:
        result = subprocess.run(['docker', 'inspect', identity], capture_output=True, text=True, check=True, timeout=30)
        for container in json.loads(result.stdout):
            for mount in container.get('Mounts', []):
                source = Path(mount.get('Source', '/__none__')).resolve()
                if source == instance or instance in source.parents:
                    raise ValueError('A running container still mounts this instance')


def checksum(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def backup(instance, output, project):
    instance, output = Path(instance).resolve(), Path(output).resolve()
    if output == instance or instance in output.parents:
        raise ValueError('Store the archive outside the instance folder')
    stopped(instance, project)
    required = [instance / 'instance.env', instance / 'data/config/account.json', instance / 'secrets/credentials.env']
    if not all(p.is_file() and not p.is_symlink() for p in required):
        raise ValueError('Instance configuration or credentials missing')
    aid = json.loads(required[1].read_text())['account']['id']
    files = []
    for root in [instance / 'data', instance / 'secrets']:
        if root.is_symlink():
            raise ValueError('Backup refuses symlink directories')
        for directory, dirs, names in os.walk(root, followlinks=False):
            directory = Path(directory)
            for name in list(dirs):
                path = directory / name
                if path.is_symlink():
                    raise ValueError('Backup refuses symlinks')
                if path == instance / 'data/model-cache':
                    dirs.remove(name)
            for name in names:
                path = directory / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Backup refuses special files')
                files.append(path)
    files.append(instance / 'instance.env')
    for path in files:
        if path.suffix == '.sqlite3':
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite integrity check failed')
    manifest = {'schema': 1, 'account_id': aid, 'model_cache_included': False,
                'files': {str(p.relative_to(instance)): checksum(p) for p in sorted(files)}}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle, tarfile.open(fileobj=handle, mode='w:gz') as archive:
            for path in sorted(files):
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(instance)))
                info.mode = 0o600
                with path.open('rb') as source:
                    archive.addfile(info, source)
            data = json.dumps(manifest, sort_keys=True).encode()
            info = tarfile.TarInfo('backup-manifest.json')
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {'backed_up': True, 'files': len(files), 'model_cache_included': False}


def verify_archive(archive):
    members = archive.getmembers()
    if len(members) > 100000:
        raise ValueError('Too many archive members')
    names = set()
    for item in members:
        path = PurePosixPath(item.name)
        if not item.isfile() or path.is_absolute() or '..' in path.parts or '\\' in item.name or str(path) != item.name or item.name in names:
            raise ValueError('Unsafe archive member')
        if item.name != 'backup-manifest.json' and item.name != 'instance.env' and path.parts[0] not in {'data', 'secrets'}:
            raise ValueError('Unexpected archive path')
        names.add(item.name)
    member = archive.getmember('backup-manifest.json')
    if member.size > 32 * 1024 * 1024:
        raise ValueError('Manifest too large')
    manifest = json.load(archive.extractfile(member))
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict) or names != set(manifest['files']) | {'backup-manifest.json'}:
        raise ValueError('Invalid backup manifest')
    for name, expected in manifest['files'].items():
        h = hashlib.sha256()
        with archive.extractfile(name) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise ValueError('Backup checksum mismatch')
    return manifest


def verify(path):
    with tarfile.open(path, 'r:gz') as archive:
        manifest = verify_archive(archive)
    return {'verified': True, 'files': len(manifest['files'])}


def restore(archive_path, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Restore requires a new destination directory')
    with tarfile.open(archive_path, 'r:gz') as archive:
        manifest = verify_archive(archive)
        destination.mkdir(mode=0o700, parents=True)
        try:
            for name in manifest['files']:
                path = destination / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(name) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output)
                path.chmod(0o600)
            config_path = destination / 'data/config/account.json'
            config = json.loads(config_path.read_text())
            aid = config['account']['id']
            if aid != manifest['account_id']:
                raise ValueError('Backup account identity mismatch')
            config['account'].update(enabled=False, shadow=True)
            config_path.write_text(json.dumps(config, indent=2))
            from hosting.controls import Controls
            Controls(destination / 'data/state/controls.sqlite3').set('backup-restore', aid, enabled=False, shadow=True)
            # This old runtime path/port registry is regenerated at startup.
            (destination / 'data/state/restart-request.json').unlink(missing_ok=True)
        except Exception:
            shutil.rmtree(destination)
            raise
    return {'restored': True, 'account_paused': True, 'files': len(manifest['files'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('backup')
    create.add_argument('--instance-dir', required=True)
    create.add_argument('--output', required=True)
    create.add_argument('--project', required=True)
    check = sub.add_parser('verify')
    check.add_argument('--archive', required=True)
    load = sub.add_parser('restore')
    load.add_argument('--archive', required=True)
    load.add_argument('--destination', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'backup':
            result = backup(args.instance_dir, args.output, args.project)
        elif args.action == 'verify':
            result = verify(args.archive)
        else:
            result = restore(args.archive, args.destination)
        print(json.dumps(result))
    except Exception:
        raise SystemExit('Operation failed. Check stopped containers, paths, archive integrity and permissions. No credentials displayed.')


if __name__ == '__main__':
    main()
