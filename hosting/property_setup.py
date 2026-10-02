"""Account property discovery and durable selections, applied on restart."""
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from hosting.config import secret
from hosting.pdf_ingestion import write_atomic

class DiscoveryError(ValueError):
    """Safe messages containing no API payloads or credentials."""


SAFE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}')


def discovery(account, get=None):
    get = get or requests.get
    properties = {}
    for page in range(1, 101):
        response = get('https://public.api.hospitable.com/v2/properties',
            params={'page': page, 'per_page': 50},
            headers={'Authorization': 'Bearer ' + secret(account.get('api_key_env', 'HOSPITABLE_PAT')), 'Accept': 'application/json'},
            timeout=20, allow_redirects=False)
        if response.status_code != 200:
            raise DiscoveryError('Hospitable could not list properties; check PAT permissions or retry later')
        payload = response.json()
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise DiscoveryError('Hospitable returned an unexpected property list')
        previous = len(properties)
        for row in rows:
            if not isinstance(row, dict):
                raise DiscoveryError('Hospitable returned an unexpected property record')
            pid = row.get('id') or row.get('uuid')
            if not isinstance(pid, str) or not SAFE_ID.fullmatch(pid):
                raise DiscoveryError('Hospitable returned an unsupported property ID')
            name = row.get('name') or row.get('public_name') or pid
            timezone = row.get('timezone') or 'UTC'
            if not isinstance(name, str) or len(name) > 500 or not isinstance(timezone, str):
                raise DiscoveryError('Hospitable returned an unsupported property name or timezone')
            try:
                ZoneInfo(timezone)
            except (ValueError, KeyError):
                configured = account.get('properties', {}).get(pid, {}).get('timezone')
                if configured:
                    ZoneInfo(configured)
                    timezone = configured
                elif re.fullmatch(r'[+-]\d{2}:?\d{2}', timezone):
                    properties[pid] = {'name': name, 'timezone': None, 'timezone_required': True}
                    continue
                else:
                    raise DiscoveryError('Hospitable returned an unrecognised timezone. Check the property timezone in Hospitable.')
            properties[pid] = {'name': name, 'timezone': timezone}
        meta = payload.get('meta') or {}
        last = meta.get('last_page')
        links = payload.get('links') or {}
        if last is not None:
            if type(last) is not int or last < 1 or last > 100:
                raise ValueError('Unsupported property pagination')
            more = page < last
        elif 'next' in links:
            more = bool(links['next'])
        else:
            more = len(rows) == 50
        if not more:
            return properties
        if len(properties) == previous:
            raise ValueError('Property pagination did not advance')
    raise ValueError('Too many pages of properties')


def selection_path(root):
    return Path(root) / 'state/property-selection.json'


def read_selection(root, aid):
    path = selection_path(root)
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError('Property selection must not be a symlink')
    value = json.loads(path.read_text())
    if value.get('schema') != 1 or value.get('account_id') != aid or not isinstance(value.get('properties'), dict) or len(value['properties']) > 64:
        raise ValueError('Invalid account property selection')
    for pid, prop in value['properties'].items():
        if not SAFE_ID.fullmatch(pid) or set(prop) != {'name', 'timezone'} or not isinstance(prop['name'], str) or not prop['name'].strip():
            raise ValueError('Invalid selected property')
        ZoneInfo(prop['timezone'])
    return value['properties']


def apply_selection(root, account):
    selected = read_selection(root, account['id'])
    result = dict(account.get('properties', {}))
    defaults = next(iter(result.values()), {})
    for pid, prop in selected.items():
        if pid not in result:
            result[pid] = {**prop, 'enabled': False, 'shadow': True,
                'model_key_env': account.get('model_key_env', defaults.get('model_key_env', 'ANTHROPIC_API_KEY')),
                'model': account.get('model', defaults.get('model', 'claude-sonnet-4-6'))}
    return {**account, 'properties': result}


def save_selection(root, aid, selected):
    path = selection_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('Property selection must not be a symlink')
    write_atomic(path, json.dumps({'schema': 1, 'account_id': aid, 'properties': selected}, indent=2))
