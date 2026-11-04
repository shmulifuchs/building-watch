"""Reolink connector tests against a simulated camera (no real camera or network needed)."""
import io
import json
import os
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from buildingwatch.reolink import (CameraError, CameraUnsupported, CertificateMismatch, Connector,
                                   ReolinkClient, Spool, certificate_fingerprint, load_camera_settings)
from buildingwatch.service import Engine, JsonlInput
from camera_setup import check_camera, save_settings
from test_building_watch import T, config

PASSWORD = 'FAKE-CAMERA-PASSWORD'
JPEG = b'\xff\xd8' + b'\x00' * 4000 + b'\xff\xd9'


class FakeCamera:
    """Minimal Reolink HTTP API: Login, GetAiState, Snap."""
    def __init__(self, tls_files=None):
        self.person = False
        self.people_supported = True
        self.frame = JPEG
        self.lease = 3600
        self.tokens = set()
        self.logins = 0
        self.requests = []
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, payload, kind='application/json'):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def error(self, command, code):
                self.reply([{'cmd': command, 'code': 1, 'error': {'detail': 'error', 'rspCode': code}}])

            def do_POST(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                camera.requests.append((self.path, body))
                command = query['cmd'][0]
                if command == 'Login':
                    user = body[0]['param']['User']
                    if user['userName'] != 'admin' or user['password'] != PASSWORD:
                        return self.error('Login', -7)
                    camera.logins += 1
                    token = 'tok{}'.format(camera.logins)
                    camera.tokens.add(token)
                    return self.reply([{'cmd': 'Login', 'code': 0,
                                        'value': {'Token': {'name': token, 'leaseTime': camera.lease}}}])
                if query.get('token', [None])[0] not in camera.tokens:
                    return self.error(command, -6)
                if command == 'GetAiState':
                    people = {'alarm_state': int(camera.person), 'support': int(camera.people_supported)}
                    return self.reply([{'cmd': 'GetAiState', 'code': 0,
                                        'value': {'channel': 0, 'people': people,
                                                  'vehicle': {'alarm_state': 0, 'support': 1}}}])
                self.error(command, -9)

            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                camera.requests.append((self.path, None))
                if query.get('token', [None])[0] not in camera.tokens:
                    return self.error('Snap', -6)
                self.reply(camera.frame, 'image/jpeg')

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # clients that hang up early (e.g. a refused certificate) are expected

        self.server = Server(('127.0.0.1', 0), Handler)
        if tls_files:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls_files)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def expire_sessions(self):
        self.tokens.clear()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.camera = FakeCamera()
        self.temp = tempfile.TemporaryDirectory()
        self.spool_path = Path(self.temp.name) / 'camera-events.jsonl'
        self.clock = Clock(T)
        self.logs = []
        self.client = ReolinkClient('127.0.0.1', 'admin', PASSWORD, scheme='http',
                                    port=self.camera.port, clock=self.clock, timeout=2)
        self.connector = Connector(self.client, 'a', Spool(self.spool_path), clock=self.clock,
                                   log=self.logs.append)

    def tearDown(self):
        self.camera.close()
        self.temp.cleanup()

    def events(self):
        if not self.spool_path.exists():
            return []
        return [json.loads(line) for line in self.spool_path.read_text().splitlines()]

    def poll(self, seconds=1, times=1):
        written = []
        for _ in range(times):
            written += self.connector.poll_once()
            self.clock.at += seconds
        return written

    def test_first_poll_reports_a_usable_frame_and_nobody_present(self):
        written = self.poll()
        self.assertEqual([e['type'] for e in written], ['heartbeat'])
        self.assertEqual(written[0]['building'], 'a')

    def test_heartbeats_are_spaced_well_inside_the_service_threshold(self):
        self.poll(times=60)
        beats = [e for e in self.events() if e['type'] == 'heartbeat']
        self.assertEqual(len(beats), 3)  # every 20 s; service threshold is 60 s

    def test_person_in_view_is_reported_every_ten_seconds_not_every_poll(self):
        self.poll()
        self.camera.person = True
        self.poll(times=25)
        self.camera.person = False
        self.poll(times=5)
        self.camera.person = True
        self.poll()
        activity = [e for e in self.events() if e['type'] == 'activity']
        self.assertEqual(len(activity), 3 + 1)  # 0 s, 10 s, 20 s, then the new sighting

    def test_event_ids_are_unique_and_events_pass_service_validation(self):
        self.camera.person = True
        self.poll(times=30)
        events = self.events()
        self.assertEqual(len({e['id'] for e in events}), len(events))
        engine = Engine([config()], ':memory:')
        try:
            results = [engine.ingest(e, T + 60) for e in events]
            self.assertTrue(all(r == 'buffered' for r in results), results)
        finally:
            engine.close()

    def test_expired_session_logs_in_again_without_losing_a_poll(self):
        self.poll()
        self.camera.expire_sessions()
        self.camera.person = True
        written = self.poll()
        self.assertEqual([e['type'] for e in written], ['activity'])
        self.assertEqual(self.camera.logins, 2)

    def test_session_is_renewed_before_its_lease_runs_out(self):
        self.camera.lease = 120
        self.poll()
        self.clock.at += 100
        self.poll()
        self.assertEqual(self.camera.logins, 2)

    def test_password_and_tokens_never_appear_in_urls_or_logs(self):
        self.poll(times=3)
        self.camera.close()
        self.poll(times=4)
        for path, _ in self.camera.requests:
            self.assertNotIn(PASSWORD, path)
        text = '\n'.join(self.logs)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn('tok1', text)

    def test_wrong_password_is_reported_as_a_camera_problem(self):
        client = ReolinkClient('127.0.0.1', 'admin', 'wrong', scheme='http', port=self.camera.port)
        with self.assertRaisesRegex(CameraError, 'rejected the login'):
            client.login()

    def test_unusable_frame_does_not_count_as_a_heartbeat(self):
        for frame in (b'not a picture' * 400, b'\xff\xd8\xff\xd9', JPEG[:-2] + b'\x00\x00\x00'):
            self.camera.frame = frame
            self.assertEqual(self.poll(), [])
        self.assertEqual(len(self.logs), 3)
        self.camera.frame = JPEG
        self.assertEqual([e['type'] for e in self.poll()], ['heartbeat'])

    def test_camera_without_person_detection_stops_with_a_clear_reason(self):
        self.camera.people_supported = False
        with self.assertRaises(CameraUnsupported):
            self.poll()

    def test_outage_writes_one_offline_event_and_recovery_reports_immediately(self):
        self.poll()
        self.camera.close()
        written = self.poll(times=6)
        self.assertEqual([e['type'] for e in written], ['offline'])
        self.assertEqual(len(self.logs), 2)  # first failure and offline report, not one per poll
        self.camera = FakeCamera()
        self.client.port = self.camera.port
        self.client.token = None
        self.assertEqual([e['type'] for e in self.poll()], ['heartbeat'])

    def test_end_to_end_with_the_monitoring_service(self):
        engine = Engine([config()], ':memory:', reorder_seconds=0)
        source = JsonlInput(engine, self.spool_path)
        try:
            self.poll(times=5)
            self.camera.person = True
            self.poll(times=5)
            self.camera.person = False
            self.poll(times=5)
            engine.advance(source.read(self.clock.at))
            self.camera.close()
            self.poll(times=150)  # camera unreachable for 150 s
            engine.advance(source.read(self.clock.at))
            self.camera = FakeCamera()
            self.client.port, self.client.token = self.camera.port, None
            self.poll(times=2)
            engine.advance(source.read(self.clock.at))
            kinds = [a['type'] for a in engine.alerts()]
            self.assertEqual(kinds.count('first_arrival'), 1)
            self.assertEqual(kinds.count('camera_offline'), 1)
            self.assertEqual(kinds.count('camera_recovered'), 1)
            self.assertEqual(engine.store.state('a')['occupancy'], 'person_detected')
            audit = [row['kind'] for row in engine.db.execute('SELECT kind FROM bw_audit')]
            self.assertFalse([k for k in audit if k.startswith('quarantine')], audit)
        finally:
            engine.close()


