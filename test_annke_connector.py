"""ANNKE connector and clip tests against a simulated camera (no real camera needed)."""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from buildingwatch.annke import AnnkeClient, camera_client
from buildingwatch.delivery import Whapi
from buildingwatch.recorder import ClipMaker, Recorder, find_ffmpeg, list_segments
from buildingwatch.reolink import (CameraError, CameraUnsupported, Connector, ReolinkClient, Spool,
                                   load_camera_settings)
from buildingwatch.store import Store
from camera_setup import check_camera

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


def add_job(store, start, end, destination='123@g.us'):
    body = dict(building_id='office-1', building_name='First Office', destination=destination,
                type='suspicious_activity', start=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                end=datetime.fromtimestamp(end, timezone.utc).isoformat(), duration_seconds=end - start,
                created_at=datetime.fromtimestamp(start, timezone.utc).isoformat(), part=1)
    return store.db.execute('INSERT INTO bw_media(building,key,body,status) VALUES(?,?,?,?)',
                            ('office-1', 'job:' + str(start), json.dumps(body), 'awaiting_recording')).lastrowid


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
        self.fail = False

    def fake_ffmpeg(self, command, **options):
        self.calls.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, 'ffmpeg')
        Path(command[-1]).write_bytes(b'fake mp4')

    def maker(self, **options):
        return ClipMaker(self.store, self.segments, self.clips, 'ffmpeg', clock=self.clock,
                         log=lambda text: None, run_command=self.fake_ffmpeg, **options)

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
        self.fail = True
        job = add_job(self.store, self.START, self.START + 8)
        maker = self.maker(max_attempts=3)
        for _ in range(4):
            maker.run_once()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.status(job), 'clip_failed')
        self.assertEqual(self.queued()[0]['type'], 'footage_unavailable')
        self.assertEqual([p for p in self.clips.iterdir()], [])

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
            maker = ClipMaker(store, root / 'recordings', root / 'clips', ffmpeg, segment_seconds=2,
                              settle_seconds=0, log=print)
            self.assertEqual(maker.run_once(), [job])
            body = json.loads(store.db.execute('SELECT body FROM bw_queue').fetchone()['body'])
            clip = Path(body['media_path'])
            details = subprocess.run([ffmpeg, '-hide_banner', '-i', str(clip)], capture_output=True, text=True).stderr
            self.assertIn('h264', details)
            self.assertIn('yuv420p', details)
            self.assertIn('1280x720', details)
            seconds = re.search(r'Duration: 00:00:(\d+\.\d+)', details)
            self.assertAlmostEqual(float(seconds.group(1)), 5, delta=1.5)

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


if __name__ == '__main__':
    unittest.main()
