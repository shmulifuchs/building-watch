import fcntl
import json
import threading
import time
from pathlib import Path
from .config import validate
from .store import Store
from .intake import Intake
from .rules import Rules
from .visits import Visits


class Engine:
    def __init__(self, configs, db_path=':memory:', operators=None, reorder_seconds=5):
        self.buildings = validate(configs)
        if reorder_seconds < 0 or reorder_seconds > 60:
            raise ValueError('Reorder window must be 0–60 seconds')
        self.store = Store(db_path)
        self.db = self.store.db
        self.visits = Visits(self.store,self.buildings,operators)
        self.rules = Rules(self.store,self.visits)
        self.intake = Intake(self.store,self.buildings,self.rules)
        self.reorder_seconds = reorder_seconds

    def ingest(self, event, received_at, replay=False):
        return self.intake.ingest(event,received_at,replay)

    def advance(self, now, flush=False):
        cutoff = now if flush else now-self.reorder_seconds
        self.intake.drain(cutoff)
        for b in self.buildings.values():
            with self.store.transaction():
                s = self.store.state(b.id)
                if cutoff < s.get('watermark',float('-inf')):
                    continue
                self.rules.advance(b,s,cutoff)
                s['watermark'] = cutoff
                self.store.save(b.id,s)

    def alerts(self):
        return [dict(json.loads(row['body']),id=row['id'],delivery_status=row['status'],
                     attempts=row['attempts'],provider_id=row['provider_id'],error=row['error'])
                for row in self.db.execute('SELECT * FROM bw_queue ORDER BY id')]

    def media_jobs(self):
        return [dict(json.loads(row['body']),id=row['id'],status=row['status'])
                for row in self.db.execute('SELECT * FROM bw_media ORDER BY id')]

    def close(self):
        self.db.close()


class Scheduler:
    def __init__(self, engine, clock=time.time, interval=1):
        self.engine,self.clock,self.interval = engine,clock,interval
    def run_once(self):
        self.engine.advance(self.clock())
    def run(self, stop, input_source=None):
        while not stop.is_set():
            if input_source:
                # Do not move timers past events still waiting in the spool.
                self.engine.advance(input_source.read(self.clock()))
            else:
                self.run_once()
            stop.wait(self.interval)


class JsonlInput:
    """Append-only local spool; partial trailing lines wait for completion."""
    def __init__(self, engine, path):
        self.engine,self.path = engine,Path(path)
        self.source = str(self.path.resolve())
    def read(self, now):
        if not self.path.exists():
            return now
        row = self.engine.db.execute('SELECT position FROM bw_offsets WHERE source=?',(self.source,)).fetchone()
        position = row['position'] if row else 0
        size = self.path.stat().st_size
        batch_key = 'spool-batch:'+self.source
        row = self.engine.db.execute('SELECT value FROM bw_meta WHERE key=?',(batch_key,)).fetchone()
        batch = json.loads(row['value']) if row else None
        if size < position or (batch and size < batch['end']):
            self.engine.intake.quarantine_line(now,'input_file_truncated; resetting spool offset')
            position = 0
            batch = None
            self.engine.db.execute('DELETE FROM bw_meta WHERE key=?',('spool-skip:'+self.source,))
        if batch is None:
            # Capture a finite prefix, so a growing spool cannot hold timers
            # forever. Persist its horizon across batches and worker restarts.
            batch = dict(end=size,at=now)
            self.engine.db.execute('INSERT OR REPLACE INTO bw_meta VALUES(?,?)',(batch_key,json.dumps(batch)))
        skip_key = 'spool-skip:'+self.source
        skipping = self.engine.db.execute('SELECT 1 FROM bw_meta WHERE key=?',(skip_key,)).fetchone() is not None
        bytes_read = 0
        partial = False
        with self.path.open('rb') as stream:
            stream.seek(position)
            for _ in range(1000):
                remaining = batch['end'] - stream.tell()
                if remaining <= 0:
                    break
                line = stream.readline(min(1_048_577,remaining))
                if not line:
                    break
                bytes_read += len(line)
                oversized = len(line) > 1_048_576
                if oversized and not skipping:
                    self.engine.intake.quarantine_line(now,'oversized_line')
                    skipping = True
                    self.engine.db.execute('INSERT OR REPLACE INTO bw_meta VALUES(?,?)',(skip_key,'1'))
                if skipping:
                    if line.endswith(b'\n'):
                        skipping = False
                        self.engine.db.execute('DELETE FROM bw_meta WHERE key=?',(skip_key,))
                elif not line.endswith(b'\n'):
                    partial = True
                    break
                else:
                    try:
                        event = json.loads(line)
                    except (ValueError,UnicodeError):
                        self.engine.intake.quarantine_line(now,'malformed_json')
                    else:
                        self.engine.ingest(event,now)
                position = stream.tell()
                self.engine.db.execute('INSERT OR REPLACE INTO bw_offsets VALUES(?,?)',(self.source,position))
                if bytes_read >= 4_194_304:
                    break
        if position >= batch['end'] or partial:
            self.engine.db.execute('DELETE FROM bw_meta WHERE key=?',(batch_key,))
        return batch['at']


class WorkerLock:
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.file = open(self.path,'a')
        try:
            fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError('Another worker owns this role for this database') from None
        return self
    def __exit__(self,*args):
        self.file.close()
