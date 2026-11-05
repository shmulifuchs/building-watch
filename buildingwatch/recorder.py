"""Rolling camera recording and automatic clip creation, using ffmpeg.

Recorder keeps short video segments from the camera, named by their UTC start time, and
deletes them after the retention period. Audio is deliberately not recorded.

ClipMaker turns each footage request (bw_media job) into a small H.264 MP4 and attaches it
to the delivery queue through attach_media. Many cameras send H.265, which does not play
reliably in WhatsApp, so clips are always re-encoded. If no footage exists, the building
gets a text alert instead of silence.
"""
import calendar
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from buildingwatch.delivery import attach_media

SEGMENT_NAME = re.compile(r'^(\d{8}T\d{6})Z\.ts$')
CLIP_NAME = re.compile(r'^clip-\d+\.mp4$')


def find_ffmpeg():
    return (shutil.which('ffmpeg')
            or next((path for path in ('/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg')
                     if Path(path).is_file()), None))


def segment_start(path):
    match = SEGMENT_NAME.match(Path(path).name)
    if not match:
        return None
    return float(calendar.timegm(time.strptime(match.group(1), '%Y%m%dT%H%M%S')))


def list_segments(folder, segment_seconds=10):
    """Return (start, end, path) in time order. Gaps in recording stay visible as gaps."""
    found = sorted((start, path) for start, path in
                   ((segment_start(item), item) for item in Path(folder).glob('*.ts'))
                   if start is not None)
    result = []
    for index, (start, path) in enumerate(found):
        end = start + segment_seconds
        if index + 1 < len(found):
            end = min(end + 2, found[index + 1][0])  # segments can run slightly long
        result.append((start, end, path))
    return result


class Recorder:
    def __init__(self, source, folder, ffmpeg, retention_hours=12, segment_seconds=10,
                 clock=time.time, log=print):
        self.source, self.folder, self.ffmpeg = source, Path(folder), ffmpeg
        self.retention_seconds = retention_hours * 3600
        self.segment_seconds = segment_seconds
        self.clock, self.log = clock, log

    def command(self):
        input_options = (['-rtsp_transport', 'tcp', '-timeout', '10000000']
                         if self.source.startswith('rtsp') else [])
        return [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'quiet', *input_options,
                '-i', self.source, '-map', '0:v:0', '-an', '-c', 'copy', '-f', 'segment',
                '-segment_time', str(self.segment_seconds), '-segment_format', 'mpegts',
                '-strftime', '1',
                str(self.folder / '%Y%m%dT%H%M%SZ.ts')]

    def prune(self):
        limit = self.clock() - self.retention_seconds
        removed = 0
        for item in self.folder.glob('*.ts'):
            start = segment_start(item)
            if start is not None and start < limit:
                item.unlink(missing_ok=True)
                removed += 1
        return removed

    def run(self, stop, restart_seconds=5):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        while not stop.is_set():
            process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=dict(os.environ, TZ='UTC'))
            try:
                checks = 0
                while process.poll() is None and not stop.wait(1):
                    checks += 1
                    if checks % 60 == 0:
                        self.prune()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            self.prune()
            if not stop.is_set():
                # The source address contains the password, so it is never logged.
                self.log('Recording stopped (code {}); retrying.'.format(process.returncode))
                stop.wait(restart_seconds)


