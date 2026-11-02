"""Publish status, without secrets or footage, to the owner's private dashboard."""
import argparse
import http.client
import json
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from buildingwatch.delivery import NoRedirect
from buildingwatch.service import WorkerLock
from buildingwatch.status import snapshot


def publish(payload, connection):
    url = connection['url'].rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Dashboard connection must use its exact HTTPS origin')
    token = connection.get('token')
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Dashboard access has not been connected')
    request = urllib.request.Request(url + '/api/status', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'OAI-Sites-Authorization': 'Bearer ' + token, 'User-Agent': 'BuildingWatch/2'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            result = json.load(response)
            if not isinstance(result,dict) or result.get('ok') is not True:
                raise ValueError('Dashboard did not confirm the update')
    except urllib.error.HTTPError as error:
        raise ValueError('Dashboard update was rejected (HTTP {}). Check the private connection.'.format(error.code)) from None
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException, RecursionError):
        raise ValueError('Dashboard could not be reached. Its display will mark old status as stale.') from None


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=root / '.local/buildings.json')
    parser.add_argument('--db', type=Path, default=root / '.local/watch.sqlite3')
    parser.add_argument('--connection', type=Path, default=root / '.local/dashboard.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not args.connection.exists():
        parser.error('The private dashboard connection has not been configured on this Mac')
    connection = json.loads(args.connection.read_text())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with WorkerLock(str(args.connection.parent / 'dashboard-publisher.lock')):
            while not stop.is_set():
                try:
                    config = args.config if args.config.exists() else root / 'buildings.pilot.example.json'
                    publish(snapshot(config, args.db), connection)
                    print('Building Watch status updated on your private dashboard.', flush=True)
                except (ValueError, OSError) as error:
                    print(str(error), flush=True)
                    if args.once:
                        return 1
                except sqlite3.Error:
                    print('Monitoring records are temporarily unavailable; the dashboard will mark old status as stale.', flush=True)
                    if args.once:
                        return 1
                if args.once:
                    break
                stop.wait(15)
    except RuntimeError as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
