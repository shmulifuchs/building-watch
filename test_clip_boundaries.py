"""Regression coverage imported from the v2.1 handoff snapshot."""
import unittest
from buildingwatch.calendar_utils import instant
from buildingwatch.service import Engine
from test_building_watch import T, config, timestamp


class ClipBoundaryTests(unittest.TestCase):
    def make_event(self,duration,chunk_seconds=10,advance_during_event=False):
        engine=Engine([config(clip_chunk_seconds=chunk_seconds)])
        self.addCleanup(engine.close)
        def feed(kind,offset,identity):
            event=dict(id=identity,building='a',type=kind,at=timestamp(T+offset))
            engine.ingest(event,T+offset,replay=True)
            engine.advance(T+offset,flush=True)
        feed('suspicious_start',0,'flag')
        if advance_during_event:
            for second in range(1,int(duration)+1):
                feed('activity',second,'presence-'+str(second))
        feed('suspicious_end',duration,'end')
        return engine,[j for j in engine.media_jobs() if j['type']=='suspicious_activity']

    def assert_complete(self,jobs,duration,maximum=10):
        self.assertEqual(jobs[0]['start'],timestamp(T-3))
        self.assertEqual(jobs[-1]['end'],timestamp(T+duration+3))
        self.assertTrue(jobs[-1]['final_part'])
        for part,job in enumerate(jobs,1):
            self.assertEqual(job['part'],part)
            self.assertGreaterEqual(job['duration_seconds'],1)
            self.assertLessEqual(job['duration_seconds'],maximum)
        for left,right in zip(jobs,jobs[1:]):
            self.assertEqual(left['end'],right['start'])
        self.assertAlmostEqual(sum(j['duration_seconds'] for j in jobs),duration+6,places=6)

    def test_018_second_final_tail_is_usable_and_keeps_all_footage(self):
        _,jobs=self.make_event(24.018)
        self.assert_complete(jobs,24.018)

    def test_final_tail_around_exact_chunk_boundaries(self):
        for duration in (0,3.999999,4,4.000001,4.018,13.999999,14,14.018,24.999,25,25.001):
            with self.subTest(duration=duration):
                _,jobs=self.make_event(duration)
                self.assert_complete(jobs,duration)

    def test_final_tail_after_periodic_chunk_emission(self):
        _,jobs=self.make_event(24.018,advance_during_event=True)
        self.assert_complete(jobs,24.018)

    def test_fractional_tail_with_normal_sixty_second_chunks(self):
        _,jobs=self.make_event(174.018,chunk_seconds=60,advance_during_event=True)
        self.assert_complete(jobs,174.018,maximum=60)

    def test_one_second_setting_preserves_tail_with_marked_overlap(self):
        _,jobs=self.make_event(.018,chunk_seconds=1)
        self.assertEqual(jobs[0]['start'],timestamp(T-3))
        self.assertEqual(jobs[-1]['end'],timestamp(T+3.018))
        self.assertTrue(all(j['duration_seconds']==1 for j in jobs))
        self.assertGreater(jobs[-1]['overlap_seconds'],0)
        for previous,current in zip(jobs,jobs[1:]):
            self.assertLessEqual(instant(current['start']),instant(previous['end']))

    def test_pre_fix_released_part_is_not_modified_to_repair_tail(self):
        engine=Engine([config(clip_chunk_seconds=10)])
        self.addCleanup(engine.close)
        current=dict(start=T,next_start=T+27,last_activity=T+24.018,part=4,
                     recording_gap_possible=False)
        b=engine.buildings['a']
        for part in range(1,4):
            start=T-3+(part-1)*10
            engine.rules.media(b,'suspicious:'+str(T)+':'+str(part),
                               'suspicious_activity',T+27,start,start+10,
                               part=part,final_part=False)
        engine.db.execute("UPDATE bw_media SET status='attached'")
        before=[dict(row) for row in engine.db.execute('SELECT * FROM bw_media ORDER BY id')]
        engine.rules.chunks(b,current,T+27.018,True,'explicit_end')
        after=[dict(row) for row in engine.db.execute('SELECT * FROM bw_media ORDER BY id')]
        self.assertEqual(before,after[:3])
        last=engine.media_jobs()[-1]
        self.assertEqual(last['duration_seconds'],1)
        self.assertEqual(last['end'],timestamp(T+27.018))
        self.assertAlmostEqual(last['overlap_seconds'],.982,places=6)
        self.assertTrue(last['final_part'])


