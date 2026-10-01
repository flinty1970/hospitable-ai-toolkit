"""Fixed provider endpoints and private, account-scoped AI settings."""
import json
import os
import re
from pathlib import Path
import requests
from hosting.pdf_ingestion import write_atomic
from hosting.indexing import index_lock

PROVIDERS = {
    'anthropic': {'name': 'Anthropic (Claude)', 'env': 'ANTHROPIC_API_KEY', 'base': 'https://api.anthropic.com/v1'},
    'openai': {'name': 'OpenAI (ChatGPT models)', 'env': 'OPENAI_API_KEY', 'base': 'https://api.openai.com/v1'},
    'xai': {'name': 'xAI (Grok)', 'env': 'XAI_API_KEY', 'base': 'https://api.x.ai/v1'},
    'google': {'name': 'Google (Gemini)', 'env': 'GEMINI_API_KEY', 'base': 'https://generativelanguage.googleapis.com/v1beta/openai'},
}

def validate(provider, model=None, key=None):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError('Unsupported AI provider')
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', model)):
        raise ValueError('Invalid model ID')
    if key is not None and (not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError('Invalid API key')

def read_settings(root, aid):
    path = Path(root) / 'state/ai-settings.json'
    if path.is_symlink():
        raise ValueError('Invalid AI settings path')
    if not path.exists():
        return {'schema': 1, 'account_id': aid, 'keys': {}}
    if path.stat().st_mode & 0o077:
        raise ValueError('AI settings must be private')
    value = json.loads(path.read_text())
    if value.get('schema') != 1 or value.get('account_id') != aid or not isinstance(value.get('keys'), dict):
        raise ValueError('Invalid AI settings account')
    for provider, key in value['keys'].items():
        validate(provider, key=key)
    if 'provider' in value:
        validate(value['provider'], value.get('model', ''))
    return value

def key_for(settings, provider, supplied=None):
    validate(provider, key=supplied)
    key = supplied or settings['keys'].get(provider) or os.environ.get(PROVIDERS[provider]['env'], '')
    if not key:
        raise ValueError('Enter an API key for this provider')
    return key

def public_settings(root, aid, account):
    settings = read_settings(root, aid)
    first = next(iter(account['properties'].values()), account)
    return {'provider': settings.get('provider', 'anthropic'),
            'model': settings.get('model', first.get('model', 'claude-sonnet-4-6')),
            'configured': 'provider' in settings,
            'providers': [{'id': p, 'name': v['name'], 'key_present': bool(settings['keys'].get(p) or os.environ.get(v['env']))} for p, v in PROVIDERS.items()]}

def save_settings(root, aid, provider, model, supplied=None):
    validate(provider, model, supplied)
    root = Path(root)
    (root / 'state').mkdir(parents=True, exist_ok=True, mode=0o700)
    with index_lock(root, True, 'ai-settings.lock'):
        settings = read_settings(root, aid)
        key_for(settings, provider, supplied)
        if supplied:
            settings['keys'][provider] = supplied
        settings.update(provider=provider, model=model)
        write_atomic(root / 'state/ai-settings.json', json.dumps(settings))
    return {'saved': True}

def response_json(response):
    if response.status_code != 200:
        # Never surface remote bodies or exceptions containing request credentials.
        raise ValueError('AI request failed; check API key, model access, billing and retry later')
    return response.json()

def headers(provider, key):
    if provider == 'anthropic':
        return {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    return {'Authorization': 'Bearer ' + key}

def list_models(provider, key):
    validate(provider, key=key)
    ids = set()
    after = None
    for _ in range(100):
        if provider == 'google':
            url = 'https://generativelanguage.googleapis.com/v1beta/models'
            params = {'pageSize': 1000}
            if after: params['pageToken'] = after
            h = {'x-goog-api-key': key}
        else:
            url = PROVIDERS[provider]['base'] + '/models'
            params = {'limit': 1000} if provider == 'anthropic' else {}
            if after: params['after_id'] = after
            h = headers(provider, key)
        payload = response_json(requests.get(url, headers=h, params=params, timeout=30, allow_redirects=False))
        rows = payload.get('models' if provider == 'google' else 'data')
        if not isinstance(rows, list): raise ValueError('Invalid model list')
        for row in rows:
            if provider == 'google' and 'generateContent' not in row.get('supportedGenerationMethods', []): continue
            name = row.get('name', '').removeprefix('models/') if provider == 'google' else row.get('id')
            if isinstance(name, str):
                try: validate(provider, name)
                except ValueError: continue
                ids.add(name)
        nxt = payload.get('nextPageToken') if provider == 'google' else payload.get('last_id') if payload.get('has_more') else None
        if not nxt: return sorted(ids)
        if nxt == after: raise ValueError('Model list did not advance')
        after = nxt
    raise ValueError('Too many model pages')

def generate(provider, model, key, system, message):
    validate(provider, model, key)
    if provider == 'anthropic':
        from anthropic import Anthropic
        result = Anthropic(api_key=key, timeout=60, max_retries=1).messages.create(
            model=model, max_tokens=1200, system=system, messages=[{'role': 'user', 'content': message}])
        return ''.join(b.text for b in result.content if getattr(b, 'type', None) == 'text')
    payload = {'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': message}]}
    payload['max_completion_tokens' if provider == 'openai' else 'max_tokens'] = 1200
    if provider == 'openai': payload['store'] = False
    value = response_json(requests.post(PROVIDERS[provider]['base'] + '/chat/completions',
        headers=headers(provider, key), json=payload, timeout=60, allow_redirects=False))
    text = value['choices'][0]['message']['content']
    if not isinstance(text, str) or not text.strip(): raise ValueError('Empty AI response')
    return text

def draft_text(prop, system, message):
    root = os.environ.get('TOOLKIT_DATA_DIR')
    aid = os.environ.get('TOOLKIT_ACCOUNT_ID')
    if root and aid:
        settings = read_settings(root, aid)
        if 'provider' in settings:
            return generate(settings['provider'], settings['model'], key_for(settings, settings['provider']), system, message)
    from hosting.config import secret
    return generate('anthropic', prop.get('model', 'claude-sonnet-4-6'), secret(prop['model_key_env']), system, message)

def test_connection(provider, model, key):
    text = generate(provider, model, key, 'Return ONLY the JSON object {"ok":true}.', 'Connection test. No guest or property data is included.')
    if json.loads(text) != {'ok': True}: raise ValueError('Model did not return the required JSON format; choose another model')
    return {'ok': True}
