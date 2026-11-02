"""Regression checks for configuration, timestamp and heartbeat contracts."""
import subprocess
import sys
import unittest
from pathlib import Path
from buildingwatch.calendar_utils import instant
from buildingwatch.config import Building
from buildingwatch.service import Engine
from test_building_watch import config, T, timestamp


class ReviewFindingsTests(unittest.TestCase):
    def test_date_configuration_requires_canonical_keys(self):
        for key, value in [('date_overrides', {'20261224': []}),
                           ('custom_holidays', {'20261224': 'Holiday'}),
                           ('removed_holidays', ['20261224'])]:
            c = config(); c[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'YYYY-MM-DD'):
                Building(c)
        for invalid in ('2026-02-30', '2026-2-03', '2026-W52-4'):
            c = config(); c['custom_holidays'] = {invalid:'Holiday'}
            with self.subTest(value=invalid), self.assertRaises(ValueError): Building(c)
        c = config(); c['date_overrides'] = {'2026-12-24': []}
        self.assertEqual(Building(c).hours(instant('2026-12-24T12:00:00Z').date()), [])

    def test_fractional_seconds_work_on_python_39_and_later(self):
        for fraction, micros in [('1',100000), ('12',120000), ('123',123000),
                                 ('1234',123400), ('12345',123450),
                                 ('123456',123456), ('123456789',123456)]:
            with self.subTest(fraction=fraction):
                value = instant('2026-11-02T09:00:01.'+fraction+'-05:00')
                self.assertEqual(value.microsecond, micros)
                self.assertEqual(value.hour, 14)
        engine = Engine([config()]); self.addCleanup(engine.close)
        for index, fraction in enumerate(('5','1234')):
            value = dict(id=str(index),building='a',type='heartbeat',at='2026-11-02T09:00:01.'+fraction+'-05:00')
            self.assertEqual(engine.ingest(value,T+2), 'buffered')
        engine.advance(T+10)
        self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM bw_events WHERE status='processed'").fetchone()[0], 2)

    def test_heartbeats_at_half_threshold_tolerate_jitter(self):
        engine = Engine([config()]); self.addCleanup(engine.close)
        def feed(kind, at, identifier):
            engine.ingest(dict(id=str(identifier),building='a',type=kind,at=timestamp(T+at)),T+at)
            engine.advance(T+at,flush=True)
        feed('heartbeat',0,'h0'); feed('exit',1,'exit')
        for n in range(1,31): feed('heartbeat', n*30+(3 if n%2 else 0), 'h'+str(n))
        engine.advance(T+902,flush=True)
        alerts = [row['type'] for row in engine.alerts()]
        self.assertIn('possible_last_departure', alerts)
        self.assertNotIn('departure_unverified', alerts)

    def test_missing_live_database_suggests_start(self):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copy(Path(__file__).with_name('watch'), root/'watch')
            shutil.copytree(Path(__file__).with_name('buildingwatch'),root/'buildingwatch',ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run([sys.executable,str(root/'watch'),'status'],capture_output=True,text=True,check=True)
        self.assertIn('./watch start', result.stdout)


if __name__ == '__main__': unittest.main()
