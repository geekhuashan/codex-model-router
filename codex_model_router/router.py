"""Loopback Responses router. No prompts, credentials or response bodies are logged."""
import argparse
import os
import asyncio
import json
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import sqlite3

import aiohttp
from aiohttp import web

MAX_BODY = 32 * 1024 * 1024
HOP = {'host', 'content-length', 'content-encoding', 'connection', 'upgrade',
       'transfer-encoding', 'keep-alive', 'proxy-authorization', 'proxy-authenticate',
       'te', 'trailer', 'accept-encoding'}
CUSTOM_HEADERS = {'accept', 'content-type', 'user-agent', 'openai-beta'}


def read_secret(source):
    """Credentials come from an environment variable or private file, never config literals."""
    kind = source.get('kind')
    if kind == 'env':
        value = os.environ.get(source['key'], '').strip()
    elif kind == 'file':
        value = Path(source['path']).expanduser().read_text().strip()
    elif kind == 'dotenv':
        value = ''
        for line in Path(source['path']).expanduser().read_text().splitlines():
            key, sep, candidate = line.strip().removeprefix('export ').partition('=')
            if sep and key == source['key']:
                value = candidate.strip().strip("\"'")
                break
    else:
        raise ValueError('Unsupported credential source')
    if not value:
        raise ValueError('Credential source is unavailable')
    return value


class Provenance:
    """Persist only opaque state hashes and their source, never conversation text."""
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS state (hash TEXT PRIMARY KEY, source TEXT)')

    def known(self, value, source):
        return self.source(value) == source

    def source(self, value):
        row = self.db.execute('SELECT source FROM state WHERE hash=?',
                              (hashlib.sha256(value.encode()).hexdigest(),)).fetchone()
        return row[0] if row else None

    def observe(self, value, source):
        if isinstance(value, dict):
            encrypted = value.get('encrypted_content')
            if isinstance(encrypted, str):
                self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                                (hashlib.sha256(encrypted.encode()).hexdigest(), source))
                self.db.commit()
            for child in value.values():
                self.observe(child, source)
        elif isinstance(value, list):
            for child in value:
                self.observe(child, source)


def normalize_history(data, source, provenance, native=False):
    """Keep visible history and tool results; discard only foreign hidden reasoning.

    Compaction can contain the only copy of earlier context, so never discard it.
    Unknown native state is accepted for pre-installation native tasks.
    """
    items = data.get('input')
    if not isinstance(items, list):
        return False
    kept = []
    changed = False
    for item in items:
        encrypted = item.get('encrypted_content')
        previous = provenance.source(encrypted) if encrypted and provenance else None
        foreign = bool(encrypted) and previous != source and not (native and previous is None)
        if foreign:
            if item.get('type') == 'reasoning':
                changed = True
                continue
            # Preserve opaque context; upstream decides whether it can interpret it.
        kept.append(item)
    if changed:
        data['input'] = kept
    return changed


def prepare(body, route, headers, provenance=None):
    """Native headers stay native. Third-party requests get fresh credentials."""
    outgoing = {k: v for k, v in headers.items() if k.lower() not in HOP}
    outgoing['Accept-Encoding'] = 'identity'
    if route is None:
        source = 'ChatGPT' if any(k.lower() == 'chatgpt-account-id' for k in headers) else 'OpenAI API'
        data = json.loads(body)
        if normalize_history(data, source, provenance, native=True):
            body = json.dumps(data, ensure_ascii=False).encode()
        return body, outgoing
    outgoing = {k: v for k, v in outgoing.items() if k.lower() in CUSTOM_HEADERS}
    outgoing['Authorization'] = 'Bearer ' + read_secret(route['credential'])
    outgoing['Accept-Encoding'] = 'identity'
    data = json.loads(body)
    data['model'] = route['model']
    normalize_history(data, route['source'], provenance)
    data.pop('service_tier', None)
    return json.dumps(data, ensure_ascii=False).encode(), outgoing


