"""Metadata-only response observations; never retain response text or headers."""
from collections import deque
from datetime import datetime, timezone
import json
import time
import uuid


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Observation:
    def __init__(self, source, model, upstream_host, operation):
        self.started = time.monotonic()
        self.event = dict(request_id=uuid.uuid4().hex[:16], time=timestamp(),
                          source=source, model=model, upstream_host=upstream_host,
                          operation=operation, status=None, outcome='in_progress', usage={})
        self.buffer = b''
        self.terminal = None
        self.overflow = False

    def observe(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get('type')
        body = value.get('response', value)
        if not isinstance(body, dict):
            return
        state = body.get('status')
        if kind == 'response.completed' or state == 'completed':
            self.terminal = 'completed'
        elif kind in ('response.failed', 'error') or state == 'failed':
            self.terminal = 'failed'
        elif kind == 'response.incomplete' or state == 'incomplete':
            self.terminal = 'incomplete'
        if body.get('object') == 'response.compaction' and isinstance(body.get('output'), list):
            if any(isinstance(i, dict) and i.get('type') == 'compaction' for i in body['output']):
                self.terminal = 'compacted'
        usage = body.get('usage')
        if isinstance(usage, dict):
            for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                v = usage.get(key)
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                    self.event['usage'][key] = v
            details = usage.get('input_tokens_details', {})
            if isinstance(details, dict):
                v = details.get('cached_tokens')
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                    self.event['usage']['cached_tokens'] = v

    def feed(self, chunk, is_sse):
        if self.overflow:
            return
        self.buffer += chunk
        if is_sse:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if line.startswith(b'data:'):
                    self.parse(line[5:])
        if len(self.buffer) > 32 * 1024 * 1024:
            self.buffer = b''
            self.overflow = True

    def parse(self, raw):
        try:
            self.observe(json.loads(raw))
        except (ValueError, UnicodeError):
            pass

    def finish(self, status, is_sse=False, error=None):
        if not is_sse:
            self.parse(self.buffer)
        elif self.buffer.startswith(b'data:'):
            self.parse(self.buffer[5:])
        outcome = error or (f'http_error' if status is not None and status >= 400 else self.terminal)
        self.event.update(status=status, outcome=outcome or 'unconfirmed',
                          seconds=round(time.monotonic() - self.started, 2), finished_at=timestamp())
        self.buffer = b''
        return self.event


class Monitor:
    def __init__(self):
        self.active = {}
        self.recent = deque(maxlen=100)
        self.totals = {}

    def begin(self, observation):
        self.active[observation.event['request_id']] = observation.event

    def finish(self, event):
        self.active.pop(event['request_id'], None)
        self.recent.appendleft(event)
        totals = self.totals.setdefault(event['source'], {'requests': 0, 'completed': 0, 'usage': {}})
        totals['requests'] += 1
        totals['completed'] += event['outcome'] == 'completed'
        for key, value in event['usage'].items():
            totals['usage'][key] = totals['usage'].get(key, 0) + value

    def snapshot(self):
        return {'active': list(self.active.values()), 'recent': list(self.recent),
                'totals_since_start': self.totals}
