"""Downloader passes PATH lookup and explicit FFmpeg executables consistently."""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import download_worker
import scanner_downloader as downloader


class DownloadFfmpegLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dhb_ffmpeg_location_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.executable = self.root / 'tools with spaces' / 'ffmpeg.exe'
        self.executable.parent.mkdir()
        self.executable.write_bytes(b'executable fixture')
        for target, name, value in (
            (downloader, 'APP_TEMP_DIR', self.downloads),
            (downloader, 'get_settings', lambda: {}),
            (downloader, 'probe_video_info', lambda _: {'duration': '00:00:01'}),
            (downloader, 'add_log', mock.Mock()),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        disk = mock.patch('storage_manager.ensure_disk_space')
        disk.start()
        self.addCleanup(disk.stop)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('Offline fixture'))
        network.start()
        self.addCleanup(network.stop)

    def download(self):
        return downloader.download_video('https://example.invalid/fixture', 'post-fixture')

    def worker_success(self, payload, *_args):
        output = Path(payload['out_template'].replace('%(ext)s', 'mp4'))
        output.write_bytes(b'synthetic merged video')
        return {'video_path': str(output), 'title': 'Fixture'}

    def test_path_name_omits_worker_override(self):
        for configured in ('ffmpeg', 'ffmpeg.exe'):
            with self.subTest(configured=configured), \
                    mock.patch.object(downloader, 'FFMPEG_EXE', configured), \
                    mock.patch.object(downloader, '_run_yt_dlp_worker', side_effect=self.worker_success) as worker:
                result = self.download()
                self.assertTrue(Path(result['video_path']).is_file())
                self.assertNotIn('ffmpeg_location', worker.call_args.args[0])

    def test_explicit_executable_with_spaces_reaches_worker_unchanged(self):
        with mock.patch.object(downloader, 'FFMPEG_EXE', str(self.executable)), \
                mock.patch.object(downloader, '_run_yt_dlp_worker', side_effect=self.worker_success) as worker:
            self.download()
        self.assertEqual(worker.call_args.args[0]['ffmpeg_location'], str(self.executable.resolve()))

    def test_custom_executable_name_is_resolved_on_path(self):
        with mock.patch.object(downloader, 'FFMPEG_EXE', 'custom-ffmpeg'), \
                mock.patch('shutil.which', return_value=str(self.executable)) as which, \
                mock.patch.object(downloader, '_run_yt_dlp_worker', side_effect=self.worker_success) as worker:
            self.download()
        which.assert_called_once_with('custom-ffmpeg')
        self.assertEqual(worker.call_args.args[0]['ffmpeg_location'], str(self.executable.resolve()))

    def test_missing_override_fails_before_worker_or_fallback(self):
        with mock.patch.object(downloader, 'FFMPEG_EXE', str(self.root / 'missing' / 'ffmpeg.exe')), \
                mock.patch.object(downloader, '_run_yt_dlp_worker') as worker, \
                mock.patch.object(downloader.subprocess, 'Popen') as fallback:
            with self.assertRaises(FileNotFoundError):
                self.download()
        worker.assert_not_called()
        fallback.assert_not_called()

    def test_fallback_uses_same_location_policy_as_worker(self):
        for configured, expected in (('ffmpeg', None), (str(self.executable), str(self.executable.resolve()))):
            def fallback(command, **_kwargs):
                output = Path(command[command.index('-o') + 1].replace('%(ext)s', 'mp4'))
                output.write_bytes(b'synthetic fallback video')
                return mock.Mock(poll=mock.Mock(return_value=0), returncode=0)

            with self.subTest(configured=configured), \
                    mock.patch.object(downloader, 'FFMPEG_EXE', configured), \
                    mock.patch.object(downloader, '_run_yt_dlp_worker', side_effect=RuntimeError('fixture')) as worker, \
                    mock.patch.object(downloader.subprocess, 'Popen', side_effect=fallback) as process:
                self.download()
            command = process.call_args.args[0]
            self.assertFalse(process.call_args.kwargs['shell'])
            if expected is None:
                self.assertNotIn('ffmpeg_location', worker.call_args.args[0])
                self.assertNotIn('--ffmpeg-location', command)
            else:
                self.assertEqual(worker.call_args.args[0]['ffmpeg_location'], expected)
                self.assertEqual(command[command.index('--ffmpeg-location') + 1], expected)

    def test_stop_after_partial_write_cleans_only_owned_artifacts(self):
        unrelated = self.downloads / 'existing.mp4'
        unrelated.write_bytes(b'keep this source')
        stopped = threading.Event()

        def worker(payload, *_args):
            Path(payload['out_template'].replace('%(ext)s', 'mp4.part')).write_bytes(b'partial')
            stopped.set()
            raise InterruptedError('fixture stop')

        with mock.patch.object(downloader, 'FFMPEG_EXE', 'ffmpeg'), \
                mock.patch.object(downloader, '_run_yt_dlp_worker', side_effect=worker), \
                mock.patch.object(downloader.subprocess, 'Popen') as fallback:
            with self.assertRaises(InterruptedError):
                downloader.download_video('https://example.invalid/fixture', 'post', stop_event=stopped)
        fallback.assert_not_called()
        self.assertEqual(list(self.downloads.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_bytes(), b'keep this source')

    def test_worker_accepts_omitted_location_and_preserves_explicit_location(self):
        for location in (None, str(self.executable)):
            payload = {'source_url': 'https://example.invalid/fixture',
                       'out_template': str(self.downloads / 'fixture.mp4'), 'format': 'best'}
            if location is not None:
                payload['ffmpeg_location'] = location
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            fake_ydl = mock.MagicMock()
            fake_ydl.__enter__.return_value = fake_ydl
            fake_ydl.extract_info.return_value = {'title': 'Fixture'}
            fake_ydl.prepare_filename.return_value = payload['out_template']
            with self.subTest(location=location), \
                    mock.patch.object(download_worker.sys, 'argv', ['download_worker.py', encoded]), \
                    mock.patch.object(download_worker.yt_dlp, 'YoutubeDL', return_value=fake_ydl) as factory, \
                    mock.patch.object(download_worker, '_emit'):
                self.assertEqual(download_worker.main(), 0)
            if location is None:
                self.assertNotIn('ffmpeg_location', factory.call_args.args[0])
            else:
                self.assertEqual(factory.call_args.args[0]['ffmpeg_location'], location)


if __name__ == '__main__':
    unittest.main()
