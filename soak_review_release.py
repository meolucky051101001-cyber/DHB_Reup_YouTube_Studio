"""Run bounded offline queue/render and simulated-upload acceptance on isolated SQLite."""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

import psutil
from runtime_identity import git_output

MIB = 1 << 20
WORKERS = (1, 4, 8, 1, 4, 8, 1, 4, 8, 8)
WARMUP_JOBS = 32
LIMITS = {
    'rss_growth': 256 * MIB, 'handles_growth': 32, 'threads_growth': 6,
    'db_wal_growth': 64 * MIB, 'artifact_growth': 128 * MIB, 'log_rows_growth': 15000, 'lock_ms': 2000,
}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def restrict_boundaries():
    attempts = []
    allowed = {Path(path).resolve() for name in ('ffmpeg', 'ffprobe') if (path := shutil.which(name))}
    require(len(allowed) == 2, 'Install FFmpeg and FFprobe on PATH for the offline soak.')
    windows_prefixes = tuple(os.path.normcase(subprocess.list2cmdline([str(path)]) + ' ') for path in allowed)

    def audit(event, args):
        if event == 'subprocess.Popen':
            executable, command = args[:2]
            permitted = (Path(executable).resolve() in allowed if executable else
                         isinstance(command, str) and os.path.normcase(command + ' ').startswith(windows_prefixes))
            if not permitted:
                attempts.append(event)
                raise PermissionError('Only local FFmpeg and FFprobe may launch during soak.')
            return
        if event not in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                         'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.sendto', 'socket.sendmsg'):
            return
        attempts.append(event)
        raise PermissionError('Network access, including local provider services, is forbidden during soak.')

    sys.addaudithook(audit)
    return attempts


def children():
    remaining = []
    for child in psutil.Process().children(recursive=True):
        try:
            name = Path(child.exe())
            console = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'conhost.exe'
            if name != console:
                remaining.append(child.pid)
        except psutil.NoSuchProcess:
            continue
    return remaining


