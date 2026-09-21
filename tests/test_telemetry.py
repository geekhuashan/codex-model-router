import json
from codex_model_router.telemetry import Observation, Monitor


def test_http_200_without_terminal_is_not_completed():
    obs = Observation('provider', 'alias', 'example.com', '/responses')
    obs.feed(b'data: {"type":"response.output_text.delta","delta":"PRIVATE"}\n\n', True)
    event = obs.finish(200, True)
    assert event['outcome'] == 'unconfirmed'
    assert 'PRIVATE' not in json.dumps(event)


def test_split_sse_completion_and_usage_only():
    obs = Observation('provider', 'alias', 'example.com', '/responses')
    payload = b'data: {"type":"response.completed","response":{"output":["PRIVATE"],"usage":{"input_tokens":7,"output_tokens":3,"input_tokens_details":{"cached_tokens":2},"secret":"PRIVATE"}}}\n\n'
    for byte in payload:
        obs.feed(bytes([byte]), True)
    event = obs.finish(200, True)
    assert event['outcome'] == 'completed'
    assert event['usage'] == dict(input_tokens=7, output_tokens=3, cached_tokens=2)
    assert 'PRIVATE' not in json.dumps(event)


def test_failures_and_active_lifecycle():
    monitor = Monitor()
    obs = Observation('provider', 'alias', 'example.com', '/responses')
    monitor.begin(obs)
    assert len(monitor.snapshot()['active']) == 1
    obs.feed(b'data: {"type":"response.failed"}\n\n', True)
    event = obs.finish(200, True)
    monitor.finish(event)
    assert event['outcome'] == 'failed'
    assert monitor.snapshot()['active'] == []
    assert monitor.snapshot()['recent'][0]['request_id'] == event['request_id']
    assert monitor.totals['provider']['completed'] == 0
    assert Observation('a','b','c','d').finish(429)['outcome'] == 'http_error'
    assert Observation('a','b','c','d').finish(200, error='connection_error')['outcome'] == 'connection_error'
