"""Building Watch v2: independent timer and delivery workers; imports never start a service."""
import argparse
import getpass
import json
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from buildingwatch.calendar_utils import instant
from buildingwatch.service import Engine,Scheduler,JsonlInput,WorkerLock
from buildingwatch.delivery import Delivery,Whapi,attach_media
from buildingwatch.store import database_busy


def reorder_window(value):
    number = int(value)
    if not 0 <= number <= 60:
        raise argparse.ArgumentTypeError('Reorder window must be 0–60 seconds')
    return number


def deliver_loop(worker, stop, clock=time.time):
    while not stop.is_set():
        try:
            worker.run_once(clock())
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise
            print('Delivery database is locked; retrying the next tick.', file=sys.stderr, flush=True)
        stop.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument('--config',default=str(root / '.local/buildings.json'))
    parser.add_argument('--db',default=str(root / '.local/watch.sqlite3'))
    sub = parser.add_subparsers(dest='command',required=True)
    replay = sub.add_parser('replay')
    replay.add_argument('events')
    replay.add_argument('--until',required=True,help='Simulated end time with UTC offset')
    serve = sub.add_parser('serve',help='Autonomous timers and camera spool input')
    serve.add_argument('--input',required=True)
    serve.add_argument('--reorder-seconds',type=reorder_window,default=5,
                       help='Camera reorder window in seconds (0–60; default 5)')
    for name in ('outbox','media-jobs','audit'):
        sub.add_parser(name)
    deliver = sub.add_parser('deliver',help='Send queued messages and poll confirmations')
    deliver.add_argument('--credentials',required=True)
    approve = sub.add_parser('approve-visit')
    approve.add_argument('--operators',required=True,help='Private operator hash/grant file')
    approve.add_argument('--building',required=True)
    approve.add_argument('--start',required=True)
    approve.add_argument('--end',required=True,help='Exclusive end time; grace is separately configured')
    media = sub.add_parser('attach-media',help='Trusted local recorder/operator command')
    media.add_argument('--job',required=True,type=int)
    media.add_argument('--file',required=True)
    media.add_argument('--media-root',required=True)
    args = parser.parse_args()
    if args.command not in ('serve', 'replay') and not Path(args.db).is_file():
        parser.error('Monitoring database does not exist. Start the service with ./watch start or use an existing --db path.')
    raw = json.loads(Path(args.config).read_text())
    if not isinstance(raw,dict) or set(raw) != {'buildings'}:
        parser.error('Configuration must contain only a buildings list')
    operators = json.loads(Path(args.operators).read_text()) if args.command == 'approve-visit' else None
    engine = Engine(raw['buildings'],args.db,operators,
                    reorder_seconds=getattr(args, 'reorder_seconds', 5))
    stop = threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:stop.set())
    try:
        if args.command == 'replay':
            until = instant(args.until).timestamp()
            with open(args.events) as stream:
                for line in stream:
                    if stop.is_set():
                        return 130
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        engine.intake.quarantine_line(until,'malformed_json')
                    else:
                        engine.ingest(event,until,replay=True)
            if stop.is_set():
                return 130
            engine.advance(until,flush=True)
            print(json.dumps(engine.alerts(),indent=2))
        elif args.command == 'serve':
            with WorkerLock(args.db+'.scheduler.lock'):
                Scheduler(engine).run(stop,JsonlInput(engine,args.input))
        elif args.command == 'deliver':
            with WorkerLock(args.db+'.delivery.lock'):
                worker = Delivery(engine.store,Whapi(args.credentials))
                deliver_loop(worker, stop)
        elif args.command == 'approve-visit':
            token = getpass.getpass('Operator access token: ')
            print(json.dumps(engine.visits.approve(token,args.building,args.start,args.end,time.time())))
        elif args.command == 'attach-media':
            attach_media(engine.store,args.job,args.file,args.media_root,time.time())
            print('Media attached and queued for this building.')
        elif args.command == 'media-jobs':
            print(json.dumps(engine.media_jobs(),indent=2))
        elif args.command == 'audit':
            print(json.dumps([dict(row) for row in engine.db.execute('SELECT * FROM bw_audit ORDER BY id')],indent=2))
        else:
            print(json.dumps(engine.alerts(),indent=2))
    finally:
        engine.close()


if __name__ == '__main__':
    raise SystemExit(main())
