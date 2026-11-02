"""Regression coverage for malformed inputs and durable spool catch-up."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildingwatch.service import Engine, JsonlInput, Scheduler
from buildingwatch.delivery import Delivery, Whapi
from test_building_watch import config, T, timestamp


def event(identifier, kind='heartbeat', at=T, building='a'):
    return dict(id=str(identifier), building=building, type=kind, at=timestamp(at))


class ServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = str(self.root / 'watch.sqlite3')
        self.spool = self.root / 'events.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def write_burst(self, count=8001):
        with self.spool.open('w') as stream:
            for number in range(count):
                stream.write(json.dumps(event(number, 'entry' if number == count-1 else 'heartbeat'))+'\n')

    def test_scheduler_preserves_arrival_at_end_of_large_burst(self):
        self.write_burst()
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)

        class Stop:
            tick = 0
            def is_set(self): return self.tick >= 12
            def wait(self, interval): self.tick += 1

        stop = Stop()
        Scheduler(engine, clock=lambda: T+stop.tick).run(stop, JsonlInput(engine, self.spool))
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='processed'").fetchone()[0], 8001)
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='quarantined'").fetchone()[0], 0)
        arrivals = [row for row in engine.alerts() if row['type'] == 'first_arrival']
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0]['created_at'], timestamp(T))
        self.assertGreater(engine.store.state('a')['last_timer'], T)
        # Catch-up must not disable the policy for genuinely late new input.
        self.assertEqual(engine.ingest(event('late', at=T-30), T+12), 'quarantined')

    def test_backlog_horizon_survives_restart(self):
        self.write_burst()
        engine = Engine([config()], self.db)
        try:
            self.assertIsNone(JsonlInput(engine, self.spool).read(T))
        finally:
            engine.close()
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        source = JsonlInput(engine, self.spool)
        for tick in range(8):
            horizon = source.read(T+20+tick)
            if tick < 7:
                self.assertIsNone(horizon)
            else:
                self.assertEqual(horizon, T)
                engine.advance(horizon)
        engine.advance(source.read(T+30))
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='processed'").fetchone()[0], 8001)
        self.assertEqual(len([row for row in engine.alerts() if row['type'] == 'first_arrival']), 1)

    def test_appends_do_not_extend_the_current_batch(self):
        self.write_burst(1001)
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        source = JsonlInput(engine, self.spool)
        self.assertIsNone(source.read(T))
        with self.spool.open('a') as stream:
            stream.write(json.dumps(event('appended', at=T+1))+'\n')
        self.assertEqual(source.read(T+10), T)
        self.assertIsNone(engine.db.execute("SELECT 1 FROM bw_events WHERE id='appended'").fetchone())
        horizon = source.read(T+11)
        self.assertEqual(horizon, T+11)
        engine.advance(horizon)
        self.assertEqual(engine.db.execute("SELECT status FROM bw_events WHERE id='appended'").fetchone()[0], 'processed')

    def test_removed_building_is_quarantined_without_blocking_other_buildings(self):
        engine = Engine([config(), config('b')], self.db)
        try:
            engine.ingest(event('removed'), T)
            engine.ingest(event('remaining', 'entry', building='b'), T)
        finally:
            engine.close()
        engine = Engine([config('b')], self.db)
        self.addCleanup(engine.close)
        engine.advance(T+10)
        self.assertEqual(engine.db.execute("SELECT status FROM bw_events WHERE id='removed'").fetchone()[0], 'quarantined')
        audit = engine.db.execute("SELECT building,detail FROM bw_audit WHERE kind='building_removed'").fetchone()
        self.assertEqual(audit['building'], 'a')
        self.assertEqual(json.loads(audit['detail'])['event_id'], 'removed')
        self.assertEqual(engine.db.execute("SELECT status FROM bw_events WHERE id='remaining'").fetchone()[0], 'processed')
        engine.advance(T+11)
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_audit WHERE kind='building_removed'").fetchone()[0], 1)

    def test_overflowing_dates_do_not_block_the_following_spool_event(self):
        bad = [dict(event('bad-low'), at='0001-01-01T00:00:00+01:00'),
               dict(event('bad-high'), at='9999-12-31T23:59:59-01:00')]
        self.spool.write_text(''.join(json.dumps(row)+'\n' for row in bad+[event('valid', 'entry')]))
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        source = JsonlInput(engine, self.spool)
        engine.advance(source.read(T))
        engine.advance(source.read(T+10))
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_audit WHERE kind='quarantine_invalid'").fetchone()[0], 2)
        self.assertEqual(engine.db.execute("SELECT status FROM bw_events WHERE id='valid'").fetchone()[0], 'processed')
        self.assertEqual(len([row for row in engine.alerts() if row['type'] == 'first_arrival']), 1)

    def test_old_backlog_is_processed_as_a_whole_after_restart(self):
        self.write_burst(1501)
        engine = Engine([config()], self.db)
        engine.advance(T-10, flush=True)
        engine.close()
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        source = JsonlInput(engine, self.spool)
        self.assertIsNone(source.read(T+60))
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='processed'").fetchone()[0], 0)
        engine.close()
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        source = JsonlInput(engine, self.spool)
        engine.advance(source.read(T+61))
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='quarantined'").fetchone()[0], 0)
        self.assertEqual(len([row for row in engine.alerts() if row['type'] == 'first_arrival']), 1)

    def test_year_one_and_old_pending_invalid_dates_are_quarantined(self):
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        invalid = dict(event('bad'), at='0001-01-01T00:00:00+00:00')
        self.assertEqual(engine.ingest(invalid, T), 'invalid')
        # A previous version could have persisted this row already.
        engine.db.execute('INSERT INTO bw_events VALUES(?,?,?,?,?,?)',
                          ('a', 'old-bad', -62135596800, 'old', json.dumps(invalid), 'pending'))
        engine.ingest(event('valid', 'entry'), T)
        engine.advance(T+10)
        self.assertEqual(engine.db.execute("SELECT status FROM bw_events WHERE id='old-bad'").fetchone()[0], 'quarantined')
        self.assertEqual(len([row for row in engine.alerts() if row['type'] == 'first_arrival']), 1)

    def test_unreadable_video_does_not_stop_later_alerts(self):
        cred = self.root / 'whapi.json'
        cred.write_text(json.dumps({'token':'test-token'}))
        clip = self.root / 'clip.mp4'
        clip.write_bytes(b'test')
        engine = Engine([config()], self.db)
        self.addCleanup(engine.close)
        engine.store.enqueue('a', 'video', dict(destination='12345678901@g.us', media_path=str(clip), text='video'), T)
        engine.store.enqueue('a', 'text', dict(destination='12345678901@g.us', text='text'), T)
        provider = Whapi(cred)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('unreadable')), \
             patch.object(provider, 'request', return_value={'sent':True,'message':{'id':'sent-id'}}):
            Delivery(engine.store, provider).run_once(T)
        states = {row['key']:row['status'] for row in engine.db.execute('SELECT key,status FROM bw_queue')}
        self.assertEqual(states, {'video':'pending', 'text':'accepted'})


if __name__ == '__main__':
    unittest.main()
