"""Regression checks for the second external review; all providers are fake."""
import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import building_watch
from buildingwatch.calendar_utils import instant
from buildingwatch.delivery import Delivery, ProviderError, Whapi
from buildingwatch.service import Engine, Scheduler
from dashboard_sync import publish
from test_building_watch import config, T, timestamp


class TwoTicks:
    def __init__(self, release=lambda: None): self.ticks=0;self.release=release
    def is_set(self): return self.ticks>=2
    def wait(self, _):
        self.ticks+=1
        if self.ticks==1:self.release()


class SecondReviewTests(unittest.TestCase):
    def engine(self, configs=None, **kwargs):
        value=Engine(configs or [config()], **kwargs);self.addCleanup(value.close);return value

    def test_recent_late_heartbeat_updates_health_without_rewinding_timers(self):
        e=self.engine();e.advance(T+20);before=e.store.state('a')
        e.ingest(dict(id='late-hb',building='a',type='heartbeat',at=timestamp(T+12)),T+20)
        after=e.store.state('a')
        self.assertEqual(after['heartbeat'],T+12)
        self.assertEqual(after['last_timer'],before['last_timer'])
        self.assertEqual(after['watermark'],before['watermark'])
        self.assertTrue(e.rules.healthy(e.buildings['a'],after,T+20))

    def test_late_heartbeat_cannot_override_a_newer_explicit_disconnect(self):
        e=self.engine()
        for identifier,kind,at in [('hb','heartbeat',T),('off','offline',T+2)]:
            e.ingest(dict(id=identifier,building='a',type=kind,at=timestamp(at)),at);e.advance(at,flush=True)
        e.advance(T+20)
        e.ingest(dict(id='older-hb',building='a',type='heartbeat',at=timestamp(T+1)),T+20)
        self.assertTrue(e.store.state('a')['disconnected'])

    def test_ten_minutes_of_eight_second_lag_keeps_health_and_all_recordings(self):
        e=self.engine();start=T+3600
        for n in range(35):
            now=start+n*17;e.advance(now)
            for kind in ('heartbeat','activity'):
                e.ingest(dict(id=kind+str(n),building='a',type=kind,at=timestamp(now-8)),now)
        self.assertEqual(len([a for a in e.alerts() if a['type']=='late_camera_event']),1)
        self.assertEqual(len(e.media_jobs()),35)
        self.assertTrue(e.rules.healthy(e.buildings['a'],e.store.state('a'),now))
        self.assertNotIn('camera_never_connected',[a['type'] for a in e.alerts()])

    def test_off_hours_late_alert_has_independent_persistent_bucket(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'watch.sqlite3');e=Engine([config()],path)
            now=T+3600;e.advance(now)
            for identifier,at in [('working',now-8),('closed',T-7*3600),('closed-again',T-7*3600+1)]:
                e.ingest(dict(id=identifier,building='a',type='entry',at=timestamp(at)),now)
            self.assertEqual([a['outside_scheduled_hours'] for a in e.alerts()],[False,True])
            e.close();e=Engine([config()],path)
            try:
                e.ingest(dict(id='restart',building='a',type='entry',at=timestamp(T-7*3600+2)),now+1)
                self.assertEqual(len(e.alerts()),2);self.assertEqual(len(e.media_jobs()),4)
            finally:e.close()

    def test_larger_reorder_window_processes_eight_second_lag_normally(self):
        e=self.engine(reorder_seconds=15);now=T+3600;e.advance(now)
        e.ingest(dict(id='lag',building='a',type='entry',at=timestamp(now-8)),now)
        e.advance(now+8)
        self.assertEqual(e.db.execute("SELECT status FROM bw_events WHERE id='lag'").fetchone()[0],'processed')
        self.assertIn('first_arrival',[a['type'] for a in e.alerts()])
        self.assertNotIn('late_camera_event',[a['type'] for a in e.alerts()])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);settings=root/'buildings.json'
            settings.write_text(json.dumps({'buildings':[config()]}))
            argv=['watch','--config',str(settings),'--db',str(root/'watch.sqlite3'),
                  'serve','--input',str(root/'events.jsonl'),'--reorder-seconds','15']
            with patch('sys.argv',argv),patch.object(building_watch,'Engine') as factory,\
                 patch.object(building_watch,'Scheduler'),patch.object(building_watch.signal,'signal'):
                building_watch.main()
            self.assertEqual(factory.call_args.kwargs['reorder_seconds'],15)

    def test_destination_403_pauses_only_its_building_across_ticks(self):
        e=self.engine([config(),config('b')]);calls=[]
        def send(body):
            calls.append(body['text'])
            if body['text'].startswith('a'):raise ProviderError('auth','HTTP 403')
            return {'sent':True,'message':{'id':'FAKE-'+body['text']}}
        p=Mock();p.send.side_effect=send
        for building,key in [('a','a1'),('a','a2'),('b','b1')]:
            e.store.enqueue(building,key,dict(destination=e.buildings[building].destination,text=key),T)
        w=Delivery(e.store,p);w.run_once(T)
        self.assertEqual(calls,['a1','b1'])
        e.store.enqueue('b','b2',dict(destination=e.buildings['b'].destination,text='b2'),T)
        w.run_once(T+5);self.assertEqual(calls,['a1','b1','b2'])

    def test_missing_channel_credentials_pause_every_building_without_crashing(self):
        with tempfile.TemporaryDirectory() as folder:
            e=self.engine([config(),config('b')]);p=Whapi(Path(folder)/'missing.json')
            for b in ('a','b'):e.store.enqueue(b,b,dict(destination=e.buildings[b].destination,text=b),T)
            w=Delivery(e.store,p);w.run_once(T);w.run_once(T+5)
            self.assertTrue(all(a['delivery_status']=='pending' and a['attempts']==0 for a in e.alerts()))
            self.assertEqual(e.db.execute("SELECT COUNT(*) FROM bw_audit WHERE kind='delivery_authentication_blocked'").fetchone()[0],1)

    def reminders(self, settings, start, end):
        e=self.engine([settings]);e.advance(instant(start).timestamp(),flush=True);e.advance(instant(end).timestamp(),flush=True)
        return [a for a in e.alerts() if a['type']=='schedule_checkin']

    def test_mlk_closed_stretch_does_not_add_a_sunday_reminder(self):
        values=self.reminders(config(),'2026-01-16T14:00:00-05:00','2026-01-19T16:00:00-05:00')
        self.assertEqual(len(values),1);self.assertTrue(values[0]['scheduled_for'].startswith('2026-01-16'))

    def test_christmas_friday_reminds_thursday_only(self):
        values=self.reminders(config(),'2026-12-24T14:00:00-05:00','2026-12-26T16:00:00-05:00')
        self.assertEqual(len(values),1);self.assertTrue(values[0]['scheduled_for'].startswith('2026-12-24'))

    def test_saturday_working_building_and_one_off_closure_use_own_schedule(self):
        settings=config();settings['weekly_hours']['5']=[['09:00','17:00']]
        values=self.reminders(settings,'2026-11-06T14:00:00-05:00','2026-11-08T16:00:00-05:00')
        self.assertEqual(len(values),1);self.assertTrue(values[0]['scheduled_for'].startswith('2026-11-07'))
        settings=config();settings['date_overrides']={'2026-11-03':[]}
        values=self.reminders(settings,'2026-11-02T14:00:00-05:00','2026-11-03T16:00:00-05:00')
        self.assertEqual(len(values),1);self.assertTrue(values[0]['scheduled_for'].startswith('2026-11-02'))

    def test_cli_defaults_match_pilot_and_missing_db_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);local=root/'.local';local.mkdir()
            settings=local/'buildings.json';settings.write_text(json.dumps({'buildings':[config()]}))
            with patch.object(building_watch,'__file__',str(root/'building_watch.py')),patch('sys.argv',['watch','outbox']),\
                 patch.object(building_watch.signal,'signal'),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):building_watch.main()
            db=local/'watch.sqlite3';self.assertFalse(db.exists())
            e=Engine([config()],str(db));e.close()
            with patch.object(building_watch,'__file__',str(root/'building_watch.py')),patch('sys.argv',['watch','outbox']),\
                 patch.object(building_watch.signal,'signal'),contextlib.redirect_stdout(io.StringIO()) as out:
                building_watch.main()
            self.assertEqual(json.loads(out.getvalue()),[])
            for value in ('0','15','60'):self.assertEqual(building_watch.reorder_window(value),int(value))
            for value in ('-1','61'):
                with self.assertRaises(Exception):building_watch.reorder_window(value)

    def test_scheduler_retries_real_database_lock_but_not_other_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            db=str(Path(folder)/'watch.sqlite3');e=self.engine(db_path=db);e.db.execute('PRAGMA busy_timeout=0')
            writer=sqlite3.connect(db,isolation_level=None);writer.execute('BEGIN IMMEDIATE')
            stop=TwoTicks(lambda:writer.execute('ROLLBACK'))
            try:
                with contextlib.redirect_stderr(io.StringIO()):Scheduler(e,clock=lambda:T+stop.ticks).run(stop)
                self.assertIn('last_timer',e.store.state('a'))
            finally:writer.close()
        fake=Mock();fake.advance.side_effect=sqlite3.OperationalError('no such table: broken')
        with self.assertRaises(sqlite3.OperationalError):Scheduler(fake,clock=lambda:T).run(TwoTicks())

    def test_delivery_loop_retries_database_lock_and_keeps_ambiguous_rows_held(self):
        e=self.engine();e.store.enqueue('a','send',dict(destination=config()['destination'],text='test'),T)
        e.db.execute("UPDATE bw_queue SET status='sending'")
        provider=Mock();worker=Delivery(e.store,provider)
        worker.run_once(T);self.assertEqual(e.alerts()[0]['delivery_status'],'delivery_unknown');provider.send.assert_not_called()
        worker=Mock();worker.run_once.side_effect=[sqlite3.OperationalError('database is locked'),None]
        with contextlib.redirect_stderr(io.StringIO()):building_watch.deliver_loop(worker,TwoTicks(),clock=lambda:T)
        self.assertEqual(worker.run_once.call_count,2)

    def test_pending_scan_uses_index_and_existing_database_gains_indexes(self):
        e=self.engine();plan=e.db.execute("EXPLAIN QUERY PLAN SELECT * FROM bw_events WHERE status='pending' AND at<=? ORDER BY at",(T,)).fetchall()
        self.assertTrue(any('bw_events_pending' in row['detail'] for row in plan))
        names={r['name'] for r in e.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({'bw_events_pending','bw_queue_status_due','bw_visits_building'}<=names)

    def test_optional_publisher_key_is_sent_only_when_configured(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"ok":true}'
        with patch('dashboard_sync.urllib.request.build_opener') as builder:
            builder.return_value.open.return_value=Response()
            connection={'url':'https://example.com','token':'FAKE-SITE-TOKEN','publish_key':'FAKE-PUBLISH-KEY'}
            publish({},connection)
            self.assertEqual(builder.return_value.open.call_args.args[0].headers['X-building-watch-key'],'FAKE-PUBLISH-KEY')
            del connection['publish_key'];publish({},connection)
            self.assertNotIn('X-building-watch-key',builder.return_value.open.call_args.args[0].headers)


if __name__=='__main__':unittest.main()