class ClipMaker:
    def __init__(self, store, segments, media_root, ffmpeg, clock=time.time, log=print,
                 segment_seconds=10, settle_seconds=None, missing_after=300, max_attempts=3,
                 clip_retention_days=7, run_command=subprocess.run):
        self.store, self.segments, self.media_root = store, Path(segments), Path(media_root)
        self.ffmpeg, self.clock, self.log = ffmpeg, clock, log
        self.segment_seconds = segment_seconds
        self.settle_seconds = segment_seconds + 5 if settle_seconds is None else settle_seconds
        self.missing_after, self.max_attempts = missing_after, max_attempts
        self.clip_retention_seconds = clip_retention_days * 86400
        self.run_command = run_command
        self.attempts = {}

    def command(self, joined, offset, duration, output):
        return [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'mpegts', '-i', str(joined),
                '-ss', '{:.3f}'.format(offset), '-t', '{:.3f}'.format(duration), '-an',
                '-vf', "scale='min(1280,iw)':-2", '-c:v', 'libx264', '-preset', 'veryfast',
                '-crf', '28', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]

    def _give_up(self, job, body, status, text, now):
        with self.store.transaction():
            self.store.enqueue(job['building'], 'footage-unavailable:' + job['key'],
                               dict(building_id=body['building_id'],
                                    building_name=body['building_name'],
                                    destination=body['destination'], type='footage_unavailable',
                                    created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                    text=body['building_name'] + ': ' + text), now)
            self.store.db.execute('UPDATE bw_media SET status=? WHERE id=?', (status, job['id']))
            self.store.audit(now, job['building'], 'media_' + status, job_id=job['id'])

    def _make(self, job_id, start, end, selected):
        self.media_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        joined = self.media_root / 'job-{}.working.ts'.format(job_id)
        working = self.media_root / 'job-{}.working.mp4'.format(job_id)
        output = self.media_root / 'clip-{}.mp4'.format(job_id)
        first = selected[0][0]
        begin = max(start, first)
        try:
            # MPEG-TS segments keep running timestamps, so joining their bytes is exact.
            with joined.open('wb') as stream:
                for item in selected:
                    with item[2].open('rb') as segment:
                        shutil.copyfileobj(segment, stream)
            self.run_command(self.command(joined, begin - first, end - begin, working),
                             check=True, timeout=300, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.replace(working, output)
        finally:
            joined.unlink(missing_ok=True)
            working.unlink(missing_ok=True)
        return output

    def run_once(self):
        """Handle every job whose footage should now exist. Returns finished job ids."""
        done = []
        now = self.clock()
        available = list_segments(self.segments, self.segment_seconds) if self.segments.is_dir() else []
        jobs = self.store.db.execute(
            "SELECT * FROM bw_media WHERE status='awaiting_recording' ORDER BY id").fetchall()
        for job in jobs:
            body = json.loads(job['body'])
            start = datetime.fromisoformat(body['start']).timestamp()
            end = datetime.fromisoformat(body['end']).timestamp()
            if now < end + self.settle_seconds:
                continue
            selected = [item for item in available if item[0] < end and item[1] > start]
            if not selected:
                if now >= end + self.missing_after:
                    self._give_up(job, body, 'no_footage',
                                  'Video requested for {} was not recorded. '
                                  'Check the recorder.'.format(body['start']), now)
                    self.log('No footage for clip job {}.'.format(job['id']))
                continue
            try:
                output = self._make(job['id'], start, end, selected)
                attach_media(self.store, job['id'], output, self.media_root, now)
                self.attempts.pop(job['id'], None)
                done.append(job['id'])
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                count = self.attempts.get(job['id'], 0) + 1
                self.attempts[job['id']] = count
                self.log('Clip job {} failed (attempt {}): {}'.format(
                    job['id'], count, type(error).__name__))
                if count >= self.max_attempts:
                    self.attempts.pop(job['id'], None)
                    self._give_up(job, body, 'clip_failed',
                                  'A video clip from {} could not be created. It may still '
                                  'be in the recording folder.'.format(body['start']), now)
        return done

    def prune_clips(self):
        limit = self.clock() - self.clip_retention_seconds
        if self.media_root.is_dir():
            for item in self.media_root.iterdir():
                if CLIP_NAME.match(item.name) and item.stat().st_mtime < limit:
                    item.unlink(missing_ok=True)

    def run(self, stop, interval=5):
        while not stop.is_set():
            self.run_once()
            self.prune_clips()
            stop.wait(interval)