@unittest.skipUnless(shutil.which('openssl'), 'openssl is needed to make a test certificate')
class HttpsPinningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        key, cert = (str(Path(self.temp.name) / n) for n in ('key.pem', 'cert.pem'))
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=camera', '-keyout', key, '-out', cert],
                       check=True, capture_output=True)
        self.camera = FakeCamera((cert, key))

    def tearDown(self):
        self.camera.close()
        self.temp.cleanup()

    def test_saved_fingerprint_is_accepted(self):
        pin = certificate_fingerprint('127.0.0.1', self.camera.port)
        client = ReolinkClient('127.0.0.1', 'admin', PASSWORD, port=self.camera.port, fingerprint_sha256=pin)
        self.assertTrue(client.usable_frame())

    def test_different_certificate_is_refused_before_the_password_is_sent(self):
        client = ReolinkClient('127.0.0.1', 'admin', PASSWORD, port=self.camera.port,
                               fingerprint_sha256='00' * 32)
        with self.assertRaises(CertificateMismatch):
            client.login()
        self.assertEqual(self.camera.requests, [])

    def test_setup_check_reports_camera_status(self):
        pin = certificate_fingerprint('127.0.0.1', self.camera.port)
        lines = check_camera(dict(building='a', host='127.0.0.1', port=self.camera.port, username='admin',
                                  password=PASSWORD, fingerprint_sha256=pin))
        self.assertIn('Live picture: usable', lines)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'camera.json'

    def tearDown(self):
        self.temp.cleanup()

    def settings(self, **extra):
        return dict(dict(building='a', host='192.168.1.50', username='admin', password=PASSWORD,
                         fingerprint_sha256='ab' * 32), **extra)

    def test_saved_settings_are_owner_only_and_not_overwritten(self):
        save_settings(self.path, self.settings())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with self.assertRaises(ValueError):
            save_settings(self.path, self.settings(host='192.168.1.99'))
        self.assertEqual(load_camera_settings(self.path)['host'], '192.168.1.50')
        save_settings(self.path, self.settings(host='192.168.1.99'), replace=True)
        self.assertEqual(load_camera_settings(self.path)['host'], '192.168.1.99')

    def test_https_without_a_saved_fingerprint_is_rejected(self):
        save_settings(self.path, {k: v for k, v in self.settings().items() if k != 'fingerprint_sha256'})
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            load_camera_settings(self.path)

    def test_unknown_fields_are_rejected(self):
        save_settings(self.path, self.settings(actor='manager'))
        with self.assertRaises(ValueError):
            load_camera_settings(self.path)

    def test_camera_folder_is_ignored_by_git(self):
        ignored = Path(__file__).with_name('.gitignore').read_text().split()
        self.assertIn('.local/', ignored)


if __name__ == '__main__':
    unittest.main()
