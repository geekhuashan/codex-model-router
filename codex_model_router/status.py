"""Read the running local router's metadata without making model requests."""
import argparse
import json
import webbrowser
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--dashboard', action='store_true', help='Open the local read-only dashboard')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.expanduser().read_text())
        url = f"http://127.0.0.1:{int(config['port'])}/{config['local_token']}/health"
        with urlopen(Request(url), timeout=5) as response:
            state = json.load(response)
    except (OSError, ValueError, KeyError, URLError):
        parser.exit(1, 'Router status unavailable; check configuration and running service.\n')
    if args.dashboard:
        webbrowser.open(url.rsplit('/', 1)[0] + '/dashboard')
        print('Opened local request-source dashboard.')
        return 0
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    monitor = state.get('monitor')
    if monitor is None:
        parser.exit(1, 'This router needs the monitoring update.\n')
    print('Source / selected model / upstream host / HTTP / generation / reported tokens')
    for event in monitor['active'] + monitor['recent'][:20]:
        usage = event.get('usage', {})
        tokens = f"in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')} cached={usage.get('cached_tokens', '?')}"
        print(f"{event['time']} | {event['source']} | {event['model']} | {event['upstream_host']} | "
              f"{event['status']} | {event['outcome']} | {tokens}")
    if not monitor['active'] and not monitor['recent']:
        print('No requests observed since this router started.')
    print('Tokens are upstream-reported, not an account balance or a monetary bill.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
