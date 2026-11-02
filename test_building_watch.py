import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date, timedelta
from buildingwatch.calendar_utils import instant,us_holidays
from buildingwatch.config import Building,DEFAULT_HOURS
from buildingwatch.service import Engine,Scheduler,JsonlInput,WorkerLock
from buildingwatch.delivery import Delivery,ProviderError,attach_media

T = instant('2026-11-02T09:00:00-05:00').timestamp()

def config(bid='a', **extra):
    return dict(id=bid,timezone='America/New_York',weekly_hours=copy.deepcopy(DEFAULT_HOURS),
                destination=('12345678901@g.us' if bid=='a' else '12345678902@g.us'),**extra)

def timestamp(at):
    from datetime import datetime,timezone
    return datetime.fromtimestamp(at,timezone.utc).isoformat()

class RulesTests(unittest.TestCase):
    def setUp(self):
        self.operator_token='FAKE-OPERATOR-TEST-TOKEN'
        operators={'alice':{'token_sha256':hashlib.sha256(self.operator_token.encode()).hexdigest(),'buildings':['a']}}
        self.engine=Engine([config(),config('b')],operators=operators)
        self.i=0
    def tearDown(self):
        self.engine.close()
    def raw(self,kind,offset=0,bid='a',**fields):
        self.i+=1
        return dict(id=str(self.i),building=bid,type=kind,at=timestamp(T+offset),**fields)
    def feed(self,kind,offset=0,bid='a',**fields):
        e=self.raw(kind,offset,bid,**fields)
        result=self.engine.ingest(e,T+offset,replay=True)
        self.engine.advance(T+offset,flush=True)
        return result
    def alerts(self,kind):
        return [a for a in self.engine.alerts() if a['type']==kind]
    def test_short_reordering_buffer_sorts_events(self):
        self.engine.ingest(self.raw('entry',4),T+5)
        self.engine.ingest(self.raw('entry',1),T+5)
        self.engine.advance(T+5)
        self.assertEqual(self.alerts('first_arrival'),[])
        self.engine.advance(T+10)
        self.assertEqual(self.alerts('first_arrival')[0]['created_at'],timestamp(T+1))
    def test_late_event_quarantined_and_next_event_runs(self):
        self.feed('entry',10)
        self.assertEqual(self.feed('exit',5),'quarantined')
        self.feed('exit',11)
        self.assertEqual(self.engine.store.state('a')['last_exit']['at'],T+11)
        self.assertTrue(self.engine.db.execute("SELECT 1 FROM bw_audit WHERE kind='late_beyond_buffer'").fetchone())
    def test_duplicate_content_vs_collision(self):
        e=self.raw('entry')
        self.assertEqual(self.engine.ingest(e,T),'buffered')
        self.assertEqual(self.engine.ingest(e,T),'duplicate')
        self.assertEqual(self.engine.ingest(dict(e,type='exit'),T),'collision')
        self.engine.advance(T,flush=True)
        self.assertEqual(len(self.alerts('first_arrival')),1)
        self.assertTrue(self.engine.db.execute("SELECT 1 FROM bw_audit WHERE kind='id_collision'").fetchone())
    def test_invalid_and_future_events_do_not_poison_queue(self):
        self.assertEqual(self.engine.ingest(self.raw('entry',100),T),'quarantined')
        self.assertEqual(self.engine.ingest({'building':'a','type':'visit','actor':'alice'},T),'invalid')
        self.assertEqual(self.engine.ingest(self.raw('entry'),T),'buffered')
        self.engine.advance(T,flush=True)
        self.assertEqual(len(self.alerts('first_arrival')),1)
        self.assertEqual(self.engine.ingest({'id':'bad','building':[],'type':'entry','at':timestamp(T)},T),'invalid')
    def test_scheduler_operates_without_tick_events(self):
        self.feed('heartbeat')
        class Stop:
            elapsed=0
            def is_set(self): return self.elapsed>180
            def wait(self,interval): self.elapsed+=60
        stop=Stop()
        Scheduler(self.engine,clock=lambda:T+stop.elapsed,interval=.01).run(stop)
        self.assertEqual(len(self.alerts('camera_offline')),1)
    def test_startup_is_not_an_outage(self):
        self.engine.advance(T,flush=True)
        self.engine.advance(T+299,flush=True)
        self.assertEqual(self.alerts('camera_never_connected'),[])
        self.engine.advance(T+300,flush=True)
        self.assertEqual(len(self.alerts('camera_never_connected')),2)
        self.assertEqual(self.alerts('camera_offline'),[])
        self.feed('heartbeat',301)
        self.assertEqual(len(self.alerts('camera_recovered')),1)
    def test_connected_outage_and_recovery_are_deduplicated(self):
        self.feed('heartbeat')
        self.engine.advance(T+120,flush=True)
        self.engine.advance(T+180,flush=True)
        self.assertEqual(len(self.alerts('camera_offline')),1)
        self.feed('heartbeat',181)
        self.assertEqual(len(self.alerts('camera_recovered')),1)
    def test_exit_during_bad_coverage_is_retained(self):
        self.feed('exit',8*3600)
        state=self.engine.store.state('a')
        self.assertEqual(state['last_exit']['at'],T+8*3600)
        self.assertFalse(state['departure']['verified'])
        self.engine.advance(T+8*3600+900,flush=True)
        self.assertEqual(len(self.alerts('departure_unverified')),1)
        self.assertEqual(self.alerts('possible_last_departure'),[])
    def test_monitored_departure_fires_after_fifteen_minutes(self):
        self.feed('heartbeat')
        self.feed('exit')
        for n in range(30,900,30): self.feed('heartbeat',n)
        self.assertEqual(self.alerts('possible_last_departure'),[])
        self.feed('heartbeat',900)
        self.assertEqual(len(self.alerts('possible_last_departure')),1)
    def test_person_activity_cancels_departure(self):
        self.feed('heartbeat')
        self.feed('exit')
        self.feed('activity',30)
        self.engine.advance(T+900,flush=True)
        self.assertEqual(self.alerts('possible_last_departure'),[])
        self.assertEqual(self.alerts('departure_unverified'),[])
    def test_entry_at_departure_deadline_cancels_before_timer(self):
        self.feed('heartbeat')
        self.feed('exit')
        for n in range(30,900,30): self.feed('heartbeat',n)
        self.feed('entry',900)
        self.assertEqual(self.alerts('possible_last_departure'),[])
    def test_buffer_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'watch.sqlite3')
            first=Engine([config()],path)
            event=self.raw('activity')
            first.ingest(event,T)
            first.close()
            second=Engine([config()],path)
            try:
                second.advance(T+6)
                self.assertEqual(len(second.alerts()),1)
                self.assertEqual(second.ingest(event,T+7),'duplicate')
            finally: second.close()
    def test_first_person_any_time_and_building(self):
        self.feed('activity',7*3600)
        self.feed('entry',7*3600+1)
        self.feed('activity',7*3600+1,'b')
        self.assertEqual(len(self.alerts('first_arrival')),2)
        self.assertEqual(self.alerts('first_arrival')[0]['observation_kind'],'activity')
        self.assertNotEqual(*[a['destination'] for a in self.alerts('first_arrival')])
        self.assertTrue(all(j['duration_seconds']==10 for j in self.engine.media_jobs() if j['type']=='first_arrival'))
    def test_visits_require_real_operator_authorization(self):
        start,end=timestamp(T+3600),timestamp(T+7200)
        with self.assertRaises(PermissionError): self.engine.visits.approve('wrong','a',start,end,T)
        with self.assertRaises(PermissionError): self.engine.visits.approve(self.operator_token,'b',start,end,T)
        result=self.engine.visits.approve(self.operator_token,'a',start,end,T)
        self.assertEqual(result['actor'],'alice')
        self.assertTrue(self.engine.visits.expected('a',T+3600))
        self.assertFalse(self.engine.visits.expected('a',T+7200))
        self.assertEqual(result['ends_at_exclusive'],end)
        self.assertTrue(self.engine.db.execute("SELECT 1 FROM bw_audit WHERE kind='visit_approved'").fetchone())
    def test_visit_grace_and_expiry_cleanup(self):
        self.engine.buildings['a'].c['visit_end_grace_minutes']=5
        self.engine.visits.approve(self.operator_token,'a',timestamp(T+60),timestamp(T+120),T)
        self.assertTrue(self.engine.visits.expected('a',T+120))
        self.assertFalse(self.engine.visits.expected('a',T+420))
        self.engine.advance(T+420,flush=True)
        self.assertEqual(self.engine.db.execute('SELECT status FROM bw_visits').fetchone()[0],'expired')
        self.engine.advance(T+32*86400,flush=True)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM bw_visits').fetchone()[0],0)
        self.assertTrue(self.engine.db.execute("SELECT 1 FROM bw_audit WHERE kind='visit_approved'").fetchone())
    def test_manual_timeout_does_not_run_forever(self):
        self.feed('suspicious_start')
        self.engine.advance(T+301,flush=True)
        self.assertNotIn('suspicious',self.engine.store.state('a'))
        self.assertEqual(self.alerts('suspicious_activity_finished')[0]['event_end_reason'],'manual_timeout')
    def test_long_event_preserved_in_contiguous_numbered_chunks(self):
        self.feed('suspicious_start')
        for n in range(240,8100,240): self.feed('activity',n)
        self.feed('suspicious_end',8100)
        jobs=[j for j in self.engine.media_jobs() if j['type']=='suspicious_activity']
        self.assertGreater(len(jobs),1)
        self.assertTrue(all(0<j['duration_seconds']<=60 for j in jobs))
        self.assertEqual(sum(j['duration_seconds'] for j in jobs),8106)
        self.assertEqual(jobs[0]['start'],timestamp(T-3))
        self.assertEqual(jobs[-1]['end'],timestamp(T+8103))
        for left,right in zip(jobs,jobs[1:]): self.assertEqual(left['end'],right['start'])
        self.assertTrue(jobs[-1]['final_part'])
    def test_friday_includes_monday_holiday_and_restart_catchup(self):
        friday=instant('2026-09-04T14:59:00-04:00').timestamp()
        self.engine.advance(friday,flush=True)
        self.engine.advance(friday+60,flush=True)
        first=self.alerts('schedule_checkin')[0]
        self.assertIn('Labor Day (2026-09-07)',first['text'])
        self.engine.advance(friday+120,flush=True)
        self.assertEqual(len(self.alerts('schedule_checkin')),2) # one per building
    def test_missed_friday_reminder_catches_up_after_restart(self):
        before=instant('2026-11-06T14:00:00-05:00').timestamp()
        self.engine.advance(before,flush=True)
        self.engine.advance(before+23*3600,flush=True)
        reminders=self.alerts('schedule_checkin')
        self.assertEqual(len(reminders),2)
        self.assertTrue(all(r['catchup'] for r in reminders))
    def test_spool_bad_line_partial_line_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.jsonl'
            event=self.raw('entry')
            p.write_text('bad json\n'+json.dumps(event))
            source=JsonlInput(self.engine,p)
            source.read(T)
            self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM bw_events').fetchone()[0],0)
            with p.open('a') as f: f.write('\n')
            source.read(T)
            source.read(T)
            self.engine.advance(T,flush=True)
            self.assertEqual(len(self.alerts('first_arrival')),1)
    def test_oversized_spool_line_does_not_stop_next_event(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.jsonl'
            p.write_text('x'*1_100_000+'\n'+json.dumps(self.raw('entry'))+'\n')
            JsonlInput(self.engine,p).read(T)
            self.engine.advance(T,flush=True)
            self.assertEqual(len(self.alerts('first_arrival')),1)
    def test_media_attachment_uses_correct_route_and_cannot_escape_root(self):
        self.feed('entry')
        job=self.engine.media_jobs()[0]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'media'; root.mkdir()
            clip=root/'clip.mp4'; clip.write_bytes(b'FAKE VIDEO FOR QUEUE TEST')
            outside=Path(d)/'outside.mp4'; outside.write_bytes(b'x')
            with self.assertRaises(ValueError): attach_media(self.engine.store,job['id'],outside,root,T)
            attach_media(self.engine.store,job['id'],clip,root,T)
            item=self.engine.alerts()[-1]
            self.assertEqual(item['destination'],'12345678901@g.us')
            self.assertEqual(item['media_path'],str(clip.resolve()))
            with self.assertRaises(ValueError): attach_media(self.engine.store,job['id'],clip,root,T)

class ConfigTests(unittest.TestCase):
    def test_invalid_config_is_rejected(self):
        for change in ({'timezone':'Not/AZone'},{'timezone':''},{'weekly_hours':{'7':[]}},{'weekly_hours':{'0':[]}},
                       {'destination':'PLACEHOLDER'},{'clip_chunk_seconds':True},{'manual_timeout_seconds':0},
                       {'typo_hours':[]},{'arrival_window':['05:00','12:00']}):
            c=config();c.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): Building(c)
        for key in ('timezone','weekly_hours','destination'):
            c=config(); del c[key]
            with self.assertRaises(ValueError): Building(c)
    def test_overnight_and_dst(self):
        c=config(); c['weekly_hours']['4']=[['20:00','02:00']]
        c['weekly_hours']['5']=[['20:00','02:00']]
        b=Building(c)
        for value in ('2026-11-06T21:00:00-05:00','2026-11-07T01:59:00-05:00',
                      '2026-11-01T01:30:00-04:00','2026-11-01T01:30:00-05:00'):
            self.assertTrue(b.open_at(instant(value)))
        self.assertFalse(b.open_at(instant('2026-11-07T02:00:00-05:00')))
    def test_holiday_observation_year_boundary(self):
        self.assertIn(date(2026,7,3),us_holidays(2026))
        self.assertIn(date(2027,12,31),us_holidays(2027))
        self.assertEqual(len([d for d in us_holidays(2026) if d.weekday()<5]),11)

