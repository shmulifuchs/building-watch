"""ANNKE connector and clip tests against a simulated camera (no real camera needed)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from buildingwatch.ai_review import DRAFT_PROMPT, ClipReviewer, ReviewUnavailable, load_prompt
from buildingwatch.annke import AnnkeClient, camera_client
from buildingwatch.delivery import Whapi
from buildingwatch.doctor import Doctor
from buildingwatch.recorder import ClipMaker, Recorder, find_ffmpeg, list_segments
from buildingwatch.reolink import (CameraError, CameraUnsupported, Connector, ReolinkClient, Spool,
                                   load_camera_settings, timestamp)
from buildingwatch.service import Engine
from buildingwatch.store import Store
from camera_setup import check_camera
from test_building_watch import T, config

PASSWORD = 'FAKE:CAMERA/PASSWORD'
JPEG = b'\xff\xd8' + b'\x00' * 4000 + b'\xff\xd9'
REALM, NONCE = 'FakeCamera', 'abc123'


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def alert(kind='VMD', state='active'):
    return ('--boundary\r\nContent-Type: application/xml\r\n\r\n<EventNotificationAlert version="2.0">'
            '<eventType>{}</eventType><eventState>{}</eventState></EventNotificationAlert>\r\n'
            .format(kind, state)).encode()


class FakeAnnke:
    """Digest-protected deviceInfo, picture and alertStream endpoints."""
    def __init__(self):
        self.frame, self.events_supported = JPEG, True
        self.requests, self.authorizations = [], []
        self.alerts = []
        self.stream_open = threading.Event()
        self.drop_stream = threading.Event()
        camera = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def authorized(self):
                header = self.headers.get('Authorization', '')
                camera.authorizations.append(header)
                found = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
                if not header.startswith('Digest ') or 'response' not in found:
                    return False
                first = md5('admin:{}:{}'.format(REALM, PASSWORD))
                second = md5('GET:' + self.path)
                wanted = md5(':'.join([first, NONCE, found.get('nc', ''), found.get('cnonce', ''),
                                       'auth', second]))
                return found['response'] == wanted

            def do_GET(self):
                camera.requests.append(self.path)
                if not self.authorized():
                    self.send_response(401)
                    self.send_header('WWW-Authenticate',
                                     'Digest realm="{}", nonce="{}", qop="auth"'.format(REALM, NONCE))
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/ISAPI/System/deviceInfo':
                    self.reply(b'<DeviceInfo><model>C800</model></DeviceInfo>', 'application/xml')
                elif self.path == '/ISAPI/Streaming/channels/101/picture':
                    self.reply(camera.frame, 'image/jpeg')
                elif self.path.endswith('alertStream') and camera.events_supported:
                    self.close_connection = True
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/mixed; boundary=boundary')
                    self.end_headers()
                    camera.stream_open.set()
                    while not camera.drop_stream.is_set():
                        if camera.alerts:
                            self.wfile.write(camera.alerts.pop(0))
                            self.wfile.flush()
                        time.sleep(0.02)
                else:
                    self.send_response(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()

            def reply(self, data, kind):
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.drop_stream.set()
        self.server.shutdown()
        self.server.server_close()


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def wait_for(check, seconds=3):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class AnnkeClientTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeAnnke()
        self.addCleanup(self.camera.close)
        self.clock = Clock()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def client(self, password=PASSWORD, **options):
        client = AnnkeClient('127.0.0.1', 'admin', password, port=self.camera.port,
                             clock=self.clock, retry_seconds=0.05, **options)
        self.addCleanup(client.close)
        return client

    def test_digest_sign_in_never_sends_the_password(self):
        self.assertEqual(self.client().login(), 'C800')
        sent = ' '.join(self.camera.authorizations + self.camera.requests)
        self.assertIn('Digest', sent)
        self.assertNotIn(PASSWORD, sent)

    def test_wrong_password_pauses_instead_of_locking_the_camera(self):
        client = self.client('wrong')
        with self.assertRaisesRegex(CameraError, 'rejected the login'):
            client.login()
        count = len(self.camera.requests)
        with self.assertRaisesRegex(CameraError, 'paused'):
            client.usable_frame()
        self.assertEqual(len(self.camera.requests), count)
        self.clock.now += 61
        with self.assertRaisesRegex(CameraError, 'rejected the login'):
            client.login()

    def test_errors_do_not_reveal_the_password(self):
        client = AnnkeClient('127.0.0.1', 'admin', PASSWORD, port=1, timeout=1)
        with self.assertRaises(CameraError) as caught:
            client.usable_frame()
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_only_a_complete_jpeg_counts_as_a_picture(self):
        client = self.client()
        self.assertTrue(client.usable_frame())
        self.camera.frame = JPEG[:-2]
        self.assertFalse(client.usable_frame())
        self.camera.frame = b'\xff\xd8\xff\xd9'
        self.assertFalse(client.usable_frame())

    def test_detection_events_reach_the_service_spool(self):
        client = self.client()
        spool = Path(self.folder.name) / 'events.jsonl'
        connector = Connector(client, 'office-1', Spool(spool), clock=self.clock, id_prefix='annke')
        self.assertEqual([e['type'] for e in connector.poll_once()], ['heartbeat'])
        self.assertTrue(self.camera.stream_open.wait(3))
        self.camera.alerts.append(alert('VMD', 'active'))
        self.assertTrue(wait_for(client.person_detected))
        written = connector.poll_once()
        self.assertEqual([e['type'] for e in written], ['activity'])
        self.assertTrue(written[0]['id'].startswith('annke-'))
        self.clock.now += 6
        self.assertFalse(client.person_detected())

    def test_ignored_and_inactive_events_do_not_trigger(self):
        client = self.client()
        client.feed(alert('videoloss', 'active') + alert('VMD', 'inactive'))
        self.assertEqual(client.active_until, 0)
        client.feed(alert('fielddetection', 'active')[:40])
        self.assertEqual(client.active_until, 0)
        client.feed(alert('fielddetection', 'active')[40:])
        self.assertEqual(client.active_until, self.clock.now + 5)

    def test_short_detection_survives_a_slow_poll(self):
        client = self.client()
        self.camera.events_supported = True
        client.start_events()
        self.assertTrue(wait_for(lambda: client.events_connected))
        client.feed(alert('VMD'))
        self.clock.now += 60
        client.events_down_since = self.clock.now
        self.assertTrue(client.person_detected())
        self.assertFalse(client.person_detected())

    def test_event_list_can_be_limited_to_people(self):
        client = self.client(event_types=['humanrecognition'])
        client.feed(alert('VMD'))
        self.assertEqual(client.active_until, 0)
        client.feed(alert('humanRecognition'))
        self.assertGreater(client.active_until, 0)

    def test_camera_without_events_is_reported_clearly(self):
        self.camera.events_supported = False
        client = self.client()
        client.start_events()
        self.assertTrue(wait_for(lambda: client.events_unsupported))
        with self.assertRaises(CameraUnsupported):
            client.person_detected()

    def test_lost_detection_stream_becomes_an_outage_then_recovers(self):
        client = self.client(down_after=30)
        client.start_events()
        self.assertTrue(self.camera.stream_open.wait(3))
        self.assertTrue(wait_for(lambda: client.events_connected))
        self.camera.drop_stream.set()
        self.assertTrue(wait_for(lambda: not client.events_connected))
        self.assertFalse(client.person_detected())
        self.clock.now += 31
        with self.assertRaisesRegex(CameraError, 'disconnected'):
            client.person_detected()
        self.camera.drop_stream.clear()
        self.assertTrue(wait_for(lambda: client.events_connected))
        self.assertFalse(client.person_detected())

    def test_setup_check_reports_the_model_and_motion_warning(self):
        lines = check_camera(dict(brand='annke', building='office-1', host='127.0.0.1',
                                  port=self.camera.port, scheme='http', username='admin',
                                  password=PASSWORD, channel=1))
        self.assertIn('C800', lines[0])
        self.assertIn('usable', lines[1])
        self.assertIn('motion', lines[2])

    def test_stream_address_escapes_the_password(self):
        url = AnnkeClient('192.168.1.64', 'admin', 'a/b:c@d').rtsp_url()
        self.assertEqual(url, 'rtsp://admin:a%2Fb%3Ac%40d@192.168.1.64:554/Streaming/Channels/101')

    def test_saved_settings_pick_the_right_camera_type(self):
        path = Path(self.folder.name) / 'camera.json'
        path.write_text(json.dumps(dict(brand='annke', building='office-1', host='192.168.1.64',
                                        scheme='http', username='admin', password='x', channel=1,
                                        retention_hours=6, stream='02')))
        self.assertIsInstance(camera_client(load_camera_settings(path)), AnnkeClient)
        path.write_text(json.dumps(dict(building='office-1', host='192.168.1.50', scheme='http',
                                        username='admin', password='x')))
        self.assertIsInstance(camera_client(load_camera_settings(path)), ReolinkClient)
        path.write_text(json.dumps(dict(brand='other', building='b', host='h', username='u', password='p')))
        with self.assertRaisesRegex(ValueError, 'brand'):
            load_camera_settings(path)


def segment_name(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime('%Y%m%dT%H%M%SZ.ts')


def add_job(store, start, end, destination='123@g.us', building='office-1'):
    body = dict(building_id=building, building_name='First Office', destination=destination,
                type='suspicious_activity', start=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                end=datetime.fromtimestamp(end, timezone.utc).isoformat(), duration_seconds=end - start,
                created_at=datetime.fromtimestamp(start, timezone.utc).isoformat(), part=1)
    return store.db.execute('INSERT INTO bw_media(building,key,body,status) VALUES(?,?,?,?)',
                            (building, 'job:' + str(start), json.dumps(body), 'awaiting_recording')).lastrowid


class ClipMakerTests(unittest.TestCase):
    START = 1_793_000_000.0

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.segments, self.clips = self.root / 'recordings', self.root / 'clips'
        self.segments.mkdir()
        self.store = Store()
        self.clock = Clock(self.START + 100)
        self.calls = []
        self.broken = False

    def fake_ffmpeg(self, command, **options):
        self.calls.append(command)
        if self.broken:
            raise subprocess.CalledProcessError(1, 'ffmpeg')
        Path(command[-1]).write_bytes(b'fake mp4')

    def maker(self, **options):
        options.setdefault('disk_free', lambda folder: 100_000_000_000)
        options.setdefault('pad_before', 0)
        options.setdefault('pad_after', 0)
        return ClipMaker(self.store, 'office-1', self.segments, self.clips, 'ffmpeg', clock=self.clock,
                         log=lambda text: None, run_command=self.fake_ffmpeg,
                         building_name='First Office', destination='123@g.us', **options)

    def segment(self, offset):
        (self.segments / segment_name(self.START + offset)).write_bytes(b'video')

    def status(self, job):
        return self.store.db.execute('SELECT status FROM bw_media WHERE id=?', (job,)).fetchone()['status']

    def queued(self):
        return [json.loads(row['body']) for row in self.store.db.execute('SELECT body FROM bw_queue')]

    def test_clip_uses_only_overlapping_footage_and_is_whatsapp_compatible(self):
        for offset in (0, 10, 20, 30):
            self.segment(offset)
        job = add_job(self.store, self.START + 12, self.START + 25)
        self.assertEqual(self.maker().run_once(), [job])
        command = self.calls[0]
        self.assertEqual(command[command.index('-ss') + 1], '2.000')
        self.assertEqual(command[command.index('-t') + 1], '13.000')
        self.assertEqual(command[command.index('-c:v') + 1], 'libx264')
        self.assertEqual(command[command.index('-pix_fmt') + 1], 'yuv420p')
        self.assertIn('-an', command)
        self.assertEqual(self.status(job), 'attached')
        self.assertEqual(self.queued()[0]['media_path'], str((self.clips / 'clip-1.mp4').resolve()))
        self.assertEqual(sorted(p.name for p in self.clips.iterdir()), ['clip-1.mp4'])

    def test_waits_until_the_footage_has_finished_recording(self):
        self.segment(0)
        job = add_job(self.store, self.START, self.START + 95)
        self.assertEqual(self.maker().run_once(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status(job), 'awaiting_recording')

    def test_clip_starting_inside_a_gap_begins_at_the_available_footage(self):
        self.segment(20)
        job = add_job(self.store, self.START + 5, self.START + 28)
        self.assertEqual(self.maker().run_once(), [job])
        self.assertEqual(self.calls[0][self.calls[0].index('-ss') + 1], '0.000')
        self.assertEqual(self.calls[0][self.calls[0].index('-t') + 1], '8.000')
        self.assertTrue(self.queued()[0]['footage_incomplete'])
        self.assertIn('only about 8 of 23 seconds', self.queued()[0]['text'])

    def test_gap_in_the_middle_is_reported(self):
        self.segment(0)
        self.segment(30)
        add_job(self.store, self.START, self.START + 40)
        self.maker().run_once()
        self.assertIn('Footage incomplete', self.queued()[0]['text'])
        self.assertAlmostEqual(self.queued()[0]['recorded_seconds'], 20, delta=2)  # segments may run long

    def test_footage_ending_early_is_reported(self):
        self.segment(0)
        add_job(self.store, self.START, self.START + 30)
        self.maker().run_once()
        self.assertIn('only about 10 of 30 seconds', self.queued()[0]['text'])

    def test_full_coverage_has_no_warning(self):
        for offset in (0, 10, 20):
            self.segment(offset)
        add_job(self.store, self.START + 2, self.START + 28)
        self.maker().run_once()
        self.assertNotIn('incomplete', self.queued()[0]['text'])
        self.assertNotIn('footage_incomplete', self.queued()[0])

    def test_missing_footage_sends_a_text_alert_once(self):
        job = add_job(self.store, self.START, self.START + 10)
        maker = self.maker(missing_after=300)
        maker.run_once()
        self.assertEqual(self.status(job), 'awaiting_recording')
        self.clock.now = self.START + 311
        maker.run_once()
        maker.run_once()
        self.assertEqual(self.status(job), 'no_footage')
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued()[0]['destination'], '123@g.us')
        self.assertIn('not recorded', self.queued()[0]['text'])

    def test_repeated_ffmpeg_failure_stops_and_alerts(self):
        self.segment(0)
        self.broken = True
        job = add_job(self.store, self.START, self.START + 8)
        maker = self.maker(max_attempts=3)
        for _ in range(4):
            maker.run_once()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.status(job), 'clip_failed')
        self.assertEqual(self.queued()[0]['type'], 'footage_unavailable')
        self.assertEqual([p for p in self.clips.iterdir()], [])

    def test_clips_are_padded_for_stream_lag_without_changing_the_coverage_check(self):
        for offset in (0, 10, 20, 30):
            self.segment(offset)
        add_job(self.store, self.START + 12, self.START + 25)
        self.maker(pad_before=3, pad_after=2).run_once()
        command = self.calls[0]
        self.assertEqual(command[command.index('-ss') + 1], '9.000')
        self.assertEqual(command[command.index('-t') + 1], '18.000')
        self.assertNotIn('incomplete', self.queued()[0]['text'])

    def test_padding_waits_for_the_extra_footage(self):
        self.segment(0)
        job = add_job(self.store, self.START, self.START + 90)
        self.assertEqual(self.maker(pad_after=2, settle_seconds=9).run_once(), [])
        self.assertEqual(self.status(job), 'awaiting_recording')

    def test_recording_errors_are_saved_without_the_password(self):
        source = 'rtsp://admin:p%40ss%2F1@192.168.1.64:554/Streaming/Channels/101'
        script = self.root / 'fake-ffmpeg'
        script.write_text('#!/bin/sh\necho "{0}: Connection refused" >&2\n'
                          'echo "login failed for password p@ss/1" >&2\nexit 1\n'.format(source))
        script.chmod(0o700)
        logs = []
        stop = threading.Event()
        self.addCleanup(stop.set)
        recorder = Recorder(source, self.segments, str(script), log=logs.append)
        thread = threading.Thread(target=recorder.run, args=(stop,), kwargs=dict(restart_seconds=0.01), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: logs, 5))
        stop.set()
        thread.join(10)
        saved = (self.segments / 'recorder.log').read_text()
        self.assertIn('Connection refused', saved)
        self.assertIn('login failed', saved + logs[0])
        for text in (saved, ' '.join(logs)):
            self.assertNotIn('p@ss', text)
            self.assertNotIn('p%40ss', text)
            self.assertNotIn('rtsp://', text)
        self.assertEqual((self.segments / 'recorder.log').stat().st_mode & 0o777, 0o600)

    def test_footage_never_reaches_another_building(self):
        self.segment(0)
        add_job(self.store, self.START, self.START + 8, destination='other@g.us', building='office-2')
        own = add_job(self.store, self.START + 1, self.START + 8)
        self.assertEqual(self.maker().run_once(), [own])
        self.assertEqual([body['destination'] for body in self.queued()], ['123@g.us'])
        other = self.store.db.execute("SELECT status FROM bw_media WHERE building='office-2'").fetchone()
        self.assertEqual(other['status'], 'awaiting_recording')

    def old_clip(self, job, queue_status):
        self.segment(0)
        self.maker().run_once()
        clip = self.clips / 'clip-{}.mp4'.format(job)
        self.store.db.execute('UPDATE bw_queue SET status=?', (queue_status,))
        self.clock.now += 2 * 86400
        os.utime(clip, (self.START, self.START))
        return clip

    def test_clip_waiting_to_send_is_not_deleted(self):
        job = add_job(self.store, self.START, self.START + 8)
        clip = self.old_clip(job, 'pending')
        for status in ('pending', 'sending', 'accepted', 'delivery_unknown'):
            with self.subTest(status):
                self.store.db.execute('UPDATE bw_queue SET status=?', (status,))
                self.maker().prune_clips()
                self.assertTrue(clip.exists())

    def test_failed_sign_in_keeps_the_clip_for_retry_but_other_failures_do_not(self):
        job = add_job(self.store, self.START, self.START + 8)
        clip = self.old_clip(job, 'failed')
        self.store.db.execute("UPDATE bw_queue SET error='HTTP 401'")
        self.maker().prune_clips()
        self.assertTrue(clip.exists())
        self.store.db.execute("UPDATE bw_queue SET error='HTTP 400'")
        self.maker().prune_clips()
        self.assertFalse(clip.exists())

    def test_no_clip_is_kept_past_the_final_limit(self):
        job = add_job(self.store, self.START, self.START + 8)
        clip = self.old_clip(job, 'delivery_unknown')
        self.clock.now += 6 * 86400
        self.maker().prune_clips()
        self.assertFalse(clip.exists())

    def test_clip_is_kept_for_the_first_24_hours(self):
        job = add_job(self.store, self.START, self.START + 8)
        clip = self.old_clip(job, 'delivered')
        os.utime(clip, (self.clock.now - 23 * 3600, self.clock.now - 23 * 3600))
        self.maker().prune_clips()
        self.assertTrue(clip.exists())

    def test_old_clip_is_deleted_once_delivered(self):
        job = add_job(self.store, self.START, self.START + 8)
        clip = self.old_clip(job, 'delivered')
        self.maker().prune_clips()
        self.assertFalse(clip.exists())

    def types(self):
        return [body['type'] for body in self.queued()]

    def test_stopped_recording_alerts_once_and_recovery_is_reported(self):
        maker = self.maker(stopped_after=90)
        self.segment(100)
        self.clock.now = self.START + 150
        maker.check_health()
        self.assertEqual(self.types(), [])
        self.clock.now = self.START + 300
        maker.check_health()
        maker.check_health()
        self.assertEqual(self.types(), ['recording_stopped'])
        self.assertEqual(self.queued()[0]['destination'], '123@g.us')
        self.maker(stopped_after=90).check_health()
        self.assertEqual(self.types(), ['recording_stopped'])
        self.segment(301)
        maker.check_health()
        self.assertEqual(self.types(), ['recording_stopped', 'recording_restored'])

    def test_low_disk_alerts_once(self):
        maker = self.maker(disk_free=lambda folder: 1_000_000_000, min_free_gb=5)
        maker.check_health()
        maker.check_health()
        self.assertEqual(self.types(), ['disk_low'])

    def test_low_disk_removes_oldest_video_but_keeps_the_last_hour(self):
        for offset in range(0, 3700, 10):
            self.segment(offset)
        recorder = Recorder('rtsp://x', self.segments, 'ffmpeg', retention_hours=99999,
                            clock=lambda: self.START + 3700, disk_free=lambda folder: 0)
        self.assertEqual(recorder.prune(), 10)
        self.assertEqual(len(list_segments(self.segments)), 360)

    def test_stalled_recording_is_detected(self):
        self.segment(0)
        recorder = Recorder('rtsp://x', self.segments, 'ffmpeg', clock=lambda: self.START + 200)
        self.assertFalse(recorder.stalled(self.START + 150))
        self.assertTrue(recorder.stalled(self.START + 100))

    def test_half_made_files_are_removed_on_start(self):
        self.clips.mkdir()
        leftover, finished = self.clips / 'job-3.working.ts', self.clips / 'clip-3.mp4'
        leftover.write_bytes(b'x')
        finished.write_bytes(b'x')
        self.maker().clean_leftovers()
        self.assertFalse(leftover.exists())
        self.assertTrue(finished.exists())

    def test_local_only_building_keeps_clip_off_whatsapp(self):
        self.segment(0)
        add_job(self.store, self.START, self.START + 8, destination=None)
        self.maker().run_once()
        row = self.store.db.execute('SELECT status FROM bw_queue').fetchone()
        self.assertEqual(row['status'], 'local_only')

    def test_old_video_is_deleted_but_other_files_are_not(self):
        self.segment(0)
        self.segment(50000)
        (self.segments / 'notes.ts').write_bytes(b'keep')
        recorder = Recorder('rtsp://x', self.segments, 'ffmpeg', retention_hours=1, clock=lambda: self.START + 50001)
        self.assertEqual(recorder.prune(), 1)
        self.assertEqual(len(list_segments(self.segments)), 1)
        self.assertTrue((self.segments / 'notes.ts').exists())

    def test_recording_has_no_audio_and_restarts_never_log_the_address(self):
        source = 'rtsp://admin:' + PASSWORD + '@camera/101'
        logs = []
        stop = threading.Event()
        self.addCleanup(stop.set)
        false = shutil.which('false')
        recorder = Recorder(source, self.segments, false, log=logs.append)
        self.assertIn('-an', recorder.command())
        self.assertEqual(recorder.command()[recorder.command().index('-rtsp_transport') + 1], 'tcp')
        thread = threading.Thread(target=recorder.run, args=(stop,), kwargs=dict(restart_seconds=0.01), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: logs, 5))
        stop.set()
        thread.join(10)
        self.assertNotIn(PASSWORD, ' '.join(logs))


class FakeResponse:
    def __init__(self, text='', stop_reason='end_turn'):
        self.stop_reason = stop_reason
        self.content = [types.SimpleNamespace(type='text', text=text)]


class FakeClient:
    def __init__(self, response):
        self.response, self.calls = response, []
        self.beta = types.SimpleNamespace(messages=self)

    def create(self, **options):
        self.calls.append(options)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ReviewerTests(unittest.TestCase):
    def fake_frames(self, command, **options):
        for number in (1, 2):
            Path(command[-1].replace('%02d', '0' + str(number))).write_bytes(b'jpeg')

    def review(self, response, run=None):
        self.client = FakeClient(response)
        return ClipReviewer('ffmpeg', client=self.client, run_command=run or self.fake_frames).review('clip.mp4')

    def test_valid_answer_and_request_shape(self):
        verdict = self.review(FakeResponse('{"level": 2, "summary": "Person  forcing   a door."}'))
        self.assertEqual(verdict, dict(level=2, summary='Person forcing a door.'))
        call = self.client.calls[0]
        self.assertEqual(call['model'], 'claude-opus-5-5')
        self.assertEqual(call['fallbacks'], 'default')
        self.assertEqual(call['betas'], ['server-side-fallback-2026-07-01'])
        self.assertEqual(len([b for b in call['messages'][0]['content'] if b['type'] == 'image']), 2)
        self.assertNotIn('thinking', call)

    def test_prompt_ignores_time_and_avoids_identifying_people(self):
        self.assertIn('do not make anything suspicious', DRAFT_PROMPT)
        self.assertIn('Do not identify anyone', DRAFT_PROMPT)

    def test_every_failure_becomes_unavailable(self):
        for response in (FakeResponse('', 'refusal'), FakeResponse('not json'), FakeResponse('{"level": 9, "summary": "x"}'),
                         FakeResponse('{"level": true, "summary": "x"}'), RuntimeError('network key=SECRET')):
            with self.subTest(str(response)):
                with self.assertRaises(ReviewUnavailable) as caught:
                    self.review(response)
                self.assertNotIn('SECRET', str(caught.exception))

    def test_no_frames_is_unavailable(self):
        with self.assertRaises(ReviewUnavailable):
            self.review(FakeResponse('{}'), run=lambda command, **options: None)

    def test_private_prompt_replaces_the_draft(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'ai_prompt.txt'
            self.assertEqual(load_prompt(path), DRAFT_PROMPT)
            path.write_text('My own rules')
            self.assertEqual(load_prompt(path), 'My own rules')


class StubReviewer:
    def __init__(self, level=None):
        self.level, self.seen = level, 0

    def review(self, clip):
        self.seen += 1
        if self.level is None:
            raise ReviewUnavailable('down')
        return dict(level=self.level, summary='Person at the door.')


class AiClipTests(ClipMakerTests):
    def job(self, kind='activity_review'):
        self.segment(0)
        job = add_job(self.store, self.START, self.START + 8)
        self.store.db.execute('UPDATE bw_media SET body=json_set(body,"$.type",?) WHERE id=?', (kind, job))
        return job

    def test_low_levels_stay_off_whatsapp_and_the_clip_is_removed(self):
        for level in (0, 1):
            with self.subTest(level):
                self.store = Store()
                job = self.job()
                self.maker(reviewer=StubReviewer(level)).run_once()
                self.assertEqual(self.status(job), 'ai_cleared')
                self.assertEqual(self.queued(), [])
                self.assertFalse((self.clips / 'clip-{}.mp4'.format(job)).exists())

    def test_level_two_and_up_is_sent_with_the_reason(self):
        job = self.job()
        self.maker(reviewer=StubReviewer(2)).run_once()
        self.assertEqual(self.status(job), 'attached')
        self.assertIn('AI level 2: Person at the door.', self.queued()[0]['text'])

    def test_failed_check_still_sends_the_clip(self):
        self.job()
        self.maker(reviewer=StubReviewer(None)).run_once()
        self.assertIn('AI check unavailable', self.queued()[0]['text'])

    def test_other_clips_skip_the_ai(self):
        reviewer = StubReviewer(0)
        job = self.job('late_camera_event')
        self.maker(reviewer=reviewer).run_once()
        self.assertEqual(reviewer.seen, 0)
        self.assertEqual(self.status(job), 'attached')

    def test_daily_limit_stops_paid_checks_and_alerts_once(self):
        reviewer = StubReviewer(0)
        maker = self.maker(reviewer=reviewer, ai_daily_limit=1)
        first, second, third = self.job(), None, None
        second = add_job(self.store, self.START + 1, self.START + 8)
        third = add_job(self.store, self.START + 2, self.START + 8)
        for job in (second, third):
            self.store.db.execute('UPDATE bw_media SET body=json_set(body,"$.type","activity_review") WHERE id=?', (job,))
        maker.run_once()
        self.assertEqual(reviewer.seen, 1)
        self.assertEqual(self.status(first), 'ai_cleared')
        self.assertEqual(self.status(third), 'ai_skipped')
        self.assertEqual([body['type'] for body in self.queued()], ['ai_limit_reached'])


class AiRuleTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine([config('office-1', ai_review=True)])

    def send(self, offset):
        at = T + 5 * 86400 + offset
        self.engine.intake.ingest(dict(id='e' + str(offset), building='office-1', type='activity',
                                       at=timestamp(at), detection='motion'), at)
        self.engine.advance(at + 6)

    def test_time_of_day_creates_no_alerts_and_clips_are_rate_limited(self):
        self.send(0)
        self.send(30)
        self.send(70)
        self.assertEqual([job['type'] for job in self.engine.media_jobs()], ['activity_review'] * 2)
        self.assertEqual(self.engine.alerts(), [])
        self.assertFalse(self.engine.store.state('office-1').get('suspicious'))


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.engine = Engine([config('office-1')])

    def run_event(self, detection):
        event = dict(id='e1', building='office-1', type='activity', at=timestamp(T + 3600 * 24 * 5))
        if detection:
            event['detection'] = detection
        self.engine.intake.ingest(event, T + 3600 * 24 * 5)
        self.engine.advance(T + 3600 * 24 * 5 + 10, flush=True)
        return self.engine.alerts()

    def test_motion_is_never_described_as_a_person(self):
        alerts = self.run_event('motion')
        text = ' '.join(alert['text'] for alert in alerts)
        self.assertIn('first_motion', [alert['type'] for alert in alerts])
        self.assertIn('motion', text.lower())
        self.assertNotIn('person detected', text.lower())
        self.assertNotIn('Person activity', text)
        state = self.engine.store.state('office-1')
        self.assertEqual(state['occupancy'], 'motion_detected')

    def test_camera_without_a_label_keeps_person_wording(self):
        alerts = self.run_event(None)
        self.assertIn('first_arrival', [alert['type'] for alert in alerts])

    def test_bad_label_is_rejected(self):
        event = dict(id='e2', building='office-1', type='activity', at=timestamp(T), detection='ghost')
        self.assertEqual(self.engine.intake.ingest(event, T), 'invalid')

    def test_only_person_event_types_may_claim_a_person(self):
        self.assertEqual(AnnkeClient('h', 'u', 'p').detection, 'motion')
        self.assertEqual(AnnkeClient('h', 'u', 'p', event_types=['humanRecognition']).detection, 'person')
        self.assertEqual(AnnkeClient('h', 'u', 'p', event_types=['humanRecognition', 'VMD']).detection, 'motion')

    def test_connector_labels_activity_events(self):
        spool = Path(self.folder.name) / 'events.jsonl'

        class Camera:
            def usable_frame(self):
                return True

            def person_detected(self):
                return True

        Connector(Camera(), 'office-1', Spool(spool), clock=lambda: 5000.0, detection='motion').poll_once()
        events = [json.loads(line) for line in spool.read_text().splitlines()]
        self.assertNotIn('detection', events[0])
        self.assertEqual(events[1]['detection'], 'motion')

    def test_dashboard_accepts_every_recorder_status(self):
        schema = Path('dashboard/lib/status-schema.mjs').read_text()
        for status in ('awaiting_recording', 'attached', 'no_footage', 'clip_failed', 'motion_detected'):
            self.assertIn('"{}"'.format(status), schema)


class DoctorTests(unittest.TestCase):
    PASSWORD_URL = 'rtsp://admin:SECRET@camera/101'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.local = Path(folder.name)
        self.lines, self.sent, self.commands = [], [], []
        self.stream_ok, self.camera_ok = True, True
        doctor = self

        class Provider:
            def request(self, path):
                doctor.sent.append(path)
                return {}

            def send(self, body):
                doctor.sent.append(body)
                return {'sent': True}

        self.provider = Provider()

    def configure(self, destination='12345678901@g.us', credentials=True):
        (self.local / 'camera.json').write_text(json.dumps(dict(
            brand='annke', building='a', host='192.168.1.64', scheme='http', username='admin',
            password='SECRET', channel=1)))
        (self.local / 'buildings.json').write_text(json.dumps(dict(buildings=[
            dict(config('a'), destination=destination)])))
        if credentials:
            (self.local / 'whapi.json').write_text('{"token":"fake"}')

    def fake_run(self, command, **options):
        self.commands.append(command)
        if not self.stream_ok:
            return subprocess.CompletedProcess(command, 1, stderr='failed ' + self.PASSWORD_URL)
        Path(command[-1]).write_bytes(b'video')
        return subprocess.CompletedProcess(command, 0, stderr='Stream #0:0: Video: hevc (Main), yuv420p, 3840x2160')

    def check(self, settings):
        if not self.camera_ok:
            raise CameraError('Camera could not be reached')
        return ['Signed in to the camera: C800', 'Live picture: usable', 'Detection events: connected.']

    def doctor(self, **options):
        options.setdefault('ffmpeg', 'ffmpeg')
        return Doctor(self.local, check_camera=self.check, source=self.PASSWORD_URL, provider=self.provider,
                      run_command=self.fake_run, disk_free=lambda folder: 50_000_000_000,
                      log=self.lines.append, **options)

    def output(self):
        return '\n'.join(self.lines)

    def test_all_good_is_ready_and_sends_nothing(self):
        self.configure()
        self.assertTrue(self.doctor().run())
        self.assertIn('hevc 3840x2160', self.output())
        self.assertIn('Ready.', self.output())
        self.assertEqual(self.sent, ['/health'])

    def test_send_test_sends_exactly_one_clip_to_the_group(self):
        self.configure()
        self.assertTrue(self.doctor().run(send_test=True))
        bodies = [item for item in self.sent if isinstance(item, dict)]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['destination'], '12345678901@g.us')

    def test_unreachable_camera_fails_with_advice_and_skips_video(self):
        self.configure()
        self.camera_ok = False
        self.assertFalse(self.doctor().run(send_test=True))
        self.assertIn('same network', self.output())
        self.assertIn('SKIP', self.output())
        self.assertEqual(self.commands, [])
        self.assertEqual([item for item in self.sent if isinstance(item, dict)], [])

    def test_stream_failure_never_prints_the_password(self):
        self.configure()
        self.stream_ok = False
        self.assertFalse(self.doctor().run())
        self.assertIn('no video received', self.output())
        self.assertNotIn('SECRET', self.output())

    def test_missing_setup_is_listed_step_by_step(self):
        self.assertFalse(self.doctor(ffmpeg='').run())
        for text in ('brew install ffmpeg', 'camera_setup.py', 'destination', 'whatsapp_setup.py'):
            self.assertIn(text, self.output())

    def test_bad_group_id_is_a_failure_not_a_crash(self):
        self.configure()
        path = self.local / 'buildings.json'
        path.write_text(path.read_text().replace('12345678901@g.us', '123'))
        self.assertFalse(self.doctor().run())
        self.assertIn('Destination must be', self.output())

    def test_missing_whatsapp_group_fails(self):
        self.configure(destination=None)
        self.assertFalse(self.doctor().run())
        self.assertIn('set destination', self.output())


@unittest.skipUnless(find_ffmpeg(), 'ffmpeg is not installed')
class RealVideoTest(unittest.TestCase):
    def test_live_stream_to_whatsapp_request(self):
        """Real ffmpeg: H.265 stream -> rolling segments -> H.264 clip -> WhatsApp video request."""
        ffmpeg = find_ffmpeg()
        encoders = subprocess.run([ffmpeg, '-hide_banner', '-encoders'], capture_output=True, text=True).stdout
        encoder = 'libx265' if 'libx265' in encoders else 'libx264'
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.server_close()
            source = subprocess.Popen(
                [ffmpeg, '-nostdin', '-loglevel', 'quiet', '-re', '-f', 'lavfi', '-i',
                 'testsrc=size=1920x1080:rate=10', '-t', '30', '-c:v', encoder, '-preset', 'ultrafast', '-g', '10',
                 '-f', 'mpegts', 'tcp://127.0.0.1:{}?listen=1'.format(port)])
            self.addCleanup(source.kill)
            self.addCleanup(source.wait)
            time.sleep(1)
            stop = threading.Event()
            recorder = Recorder('tcp://127.0.0.1:{}'.format(port), root / 'recordings', ffmpeg,
                                segment_seconds=2, log=lambda text: None)
            self.addCleanup(stop.set)
            thread = threading.Thread(target=recorder.run, args=(stop,), daemon=True)
            thread.start()
            time.sleep(16)
            stop.set()
            thread.join(15)
            footage = list_segments(root / 'recordings', 2)
            self.assertGreaterEqual(len(footage), 4)

            store = Store()
            job = add_job(store, footage[0][0] + 1, footage[0][0] + 6)
            maker = ClipMaker(store, 'office-1', root / 'recordings', root / 'clips', ffmpeg, segment_seconds=2,
                              settle_seconds=0, log=print)
            self.assertEqual(maker.run_once(), [job])
            body = json.loads(store.db.execute('SELECT body FROM bw_queue').fetchone()['body'])
            clip = Path(body['media_path'])
            details = subprocess.run([ffmpeg, '-hide_banner', '-i', str(clip)], capture_output=True, text=True).stderr
            self.assertIn('h264', details)
            self.assertIn('yuv420p', details)
            self.assertIn('1280x720', details)
            seconds = re.search(r'Duration: 00:00:(\d+\.\d+)', details)
            self.assertAlmostEqual(float(seconds.group(1)), 8, delta=1.5)  # 5 requested, starting 1 second in, plus 2 of padding

            (root / 'whapi.json').write_text('{"token":"fake"}')
            provider = Whapi(root / 'whapi.json')
            sent = []
            provider.request = lambda path, payload: sent.append((path, payload)) or {'sent': True}
            provider.send(body)
            self.assertEqual(sent[0][0], '/messages/video')
            self.assertEqual(sent[0][1]['to'], '123@g.us')
            self.assertEqual(sent[0][1]['mime_type'], 'video/mp4')
            self.assertTrue(sent[0][1]['media'].startswith('data:video/mp4;'))
            self.assertIn('Part 1.', sent[0][1]['caption'])
            self.assertNotIn('incomplete', sent[0][1]['caption'])


if __name__ == '__main__':
    unittest.main()
