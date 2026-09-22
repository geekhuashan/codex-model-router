"""Explicit, reversible bridge for tasks pinned to a legacy custom provider.

Restart all Codex App/CLI processes after either operation. In-flight requests
and already loaded provider configurations are not changed by editing this file.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets

from .configure import read_json, save_json, toml_module, write_private

RESERVED = {'openai', 'ollama', 'lmstudio'}


def _provider(value):
    if value in RESERVED or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value):
        raise ValueError('Specify a custom provider ID; built-in providers cannot be bridged')
    return value


def _references_provider(value, provider):
    if isinstance(value, dict):
        if value.get('kind') == 'codex_provider' and value.get('provider') == provider:
            return True
        return any(_references_provider(item, provider) for item in value.values())
    if isinstance(value, list):
        return any(_references_provider(item, provider) for item in value)
    return False


def enable(args):
    toml = toml_module()
    provider = _provider(args.provider)
    receipt_path = args.state_dir / ('legacy-' + provider + '.json')
    if receipt_path.exists():
        raise ValueError('Bridge already recorded; restore it before installing again')
    router = read_json(args.state_dir / 'router.json')
    if _references_provider(router.get('routes', {}), provider):
        raise ValueError('Migrate route credentials out of this provider before bridging it')
    port = router['port']
    token = router['local_token']
    if (type(port) is not int or not 1 <= port <= 65535
            or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', token)):
        raise ValueError('Invalid router listener configuration')
    path = args.codex_home / 'config.toml'
    current = path.read_text(encoding='utf-8')
    document = toml.parse(current)
    providers = document.get('model_providers', {})
    if provider not in providers or not isinstance(providers[provider], dict):
        raise ValueError('Custom provider does not exist')
    before = providers[provider].unwrap()
    root_before = {'present': 'model_provider' in document,
                   'value': document.get('model_provider')}
    root_changed = document.get('model_provider') == provider
    installed = {'name': 'Local model router',
                 'base_url': f'http://127.0.0.1:{port}/{token}/v1',
                 'wire_api': 'responses', 'requires_openai_auth': True}
    document['model_providers'][provider] = installed
    if root_changed:
        document['model_provider'] = 'openai'
    backup = 'legacy-backup-' + secrets.token_hex(12) + '.toml'
    write_private(args.state_dir / backup, current)
    if path.read_text(encoding='utf-8') != current:
        raise ValueError('Configuration changed concurrently')
    save_json(receipt_path, {'version': 1, 'provider': provider,
                            'config_path': str(path), 'backup': backup,
                            'before': before, 'installed': installed,
                            'root_before': root_before, 'root_changed': root_changed,
                            'root_installed': 'openai' if root_changed else document.get('model_provider')})
    write_private(path, toml.dumps(document))


def restore(args):
    toml = toml_module()
    provider = _provider(args.provider)
    receipt_path = args.state_dir / ('legacy-' + provider + '.json')
    receipt = read_json(receipt_path)
    path = args.codex_home / 'config.toml'
    if receipt['provider'] != provider or receipt['config_path'] != str(path):
        raise ValueError('Receipt belongs to another configuration')
    current = path.read_text(encoding='utf-8')
    document = toml.parse(current)
    # A crash after saving the receipt but before replacing config leaves the
    # original values in place. Retire that pending receipt without rewriting.
    root_before = receipt['root_before']
    root_is_original = (('model_provider' in document) == root_before['present']
                        and document.get('model_provider') == root_before['value'])
    if (document.get('model_providers', {}).get(provider) == receipt['before']
            and (not receipt['root_changed'] or root_is_original)):
        receipt_path.unlink()
        return
    if document.get('model_providers', {}).get(provider) != receipt['installed']:
        raise ValueError('Bridged provider changed externally; refusing to overwrite')
    if receipt['root_changed'] and document.get('model_provider') != receipt['root_installed']:
        raise ValueError('Root provider changed externally; refusing to overwrite')
    backup = receipt['backup']
    if not isinstance(backup, str) or Path(backup).name != backup:
        raise ValueError('Invalid backup reference')
    original = toml.parse((args.state_dir / backup).read_text(encoding='utf-8'))
    if original['model_providers'][provider] != receipt['before']:
        raise ValueError('Backup does not match receipt')
    document['model_providers'][provider] = original['model_providers'][provider]
    if receipt['root_changed']:
        if receipt['root_before']['present']:
            document['model_provider'] = receipt['root_before']['value']
        else:
            document.pop('model_provider', None)
    if path.read_text(encoding='utf-8') != current:
        raise ValueError('Configuration changed concurrently')
    write_private(path, toml.dumps(document))
    receipt_path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('enable', 'restore'):
        sub = commands.add_parser(command)
        sub.add_argument('--codex-home', required=True, type=Path)
        sub.add_argument('--state-dir', required=True, type=Path)
        sub.add_argument('--provider', required=True)
    args = parser.parse_args(argv)
    args.codex_home = args.codex_home.expanduser().resolve()
    args.state_dir = args.state_dir.expanduser().resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = args.state_dir / '.configure.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.exit(1, 'error: Configuration is locked by another operation\n')
    try:
        os.close(fd)
        try:
            globals()[args.command](args)
        except (ValueError, KeyError, TypeError, OSError):
            # Never include configuration/parser text or credential-bearing URLs.
            parser.exit(1, 'error: Bridge operation refused. Check custom provider ID, route credential dependencies, installation receipt and external changes.\n')
    finally:
        lock.unlink(missing_ok=True)
    print('Legacy bridge ' + args.command + ' complete. Restart all Codex App and CLI processes to apply; running requests are unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
