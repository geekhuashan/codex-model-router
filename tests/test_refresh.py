"""Refresh tests never read local credentials or contact real upstreams."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from codex_model_router import refresh
from codex_model_router.configure import read_json, save_json


@pytest.fixture
def state(tmp_path):
    auth = tmp_path / 'auth.json'
    save_json(auth, {'tokens': {'access_token': 'fake-token', 'account_id': 'private-account'}})
    config = {'native_models': ['old'], 'routes': {'example/old': {
        'source': 'Example', 'base_url': 'https://example.test/v1',
        'model': 'old', 'credential': {'kind': 'env', 'key': 'SYNTHETIC_KEY'}, 'kind': 'responses'}},
        'refresh': {'enabled': True, 'native': {
            'url': 'https://chatgpt.com/backend-api/codex/models', 'auth_file': str(auth)},
            'providers': [{'prefix': 'example', 'route_template': 'example/old'}]}}
    save_json(tmp_path / 'router.json', config)
    save_json(tmp_path / 'models.json', {'models': [{'slug': 'old', 'display_name': 'My override'}]})
    return tmp_path / 'router.json'


def metadata(slug):
    return {'slug': slug, 'display_name': slug.title(), 'context_window': 200000,
            'supported_reasoning_levels': [{'effort': 'high'}], 'model_messages': {'base_instructions': 'fixture'},
            'tool_mode': 'tool_search', 'prefer_websockets': True, 'use_responses_lite': True,
            'service_tiers': ['fast'], 'extra_future_field': {'keep': True}}


async def fetch(session, url, headers, params=None):
    if 'chatgpt.com' in url:
        assert headers['ChatGPT-Account-ID'] == 'private-account'
        return {'models': [metadata('old'), metadata('new')]}
    assert headers == {'Authorization': 'Bearer fixture-key'}
    return {'data': [{'id': 'new'}, {'id': 'unknown-model'}]}


@pytest.mark.asyncio
async def test_additive_verified_merge_and_repeat(state):
    with patch.object(refresh, '_fetch', fetch):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
        assert set(report['added']) == {'new', 'example/new'}
        assert report['pending'] == ['example/unknown-model']
        models = {m['slug']: m for m in read_json(state.parent / 'models.json')['models']}
        assert models['old']['display_name'] == 'My override'
        assert models['new']['display_name'] == 'Pro · New'
        assert models['example/new']['extra_future_field'] == {'keep': True}
        for field in ('prefer_websockets', 'use_responses_lite'):
            assert models['example/new'][field] is False
        assert models['example/new']['tool_mode'] is None
        again = await refresh.refresh_once(state, lambda _: 'fixture-key')
        assert again['added'] == []
        assert again['needs_app_restart'] is True
        assert read_json(state)['routes']['example/new']['model'] == 'new'
    assert 'fake-token' not in json.dumps(report)
    assert (state.parent / 'refresh-status.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_failure_keeps_catalog_and_no_provider_guess(state):
    before = (state.parent / 'models.json').read_bytes()
    async def broken(session, url, headers, params=None):
        if 'chatgpt.com' in url:
            raise ValueError('fake-token')
        return {'data': [{'id': 'new'}]}
    with patch.object(refresh, '_fetch', broken):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert report['outcome'] == 'partial_failure'
    assert report['added'] == []
    assert report['pending'] == ['example/new']
    assert 'fake-token' not in json.dumps(report)
    assert (state.parent / 'models.json').read_bytes() == before


@pytest.mark.asyncio
async def test_concurrent_manual_changes_and_exclusions_preserved(state):
    async def edited(session, url, headers, params=None):
        current = read_json(state)
        current['unrelated_setting'] = 'preserve me'
        save_json(state, current)
        return await fetch(session, url, headers, params)
    config = read_json(state)
    config['refresh']['exclude'] = ['new']
    save_json(state, config)
    with patch.object(refresh, '_fetch', edited):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert report['added'] == []
    assert read_json(state)['unrelated_setting'] == 'preserve me'


@pytest.mark.asyncio
async def test_changed_template_not_overwritten(state):
    async def edited(session, url, headers, params=None):
        if 'example.test' in url:
            current = read_json(state)
            current['routes']['example/old']['base_url'] = 'https://new.example/v1'
            save_json(state, current)
        return await fetch(session, url, headers, params)
    with patch.object(refresh, '_fetch', edited):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert report['outcome'] == 'partial_failure'
    assert 'example/new' not in read_json(state)['routes']


@pytest.mark.asyncio
async def test_changed_policy_aborts_commit_and_lock_busy(state):
    before = (state.parent / 'models.json').read_bytes()
    async def edited(session, url, headers, params=None):
        current = read_json(state)
        current['refresh']['enabled'] = False
        save_json(state, current)
        return await fetch(session, url, headers, params)
    with patch.object(refresh, '_fetch', edited):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert report['outcome'] == 'configuration_changed'
    assert (state.parent / 'models.json').read_bytes() == before
    (state.parent / '.configure.lock').touch()
    with patch.object(refresh, '_fetch', fetch):
        assert (await refresh.refresh_once(state, lambda _: 'fixture-key'))['outcome'] == 'busy'


@pytest.mark.asyncio
async def test_security_endpoint_validation_before_credentials(state):
    config = read_json(state)
    config['refresh']['native']['url'] = 'https://evil.test/backend-api/codex/models'
    config['refresh']['providers'][0]['models_url'] = 'https://evil.test/models'
    save_json(state, config)
    with patch.object(refresh, '_fetch', side_effect=AssertionError('must not fetch')):
        report = await refresh.refresh_once(state, lambda _: (_ for _ in ()).throw(AssertionError('must not read key')))
    assert all(s['outcome'] == 'fetch_failed' for s in report['sources'])


@pytest.mark.asyncio
async def test_redirect_not_followed_and_body_limit():
    hits = []
    async def redirect(request):
        raise web.HTTPFound('/leak')
    async def leak(request):
        hits.append(request.headers.get('Authorization'))
        return web.json_response({'data': []})
    async def huge(request):
        return web.Response(body=b'x' * 32)
    app = web.Application()
    app.router.add_get('/redirect', redirect)
    app.router.add_get('/leak', leak)
    app.router.add_get('/huge', huge)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with refresh.aiohttp.ClientSession() as session:
            with pytest.raises(ValueError):
                await refresh._fetch(session, f'http://127.0.0.1:{port}/redirect', {'Authorization': 'fixture'})
            with patch.object(refresh, 'MAX_BYTES', 16), pytest.raises(ValueError):
                await refresh._fetch(session, f'http://127.0.0.1:{port}/huge', {})
        assert hits == []
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_disabled_periodic_does_not_fetch(state):
    config = read_json(state)
    config['refresh']['enabled'] = False
    save_json(state, config)
    with patch.object(refresh, 'refresh_once', side_effect=AssertionError('disabled')), \
            patch.object(refresh.asyncio, 'sleep', side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await refresh.run_periodic(state, lambda _: 'fixture')


def test_partial_native_metadata_rejected():
    with pytest.raises(ValueError):
        refresh._native_models({'models': [{'slug': 'new', 'display_name': 'New'}]})


@pytest.mark.asyncio
async def test_unrelated_bracket_and_colon_ids_do_not_block_known_models(state):
    async def mixed(session, url, headers, params=None):
        if 'chatgpt.com' in url:
            return await fetch(session, url, headers, params)
        return {'data': [{'id': 'new'}, {'id': 'claude-sonnet-5[1m]'}, {'id': 'model:latest'}]}
    with patch.object(refresh, '_fetch', mixed):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert 'example/new' in report['added']
    assert report['pending'] == ['example/claude-sonnet-5[1m]', 'example/model:latest']
    assert report['outcome'] == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, [], {'refresh': None}, {'refresh': []},
                                {'refresh': {'providers': None}},
                                {'refresh': {'providers': [None]}},
                                {'refresh': {'native': []}},
                                {'refresh': {'exclude': [None]}}])
async def test_malformed_configuration_reports_and_periodic_retries(state, bad):
    save_json(state, bad)
    with patch.object(refresh, '_fetch', side_effect=AssertionError('no network')):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
        assert report['outcome'] == 'invalid_configuration'
        with patch.object(refresh.asyncio, 'sleep', side_effect=asyncio.CancelledError) as sleeper:
            with pytest.raises(asyncio.CancelledError):
                await refresh.run_periodic(state, lambda _: 'fixture-key')
            sleeper.assert_called_once()


@pytest.mark.asyncio
async def test_hidden_native_models_not_cloned_to_provider(state):
    async def hidden(session, url, headers, params=None):
        if 'chatgpt.com' in url:
            model = metadata('internal-review')
            model['visibility'] = 'hide'
            return {'models': [model]}
        return {'data': [{'id': 'internal-review'}]}
    with patch.object(refresh, '_fetch', hidden):
        report = await refresh.refresh_once(state, lambda _: 'fixture-key')
    assert report['added'] == ['internal-review']
    assert 'example/internal-review' not in read_json(state)['routes']
