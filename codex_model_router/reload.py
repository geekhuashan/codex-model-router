"""Read route snapshots between requests without interrupting active streams."""
import asyncio
from contextlib import suppress
import json
from pathlib import Path

from .configure import valid_base_url


class RouteSnapshot:
    def __init__(self, config, config_path=None):
        self.current = config
        self.path = Path(config_path) if config_path else None
        self.stamp = None

    def get(self):
        if self.path is None:
            return self.current
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if stamp == self.stamp:
                return self.current
            candidate = json.loads(self.path.read_text())
            if not isinstance(candidate, dict):
                return self.current
            # These require a process restart; do not partially apply such edits.
            for key in ('local_token', 'port', 'state_db'):
                if candidate.get(key) != self.current.get(key):
                    return self.current
            native = candidate['native_models']
            routes = candidate['routes']
            if (not isinstance(native, list) or not all(isinstance(x, str) and x for x in native)
                    or not isinstance(routes, dict) or set(native).intersection(routes)):
                return self.current
            for alias, route in routes.items():
                if (not isinstance(alias, str) or not alias or not isinstance(route, dict)
                        or not isinstance(route.get('model'), str) or not route['model']
                        or not isinstance(route.get('source'), str) or not route['source']
                        or not isinstance(route.get('credential'), dict)):
                    return self.current
                if not isinstance(route.get('base_url'), str):
                    return self.current
                valid_base_url(route['base_url'])
            for key in ('chatgpt_url', 'openai_url'):
                if not isinstance(candidate.get(key), str):
                    return self.current
                valid_base_url(candidate[key])
            self.current = candidate
            self.stamp = stamp
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Keep the last valid snapshot on partial writes or invalid config.
        return self.current


async def start_refresh(config_path, secret_reader):
    if config_path is None:
        return None
    from .refresh import run_periodic
    return asyncio.create_task(run_periodic(Path(config_path), secret_reader))


async def stop_refresh(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def refresh_status(config_path):
    if config_path is None:
        return None
    try:
        return json.loads((Path(config_path).parent / 'refresh-status.json').read_text())
    except (OSError, ValueError):
        return None