class Soak:
    def __init__(self, root, log):
        os.environ['DHB_ROOT'] = str(root / 'no-legacy-import')
        import config
        config.APP_DATA_DIR = root / 'data'
        config.APP_DB_PATH = config.APP_DATA_DIR / 'soak.sqlite'
        config.APP_TEMP_DIR = root / 'temp'
        config.APP_OUTPUT_DIR = root / 'output'
        config.APP_PROFILES_DIR = root / 'profiles'
        for path in (config.APP_DATA_DIR, config.APP_TEMP_DIR, config.APP_OUTPUT_DIR, config.APP_PROFILES_DIR):
            path.mkdir()
        config.FFMPEG_EXE = shutil.which('ffmpeg') or config.FFMPEG_EXE
        config.FFPROBE_EXE = shutil.which('ffprobe') or config.FFPROBE_EXE
        from validate_review_release import sha256_file
        import database
        import pipeline_manager
        self.db = database
        self.pm = pipeline_manager
        self.config = config
        self.hash_file = sha256_file
        self.manager = self.pm.PipelineManager()
        self.uploader = self.pm.PipelineManager()
        self.log = log
        self.root = root
        self.lock = threading.Lock()
        self.calls = []
        self.active_render = set()
        self.rendered = set()
        self.render_peak = 0
        self.upload_active = False
        self.violations = []
        self.real_render = self.pm.render_video
        self.sources = {}
        self.source = root / 'source.mp4'
        subprocess.run([config.FFMPEG_EXE, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'color=black:s=96x160:r=12:d=0.5', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(self.source)], check=True, timeout=15)
        self.source_hash = sha256_file(self.source)
        self.db.init_db()
        self.manager.clear_stale_claims()
        with closing(self.db.get_conn()) as conn, conn:
            conn.executemany("INSERT INTO youtube_channels(id,name,channel_id,upload_mode,oauth_token_json) "
                             "VALUES(?, ?, ?, 'youtube_api', ?)",
                             [(i, f'Synthetic {i}', 'UC' + letter * 22,
                               '{"access_token":"offline-fixture-only"}') for i, letter in ((501, 'a'), (502, 'b'))])

    def report(self, event, **info):
        self.log.write(json.dumps({'event': event, 'epoch': time.time(), **info}) + '\n')
        self.log.flush()

    def measure_db_lock(self):
        start = time.perf_counter()
        with closing(self.db.get_conn()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        return round((time.perf_counter() - start) * 1000, 2)

    def sample(self):
        process = psutil.Process()
        lock_ms = self.measure_db_lock()
        with closing(self.db.get_conn()) as conn:
            log_rows = conn.execute('SELECT COUNT(*) FROM system_logs').fetchone()[0]
        db_wal = sum(path.stat().st_size for path in self.config.APP_DATA_DIR.glob('soak.sqlite*')
                     if not path.name.endswith('-shm'))
        artifacts = sum(file.stat().st_size for path in (self.config.APP_OUTPUT_DIR, self.config.APP_TEMP_DIR)
                        for file in path.rglob('*') if file.is_file())
        handles = process.num_handles() if os.name == 'nt' else process.num_fds()
        return {'rss': process.memory_info().rss, 'threads': process.num_threads(), 'handles': handles,
                'db_wal': db_wal, 'artifact': artifacts, 'lock_ms': lock_ms, 'log_rows': log_rows,
                'children': children()}

    def seed(self, label, size, *, upload=False):
        sources = []
        for i in range(size):
            path = self.root / f'input-{len(self.sources) + 1}-{i}.mp4'
            shutil.copyfile(self.source, path)
            # Give each synthetic input a unique, valid MP4 free-space box.
            with path.open('ab') as handle:
                handle.write(struct.pack('>I4sII', 16, b'free', len(self.sources) + 1, i))
            sources.append((path, self.hash_file(path)))
        self.sources[label] = sources
        with closing(self.db.get_conn()) as conn, conn:
            conn.executemany(
                "INSERT INTO video_queue(project_name,title,post_id,target_type,target_channel_id,"
                "source_type,platform,input_video_path,render_mode,visibility,status) "
                "VALUES(?,?,?,'CHANNEL',?,'File','LocalFile',?,'md5_cut','private',?)",
                [(label, f'{label} {i}', f'{label}-{i}', 501 + i % 2, str(path),
                  'Rendered' if upload else 'Pending') for i, (path, _) in enumerate(sources)])
            return [row[0] for row in conn.execute('SELECT id FROM video_queue WHERE project_name=? ORDER BY id', (label,))]

    def render(self, **kwargs):
        item_id = kwargs['owner_id']
        with self.lock:
            if item_id in self.rendered or item_id in self.active_render:
                self.violations.append('Duplicate render of the same item.')
            self.active_render.add(item_id)
            self.render_peak = max(self.render_peak, len(self.active_render))
        try:
            with closing(self.db.get_conn()) as conn:
                if not conn.execute('SELECT claim_token FROM video_queue WHERE id=?', (item_id,)).fetchone()[0]:
                    self.violations.append('Render worker started without a durable claim.')
            return self.real_render(**kwargs)
        finally:
            with self.lock:
                self.active_render.discard(item_id)
                self.rendered.add(item_id)

    def fake_upload(self, *, video_item, channel, evidence_sink, **_kwargs):
        with self.lock:
            if self.upload_active:
                self.violations.append('Concurrent uploader entry.')
            self.upload_active = True
            order = len(self.calls)
            entry = {'id': video_item['id'], 'channel': channel['id'], 'start_monotonic': time.monotonic(),
                     'start_epoch': time.time()}
            self.calls.append(entry)
        try:
            if video_item['project_name'] != 'Synthetic upload lane':
                self.violations.append('Prepare/render job attempted to upload.')
                raise RuntimeError('Wrong lane reached mock provider.')
            video_id = f'Fixture{order:04}'
            evidence = {'backend': 'youtube_api', 'video_id': video_id, 'expected': {'category_id': '22'}}
            with closing(self.db.get_conn()) as conn:
                slot = conn.execute('SELECT state,queue_id,attempt_id FROM upload_slot').fetchone()
                if slot['state'] != 'active' or slot['queue_id'] != video_item['id'] or not slot['attempt_id']:
                    self.violations.append('Mock uploader entered without its durable slot.')
            evidence_sink({**evidence, 'verification_state': 'received'})
            time.sleep(.25)
            if order:
                evidence_sink({**evidence, 'verification_state': 'mismatch', 'observed': {'category_id': '24'},
                               'code': 'CATEGORY_MISMATCH'})
                raise RuntimeError('Synthetic readback mismatch.')
            evidence_sink({**evidence, 'verification_state': 'verified', 'observed': {'category_id': '22'}})
            return {'url': f'https://youtu.be/{video_id}',
                    'warnings': [{'step': 'thumbnail', 'code': 'HTTP_403', 'message': 'Fixture warning'}]}
        finally:
            with self.lock:
                entry.update(end_monotonic=time.monotonic(), end_epoch=time.time())
                self.upload_active = False

    def render_round(self, label, size, workers):
        self.db.save_settings({'max_concurrent_threads': workers, 'max_concurrent_renders': 8,
                               'upload_delay_seconds': 0})
        ids = self.seed(label, size)
        self.render_peak = 0
        start = time.monotonic()
        launched = self.manager.start_batch(ids, do_download=False, do_render=True, do_upload=False)
        require(launched['ok'], 'Render batch could not start.')
        lock_checks = []
        while self.manager.is_running and time.monotonic() < start + 290:
            lock_checks.append(self.measure_db_lock())
            time.sleep(.5)
        require(not self.manager.is_running, 'Render batch failed to drain.')
        with closing(self.db.get_conn()) as conn:
            rows = conn.execute('SELECT status,error_message,claim_token,target_channel_id,input_video_path,output_video_path '
                                'FROM video_queue WHERE project_name=? ORDER BY id', (label,)).fetchall()
            output_paths = [row['output_video_path'] for row in rows]
        if any(row['status'] != 'Rendered' for row in rows):
            self.report('render_failure', label=label, reasons=sorted({
                (row['status'], row['error_message']) for row in rows if row['status'] != 'Rendered'
            }))
        require(len(rows) == size and set(ids) <= self.rendered, 'Render round missed a job.')
        require(all(row['status'] == 'Rendered' and not row['claim_token']
                    and row['target_channel_id'] == 501 + i % 2
                    and row['input_video_path'] == str(self.sources[label][i][0]) for i, row in enumerate(rows)),
                'Render terminal state, claim, destination or source path was wrong.')
        require(len(set(output_paths)) == size and all(Path(path).is_file() for path in output_paths),
                'Render outputs were missing or reused.')
        require(1 <= self.render_peak <= workers, 'Render concurrency exceeded configured workers.')
        require(all(self.hash_file(path) == digest for path, digest in self.sources[label]), 'Source content changed.')
        require(not self.violations, '; '.join(self.violations))
        return {'label': label, 'jobs': size, 'workers': workers, 'peak_render': self.render_peak,
                'seconds': round(time.monotonic() - start, 3), 'peak_lock_ms': max(lock_checks, default=0),
                'sample': self.sample()}

    def start_uploads(self, count):
        ids = self.seed('Synthetic upload lane', count, upload=True)
        settings = {'max_concurrent_threads': 2, 'upload_delay_seconds': 0}
        self.db.save_settings(settings)
        options = [False, False, True]
        preview = self.uploader.preview_distribution(ids, options)
        require(preview.get('preview_token'), 'Simulated upload preview failed.')
        launched = self.uploader.start_batch(ids, *options, preview_token=preview['preview_token'])
        require(launched['ok'], 'Simulated upload batch could not start.')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.calls:
            time.sleep(.05)
        require(self.calls, 'Simulated upload never reached its boundary.')
        return ids

    def verify_uploads(self, ids):
        with closing(self.db.get_conn()) as conn:
            attempts = conn.execute('SELECT queue_id,outcome,evidence_json FROM upload_attempts').fetchall()
            reservations = conn.execute('SELECT queue_id,state FROM content_reservations').fetchall()
            history = conn.execute('SELECT channel_id FROM published_history').fetchall()
            rows = conn.execute("SELECT id, status, post_upload_warnings_json FROM video_queue "
                                "WHERE project_name='Synthetic upload lane'").fetchall()
            slot = conn.execute('SELECT state FROM upload_slot').fetchone()
        require(len(self.calls) == len(attempts) == len(ids) and {entry['id'] for entry in self.calls} == set(ids),
                'Mock upload calls or attempts were retried or missing.')
        first = self.calls[0]
        terminal = {row['id']: row for row in rows}
        require(terminal[first['id']]['status'] == 'Completed' and json.loads(terminal[first['id']]['post_upload_warnings_json']),
                'Completed upload lost its warning.')
        evidence = {row['queue_id']: json.loads(row['evidence_json']) for row in attempts}
        require(evidence[first['id']]['verification_state'] == 'verified'
                and len(history) == 1 and history[0]['channel_id'] == first['channel']
                and slot['state'] == 'cooldown', 'Verified upload history, evidence or cooldown was lost.')
        gap = None
        if len(ids) == 2:
            second = self.calls[1]
            gap = second['start_monotonic'] - first['end_monotonic']
            require(gap >= self.pm.UPLOAD_MIN_GAP_SECONDS == 1800, 'Global cooldown elapsed too early.')
            require(terminal[second['id']]['status'] == 'NeedsReview'
                    and evidence[second['id']]['verification_state'] == 'mismatch'
                    and [(row['queue_id'], row['state']) for row in reservations] == [(second['id'], 'NeedsReview')],
                    'Uncertain upload lost its review hold or evidence.')
        restarted = self.pm.PipelineManager()
        restarted.clear_stale_claims()
        for item_id in ids:
            restarted.process_single_item(item_id, do_download=False, do_render=False, do_upload=True)
        require(not restarted.is_running and len(self.calls) == len(ids), 'An upload retried after restart.')
        with closing(self.db.get_conn()) as conn:
            require(attempts == conn.execute('SELECT queue_id,outcome,evidence_json FROM upload_attempts').fetchall()
                    and reservations == conn.execute('SELECT queue_id,state FROM content_reservations').fetchall(),
                    'Restart changed existing evidence or reservation.')
        return {'calls': self.calls, 'gap_seconds': gap, 'attempts': len(attempts),
                'reservations': len(reservations), 'history': len(history)}

    @staticmethod
    def wait(manager, deadline):
        while manager.is_running and time.monotonic() < deadline:
            time.sleep(.1)
        require(not manager.is_running, 'A batch failed to drain within its deadline.')


def run(args, log, root):
    denied = restrict_boundaries()
    soak = Soak(root, log)
    try:
        with (mock.patch.object(soak.pm, 'upload_video_to_youtube', side_effect=soak.fake_upload),
              mock.patch.object(soak.pm, 'render_video', side_effect=soak.render)):
            soak.pm.render_queue.configure(8)
            ready = threading.Barrier(8)
            pool_workers = [soak.pm.render_queue.submit(ready.wait, 15) for _ in range(8)]
            for worker in pool_workers:
                worker.result(timeout=20)
            soak.render_round('Warmup', WARMUP_JOBS, 8)
            baseline = soak.sample()
            require(not baseline['children'], 'A warmup child process survived drain.')
            soak.report('baseline', pilot=args.pilot, baseline=baseline, limits=LIMITS)
            started = time.monotonic()
            ids = soak.start_uploads(1 if args.pilot else 2)
            rounds = WORKERS[:2] if args.pilot else WORKERS
            for round_no, workers in enumerate(rounds):
                if not args.pilot:
                    deadline = started + round_no * args.minutes * 60 / len(rounds)
                    time.sleep(max(0, deadline - time.monotonic()))
                result = soak.render_round(f'Round {round_no + 1}', 8 if args.pilot else 100, workers)
                sample = result['sample']
                soak.report('round', **result)
                require(not sample['children'] and max(sample['lock_ms'], result['peak_lock_ms']) <= LIMITS['lock_ms'],
                        'Render children or excessive SQLite lock latency after drain.')
                for key in ('rss', 'handles', 'threads', 'db_wal', 'artifact', 'log_rows'):
                    require(sample[key] <= baseline[key] + LIMITS[key + '_growth'], f'Limit exceeded: {key}.')
                require(not denied, 'A prohibited external boundary was reached.')
            if not args.pilot:
                time.sleep(max(0, started + args.minutes * 60 - time.monotonic()))
            soak.wait(soak.uploader, started + (25 if args.pilot else args.minutes * 60 + 60))
            uploads = soak.verify_uploads(ids)
            require(soak.hash_file(soak.source) == soak.source_hash
                    and all(soak.hash_file(path) == digest for rows in soak.sources.values() for path, digest in rows),
                    'Source content changed during soak.')
            require(not children() and not denied and not soak.violations, 'Soak invariants failed after drain.')
            summary = {'pilot': args.pilot, 'revision': args.revision, 'jobs': len(soak.rendered) - WARMUP_JOBS,
                       'seconds': round(time.monotonic() - started, 3), 'uploads': uploads, 'final': soak.sample()}
    finally:
        soak.manager._stop_event.set()
        soak.uploader._stop_event.set()
        for manager in (soak.manager, soak.uploader):
            soak.wait(manager, time.monotonic() + 15)
        soak.pm.render_queue.shutdown()
        deadline = time.monotonic() + 5
        while threading.active_count() != 1 and time.monotonic() < deadline:
            time.sleep(.05)
        require(threading.active_count() == 1 and not children(), 'Owned thread or process survived teardown.')
    soak.report('complete', **summary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New JSONL report path')
    parser.add_argument('--minutes', type=int, default=60)
    parser.add_argument('--pilot', action='store_true', help='Short harness check without cooldown acceptance')
    args = parser.parse_args()
    if not 60 <= args.minutes <= 120:
        parser.error('Choose 60 to 120 minutes for the acceptance soak.')
    source_root = Path(__file__).resolve().parent
    args.revision = git_output(source_root, 'rev-parse', 'HEAD')
    if not args.pilot and git_output(source_root, 'status', '--porcelain'):
        parser.error('Run acceptance on a clean, committed revision.')
    with (args.output.open('x', encoding='utf-8') as log,
          tempfile.TemporaryDirectory(prefix='dhb-offline-soak-') as folder):
        run(args, log, Path(folder))
