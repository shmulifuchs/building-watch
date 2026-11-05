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
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from buildingwatch.delivery import attach_media

SEGMENT_NAME = re.compile(r'^(\d{8}T\d{6})Z\.ts$')
CLIP_NAME = re.compile(r'^clip-\d+\.mp4$')
WORKING_NAME = re.compile(r'^job-\d+\.working\.(ts|mp4)$')
# A clip may be deleted only after delivery is settled; a failed send can still be retried.
SETTLED = ('delivered', 'local_only', 'delivery_unconfirmed')
GIGABYTE = 1_000_000_000


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


def covered_seconds(selected, start, end):
    """Seconds of the requested interval that the selected segments actually cover."""
    covered, reached = 0.0, start
    for segment_begin, segment_end, _ in selected:
        begin, finish = max(segment_begin, reached), min(segment_end, end)
        if finish > begin:
            covered += finish - begin
            reached = finish
    return covered


def latest_segment(folder):
    starts = [segment_start(item) for item in Path(folder).glob('*.ts')]
    return max((start for start in starts if start is not None), default=None)


class Recorder:
    def __init__(self, source, folder, ffmpeg, retention_hours=12, segment_seconds=10,
                 clock=time.time, log=print, min_free_gb=5, disk_free=None, stall_seconds=60):
        self.source, self.folder, self.ffmpeg = source, Path(folder), ffmpeg
        self.retention_seconds = retention_hours * 3600
        self.segment_seconds = segment_seconds
        self.clock, self.log = clock, log
        self.min_free_bytes = min_free_gb * GIGABYTE
        self.disk_free = disk_free or (lambda folder: shutil.disk_usage(folder).free)
        self.stall_seconds = stall_seconds

    def stalled(self, started):
        """ffmpeg can stay running while writing nothing; treat that as a failure."""
        newest = latest_segment(self.folder)
        return self.clock() - max(started, newest or 0) > self.stall_seconds

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
        # Low disk: give up the oldest video, but always keep the most recent hour.
        remaining = [item[2] for item in list_segments(self.folder, self.segment_seconds)]
        keep = int(3600 / self.segment_seconds)
        while len(remaining) > keep and self.disk_free(self.folder) < self.min_free_bytes:
            remaining.pop(0).unlink(missing_ok=True)
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
                started = self.clock()
                while process.poll() is None and not stop.wait(1):
                    checks += 1
                    if checks % 60 == 0:
                        self.prune()
                    if checks % 5 == 0 and self.stalled(started):
                        self.log('Recording stalled; restarting it.')
                        break
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
    def __init__(self, store, building, segments, media_root, ffmpeg, clock=time.time, log=print,
                 segment_seconds=10, settle_seconds=None, missing_after=300, max_attempts=3,
                 clip_retention_days=7, run_command=subprocess.run, gap_tolerance=1.0,
                 building_name=None, destination=None, stopped_after=90, min_free_gb=5,
                 disk_free=None):
        self.store, self.segments, self.media_root = store, Path(segments), Path(media_root)
        self.building, self.building_name = building, building_name or building
        self.destination = destination
        self.started = clock()
        self.stopped_after = stopped_after
        self.min_free_bytes = min_free_gb * GIGABYTE
        self.disk_free = disk_free or (lambda folder: shutil.disk_usage(folder).free)
        self.ffmpeg, self.clock, self.log = ffmpeg, clock, log
        self.segment_seconds = segment_seconds
        self.settle_seconds = segment_seconds + 5 if settle_seconds is None else settle_seconds
        self.missing_after, self.max_attempts = missing_after, max_attempts
        self.clip_retention_seconds = clip_retention_days * 86400
        self.run_command = run_command
        self.gap_tolerance = gap_tolerance
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
            "SELECT * FROM bw_media WHERE status='awaiting_recording' AND building=? ORDER BY id",
            (self.building,)).fetchall()
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
                covered = covered_seconds(selected, start, end)
                missing = end - start - covered > self.gap_tolerance
                attach_media(self.store, job['id'], output, self.media_root, now,
                             recorded_seconds=round(covered, 1) if missing else None)
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
            marks = ','.join('?' * len(SETTLED))
            for item in self.media_root.iterdir():
                if not CLIP_NAME.match(item.name) or item.stat().st_mtime >= limit:
                    continue
                waiting = self.store.db.execute(
                    "SELECT 1 FROM bw_media m JOIN bw_queue q ON q.building=m.building "
                    "AND q.key='video:'||m.key WHERE m.path=? AND q.status NOT IN (" + marks + ") LIMIT 1",
                    (str(item.resolve()), *SETTLED)).fetchone()
                if not waiting:
                    item.unlink(missing_ok=True)

    def clean_leftovers(self):
        """Remove half-made files from an interrupted run; its jobs are simply redone."""
        if self.media_root.is_dir():
            for item in self.media_root.iterdir():
                if WORKING_NAME.match(item.name):
                    item.unlink(missing_ok=True)

    def _flag(self, name, active, now, on_kind, on_text, off_kind=None, off_text=None):
        """Alert once when a problem starts, surviving restarts through bw_meta."""
        key = name + ':' + self.building
        with self.store.transaction():
            known = self.store.db.execute('SELECT 1 FROM bw_meta WHERE key=?', (key,)).fetchone()
            if bool(known) == active:
                return
            kind, text = (on_kind, on_text) if active else (off_kind, off_text)
            if active:
                self.store.db.execute('INSERT INTO bw_meta VALUES(?,?)', (key, str(now)))
            else:
                self.store.db.execute('DELETE FROM bw_meta WHERE key=?', (key,))
            if kind:
                self.store.enqueue(self.building, kind + ':' + str(now),
                                   dict(building_id=self.building, building_name=self.building_name,
                                        destination=self.destination, type=kind,
                                        created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                        text=self.building_name + ': ' + text), now)
                self.store.audit(now, self.building, kind)

    def check_health(self):
        now = self.clock()
        newest = latest_segment(self.segments) if self.segments.is_dir() else None
        flagged = self.store.db.execute('SELECT value FROM bw_meta WHERE key=?',
                                        ('recording-down:' + self.building,)).fetchone()
        # A restart alone proves nothing: recovery needs video newer than the outage alert.
        down = ((newest or 0) <= float(flagged['value']) if flagged
                else now - max(self.started, newest or 0) > self.stopped_after)
        self._flag('recording-down', down, now,
                   'recording_stopped', 'Video recording has stopped, even if the camera looks '
                   'online. New clips cannot be made until it recovers.',
                   'recording_restored', 'Video recording is working again.')
        folder = self.segments if self.segments.is_dir() else self.segments.parent
        self._flag('disk-low', self.disk_free(folder) < self.min_free_bytes, now, 'disk_low',
                   'Disk space is low. The oldest video is being removed to keep recording.')

    def run(self, stop, interval=5):
        self.clean_leftovers()
        while not stop.is_set():
            try:
                self.run_once()
                self.check_health()
                self.prune_clips()
            except sqlite3.Error as error:
                self.log('Clip check will retry: ' + type(error).__name__)
            stop.wait(interval)