def create_app(config, *, session=None):
    app = web.Application(client_max_size=MAX_BODY)
    app['config'] = config
    app['routes'] = config['routes']
    app['session'] = session
    app['stats'] = {'requests': 0, 'by_source': {}, 'last': None}
    app['log'] = logging.getLogger('model-router')
    app['provenance'] = Provenance(config.get('state_db', ':memory:'))

    async def startup(app):
        if app['session'] is None:
            app['session'] = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=600),
                auto_decompress=True, trust_env=False, cookie_jar=aiohttp.DummyCookieJar())

    async def cleanup(app):
        await app['session'].close()
        app['provenance'].db.close()

    async def health(request):
        return web.json_response({'status': 'ok', 'models': list(app['routes']), **app['stats']})

    async def handle(request):
        if request.headers.get('Origin'):
            raise web.HTTPForbidden(text='Browser-origin requests are disabled')
        suffix = '/' + request.match_info['tail']
        if suffix not in ('/responses', '/responses/compact', '/v1/responses', '/v1/responses/compact'):
            raise web.HTTPNotFound()
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return web.json_response({'error': {'message': 'Use HTTP Responses transport'}}, status=426)
        if request.method != 'POST':
            raise web.HTTPMethodNotAllowed(request.method, ['POST'])
        if not request.headers.get('Authorization', '').startswith('Bearer '):
            raise web.HTTPUnauthorized()
        try:
            raw = await request.read()  # aiohttp decodes request compression.
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Request must be a JSON object')
            if isinstance(data.get('input'), list) and any(not isinstance(i, dict) for i in data['input']):
                raise ValueError('Input items must be JSON objects')
            model = data.get('model')
            if not isinstance(model, str) or not model:
                raise ValueError('Model is required; no implicit billing fallback')
            route = app['routes'].get(model)
            if not route and model not in config['native_models']:
                raise ValueError('Unknown model; no implicit billing fallback')
            body, headers = prepare(raw, route, request.headers, app['provenance'])
        except (ValueError, KeyError, OSError) as exc:
            # Only validation messages are exposed; never source file contents.
            message = str(exc) if isinstance(exc, ValueError) else 'Credential unavailable'
            return web.json_response({'error': {'message': message}}, status=400)
        suffix = suffix.removeprefix('/v1')
        if route:
            base, source = route['base_url'], route['source']
        elif request.headers.get('ChatGPT-Account-ID'):
            base, source = config['chatgpt_url'], 'ChatGPT'
        else:
            base, source = config['openai_url'], 'OpenAI API'
        url = base.rstrip('/') + suffix
        stats = app['stats']
        stats['requests'] += 1
        stats['by_source'][source] = stats['by_source'].get(source, 0) + 1
        started = time.monotonic()
        response = None
        status = 502
        try:
            async with app['session'].post(url, data=body, headers=headers, allow_redirects=False) as upstream:
                status = upstream.status
                safe_headers = {k: v for k, v in upstream.headers.items()
                                if k.lower() not in HOP and k.lower() != 'location'}
                # Never follow or expose credential-bearing upstream redirects.
                safe_headers.pop('Location', None)
                response = web.StreamResponse(status=status, headers=safe_headers)
                await response.prepare(request)
                buffer = b''
                is_sse = 'text/event-stream' in upstream.headers.get('Content-Type', '')
                async for chunk in upstream.content.iter_any():
                    buffer += chunk
                    if is_sse:
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if line.startswith(b'data:'):
                                try:
                                    app['provenance'].observe(json.loads(line[5:]), source)
                                except (ValueError, UnicodeError):
                                    pass
                    if len(buffer) > MAX_BODY:
                        buffer = b''
                    await response.write(chunk)
                if not is_sse:
                    try:
                        app['provenance'].observe(json.loads(buffer), source)
                    except (ValueError, UnicodeError):
                        pass
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            if response is not None and response.prepared:
                response.force_close()
                return response
            return web.json_response({'error': {'message': 'Upstream connection failed'}}, status=502)
        finally:
            event = {'source': source, 'model': model, 'status': status,
                     'seconds': round(time.monotonic() - started, 2)}
            stats['last'] = event
            app['log'].info(json.dumps(event))

    prefix = '/' + config['local_token']
    app.router.add_get(prefix + '/health', health)
    app.router.add_route('*', prefix + '/{tail:.*}', handle)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    handler = RotatingFileHandler(config_path.parent / 'activity.log', maxBytes=1024 * 1024, backupCount=2)
    logging.getLogger('model-router').addHandler(handler)
    logging.getLogger('model-router').setLevel(logging.INFO)
    web.run_app(create_app(config), host='127.0.0.1', port=config['port'],
                access_log=None, print=None, handler_cancellation=True)


if __name__ == "__main__":
    main()
