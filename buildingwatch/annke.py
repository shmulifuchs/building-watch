"""ANNKE camera connector (C800 and other ISAPI models).

Standard library only. It speaks the same local camera API family as other ISAPI cameras:
  * /ISAPI/System/deviceInfo                  -> confirms the login and reads the model
  * /ISAPI/Streaming/channels/<id>/picture    -> a live JPEG; a complete JPEG is a heartbeat
  * /ISAPI/Event/notification/alertStream     -> long-lived stream of camera detection events

Requests use HTTP digest authentication, so the password itself is never sent over the
network. Credentials and request URLs are never logged. The class exposes the same
usable_frame()/person_detected() methods as ReolinkClient, so the existing Connector can
poll it unchanged.

Important: the standard C800 reports motion ("VMD"). That is movement, not a confirmed
person. Choose narrower event_types if the camera offers human detection.
"""
import http.client
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from buildingwatch.reolink import (JPEG_END, JPEG_START, CameraError, CameraUnsupported,
                                   ReolinkClient)

DEFAULT_EVENT_TYPES = ('vmd', 'fielddetection', 'linedetection', 'regionentrance')
MAX_RESPONSE_BYTES = 8_000_000
MAX_EVENT_BUFFER = 1_000_000
END_OF_ALERT = b'</EventNotificationAlert>'
EVENT_TYPE = re.compile(rb'<eventType>\s*([^<]+?)\s*</eventType>', re.I)
EVENT_STATE = re.compile(rb'<eventState>\s*([^<]+?)\s*</eventState>', re.I)
MODEL = re.compile(rb'<model>\s*([^<]+?)\s*</model>', re.I)
NETWORK_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)


class AnnkeClient:
    def __init__(self, host, username, password, scheme='http', port=None, channel=1, timeout=5,
                 min_frame_bytes=2048, clock=time.time, event_types=DEFAULT_EVENT_TYPES,
                 hold_seconds=5, down_after=30, retry_seconds=5, rtsp_port=554, stream='01'):
        if scheme != 'http':
            raise ValueError('ANNKE connector uses http with digest sign-in')
        if not isinstance(channel, int) or isinstance(channel, bool) or channel < 1:
            raise ValueError('ANNKE camera channel must be 1 or higher')
        if stream not in ('01', '02'):
            raise ValueError('Recording stream must be 01 (main) or 02 (sub)')
        self.host, self.port = host, port or 80
        self.username, self.password = username, password
        self.channel, self.timeout = channel, timeout
        self.min_frame_bytes, self.clock = min_frame_bytes, clock
        self.event_types = {str(item).lower() for item in event_types}
        self.hold_seconds, self.down_after = hold_seconds, down_after
        self.retry_seconds = retry_seconds
        self.rtsp_port, self.stream = rtsp_port, stream
        self.base = 'http://{}:{}'.format(host, self.port)
        self.login_paused_until = 0
        self.active_until = 0
        self.buffer = b''
        self.events_connected = False
        self.events_down_since = None
        self.events_unsupported = False
        self.closed = threading.Event()
        self.thread = None

    def _opener(self):
        # One opener per thread: digest handlers keep per-connection nonce state.
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, self.base, self.username, self.password)
        return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(passwords))

    def _rejected(self):
        # Cameras lock the account after repeated bad passwords; never hammer them.
        self.login_paused_until = self.clock() + 60
        return CameraError('Camera rejected the login; check the username and password')

    def _get(self, path):
        if self.clock() < self.login_paused_until:
            raise CameraError('Camera login paused after a rejected password; retrying in a minute')
        try:
            with self._opener().open(self.base + path, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise CameraError('Camera answer was too large')
                return response.headers.get('Content-Type', ''), data
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise self._rejected() from None
            raise CameraError('Camera refused the request (HTTP {})'.format(error.code)) from None
        except NETWORK_ERRORS:
            raise CameraError('Camera could not be reached') from None

    def login(self):
        """Confirm the password and return the model name."""
        _, data = self._get('/ISAPI/System/deviceInfo')
        match = MODEL.search(data)
        return match.group(1).decode(errors='replace') if match else 'unknown model'

    def usable_frame(self):
        kind, data = self._get('/ISAPI/Streaming/channels/{}01/picture'.format(self.channel))
        if 'image' not in kind.lower() and not data.startswith(JPEG_START):
            return False
        return (len(data) >= self.min_frame_bytes and data.startswith(JPEG_START)
                and data.rstrip(b'\x00').endswith(JPEG_END))

    # -- detection events ------------------------------------------------------
    def feed(self, data):
        """Read complete alerts from the stream; active ones hold detection on briefly."""
        self.buffer = (self.buffer + data)[-MAX_EVENT_BUFFER:]
        while END_OF_ALERT in self.buffer:
            block, self.buffer = self.buffer.split(END_OF_ALERT, 1)
            kind, state = EVENT_TYPE.search(block), EVENT_STATE.search(block)
            if (kind and state and state.group(1).lower() == b'active'
                    and kind.group(1).decode(errors='replace').lower() in self.event_types):
                self.active_until = self.clock() + self.hold_seconds

    def _listen(self):
        path = self.base + '/ISAPI/Event/notification/alertStream'
        opener = self._opener()
        while not self.closed.is_set():
            pause = self.retry_seconds
            try:
                with opener.open(path, timeout=30) as response:
                    self.events_connected, self.events_down_since = True, None
                    while not self.closed.is_set():
                        data = response.read1(4096)
                        if not data:
                            break
                        self.feed(data)
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    self._rejected()
                    pause = 60
                elif error.code == 404:
                    self.events_unsupported = True
                    pause = 60
            except NETWORK_ERRORS:
                pass
            if self.events_connected or self.events_down_since is None:
                self.events_down_since = self.clock()
            self.events_connected = False
            self.closed.wait(pause)

    def start_events(self):
        if self.thread is None:
            self.events_down_since = self.clock()
            self.thread = threading.Thread(target=self._listen, name='annke-events', daemon=True)
            self.thread.start()

    def close(self):
        self.closed.set()

    def person_detected(self):
        """True while a recent camera detection is active. On a C800 this usually means motion."""
        self.start_events()
        if self.events_unsupported:
            raise CameraUnsupported('This camera does not offer a detection event stream')
        if not self.events_connected and self.clock() - self.events_down_since >= self.down_after:
            raise CameraError('Camera detection stream is disconnected')
        return self.clock() < self.active_until

    def rtsp_url(self):
        """Contains the password. Pass only to the recorder; never print or log it."""
        return 'rtsp://{}:{}@{}:{}/Streaming/Channels/{}{}'.format(
            quote(self.username, safe=''), quote(self.password, safe=''),
            self.host, self.rtsp_port, self.channel, self.stream)


def camera_client(settings):
    """Build the right client from saved camera settings; older files mean Reolink."""
    fields = {key: value for key, value in settings.items()
              if key not in ('building', 'brand', 'retention_hours')}
    return (AnnkeClient(**fields) if settings.get('brand') == 'annke' else ReolinkClient(**fields))
