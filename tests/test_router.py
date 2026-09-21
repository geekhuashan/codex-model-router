"""HTTP contract tests; every upstream is an ephemeral loopback mock."""
import asyncio
import json

import pytest
import pytest_asyncio
import zstandard
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_model_router.router import create_app


@pytest_asyncio.fixture
async def harness(tmp_path):
    seen = []
    release = asyncio.Event()

    async def upstream(request):
        body = await request.json()
        seen.append((request.path, dict(request.headers), body))
        if body.get('fixture') == 'redirect':
            return web.Response(status=307, headers={'Location': '/redirect-target'})
        if body.get('stream'):
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            event = {'type': 'response.output_item.done', 'item': {
                'type': 'reasoning', 'encrypted_content': 'opaque-mock-state'}}
            await response.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
            await release.wait()
            await response.write(b'data: {"type":"response.completed"}\n\n')
            await response.write_eof()
            return response
        return web.json_response({'output': [{'type': 'reasoning',
                                             'encrypted_content': 'opaque-mock-state'}]})

    remote = web.Application()
    remote.router.add_route('*', '/{tail:.*}', upstream)
    server = TestServer(remote)
    await server.start_server()
    secret = tmp_path / 'mock.env'
    secret.write_text('MOCK_KEY=mock-third-party-key\n')
    credential = {'kind': 'dotenv', 'path': str(secret), 'key': 'MOCK_KEY'}
    config = {
        'local_token': 'local-mock-token', 'native_models': ['native-model'],
        'chatgpt_url': str(server.make_url('/backend-api/codex')),
        'openai_url': str(server.make_url('/openai/v1')),
        'routes': {
            'custom/alpha': {'model': 'actual-alpha', 'source': 'CLIProxy',
                             'base_url': str(server.make_url('/custom/v1')),
                             'credential': credential},
            'custom/beta': {'model': 'actual-beta', 'source': 'Other',
                            'base_url': str(server.make_url('/other/v1')),
                            'credential': credential},
        },
    }
    client = TestClient(TestServer(create_app(config)))
    await client.start_server()
    try:
        yield client, seen, release
    finally:
        release.set()
        await client.close()
        await server.close()


HEADERS = {'Authorization': 'Bearer mock-pro-token', 'ChatGPT-Account-ID': 'mock-account',
           'X-Private-Account-Metadata': 'mock-private', 'OpenAI-Beta': 'responses=v1'}
PATH = '/local-mock-token/v1/responses'


@pytest.mark.asyncio
@pytest.mark.parametrize('model,path', [('native-model', '/backend-api/codex/responses'),
                                      ('custom/alpha', '/custom/v1/responses')])
async def test_paths_and_credentials(harness, model, path):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': model, 'input': []}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    actual_path, headers, body = seen[-1]
    assert actual_path == path
    assert headers['OpenAI-Beta'] == 'responses=v1'
    if model == 'native-model':
        assert headers['Authorization'] == 'Bearer mock-pro-token'
        assert headers['ChatGPT-Account-ID'] == 'mock-account'
        assert body['model'] == model
    else:
        assert headers['Authorization'] == 'Bearer mock-third-party-key'
        assert 'ChatGPT-Account-ID' not in headers
        assert 'X-Private-Account-Metadata' not in headers
        assert body['model'] == 'actual-alpha'


@pytest.mark.asyncio
async def test_native_api_key_path(harness):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'native-model'},
                                 headers={'Authorization': 'Bearer mock-api-key'})
    assert response.status == 200
    await response.read()
    assert seen[-1][0] == '/openai/v1/responses'
    assert seen[-1][1]['Authorization'] == 'Bearer mock-api-key'


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{}, {'model': 'missing'}, {'model': None}])
async def test_unknown_and_missing_model_fail_closed(harness, body):
    client, seen, _ = harness
    response = await client.post(PATH, json=body, headers=HEADERS)
    assert response.status == 400
    assert not seen


@pytest.mark.asyncio
async def test_websocket_fallback(harness):
    client, seen, _ = harness
    response = await client.get(PATH, headers={**HEADERS, 'Upgrade': 'websocket', 'Connection': 'Upgrade'})
    assert response.status == 426
    assert not seen


@pytest.mark.asyncio
async def test_zstd_request(harness):
    client, seen, _ = harness
    body = zstandard.ZstdCompressor().compress(json.dumps({'model': 'custom/alpha', 'input': []}).encode())
    response = await client.post(PATH, data=body, headers={**HEADERS, 'Content-Encoding': 'zstd',
                                                        'Content-Type': 'application/json'})
    assert response.status == 200, await response.text()
    await response.read()
    assert seen[-1][2]['model'] == 'actual-alpha'
    assert 'Content-Encoding' not in seen[-1][1]


