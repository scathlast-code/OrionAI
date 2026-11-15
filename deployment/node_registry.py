"""Configuration-only registry; no remote execution or deployment implied."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_ROLES = frozenset({'all','telegram','miniapp','greenpost','monitoring','llm','search','documents','utm','analytics','backups','media','help','database'})

class ConfigurationError(ValueError): pass

def validate(config: dict) -> dict:
    nodes = config.get('nodes')
    if not isinstance(nodes,list) or not 1 <= len(nodes) <= 10: raise ConfigurationError('1–10 вузлів')
    ids=set()
    for node in nodes:
        nid=node.get('id')
        if not isinstance(nid,str) or not nid or nid in ids: raise ConfigurationError('некоректний або повторний ID')
        ids.add(nid)
        if not isinstance(node.get('enabled'),bool): raise ConfigurationError('enabled має бути boolean')
        roles=node.get('roles')
        if not isinstance(roles,list) or any(r not in ALLOWED_ROLES for r in roles): raise ConfigurationError('невідома роль')
        endpoint=node.get('endpoint')
        if endpoint is not None:
            p=urlparse(endpoint)
            if p.scheme!='https' or not p.hostname or p.username or p.password: raise ConfigurationError('endpoint має бути HTTPS URL')
    if config.get('default_mode') not in ('single','distributed'): raise ConfigurationError('невідомий режим')
    if config['default_mode']=='single' and not any(n['enabled'] and 'all' in n['roles'] for n in nodes): raise ConfigurationError('для single потрібен вузол all')
    return config

def load(path: str|Path) -> dict:
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))

def assign(config:dict, role:str) -> str|None:
    validate(config)
    if role not in ALLOWED_ROLES: raise ConfigurationError('невідома роль')
    for node in config['nodes']:
        if node['enabled'] and (role in node['roles'] or 'all' in node['roles']): return node['id']
    return None
