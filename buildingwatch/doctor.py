"""One-command readiness check: can this system watch the camera, make clips and send them?

It uses the real camera, the real video stream and real ffmpeg. It sends no WhatsApp
message unless send_test is True. The stream address contains the camera password, so
ffmpeg's output is never printed; only the codec and picture size are read from it.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from buildingwatch.ai_review import ClipReviewer, ReviewUnavailable, load_prompt
from buildingwatch.annke import camera_client
from buildingwatch.config import validate
from buildingwatch.delivery import ProviderError, Whapi
from buildingwatch.recorder import ClipMaker, find_ffmpeg
from buildingwatch.reolink import CameraError, load_camera_settings

MIN_FREE_BYTES = 5_000_000_000
MAX_CLIP_BYTES = 90_000_000
VIDEO = re.compile(r'Video: (\w+).*?(\d{3,5})x(\d{3,5})')


class Doctor:
    def __init__(self, local, ffmpeg=None, check_camera=None, source=None, provider=None,
                 run_command=subprocess.run, disk_free=None, log=print, reviewer=None):
        self.local = Path(local)
        self.ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg()
        self.check_camera, self.source = check_camera, source
        self.provider = provider or Whapi(self.local / 'whapi.json')
        self.run_command, self.log = run_command, log
        self.disk_free = disk_free or (lambda folder: shutil.disk_usage(folder).free)
        self.failed = False
        self.reviewer, self.ai_on = reviewer, False

    def report(self, state, name, detail=''):
        self.failed = self.failed or state == 'FAIL'
        self.log('{:<4}  {}{}'.format(state, name, ': ' + detail if detail else ''))

    def settings(self):
        path = self.local / 'camera.json'
        if not path.exists():
            self.report('FAIL', 'Camera settings', 'run python3 camera_setup.py --host <camera IP address>')
            return None
        settings = load_camera_settings(path)
        if settings.get('brand') != 'annke':
            self.report('FAIL', 'Camera settings', 'recording supports ANNKE cameras only')
            return None
        self.report('PASS', 'Camera settings', settings['host'])
        return settings

    def camera(self, settings):
        if self.check_camera is None:
            from camera_setup import check_annke as check
        else:
            check = self.check_camera
        try:
            for line in check(settings):
                self.report('FAIL' if 'NOT usable' in line else 'PASS', 'Camera', line)
        except (CameraError, OSError, ValueError) as error:
            self.report('FAIL', 'Camera', '{}. Check its power, cable and that this Mac is on '
                                          'the same network.'.format(error))
            return False
        return not self.failed

    def stream(self, settings, folder):
        """Record a few real seconds, exactly as the recorder would."""
        source = self.source or camera_client(settings).rtsp_url()
        output = folder / 'sample.ts'
        options = ['-rtsp_transport', 'tcp', '-timeout', '10000000'] if source.startswith('rtsp') else []
        command = [self.ffmpeg, '-nostdin', '-hide_banner', *options, '-i', source, '-map', '0:v:0',
                   '-an', '-c', 'copy', '-t', '6', '-f', 'mpegts', '-y', str(output)]
        try:
            result = self.run_command(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, timeout=40)
        except (subprocess.SubprocessError, OSError):
            self.report('FAIL', 'Video stream', 'timed out or could not start')
            return None
        if result.returncode != 0 or not output.is_file() or not output.stat().st_size:
            self.report('FAIL', 'Video stream', 'no video received. Check the RTSP setting on the camera.')
            return None
        found = VIDEO.search(result.stderr or '')
        self.report('PASS', 'Video stream', '{} {}x{}'.format(*found.groups()) if found else 'video received')
        return output

    def clip(self, sample, folder):
        output = folder / 'test-clip.mp4'
        command = ClipMaker(None, 'test', folder, folder, self.ffmpeg).command(sample, 0, 4, output)
        try:
            self.run_command(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=120)
        except (subprocess.SubprocessError, OSError):
            self.report('FAIL', 'Clip creation', 'ffmpeg could not make a clip')
            return None
        size = output.stat().st_size if output.is_file() else 0
        if not 0 < size <= MAX_CLIP_BYTES:
            self.report('FAIL', 'Clip creation', 'clip size is not usable')
            return None
        self.report('PASS', 'Clip creation', '{:.1f} MB H.264 test clip'.format(size / 1_000_000))
        return output

    def whatsapp(self, settings):
        config = self.local / 'buildings.json'
        destination = None
        if config.exists() and settings:
            try:
                buildings = validate(json.loads(config.read_text())['buildings'])
            except (KeyError, TypeError, ValueError) as error:
                self.report('FAIL', 'Building settings', str(error))
                return None
            if settings['building'] in buildings:
                destination = buildings[settings['building']].destination
                self.ai_on = buildings[settings['building']].c['ai_review']
        if destination is None:
            self.report('FAIL', 'WhatsApp group', 'set destination in .local/buildings.json')
        else:
            self.report('PASS', 'WhatsApp group', 'configured')
        if not (self.local / 'whapi.json').is_file():
            self.report('FAIL', 'WhatsApp sign-in', 'run python3 whatsapp_setup.py --file .local/whapi.json')
            return None
        try:
            self.provider.request('/health')
        except ProviderError as error:
            self.report('FAIL', 'WhatsApp sign-in', str(error))
            return None
        self.report('PASS', 'WhatsApp sign-in', 'Whapi answered')
        return destination

    def ai(self, clip):
        """Makes one real, paid AI request, so it runs only with --send-test."""
        try:
            verdict = (self.reviewer or ClipReviewer(self.ffmpeg, load_prompt(self.local / 'ai_prompt.txt'))).review(clip)
        except ReviewUnavailable as error:
            self.report('FAIL', 'AI check', '{}. Needs the anthropic package and an API key.'.format(error))
            return
        self.report('PASS', 'AI check', 'level {}: {}'.format(verdict['level'], verdict['summary']))

    def send(self, destination, clip):
        try:
            result = self.provider.send(dict(destination=destination, media_path=str(clip),
                                             text='Building Watch test clip'))
        except ProviderError as error:
            self.report('FAIL', 'Test clip sent', str(error))
            return
        accepted = result.get('sent') is True
        self.report('PASS' if accepted else 'FAIL', 'Test clip sent',
                    'accepted; check that it plays in the group' if accepted else 'not accepted')

    def run(self, send_test=False):
        self.local.mkdir(mode=0o700, exist_ok=True)
        if self.ffmpeg:
            self.report('PASS', 'Video tool', 'ffmpeg found')
        else:
            self.report('FAIL', 'Video tool', 'run brew install ffmpeg')
        free = self.disk_free(self.local)
        self.report('PASS' if free >= MIN_FREE_BYTES else 'FAIL', 'Disk space',
                    '{:.0f} GB free'.format(free / 1_000_000_000))
        settings = self.settings()
        clip = None
        with tempfile.TemporaryDirectory(dir=str(self.local)) as name:
            folder = Path(name)
            if settings and self.camera(settings) and self.ffmpeg:
                sample = self.stream(settings, folder)
                clip = self.clip(sample, folder) if sample else None
            else:
                self.report('SKIP', 'Video stream and clip', 'needs the checks above to pass')
            destination = self.whatsapp(settings)
            if send_test and clip and self.ai_on:
                self.ai(clip)
            if send_test and destination and clip:
                self.send(destination, clip)
            elif send_test:
                self.report('SKIP', 'Test clip sent', 'needs the checks above to pass')
        self.log('')
        self.log('Not ready: fix each FAIL, then run this again.' if self.failed
                 else 'Ready. Start ./watch start, ./watch camera, ./watch record and ./watch deliver.')
        return not self.failed

