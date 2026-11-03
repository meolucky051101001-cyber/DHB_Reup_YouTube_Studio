import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline_manager as pm
import render_engine as render_engine


def _filter_complex_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


class IntroOutroConcatTests(unittest.TestCase):
    """Concat chuẩn hóa khung hình/âm thanh cho intro/outro, kể cả khi chỉ bật một phía."""

    def _apply(self, intro, outro, main_path):
        captured = {}

        def fake_pick(folder, name, use_random, exts):
            if folder.name == "_Intro":
                return intro
            if folder.name == "_Outro":
                return outro
            return None

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

        cfg = {"intro_enabled": bool(intro), "outro_enabled": bool(outro)}
        with mock.patch.object(render_engine, "_pick_media", side_effect=fake_pick), \
                mock.patch.object(
                    render_engine, "probe_video_info",
                    return_value={"has_audio": True, "seconds": 10.0},
                ), \
                mock.patch.object(render_engine, "_run_ffmpeg_with_progress", side_effect=fake_run):
            render_engine._apply_intro_outro(
                main_path, cfg, 1080, 1920, 30, owner="monitor",
            )
        return captured

    def test_outro_only_normalizes_outro_frame(self):
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main.mp4"
            main.write_bytes(b"main")
            outro = Path(td) / "outro.mp4"
            outro.write_bytes(b"outro")

            captured = self._apply(None, outro, str(main))
            fc = _filter_complex_of(captured["cmd"])

            # Main (i=0) đã đúng khung, outro (i=1) phải được scale/pad về cùng khung
            self.assertIn("[0:v]setpts=PTS-STARTPTS,fps=30", fc)
            self.assertNotIn("[0:v]scale=", fc)
            self.assertIn("[1:v]setpts=PTS-STARTPTS,scale=1080:1920", fc)
            self.assertIn("pad=1080:1920", fc)
            self.assertIn("concat=n=2:v=1:a=1", fc)
            self.assertEqual(captured["kwargs"]["owner"], "monitor")

    def test_intro_and_outro_normalize_both_ends(self):
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main.mp4"
            main.write_bytes(b"main")
            intro = Path(td) / "intro.mp4"
            intro.write_bytes(b"intro")
            outro = Path(td) / "outro.mp4"
            outro.write_bytes(b"outro")

            captured = self._apply(intro, outro, str(main))
            fc = _filter_complex_of(captured["cmd"])

            self.assertIn("[0:v]setpts=PTS-STARTPTS,scale=1080:1920", fc)
            self.assertIn("[1:v]setpts=PTS-STARTPTS,fps=30", fc)
            self.assertNotIn("[1:v]scale=", fc)
            self.assertIn("[2:v]setpts=PTS-STARTPTS,scale=1080:1920", fc)
            self.assertIn("concat=n=3:v=1:a=1", fc)


class _FakeFFmpegProc:
    """Giả lập Popen của FFmpeg để kiểm tra kill + reap khi render lỗi/bị dừng."""

    def __init__(self, pid, lines):
        self.pid = pid
        self._lines = list(lines)
        self.killed = False
        self.wait_calls = 0
        self.returncode = None
        self.stderr = self
        self.stdout = None

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def close(self):
        pass


class FFmpegOrphanTests(unittest.TestCase):
    def setUp(self):
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS.clear()
        self.addCleanup(self._clear_registry)

    def _clear_registry(self):
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS.clear()

    def test_progress_callback_error_kills_and_reaps_process(self):
        fake = _FakeFFmpegProc(1111, ["frame=1 time=00:00:05.00 bitrate=100kbits/s\n"])

        def progress(pct, msg):
            if pct > 5:
                raise RuntimeError("DB update failed")

        with tempfile.TemporaryDirectory() as td:
            partial = str(Path(td) / "partial.mp4")
            Path(partial).write_bytes(b"x")
            with mock.patch("render_engine.subprocess.Popen", return_value=fake), \
                    mock.patch("render_engine.subprocess.run", side_effect=lambda *a, **k: fake.kill()):
                with self.assertRaises(RuntimeError):
                    render_engine._run_ffmpeg_with_progress(
                        ["ffmpeg"], 10.0, progress, partial_output=partial, owner="batch",
                    )
            self.assertFalse(os.path.exists(partial))

        self.assertTrue(fake.killed)
        self.assertGreaterEqual(fake.wait_calls, 1)
        self.assertEqual(render_engine._ACTIVE_FFMPEG_PROCS, {})

    def test_stop_event_kills_and_reaps_process(self):
        fake = _FakeFFmpegProc(2222, ["frame=1 time=00:00:05.00 bitrate=100kbits/s\n"])
        stop_event = threading.Event()

        def progress(pct, msg):
            if pct > 5:
                stop_event.set()

        with tempfile.TemporaryDirectory() as td:
            partial = str(Path(td) / "partial.mp4")
            Path(partial).write_bytes(b"x")
            with mock.patch("render_engine.subprocess.Popen", return_value=fake), \
                    mock.patch("render_engine.subprocess.run", side_effect=lambda *a, **k: fake.kill()):
                with self.assertRaises(InterruptedError):
                    render_engine._run_ffmpeg_with_progress(
                        ["ffmpeg"], 10.0, progress,
                        stop_event=stop_event, partial_output=partial, owner="batch",
                    )
            self.assertFalse(os.path.exists(partial))

        self.assertTrue(fake.killed)
        self.assertGreaterEqual(fake.wait_calls, 1)
        self.assertEqual(render_engine._ACTIVE_FFMPEG_PROCS, {})