@pytest.mark.asyncio
async def test_sse_flush_and_same_source_provenance(harness):
    client, seen, release = harness
    response = await client.post(PATH, json={'model': 'custom/alpha', 'stream': True}, headers=HEADERS)
    assert response.status == 200
    first = await asyncio.wait_for(response.content.readuntil(b'\n\n'), timeout=2)
    assert b'opaque-mock-state' in first
    assert not release.is_set(), 'First event must arrive before upstream completes'
    release.set()
    assert b'response.completed' in await response.read()
    response = await client.post(PATH, json={'model': 'custom/alpha', 'input': [
        {'type': 'reasoning', 'encrypted_content': 'opaque-mock-state'}]}, headers=HEADERS)
    assert response.status == 200, await response.text()
    await response.read()
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['custom/beta', 'native-model'])
async def test_cross_source_compaction_forwarded_without_data_loss(harness, target):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'custom/alpha'}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    response = await client.post(PATH, json={'model': target, 'input': [
        {'type': 'compaction', 'encrypted_content': 'opaque-mock-state'}]}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][2]['input'] == [{'type': 'compaction', 'encrypted_content': 'opaque-mock-state'}]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_foreign_reasoning_removed_but_visible_history_preserved(harness):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'native-model'}, headers=HEADERS)
    await response.read()
    visible = [{'role': 'user', 'content': 'My name is ExampleName'},
               {'role': 'assistant', 'content': 'Hello ExampleName'},
               {'role': 'user', 'content': 'What is my name?'}]
    response = await client.post(PATH, json={'model': 'custom/alpha', 'input': [
        {'type': 'reasoning', 'encrypted_content': 'opaque-mock-state'}, *visible]}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][2]['input'] == visible



@pytest.mark.asyncio
@pytest.mark.parametrize('model,expected', [('native-model', '/backend-api/codex/responses/compact'),
                                         ('custom/alpha', '/custom/v1/responses/compact')])
async def test_compact_uses_same_route(harness, model, expected):
    client, seen, _ = harness
    response = await client.post(PATH + '/compact', json={'model': model, 'input': []}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][0] == expected


@pytest.mark.asyncio
async def test_redirect_not_followed(harness):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'custom/alpha', 'fixture': 'redirect'}, headers=HEADERS,
                                 allow_redirects=False)
    assert response.status == 307
    assert len(seen) == 1
    assert 'Location' not in response.headers

@pytest.mark.asyncio
async def test_custom_reasoning_to_pro_keeps_messages(harness):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'custom/alpha'}, headers=HEADERS)
    await response.read()
    visible = [{'role': 'user', 'content': 'My name is ExampleName'},
               {'role': 'assistant', 'content': 'Hello ExampleName'},
               {'role': 'user', 'content': 'What is my name?'}]
    response = await client.post(PATH, json={'model': 'native-model', 'input': [
        {'type': 'reasoning', 'encrypted_content': 'opaque-mock-state'}, *visible]}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][2]['input'] == visible


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['custom/beta', 'native-model'])
async def test_response_id_forwarded_after_reasoning_filter(harness, target):
    client, seen, _ = harness
    response = await client.post(PATH, json={'model': 'custom/alpha'}, headers=HEADERS)
    await response.read()
    visible = {'role': 'user', 'content': 'Keep my context'}
    response = await client.post(PATH, json={'model': target,
        'previous_response_id': 'resp-example', 'input': [
        {'type': 'reasoning', 'encrypted_content': 'opaque-mock-state'}, visible]}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][2]['previous_response_id'] == 'resp-example'
    assert seen[-1][2]['input'] == [visible]

@pytest.mark.asyncio
async def test_monitor_and_dashboard_report_routing_without_credentials(harness):
    client, seen, release = harness
    response = await client.post(PATH, json={'model': 'custom/alpha', 'stream': True}, headers=HEADERS)
    await response.content.readuntil(b'\n\n')
    state = await (await client.get('/local-mock-token/health')).json()
    assert state['monitor']['active'][0]['source'] == 'CLIProxy'
    release.set()
    await response.read()
    state = await (await client.get('/local-mock-token/health')).json()
    assert state['monitor']['recent'][0]['outcome'] == 'completed'
    assert not state['monitor']['active']
    assert 'mock-third-party-key' not in json.dumps(state)
    assert 'mock-pro-token' not in json.dumps(state)
    assert (await client.get('/local-mock-token/dashboard')).status == 200

@pytest.mark.asyncio
async def test_plaintext_reasoning_from_another_provider_is_not_replayed(harness):
    client, seen, _ = harness
    visible = [{'role': 'user', 'content': 'Remember the report'},
               {'type': 'function_call_output', 'call_id': 'example', 'output': 'report saved'}]
    response = await client.post(PATH, json={'model': 'custom/alpha', 'input': [
        {'type': 'reasoning', 'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'PRIVATE'}]},
        *visible]}, headers=HEADERS)
    assert response.status == 200
    await response.read()
    assert seen[-1][2]['input'] == visible


def test_reasoning_content_capability_and_opaque_context_preservation():
    from copy import deepcopy
    from codex_model_router.router import normalize_history, Provenance
    provenance = Provenance()
    item = {'type': 'reasoning', 'encrypted_content': 'opaque',
            'content': [{'type': 'reasoning_text', 'text': 'PRIVATE'}]}
    provenance.observe(item, 'Example')
    data = {'input': [deepcopy(item), {'type': 'compaction', 'encrypted_content': 'context'}]}
    normalize_history(data, 'Example', provenance)
    assert data['input'][0] == {'type': 'reasoning', 'encrypted_content': 'opaque'}
    assert data['input'][1] == {'type': 'compaction', 'encrypted_content': 'context'}
    data = {'input': [deepcopy(item)]}
    assert not normalize_history(data, 'Example', provenance, accepts_reasoning_content=True)
    assert data['input'][0] == item
    provenance.db.close()
