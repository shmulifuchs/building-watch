"""Reolink camera connector: turns camera person detection and live snapshots into spool events.

Standard library only. The connector polls the camera's local HTTP(S) API:
  * Login             -> session token (password is sent in the request body, never the URL)
  * GetAiState        -> people.alarm_state, the camera's own person detection
  * Snap              -> a live JPEG; a complete JPEG counts as a usable frame (heartbeat)

It appends one JSON line per event to the service's append-only spool. Events are
time-stamped with this computer's clock, so the camera clock does not need to match.
It never logs credentials, session tokens or request URLs.
"""
import hashlib
import http.client
import json
import os
import secrets
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

LOGIN_EXPIRED_CODES = {-6, -5}  # "please login first" / session invalid
JPEG_START, JPEG_END = b'\xff\xd8', b'\xff\xd9'
CAMERA_FIELDS = {'building', 'host', 'port', 'scheme', 'username', 'password', 'channel',
                 'fingerprint_sha256', 'min_frame_bytes', 'brand', 'event_types',
                 'retention_hours', 'rtsp_port', 'stream'}


class CameraError(Exception):
    """The camera could not be reached or returned an unusable answer."""


class CameraUnsupported(CameraError):
    """The camera does not offer person detection through its API."""


class CertificateMismatch(CameraError):
    """The HTTPS certificate does not match the one saved during setup."""


class LoginExpired(CameraError):
    """The camera session ended; log in again."""


def timestamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def certificate_fingerprint(host, port=443, timeout=5):
    """Fetch the camera's self-signed certificate fingerprint (trust on first use)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def load_camera_settings(path):
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or set(raw) - CAMERA_FIELDS:
        raise ValueError('Camera settings contain unsupported fields')
    for key in ('building', 'host', 'username', 'password'):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError('Camera settings need ' + key)
    if raw.get('brand', 'reolink') not in ('reolink', 'annke'):
        raise ValueError('Camera brand must be reolink or annke')
    scheme = raw.get('scheme', 'https')
    if scheme not in ('http', 'https'):
        raise ValueError('Camera scheme must be http or https')
    port = raw.get('port', 443 if scheme == 'https' else 80)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError('Camera port must be a number from 1 to 65535')
    channel = raw.get('channel', 0)
    if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
        raise ValueError('Camera channel must be a whole number, usually 0')
    if scheme == 'https' and not raw.get('fingerprint_sha256'):
        raise ValueError('HTTPS camera settings need the certificate fingerprint saved by camera_setup.py')
    return raw


class ReolinkClient:
    def __init__(self, host, username, password, scheme='https', port=None, channel=0,
                 fingerprint_sha256=None, timeout=5, min_frame_bytes=2048, clock=time.time):
        if scheme not in ('http', 'https'):
            raise ValueError('Camera scheme must be http or https')
        if scheme == 'https' and not fingerprint_sha256:
            raise ValueError('HTTPS requires a saved certificate fingerprint')
        self.host, self.scheme = host, scheme
        self.port = port or (443 if scheme == 'https' else 80)
        self.username, self.password = username, password
        self.channel, self.timeout = int(channel), timeout
        self.fingerprint = (fingerprint_sha256 or '').lower().replace(':', '')
        self.min_frame_bytes = min_frame_bytes
        self.clock = clock
        self.token, self.token_expires = None, 0
        self.login_paused_until = 0

    # -- transport -------------------------------------------------------------
    def _connection(self):
        if self.scheme == 'http':
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.connect()
            return conn
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # cameras use self-signed certificates; pinned below
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        conn.connect()
        presented = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if not secrets.compare_digest(presented, self.fingerprint):
            conn.close()
            raise CertificateMismatch('Camera certificate changed; rerun camera_setup.py if this was expected')
        return conn

    def _request(self, method, path, query, body=None):
        try:
            conn = self._connection()
        except CertificateMismatch:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise CameraError('Camera unreachable: ' + type(error).__name__) from None
        try:
            payload = None if body is None else json.dumps(body).encode()
            headers = {'Content-Type': 'application/json'} if payload else {}
            conn.request(method, path + '?' + urlencode(query), body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read(20_000_000)
            if response.status != 200:
                raise CameraError('Camera answered HTTP {}'.format(response.status))
            return response.getheader('Content-Type', ''), data
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise CameraError('Camera connection failed: ' + type(error).__name__) from None
        finally:
            conn.close()

    @staticmethod
    def _value(data, command):
        try:
            answer = json.loads(data)
        except ValueError:
            raise CameraError(command + ' returned an unreadable answer') from None
        item = answer[0] if isinstance(answer, list) and answer else answer
        if not isinstance(item, dict):
            raise CameraError(command + ' returned an unexpected answer')
        if item.get('code', 0) != 0 or 'error' in item:
            code = (item.get('error') or {}).get('rspCode')
            if code in LOGIN_EXPIRED_CODES:
                raise LoginExpired(command + ' needs a new login')
            raise CameraError('{} failed (camera code {})'.format(command, code))
        value = item.get('value')
        return value if isinstance(value, dict) else {}

    # -- API -------------------------------------------------------------------
    def login(self):
        # Reolink cameras lock the account after repeated failed logins; never hammer them.
        if self.clock() < self.login_paused_until:
            raise CameraError('Camera login paused after a rejected password; retrying in a minute')
        body = [{'cmd': 'Login', 'action': 0,
                 'param': {'User': {'Version': '0', 'userName': self.username, 'password': self.password}}}]
        _, data = self._request('POST', '/api.cgi', {'cmd': 'Login'}, body)
        try:
            value = self._value(data, 'Login')
        except CameraError:
            value = {}
        token = (value.get('Token') or {})
        if not token.get('name'):
            self.login_paused_until = self.clock() + 60
            raise CameraError('Camera rejected the login; check the username and password')
        self.token = token['name']
        lease = token.get('leaseTime', 3600)
        lease = lease if isinstance(lease, (int, float)) and lease > 0 else 3600
        self.token_expires = self.clock() + max(lease - 60, 30)

    def _authorized(self, make_request):
        if not self.token or self.clock() >= self.token_expires:
            self.login()
        try:
            return make_request()
        except LoginExpired:
            self.token = None
            self.login()
            return make_request()

    def _command(self, command):
        def call():
            body = [{'cmd': command, 'action': 0, 'param': {'channel': self.channel}}]
            _, data = self._request('POST', '/api.cgi', {'cmd': command, 'token': self.token}, body)
            return self._value(data, command)
        return self._authorized(call)

    def person_detected(self):
        people = self._command('GetAiState').get('people')
        if not isinstance(people, dict) or not people.get('support', 1):
            raise CameraUnsupported('This camera does not report person detection')
        return bool(people.get('alarm_state'))

    def usable_frame(self):
        def call():
            query = {'cmd': 'Snap', 'channel': self.channel,
                     'rs': secrets.token_hex(6), 'token': self.token}
            kind, data = self._request('GET', '/cgi-bin/api.cgi', query)
            if 'json' in kind or data[:1] in (b'[', b'{'):
                self._value(data, 'Snap')  # raises LoginExpired or CameraError
                raise CameraError('Snap returned no image')
            return data
        data = self._authorized(call)
        return (len(data) >= self.min_frame_bytes and data.startswith(JPEG_START)
                and data.rstrip(b'\x00').endswith(JPEG_END))


class Spool:
    """Append complete JSON lines; one write per line so the reader never sees a mixed line."""
    def __init__(self, path):
        self.path = Path(path)

    def append(self, event):
        line = (json.dumps(event, separators=(',', ':')) + '\n').encode()
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


class Connector:
    """Polls one camera for one building and writes heartbeat/activity/offline events."""
    def __init__(self, client, building, spool, clock=time.time, heartbeat_every=20,
                 activity_every=10, offline_after_failures=3, log=print, id_prefix='reolink'):
        self.client, self.building, self.spool = client, building, spool
        self.clock, self.log = clock, log
        self.heartbeat_every, self.activity_every = heartbeat_every, activity_every
        self.offline_after_failures = offline_after_failures
        self.id_prefix = id_prefix
        self.run_id = secrets.token_hex(4)
        self.sequence = 0
        self.last_heartbeat = self.last_activity = None
        self.person = False
        self.failures = 0
        self.offline_written = False
        self.bad_frames = 0

    def _write(self, kind, at, reason=None):
        self.sequence += 1
        event = {'id': '{}-{}-{}-{}'.format(self.id_prefix, self.run_id, self.sequence, kind),
                 'building': self.building, 'type': kind, 'at': timestamp(at)}
        if reason:
            event['reason'] = reason
        self.spool.append(event)
        return event

    def poll_once(self):
        """One poll. Returns the events written."""
        written = []
        now = self.clock()
        try:
            if self.last_heartbeat is None or now - self.last_heartbeat >= self.heartbeat_every:
                if self.client.usable_frame():
                    if self.bad_frames:
                        self.log('Usable picture again after {} unusable frames.'.format(self.bad_frames))
                    self.bad_frames = 0
                    written.append(self._write('heartbeat', now))
                    self.last_heartbeat = now
                else:
                    # No heartbeat is written; the service raises the offline alert on its own.
                    self.bad_frames += 1
                    if self.bad_frames == 1 or self.bad_frames % 60 == 0:
                        self.log('Camera answered but the picture is not usable ({} in a row).'.format(self.bad_frames))
            present = self.client.person_detected()
        except CameraUnsupported:
            raise
        except CameraError as error:
            self.failures += 1
            # Log the first failure, the offline report, then once a minute, not every poll.
            if self.failures in (1, self.offline_after_failures) or self.failures % 60 == 0:
                self.log('Camera check failed ({} in a row): {}'.format(self.failures, error))
            if self.failures >= self.offline_after_failures and not self.offline_written:
                written.append(self._write('offline', now, 'Camera unreachable from the connector'))
                self.offline_written = True
                self.last_heartbeat = None  # report the next usable frame immediately
            return written
        if self.failures:
            self.log('Camera reachable again after {} failed checks.'.format(self.failures))
        self.failures, self.offline_written = 0, False
        if present and (not self.person or self.last_activity is None
                        or now - self.last_activity >= self.activity_every):
            written.append(self._write('activity', now))
            self.last_activity = now
        if not present:
            self.last_activity = None
        self.person = present
        return written

    def run(self, stop, interval=1):
        while not stop.is_set():
            self.poll_once()
            stop.wait(interval)