class OwnerScopedKillTests(unittest.TestCase):
    def setUp(self):
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS.clear()
        self.addCleanup(self._clear_registry)

    def _clear_registry(self):
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS.clear()

    def test_batch_kill_leaves_monitor_render_running(self):
        batch_proc = _FakeFFmpegProc(111, [])
        monitor_proc = _FakeFFmpegProc(222, [])
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS[111] = (batch_proc, "batch")
            render_engine._ACTIVE_FFMPEG_PROCS[222] = (monitor_proc, "monitor")

        with mock.patch(
            "render_engine.subprocess.run",
            side_effect=lambda cmd, **kwargs: batch_proc.kill(),
        ) as taskkill:
            render_engine.kill_all_render_processes(owner="batch")

        self.assertTrue(batch_proc.killed)
        self.assertFalse(monitor_proc.killed)
        self.assertEqual(monitor_proc.wait_calls, 0)
        self.assertIn(222, render_engine._ACTIVE_FFMPEG_PROCS)
        self.assertNotIn(111, render_engine._ACTIVE_FFMPEG_PROCS)
        if os.name == "nt":
            self.assertEqual(taskkill.call_args[0][0], ["taskkill", "/PID", "111", "/T", "/F"])
        else:
            taskkill.assert_not_called()

    def test_global_kill_stops_every_lane(self):
        batch_proc = _FakeFFmpegProc(111, [])
        monitor_proc = _FakeFFmpegProc(222, [])
        with render_engine._ACTIVE_FFMPEG_LOCK:
            render_engine._ACTIVE_FFMPEG_PROCS[111] = (batch_proc, "batch")
            render_engine._ACTIVE_FFMPEG_PROCS[222] = (monitor_proc, "monitor")

        def _kill_pid(cmd, **kwargs):
            pid = int(cmd[2])
            {111: batch_proc, 222: monitor_proc}[pid].kill()

        with mock.patch("render_engine.subprocess.run", side_effect=_kill_pid) as taskkill:
            render_engine.kill_all_render_processes()

        self.assertTrue(batch_proc.killed)
        self.assertTrue(monitor_proc.killed)
        self.assertEqual(render_engine._ACTIVE_FFMPEG_PROCS, {})
        self.assertEqual(taskkill.call_count, 2 if os.name == "nt" else 0)


class _FakeCursor:
    def __init__(self, row, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=()):
        return _FakeCursor(self._row)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class RenderOwnerPropagationTests(unittest.TestCase):
    def _run_process(self, owner_kwargs):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "input.mp4"
            input_path.write_bytes(b"input")
            output_path = Path(td) / "output.mp4"
            output_path.write_bytes(b"output")
            thumb_path = Path(td) / "thumb.jpg"
            thumb_path.write_bytes(b"thumb")
            row = {
                "id": 1,
                "title": "Test",
                "status": "Pending",
                "published_url": "",
                "input_video_path": str(input_path),
                "output_video_path": "",
                "thumbnail_path": str(thumb_path),
                "render_config_json": "{}",
                "resolution": "",
                "render_mode": "shorts_9_16",
                "cut_start": "00:00:00",
                "cut_end": "00:00:00",
            }
            manager = pm.PipelineManager.__new__(pm.PipelineManager)
            manager._stop_event = threading.Event()
            manager._batch_upload_box = {"n": 0}
            manager._lock = threading.Lock()

            with mock.patch.object(pm, "get_conn", side_effect=lambda: _FakeConn(row)), \
                    mock.patch.object(pm, "add_log"), \
                    mock.patch.object(pm, "get_settings", return_value={}), \
                    mock.patch.object(pm, "sha256_file", return_value="test-fingerprint"), \
                    mock.patch.object(pm, "_store_content_fingerprint", return_value="stored"), \
                    mock.patch.object(
                        pm.render_queue, "submit",
                        return_value=_FakeFuture(str(output_path)),
                    ) as submit_mock:
                manager.process_single_item(
                    1, do_download=False, do_render=True, do_upload=False,
                    stop_event=threading.Event(), **owner_kwargs,
                )
            return submit_mock.call_args

    def test_monitor_lane_passes_owner_to_render_video(self):
        call = self._run_process({"owner": "monitor"})
        self.assertIs(call.args[0], pm.render_video)
        self.assertEqual(call.kwargs["owner"], "monitor")

    def test_batch_lane_defaults_to_batch_owner(self):
        call = self._run_process({})
        self.assertEqual(call.kwargs["owner"], "batch")

    def test_safe_wrapper_forwards_owner(self):
        manager = pm.PipelineManager.__new__(pm.PipelineManager)
        manager.process_single_item = mock.Mock()
        sem = threading.Semaphore(0)
        with mock.patch.object(pm, "release_item_claim") as release:
            manager._process_single_item_safe(
                7, 0, True, True, True, sem, "tok", False, owner="monitor",
            )
        self.assertEqual(manager.process_single_item.call_args.kwargs["owner"], "monitor")
        release.assert_called_once_with(7, "tok")


if __name__ == "__main__":
    unittest.main()
