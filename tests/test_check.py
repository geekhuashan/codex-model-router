import json

import pytest

from codex_model_router.check import ProbeError, check, http_error, main, parse_sse, run_checks

MARKER = 'random-test-marker'
ROUTE = {'model': 'test-model', 'source': 'Example'}


def response(text):
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}]}


class MockRequests:
    def __init__(self, break_case=None):
        self.calls = []
        self.break_case = break_case

    async def __call__(self, endpoint, data):
        self.calls.append((endpoint, data))
        if endpoint.endswith('compact'):
            if self.break_case == 'compact_unsupported':
                raise http_error(404)
            return {'output': [{'type': 'compaction', 'encrypted_content': 'opaque-mock'}]}
        if data.get('stream'):
            events = [{'type': 'response.output_text.delta', 'delta': 'Hello'},
                      {'type': 'response.completed', 'response': response('Hello')}]
            return events[:1] if self.break_case == 'stream_incomplete' else events
        if data.get('tool_choice') == {'type': 'function', 'name': 'read_challenge'}:
            return {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'read_challenge',
                    'call_id': 'mock-call', 'arguments': '{}'}]}
        if any(i.get('type') == 'function_call_output' for i in data['input']):
            return response('wrong' if self.break_case == 'tool_ignored' else MARKER)
        if any(i.get('type') == 'compaction' for i in data['input']):
            if self.break_case == 'compact_rate_limit':
                raise http_error(429)
            return response('wrong' if self.break_case == 'compact_lost' else MARKER)
        if len(data['input']) > 1:
            return response('wrong' if self.break_case == 'history_lost' else MARKER)
        return response('OK')


@pytest.mark.asyncio
async def test_full_roundtrips_and_no_marker_leak():
    request = MockRequests()
    results = await run_checks(ROUTE, request, marker_factory=lambda: MARKER)
    assert all(value['status'] == 'pass' for value in results.values())
    assert len(request.calls) == 7
    tool_calls = [data for _, data in request.calls if data.get('tools')]
    assert MARKER not in json.dumps(tool_calls[0])
    assert MARKER in json.dumps(tool_calls[1])
    assert any(i.get('type') == 'function_call_output' for i in tool_calls[1]['input'])
    compact_followup = request.calls[-1][1]
    assert MARKER not in json.dumps(compact_followup)
    assert results['compaction']['continuation_verified']
    assert all(data['max_output_tokens'] == 512 for ep, data in request.calls if ep == '/responses')


@pytest.mark.asyncio
@pytest.mark.parametrize('case,name,reason', [
    ('stream_incomplete', 'streaming', 'missing_completed_event'),
    ('tool_ignored', 'tool_roundtrip', 'tool_result_not_used'),
    ('history_lost', 'visible_history', 'history_marker_mismatch'),
    ('compact_lost', 'compaction', 'compacted_marker_mismatch'),
])
async def test_semantic_failures(case, name, reason):
    result = await run_checks(ROUTE, MockRequests(case), marker_factory=lambda: MARKER)
    assert result[name]['status'] == 'fail'
    assert result[name]['reason'] == reason
    assert sum(v['status'] == 'pass' for v in result.values()) == 3
    if name == 'compaction':
        assert result[name]['endpoint_available']
        assert result[name]['continuation_verified'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('case,status,reason', [
    ('compact_unsupported', 'unsupported', 'endpoint_not_supported'),
    ('compact_rate_limit', 'error', 'rate_limited'),
])
async def test_http_categories(case, status, reason):
    result = await run_checks(ROUTE, MockRequests(case), marker_factory=lambda: MARKER)
    assert result['compaction']['status'] == status
    assert result['compaction']['reason'] == reason


@pytest.mark.asyncio
async def test_native_model_refused_before_network():
    with pytest.raises(ValueError, match='custom model'):
        await check({'routes': {}, 'native_models': ['native']}, 'native')


def test_sse_multiline_and_invalid():
    assert parse_sse('event: response.completed\r\ndata: {"type":\r\ndata: "response.completed"}\r\n\r\ndata: [DONE]\r\n\r\n') == [{'type': 'response.completed'}]
    with pytest.raises(ProbeError):
        parse_sse('data: nope\n\n')


def test_cli_private_report_and_failure_code(tmp_path, monkeypatch, capsys):
    async def fake_check(*args):
        return {'passed': False, 'checks': {'streaming': {'status': 'fail'}}}
    monkeypatch.setattr('codex_model_router.check.check', fake_check)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    report = tmp_path / 'report.json'
    assert main(['--config', str(config), '--model', 'example/test', '--json-report', str(report)]) == 1
    assert report.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)['passed'] is False
    assert main(['--config', str(config), '--model', 'example/test', '--json-report', str(report)]) == 2

class FakeContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        yield self.body


class FakeResponse:
    def __init__(self, body, status=200, content_type='application/json'):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs = None

    def post(self, url, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.asyncio
async def test_transport_200_json_is_not_stream(monkeypatch):
    from codex_model_router.check import Transport
    monkeypatch.setenv('TEST_KEY', 'dummy-key')
    session = FakeSession(FakeResponse(b'{"status":"completed"}'))
    route = {'base_url': 'https://example.invalid/v1', 'credential': {'kind': 'env', 'key': 'TEST_KEY'}}
    with pytest.raises(ProbeError) as caught:
        await Transport(session, route)('/responses', {'stream': True})
    assert caught.value.result['reason'] == 'expected_event_stream'
    assert session.kwargs['allow_redirects'] is False
    assert set(session.kwargs['headers']) == {'Authorization'}


@pytest.mark.asyncio
async def test_transport_network_failure_is_sanitized(monkeypatch):
    import aiohttp
    from codex_model_router.check import Transport
    monkeypatch.setenv('TEST_KEY', 'dummy-key')
    session = FakeSession(aiohttp.ClientError('private-url-or-credential'))
    route = {'base_url': 'https://example.invalid/v1', 'credential': {'kind': 'env', 'key': 'TEST_KEY'}}
    with pytest.raises(ProbeError) as caught:
        await Transport(session, route)('/responses', {})
    assert caught.value.result == {'status': 'error', 'reason': 'network_or_timeout'}


@pytest.mark.asyncio
async def test_transport_429_does_not_log_body(monkeypatch):
    from codex_model_router.check import Transport
    monkeypatch.setenv('TEST_KEY', 'dummy-key')
    session = FakeSession(FakeResponse(b'private-body', status=429))
    route = {'base_url': 'https://example.invalid/v1', 'credential': {'kind': 'env', 'key': 'TEST_KEY'}}
    with pytest.raises(ProbeError) as caught:
        await Transport(session, route)('/responses', {})
    assert caught.value.result == {'status': 'error', 'reason': 'rate_limited', 'http_status': 429}
