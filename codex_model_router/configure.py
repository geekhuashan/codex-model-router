"""Local-only catalog and reversible Codex configuration management."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit


FIELDS = ('model_catalog_json', 'openai_base_url', 'model_provider')


def write_private(path: Path, text: str) -> None:
    """Replace a file atomically without exposing its contents to other users."""
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path: Path, value) -> None:
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def valid_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid base URL') from None
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value or '\\' in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('Base URL must be HTTP(S), without credentials, query or fragment')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Plain HTTP is allowed only for loopback upstreams')
    return value.rstrip('/')


def init(args) -> None:
    state = args.state_dir
    if (state / 'router.json').exists() or (state / 'models.json').exists():
        raise ValueError('State already initialized; refusing to overwrite it')
    if not 1 <= args.port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    cache = read_json(args.codex_home / 'models_cache.json')
    models = cache.get('models')
    if not isinstance(models, list) or not models:
        raise ValueError('models_cache.json must contain a nonempty models array')
    slugs = [m.get('slug') if isinstance(m, dict) else None for m in models]
    if any(not isinstance(s, str) or not s for s in slugs) or len(set(slugs)) != len(slugs):
        raise ValueError('Catalog model slugs must be nonempty and unique')
    save_json(state / 'models.json', {'models': models})
    save_json(state / 'router.json', {
        'port': args.port, 'local_token': secrets.token_urlsafe(32),
        'chatgpt_url': 'https://chatgpt.com/backend-api/codex',
        'openai_url': 'https://api.openai.com/v1',
        'native_models': slugs, 'routes': {},
        'state_db': str(state / 'provenance.sqlite3'),
    })


def add(args) -> None:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.name):
        raise ValueError('Name must contain only letters, digits, dots, underscores or hyphens')
    if not args.model or any(ord(c) <= 32 or ord(c) == 127 for c in args.model):
        raise ValueError('Model ID must be nonempty and contain no whitespace')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.api_key_env):
        raise ValueError('Invalid API key environment variable name')
    base_url = valid_base_url(args.base_url)
    state = args.state_dir
    config = read_json(state / 'router.json')
    catalog = read_json(state / 'models.json')
    alias = args.name + '/' + args.model
    if alias in config['routes'] or any(m['slug'] == alias for m in catalog['models']):
        raise ValueError('Model alias already exists')
    template = next((m for m in catalog['models'] if m['slug'] == args.template), None)
    if template is None or args.template not in config['native_models']:
        raise ValueError('Template must identify an existing native model')
    model = copy.deepcopy(template)
    model.update(slug=alias, display_name=args.label, use_responses_lite=False,
                 service_tiers=[], prefer_websockets=False, supports_search_tool=False)
    if 'additional_speed_tiers' in model:
        model['additional_speed_tiers'] = []
    if 'tool_mode' in model:
        model['tool_mode'] = None
    if 'input_modalities' in model:
        model['input_modalities'] = ['text']
    catalog['models'].append(model)
    config['routes'][alias] = {
        'source': args.name, 'kind': 'responses', 'model': args.model,
        'base_url': base_url, 'credential': {'kind': 'env', 'key': args.api_key_env},
    }
    # Install routing first: a crash must not expose an unroutable catalog entry.
    save_json(state / 'router.json', config)
    save_json(state / 'models.json', catalog)


def toml_module():
    try:
        import tomlkit
    except ImportError:
        raise ValueError('enable and restore require tomlkit; install the package dependencies') from None
    return tomlkit


def enable(args) -> None:
    tomlkit = toml_module()
    state = args.state_dir
    receipt = state / 'installation.json'
    if receipt.exists():
        raise ValueError('An installation is already recorded; restore it first')
    router = read_json(state / 'router.json')
    if not (state / 'models.json').is_file():
        raise ValueError('Model catalog is missing')
    path = args.codex_home / 'config.toml'
    existed = path.exists()
    original = path.read_text(encoding='utf-8') if existed else ''
    document = tomlkit.parse(original)
    installed = {
        'model_catalog_json': str(state / 'models.json'),
        'openai_base_url': f"http://127.0.0.1:{router['port']}/{router['local_token']}/v1",
        'model_provider': 'openai',
    }
    backup = 'config-backup-' + secrets.token_hex(8) + '.toml'
    write_private(state / backup, original)
    for key, value in installed.items():
        document[key] = value
    args.codex_home.mkdir(parents=True, exist_ok=True)
    if path.exists() != existed or (existed and path.read_text(encoding='utf-8') != original):
        raise ValueError('Codex configuration changed while preparing installation; retry')
    save_json(receipt, {'config_path': str(path), 'backup': backup, 'installed': installed})
    write_private(path, tomlkit.dumps(document))


def restore(args) -> None:
    tomlkit = toml_module()
    state = args.state_dir
    receipt_path = state / 'installation.json'
    receipt = read_json(receipt_path)
    path = args.codex_home / 'config.toml'
    if str(path) != receipt['config_path']:
        raise ValueError('Codex home differs from the recorded installation')
    current = path.read_text(encoding='utf-8')
    document = tomlkit.parse(current)
    if any(key not in document or document[key] != receipt['installed'][key] for key in FIELDS):
        raise ValueError('Installed configuration fields changed externally; refusing to overwrite them')
    original = tomlkit.parse((state / receipt['backup']).read_text(encoding='utf-8'))
    for key in FIELDS:
        if key in original:
            document[key] = original[key]
        else:
            del document[key]
    if path.read_text(encoding='utf-8') != current:
        raise ValueError('Codex configuration changed while preparing restore; retry')
    write_private(path, tomlkit.dumps(document))
    receipt_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('init', 'add', 'enable', 'restore'):
        sub = commands.add_parser(command)
        sub.add_argument('--state-dir', type=Path, required=True)
        if command != 'add':
            sub.add_argument('--codex-home', type=Path, required=True)
        if command == 'init':
            sub.add_argument('--port', type=int, default=18791)
        if command == 'add':
            for option in ('name', 'base-url', 'model', 'label', 'api-key-env', 'template'):
                sub.add_argument('--' + option, required=True)
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser().resolve()
    if hasattr(args, 'codex_home'):
        args.codex_home = args.codex_home.expanduser().resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = args.state_dir / '.configure.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.exit(1, 'error: State is locked by another configure operation\n')
    try:
        os.close(fd)
        try:
            globals()[args.command](args)
        except (ValueError, KeyError, TypeError, OSError):
            # Do not echo TOML, URLs, credentials, or parser excerpts from private files.
            parser.exit(1, 'error: Configuration operation failed; check inputs and state (existing or externally changed installations are not overwritten)\n')
    finally:
        lock.unlink(missing_ok=True)
    print(args.command + ': complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
