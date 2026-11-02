"""Offline reproductions from the external AI review; no live sends."""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import dashboard_sync
from buildingwatch.delivery import Delivery, ProviderError, Whapi
from buildingwatch.service import Engine
from buildingwatch.status import snapshot
from test_building_watch import config, T, timestamp

ROOT = Path(__file__).resolve().parent


class ExternalReviewTests(unittest.TestCase):
    def test_late_overnight_person_alert_keeps_evidence_and_current_state(self):
        at = T - 7 * 3600  # 2 a.m. local
        engine = Engine([config()]); self.addCleanup(engine.close)
        engine.advance(at + 10)
        before = copy.deepcopy(engine.store.state('a'))
        event = dict(id='delayed-entry', building='a', type='entry', at=timestamp(at))
        self.assertEqual(engine.ingest(event, at + 10), 'quarantined')
        alerts = [row for row in engine.alerts() if row['type'] == 'late_camera_event']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['observed_at'], timestamp(at))
        self.assertEqual(alerts[0]['created_at'], timestamp(at + 10))
        self.assertTrue(alerts[0]['outside_scheduled_hours'])
        self.assertEqual(engine.store.state('a'), before)
        self.assertEqual(len(engine.media_jobs()), 1)
        self.assertEqual(engine.media_jobs()[0]['start'], timestamp(at - 3))
        self.assertTrue(engine.media_jobs()[0]['recording_gap_possible'])
        self.assertEqual(engine.ingest(event, at + 20), 'duplicate')
        self.assertEqual(len(engine.alerts()), 1)
        self.assertEqual(len(engine.media_jobs()), 1)

    def test_late_heartbeat_does_not_mark_camera_recovered(self):
        engine = Engine([config()]); self.addCleanup(engine.close)
        engine.advance(T + 20)
        before = copy.deepcopy(engine.store.state('a'))
        engine.ingest(dict(id='old-hb', building='a', type='heartbeat', at=timestamp(T)), T+20)
        self.assertEqual(engine.store.state('a'), before)
        self.assertEqual(engine.alerts(), [])

    def test_late_observations_cannot_cancel_current_departure_or_close_current_event(self):
        engine = Engine([config()]); self.addCleanup(engine.close)
        engine.ingest(dict(id='flag', building='a', type='suspicious_start', at=timestamp(T)), T)
        engine.advance(T, flush=True)
        engine.ingest(dict(id='exit', building='a', type='exit', at=timestamp(T+1)), T+1)
        engine.advance(T+1, flush=True)
        before = copy.deepcopy(engine.store.state('a'))
        for kind in ('entry','exit','activity','suspicious_start','suspicious_end'):
            engine.ingest(dict(id='old-'+kind, building='a', type=kind, at=timestamp(T-10)), T+1)
        self.assertEqual(engine.store.state('a'), before)
        self.assertEqual(len([a for a in engine.alerts() if a['type']=='late_camera_event']),5)

    def test_monday_catchup_identifies_original_weekend(self):
        from buildingwatch.calendar_utils import instant
        engine = Engine([config()]); self.addCleanup(engine.close)
        engine.advance(instant('2026-11-06T14:00:00-05:00').timestamp(), flush=True)
        engine.advance(instant('2026-11-09T09:00:00-05:00').timestamp(), flush=True)
        reminders = [a for a in engine.alerts() if a['type']=='schedule_checkin']
        self.assertEqual(len(reminders),1)
        self.assertTrue(reminders[0]['catchup'])
        self.assertIn('Missed check-in scheduled for 2026-11-06', reminders[0]['text'])
        self.assertIn('weekend starting 2026-11-07', reminders[0]['text'])
        self.assertNotIn('upcoming weekend', reminders[0]['text'])

    def test_pending_event_overtaken_by_watermark_also_alerts_once(self):
        engine = Engine([config()]); self.addCleanup(engine.close)
        engine.ingest(dict(id='pending', building='a', type='exit', at=timestamp(T)), T)
        engine.store.save('a', {'watermark': T+20})
        engine.advance(T+30)
        engine.advance(T+40)
        self.assertEqual(len([a for a in engine.alerts() if a['type']=='late_camera_event']), 1)
        self.assertEqual(len(engine.media_jobs()), 1)

    def test_emoji_snapshot_fits_javascript_limit_without_splitting_character(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); settings = root/'buildings.json'; db = root/'watch.sqlite3'
            settings.write_text(json.dumps({'buildings': [config()]}))
            engine = Engine([config()], str(db))
            try:
                for key, text in [('emoji', '😀'*1500), ('boundary', 'a'*1999+'😀')]:
                    engine.store.enqueue('a', key, dict(destination=None, type='test', created_at=timestamp(T), text=text), T)
            finally: engine.close()
            texts = [a['text'] for a in snapshot(settings, db, T)['buildings'][0]['alerts']]
            self.assertTrue(all(len(t.encode('utf-16-le'))//2 <= 2000 for t in texts))
            self.assertEqual(texts, ['a'*1999, '😀'*1000])

    def test_missing_configuration_is_not_published_as_example(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); connection = root/'dashboard.json'
            connection.write_text(json.dumps({'url':'https://example.com', 'token':'FAKE-TEST-TOKEN'}))
            args = ['dashboard_sync.py', '--once', '--config', str(root/'missing.json'),
                    '--db', str(root/'watch.sqlite3'), '--connection', str(connection)]
            with patch('sys.argv', args), patch.object(dashboard_sync, 'publish') as publish, \
                 patch.object(dashboard_sync.signal, 'signal'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dashboard_sync.main(), 1)
            publish.assert_not_called()
            self.assertFalse((root/'watch.sqlite3').exists())

    def test_auth_rejection_pauses_queue_and_recovers_after_token_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            credentials = Path(folder)/'whapi.json'
            credentials.write_text(json.dumps({'token':'FAKE-EXPIRED-TOKEN'}))
            provider = Whapi(credentials)
            engine = Engine([config()]); self.addCleanup(engine.close)
            for index in range(2):
                engine.store.enqueue('a', str(index), dict(destination='12345678901@g.us', text='test'), T)
            worker = Delivery(engine.store, provider)
            seen=[]
            class Response:
                def __enter__(self): return self
                def __exit__(self,*_): pass
                def read(self,*_): return b'{"sent":true,"message":{"id":"accepted-id"}}'
            def respond(request, **kwargs):
                seen.append(request.headers['Authorization'])
                if request.headers['Authorization']=='Bearer FAKE-EXPIRED-TOKEN':
                    raise urllib.error.HTTPError(request.full_url,401,'Unauthorized',{},None)
                return Response()
            with patch.object(provider.opener, 'open', side_effect=respond):
                worker.run_once(T)
                rows = list(engine.db.execute('SELECT status,attempts FROM bw_queue ORDER BY id'))
                self.assertEqual([(r['status'],r['attempts']) for r in rows], [('pending',0),('pending',0)])
                self.assertEqual(len(seen),1)
                worker.run_once(T+5)
                self.assertEqual(len(seen),1)
                credentials.write_text(json.dumps({'token':'FAKE-REPLACEMENT-TOKEN'}))
                worker.run_once(T+61)
            self.assertEqual([a['delivery_status'] for a in engine.alerts()],['accepted','accepted'])
            self.assertEqual(seen[-1], 'Bearer FAKE-REPLACEMENT-TOKEN')

    def test_preexisting_auth_failures_recover_but_ambiguous_sends_stay_held(self):
        engine = Engine([config()]); self.addCleanup(engine.close)
        for key in ('auth', 'ambiguous', 'invalid'):
            engine.store.enqueue('a', key, dict(destination='12345678901@g.us', text=key), T)
        engine.db.execute("UPDATE bw_queue SET status='failed',error='HTTP 401',attempts=5 WHERE key='auth'")
        engine.db.execute("UPDATE bw_queue SET status='delivery_unknown',error='Response unavailable or invalid' WHERE key='ambiguous'")
        engine.db.execute("UPDATE bw_queue SET status='failed',error='HTTP 400' WHERE key='invalid'")
        provider = Mock(); provider.send.return_value={'sent':True,'message':{'id':'accepted-id'}}
        Delivery(engine.store,provider).run_once(T)
        self.assertEqual({r['key']:r['status'] for r in engine.db.execute('SELECT key,status FROM bw_queue')},
                         {'auth':'accepted','ambiguous':'delivery_unknown','invalid':'failed'})
        provider.send.assert_called_once()

    def test_readme_pilot_workers_use_same_database_and_configuration(self):
        readme = (ROOT/'README.md').read_text()
        for line in readme.splitlines():
            if line.startswith('python3 building_watch.py') and any(' '+c+' ' in line for c in ('deliver','approve-visit','attach-media')):
                with self.subTest(command=line):
                    self.assertIn('--db .local/watch.sqlite3',line)
                    self.assertIn('--config .local/buildings.json',line)


if __name__ == '__main__': unittest.main()
