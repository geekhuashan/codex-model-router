"""Explicit, potentially billable Responses probes; reports contain metadata only."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

import aiohttp

from .router import read_secret

LIMIT = 2 * 1024 * 1024


class ProbeError(Exception):
    def __init__(self, status, reason, http_status=None):
        self.result = {'status': status, 'reason': reason}
        if http_status is not None:
            self.result['http_status'] = http_status


def http_error(status):
    if status in (404, 405, 501):
        return ProbeError('unsupported', 'endpoint_not_supported', status)
    return ProbeError('error', 'rate_limited' if status == 429 else 'http_error', status)


def parse_sse(body):
    events = []
    for block in body.replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
        if data and data != '[DONE]':
            try:
                value = json.loads(data)
            except ValueError:
                raise ProbeError('fail', 'malformed_stream') from None
            if not isinstance(value, dict):
                raise ProbeError('fail', 'malformed_stream')
            events.append(value)
    return events


class Transport:
    def __init__(self, session, route):
        self.session, self.route = session, route

    async def __call__(self, endpoint, payload):
        try:
            credential = read_secret(self.route['credential'])
            async with self.session.post(
                self.route['base_url'].rstrip('/') + endpoint,
                json=payload, headers={'Authorization': 'Bearer ' + credential},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise http_error(response.status)
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > LIMIT:
                        raise ProbeError('error', 'response_too_large')
                    chunks.append(chunk)
                body = b''.join(chunks).decode('utf-8')
                if payload.get('stream'):
                    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                        raise ProbeError('fail', 'expected_event_stream')
                    return parse_sse(body)
                return json.loads(body)
        except ProbeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            raise ProbeError('error', 'network_or_timeout') from None
        except (ValueError, KeyError, OSError, UnicodeError):
            raise ProbeError('error', 'credential_or_response_invalid') from None


def require(condition, reason):
    if not condition:
        raise ProbeError('fail', reason)


def output(response):
    require(isinstance(response, dict) and response.get('status') == 'completed', 'response_not_completed')
    items = response.get('output')
    require(isinstance(items, list), 'missing_output')
    return items


def text_output(response):
    return ''.join(part.get('text', '') for item in output(response)
                   if item.get('type') == 'message'
                   for part in item.get('content', []) if part.get('type') == 'output_text')


def user(text):
    return {'role': 'user', 'content': text}


async def run_checks(route, request, *, marker_factory=lambda: secrets.token_hex(12), only=None):
    """Run bounded independent checks. request(endpoint, payload) can be mocked."""
    model = route['model']
    def payload(items, **extra):
        return {'model': model, 'input': items, 'max_output_tokens': 512,
                'store': False, **extra}

    async def streaming():
        events = await request('/responses', payload([user('Reply with one short greeting.')], stream=True))
        require(any(e.get('type') == 'response.output_text.delta' and e.get('delta') for e in events), 'missing_text_delta')
        completed = [e for e in events if e.get('type') == 'response.completed']
        require(bool(completed), 'missing_completed_event')
        require(bool(text_output(completed[-1].get('response'))), 'missing_completed_text')
        require(not any(e.get('type') in ('error', 'response.failed', 'response.incomplete') for e in events), 'stream_failure_event')

    async def history():
        marker = marker_factory()
        first_input = [user('Remember this memory marker: ' + marker + '. Reply OK.')]
        first = await request('/responses', payload(first_input))
        require(bool(text_output(first)), 'missing_first_turn_text')
        second = await request('/responses', payload(first_input + output(first) + [user('Return only the memory marker from earlier.')]))
        require(text_output(second).strip() == marker, 'history_marker_mismatch')

    async def tools():
        tool = {'type': 'function', 'name': 'read_challenge', 'description': 'Read a test challenge.',
                'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, 'strict': True}
        initial = [user('Call read_challenge exactly once. Then return only the challenge value from the tool result.')]
        first = await request('/responses', payload(initial, tools=[tool], tool_choice='auto'))
        calls = [item for item in output(first) if item.get('type') == 'function_call']
        require(len(calls) == 1 and calls[0].get('name') == 'read_challenge' and bool(calls[0].get('call_id')), 'missing_expected_tool_call')
        try:
            arguments = json.loads(calls[0].get('arguments', ''))
        except (ValueError, TypeError):
            raise ProbeError('fail', 'invalid_tool_arguments') from None
        require(arguments == {}, 'unexpected_tool_arguments')
        marker = marker_factory()  # Only tool output knows this value; no generated code is run.
        second = await request('/responses', payload(initial + output(first) + [
            {'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': json.dumps({'challenge': marker})}],
            tools=[tool], tool_choice='none'))
        require(text_output(second).strip() == marker, 'tool_result_not_used')

    async def compact():
        marker = marker_factory()
        result = await request('/responses/compact', {'model': model, 'input': [
            user('Remember this memory marker for later: ' + marker),
            {'role': 'assistant', 'content': 'I will remember the memory marker.'}]})
        items = result.get('output') if isinstance(result, dict) else None
        require(isinstance(items, list) and any(i.get('type') == 'compaction' and isinstance(i.get('encrypted_content'), str) and i['encrypted_content'] for i in items), 'missing_compaction_item')
        # Replay only opaque compaction items; plaintext replay would give a false pass.
        compact_items = [i for i in items if i.get('type') == 'compaction']
        try:
            second = await request('/responses', payload(compact_items + [user('Return only the memory marker from earlier.')]))
            require(text_output(second).strip() == marker, 'compacted_marker_mismatch')
        except ProbeError as exc:
            exc.result['endpoint_available'] = True
            exc.result['continuation_verified'] = False
            raise
        return {'endpoint_available': True, 'continuation_verified': True}

    results = {}
    for name, probe in [('streaming', streaming), ('visible_history', history), ('tool_roundtrip', tools), ('compaction', compact)]:
        if only and name not in only:
            continue
        try:
            results[name] = {'status': 'pass', **(await probe() or {})}
        except ProbeError as exc:
            results[name] = exc.result
        except (TypeError, KeyError, AttributeError):
            results[name] = {'status': 'fail', 'reason': 'malformed_response'}
    return results


async def check(config, alias, timeout=60, only=None):
    route = config.get('routes', {}).get(alias)
    if not route:
        raise ValueError('Only an explicitly configured custom model can be checked')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                    trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        results = await run_checks(route, Transport(session, route), only=only)
    return {'time': datetime.now(timezone.utc).isoformat(), 'model': alias,
            'source': route['source'], 'checks': results,
            'passed': all(item['status'] == 'pass' for item in results.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit Responses capability probes (makes billable API requests).')
    parser.add_argument('--config', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--only', action='append', choices=['streaming', 'visible_history', 'tool_roundtrip', 'compaction'], help='Run only selected checks; repeat to select multiple')
    parser.add_argument('--timeout', type=float, default=60, help='Per-request timeout in seconds (1-120)')
    parser.add_argument('--json-report', help='Write metadata-only JSON report to this new file')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error('--timeout must be between 1 and 120')
    os.umask(0o077)
    try:
        config = json.loads(Path(args.config).expanduser().read_text())
        report = asyncio.run(check(config, args.model, args.timeout, args.only))
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.json_report:
            with Path(args.json_report).expanduser().open('x') as handle:
                handle.write(rendered + '\n')
        print(rendered)
        return 0 if report['passed'] else 1
    except (ValueError, OSError, KeyError):
        print(json.dumps({'status': 'error', 'reason': 'invalid_config_or_report_path'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
