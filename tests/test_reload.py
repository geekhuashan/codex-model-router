import asyncio
import copy
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_model_router.reload import RouteSnapshot
from codex_model_router.router import create_app


def config(url):
    return {'local_token': 'test', 'native_models': ['native'], 'routes': {},
            'chatgpt_url': url + '/old', 'openai_url': url + '/old'}


def test_invalid_config_keeps_last_valid_snapshot(tmp_path):
    path = tmp_path / 'router.json'
    original = config('http://127.0.0.1:1234')
    path.write_text(json.dumps(original))
    snapshots = RouteSnapshot(original, path)
    assert snapshots.get() == original
    for bad_json in ['{partial', '[]', 'null', 'false', '42']:
        path.write_text(bad_json)
        assert snapshots.get() == original
    bad = copy.deepcopy(original)
    bad['native_models'] = 'wrong-type'
    path.write_text(json.dumps(bad))
    assert snapshots.get() == original
    bad = copy.deepcopy(original)
    bad['local_token'] = 'changed'
    path.write_text(json.dumps(bad))
    assert snapshots.get() == original
    updated = copy.deepcopy(original)
    updated['native_models'].append('new')
    path.write_text(json.dumps(updated))
    assert snapshots.get()['native_models'] == ['native', 'new']
    assert original['native_models'] == ['native']


@pytest.mark.asyncio
async def test_hot_reload_preserves_active_stream_and_new_requests(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def upstream(request):
        data = await request.json()
        seen.append((request.path, data['model']))
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        if data.get('hold'):
            started.set()
            await release.wait()
        await response.write(b'data: {"type":"response.completed"}\n\n')
        return response

    remote = web.Application()
    remote.router.add_post('/{tail:.*}', upstream)
    server = TestServer(remote)
    await server.start_server()
    initial = config(str(server.make_url('')).rstrip('/'))
    path = tmp_path / 'router.json'
    path.write_text(json.dumps(initial))
    client = TestClient(TestServer(create_app(initial, config_path=path)))
    await client.start_server()
    headers = {'Authorization': 'Bearer mock'}
    try:
        first = await client.post('/test/responses', json={'model': 'native', 'hold': True}, headers=headers)
        await asyncio.wait_for(started.wait(), 2)
        updated = copy.deepcopy(initial)
        updated['native_models'].append('new')
        updated['openai_url'] = str(server.make_url('/new'))
        path.write_text(json.dumps(updated))
        second = await client.post('/test/responses', json={'model': 'new'}, headers=headers)
        assert second.status == 200
        assert b'response.completed' in await second.read()
        release.set()
        assert b'response.completed' in await first.read()
        assert seen == [('/old/responses', 'native'), ('/new/responses', 'new')]
        denied = await client.post('/test/responses', json={'model': 'unknown'}, headers=headers)
        assert denied.status == 400
    finally:
        release.set()
        await client.close()
        await server.close()
