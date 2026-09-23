"""Opt-in catalog discovery. Never sends prompts or records credentials."""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import aiohttp

from .configure import read_json, save_json, valid_base_url

MAX_BYTES = 8 * 1024 * 1024
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:\[\]-]{0,159}\Z')
class FetchError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _failure(exc):
    if isinstance(exc, FetchError):
        return exc.code
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
        return 'network_error'
    if isinstance(exc, OSError):
        return 'local_file_error'
    return 'invalid_configuration_or_catalog'


NAMESPACE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


def _origin(url):
    if not isinstance(url, str):
        raise ValueError('invalid_url')
    p = urlsplit(valid_base_url(url))
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)


async def _fetch(session, url, headers, params=None):
    async with session.get(url, headers=headers, params=params, allow_redirects=False) as response:
        if response.status != 200:
            raise FetchError('http_' + str(response.status))
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise FetchError('catalog_too_large')
            chunks.append(chunk)
        return json.loads(b''.join(chunks))


def _native_models(data):
    models = data['models']
    if not isinstance(models, list) or not models or len(models) > 2000:
        raise ValueError('invalid_catalog')
    result = {}
    for model in models:
        if not isinstance(model, dict):
            raise ValueError('invalid_metadata')
        slug = model.get('slug')
        if (not isinstance(slug, str) or not ID.fullmatch(slug) or '/' in slug
                or slug in result or not isinstance(model.get('display_name'), str)
                or not isinstance(model.get('context_window'), int)
                or model['context_window'] <= 0
                or not isinstance(model.get('supported_reasoning_levels'), list)
                or not isinstance(model.get('model_messages'), dict)):
            raise ValueError('incomplete_metadata')
        result[slug] = model
    return result


def _provider_ids(data):
    entries = data['data']
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError('invalid_catalog')
    result = set()
    for entry in entries:
        slug = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(slug, str) or not ID.fullmatch(slug):
            raise ValueError('invalid_model_id')
        result.add(slug)
    return sorted(result)


def _alias_model(model, alias, source):
    model = copy.deepcopy(model)
    model.update(slug=alias, display_name=source + ' · ' + model['display_name'],
                 prefer_websockets=False, use_responses_lite=False,
                 supports_search_tool=False, service_tiers=[], additional_speed_tiers=[],
                 availability_nux=None, upgrade=None, tool_mode=None, input_modalities=['text'])
    return model


def _policy(config):
    if not isinstance(config, dict):
        raise ValueError('invalid_configuration')
    policy = config.get('refresh', {})
    if not isinstance(policy, dict):
        raise ValueError('invalid_refresh_policy')
    if 'native' in policy and not isinstance(policy['native'], dict):
        raise ValueError('invalid_native_policy')
    providers = policy.get('providers', [])
    exclude = policy.get('exclude', [])
    if (not isinstance(providers, list) or not all(isinstance(p, dict) for p in providers)
            or not isinstance(exclude, list) or not all(isinstance(v, str) for v in exclude)
            or not isinstance(config.get('routes', {}), dict)
            or not isinstance(config.get('native_models', []), list)):
        raise ValueError('invalid_refresh_policy')
    return policy