class FakeProvider:
    def __init__(self,outcomes=None):
        self.outcomes=list(outcomes or [])
        self.calls=[]
        self.state='pending'
    def send(self,body):
        self.calls.append(body)
        if self.outcomes:
            outcome=self.outcomes.pop(0)
            if isinstance(outcome,Exception): raise outcome
        return {'sent':True,'message':{'id':'test-message-0001'}}
    def status(self,mid): return {'id':mid,'status':self.state,'chat_id':'12345678901@g.us'}

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.engine=Engine([config()])
        self.engine.store.enqueue('a','test',{'destination':'12345678901@g.us','text':'Test'},T)
    def tearDown(self): self.engine.close()
    def status(self): return self.engine.alerts()[0]['delivery_status']
    def test_acceptance_is_not_delivery_confirmation(self):
        provider=FakeProvider();worker=Delivery(self.engine.store,provider)
        worker.run_once(T)
        self.assertEqual(self.status(),'accepted')
        worker.run_once(T+61)
        self.assertEqual(self.status(),'accepted')
        provider.state='delivered';worker.run_once(T+122)
        self.assertEqual(self.status(),'delivered')
        self.assertEqual(len(provider.calls),1)
    def test_retry_backoff_and_eventual_acceptance(self):
        provider=FakeProvider([ProviderError('retryable','HTTP 429')]);worker=Delivery(self.engine.store,provider)
        worker.run_once(T);self.assertEqual(self.status(),'pending')
        worker.run_once(T+4);self.assertEqual(len(provider.calls),1)
        worker.run_once(T+5);self.assertEqual(self.status(),'accepted')
    def test_ambiguous_result_is_not_blindly_resent(self):
        provider=FakeProvider([ProviderError('unknown','timeout')]);worker=Delivery(self.engine.store,provider)
        worker.run_once(T);worker.run_once(T+500)
        self.assertEqual(self.status(),'delivery_unknown')
        self.assertEqual(len(provider.calls),1)
    def test_retry_exhaustion_is_visible(self):
        provider=FakeProvider([ProviderError('retryable','HTTP 429')]*5);worker=Delivery(self.engine.store,provider)
        for n in range(5): worker.run_once(T+n*4000)
        self.assertEqual(self.status(),'failed')
    def test_restart_during_send_does_not_duplicate(self):
        self.engine.db.execute("UPDATE bw_queue SET status='sending'")
        provider=FakeProvider();worker=Delivery(self.engine.store,provider);worker.run_once(T)
        self.assertEqual(self.status(),'delivery_unknown')
        self.assertEqual(provider.calls,[])
    def test_wrong_route_cannot_confirm_delivery(self):
        provider=FakeProvider();provider.status=lambda mid:{'id':mid,'status':'delivered','chat_id':'99999999999@g.us'}
        worker=Delivery(self.engine.store,provider);worker.run_once(T);worker.run_once(T+61)
        self.assertEqual(self.status(),'accepted')
    def test_single_worker_lock(self):
        with tempfile.TemporaryDirectory() as d:
            with WorkerLock(str(Path(d)/'lock')):
                with self.assertRaises(RuntimeError):
                    with WorkerLock(str(Path(d)/'lock')): pass
    def test_missing_delivery_confirmation_becomes_visible(self):
        provider=FakeProvider();worker=Delivery(self.engine.store,provider)
        worker.run_once(T);worker.run_once(T+86401)
        self.assertEqual(self.status(),'delivery_unconfirmed')
        self.assertEqual(len(provider.calls),1)

if __name__=='__main__': unittest.main()
