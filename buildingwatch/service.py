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
                input_source.read(self.clock())
            self.run_once()
            stop.wait(self.interval)


class JsonlInput:
    """Append-only local spool; partial trailing lines wait for completion."""
    def __init__(self, engine, path):
        self.engine,self.path = engine,Path(path)
        self.source = str(self.path.resolve())
    def read(self, now):
        if not self.path.exists():
            return
        row = self.engine.db.execute('SELECT position FROM bw_offsets WHERE source=?',(self.source,)).fetchone()
        position = row['position'] if row else 0
        if self.path.stat().st_size < position:
            self.engine.intake.quarantine_line(now,'input_file_truncated; resetting spool offset')
            position = 0
        skip_key = 'spool-skip:'+self.source
        skipping = self.engine.db.execute('SELECT 1 FROM bw_meta WHERE key=?',(skip_key,)).fetchone() is not None
        bytes_read = 0
        with self.path.open('rb') as stream:
            stream.seek(position)
            for _ in range(1000):
                line = stream.readline(1_048_577)
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