async def refresh_once(config_path: Path, secret_reader):
    """Fetch explicitly configured sources, then merge under the configure lock.

    Reports contain only model IDs and fixed status codes. Source failure never
    replaces its last good entries. A changed refresh policy aborts the merge.
    """
    config_path = Path(config_path).expanduser().resolve()
    state = config_path.parent
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'outcome': 'ok',
              'added': [], 'pending': [], 'sources': []}
    try:
        snapshot = read_json(config_path)
        policy = _policy(snapshot)
    except (OSError, ValueError, TypeError):
        report['outcome'] = 'invalid_configuration'
        return report
    candidates = []
    verified = {}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        native = policy.get('native')
        if native:
            entry = {'source': 'native', 'outcome': 'ok', 'added': [], 'pending': []}
            report['sources'].append(entry)
            try:
                url = native['url']
                if not isinstance(url, str):
                    raise ValueError('invalid_native_endpoint')
                p = urlsplit(url)
                if (_origin(url) != ('https', 'chatgpt.com', 443)
                        or p.path != '/backend-api/codex/models'):
                    raise ValueError('invalid_native_endpoint')
                tokens = read_json(Path(native['auth_file']).expanduser())['tokens']
                headers = {'Authorization': 'Bearer ' + tokens['access_token'],
                           'ChatGPT-Account-ID': tokens['account_id'], 'originator': 'codex_cli_rs'}
                version = str(native.get('client_version', '0.155.1'))
                headers['User-Agent'] = 'codex_cli_rs/' + version
                verified = _native_models(await _fetch(session, url, headers, {'client_version': version}))
                for slug, model in verified.items():
                    value = copy.deepcopy(model)
                    value['display_name'] = native.get('label_prefix', 'Pro · ') + model['display_name']
                    candidates.append((slug, value, None, entry, None))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
                entry['outcome'] = 'fetch_failed'
                entry['error'] = _failure(exc)
        for spec in policy.get('providers', []):
            prefix = spec.get('prefix', '') if isinstance(spec, dict) else ''
            entry = {'source': prefix if isinstance(prefix, str) and NAMESPACE.fullmatch(prefix) else 'invalid',
                     'outcome': 'ok', 'added': [], 'pending': []}
            report['sources'].append(entry)
            try:
                if not isinstance(prefix, str) or not NAMESPACE.fullmatch(prefix):
                    raise ValueError('invalid_namespace')
                template = snapshot['routes'][spec['route_template']]
                if not isinstance(template, dict) or not isinstance(template.get('base_url'), str):
                    raise ValueError('invalid_route_template')
                url = spec.get('models_url') or template['base_url'].rstrip('/') + '/models'
                if _origin(url) != _origin(template['base_url']):
                    raise ValueError('cross_origin_endpoint')
                headers = {'Authorization': 'Bearer ' + secret_reader(template['credential'])}
                ids = _provider_ids(await _fetch(session, url, headers))
                for slug in ids:
                    alias = prefix + '/' + slug
                    if slug not in verified:
                        entry['pending'].append(alias)
                        continue
                    if verified[slug].get('visibility') == 'hide':
                        continue  # Internal native models are not third-party menu candidates.
                    route = copy.deepcopy(template)
                    route['model'] = slug
                    candidates.append((alias, _alias_model(verified[slug], alias, template['source']),
                                       route, entry, spec['route_template']))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
                entry['outcome'] = 'fetch_failed'
                entry['error'] = _failure(exc)
    lock = state / '.configure.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        report['outcome'] = 'busy'
        return report
    try:
        os.close(fd)
        current = read_json(config_path)
        original_config = copy.deepcopy(current)
        catalog = read_json(state / 'models.json')
        original_catalog = copy.deepcopy(catalog)
        if not isinstance(current, dict) or current.get('refresh', {}) != policy:
            report['outcome'] = 'configuration_changed'
            return report
        try:
            _policy(current)
            if (not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list)
                    or not all(isinstance(m, dict) and isinstance(m.get('slug'), str) for m in catalog['models'])):
                raise ValueError('invalid_catalog')
        except (ValueError, TypeError):
            report['outcome'] = 'invalid_configuration'
            return report
        existing = {m['slug'] for m in catalog['models']}
        excluded = set(policy.get('exclude', []))
        for slug, model, route, entry, template_id in candidates:
            if slug in excluded or (route and route['model'] in excluded):
                continue
            if template_id and current['routes'].get(template_id) != snapshot['routes'][template_id]:
                entry['outcome'] = 'configuration_changed'
                continue
            if route is None:
                if slug in current['routes']:
                    continue
                if slug not in current['native_models']:
                    current['native_models'].append(slug)
            else:
                if slug in current['native_models']:
                    continue
                current['routes'].setdefault(slug, route)
            if slug not in existing:
                catalog['models'].append(model)
                existing.add(slug)
                report['added'].append(slug)
                entry['added'].append(slug)
        for entry in report['sources']:
            entry['pending'] = [s for s in entry['pending'] if s not in existing
                                and s not in excluded and s.split('/', 1)[-1] not in excluded]
        report['pending'] = sorted({s for e in report['sources'] for s in e['pending']})
        if any(e['outcome'] != 'ok' for e in report['sources']):
            report['outcome'] = 'partial_failure'
        try:
            previous = read_json(state / 'refresh-status.json')
        except (OSError, ValueError):
            previous = {}
        report['needs_app_restart'] = bool(report['added'] or (isinstance(previous, dict) and previous.get('needs_app_restart')))
        # Noncooperating editors must not be overwritten either.
        if read_json(config_path) != original_config or read_json(state / 'models.json') != original_catalog:
            report['outcome'] = 'configuration_changed'
            return report
        # Route first: a crash may leave an invisible route, never an unroutable menu item.
        if current != original_config:
            save_json(config_path, current)
        if catalog != original_catalog:
            save_json(state / 'models.json', catalog)
        save_json(state / 'refresh-status.json', report)
    finally:
        lock.unlink(missing_ok=True)
    return report


async def run_periodic(config_path: Path, secret_reader):
    while True:
        try:
            policy = _policy(read_json(Path(config_path)))
            if policy.get('enabled', False):
                await refresh_once(config_path, secret_reader)
                interval = max(60, min(86400, int(policy.get('interval_seconds', 21600))))
            else:
                interval = 60
        except (OSError, ValueError, KeyError, TypeError, aiohttp.ClientError):
            interval = 21600
        await asyncio.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--status', action='store_true', help='Read last refresh without any network requests')
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = read_json(args.config.expanduser().resolve().parent / 'refresh-status.json')
        else:
            from .router import read_secret
            result = asyncio.run(refresh_once(args.config, read_secret))
        if not isinstance(result, dict):
            raise ValueError('invalid_status')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('outcome') == 'ok' else 1
    except (OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError, aiohttp.ClientError):
        parser.exit(1, 'error: Refresh failed; check local configuration and source availability\n')


if __name__ == '__main__':
    raise SystemExit(main())
