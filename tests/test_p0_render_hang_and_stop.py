"""Test hồi quy cho các fix phiên review 2026-11-02:

- P0: video câm + nhạc nền (-stream_loop -1) khiến FFmpeg encode vô hạn —
  nhánh shorts_9_16 và full_16_9 phải có -shortest để output dừng theo video.
- Watchdog: FFmpeg im lặng quá lâu phải bị tự hủy; bấm Dừng phải có hiệu lực
  cả khi readline() đang block (không còn dữ liệu stderr).
- download_video: nhận stop_event — dừng trước khi tải và dừng giữa chừng.
- import Excel: phần nặng tách thành _excel_import_worker (chạy threadpool).

Test dùng thư mục tạm, ffmpeg thật (nếu có) và HTTP server giả cục bộ;
không chạm dữ liệu thật của tool.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import database
import render_engine
import scanner_downloader


def _ffmpeg_available() -> bool:
    p = Path(config.FFMPEG_EXE)
    return p.exists() or shutil.which("ffmpeg") is not None


def _probe_seconds(path: str) -> float:
    info = scanner_downloader.probe_video_info(path)
    return float(info.get("seconds") or 0.0)


class RenderHangP0Tests(unittest.TestCase):
    """P0: video câm + nhạc nền lặp vô hạn phải render xong đúng chiều dài video."""

    def _make_media(self, tmp: Path):
        ff = config.FFMPEG_EXE
        src = tmp / "silent_src.mp4"
        music = tmp / "_Backgroud Music" / "test_music.mp3"
        music.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
            check=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        subprocess.run(
            [ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             str(music)],
            check=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return src, music

    def _render_with_timeout(self, render_mode: str):
        if not _ffmpeg_available():
            self.skipTest("Không có ffmpeg trên máy/runner")

        tmp = Path(tempfile.mkdtemp(prefix="dhb_test_hang_"))
        old_out = render_engine.APP_OUTPUT_DIR
        old_dhb = render_engine.DHB_RENDER_DIR
        render_engine.APP_OUTPUT_DIR = tmp
        render_engine.DHB_RENDER_DIR = tmp
        try:
            src, music = self._make_media(tmp)
            cfg = {
                "bg_music_name": music.name,
                "bg_music_volume": 0.25,
                "enforce_shorts_duration": True,
                "resolution": "1080p",
            }
            box = {}

            def _run():
                try:
                    box["output"] = render_engine.render_video(
                        str(src), render_mode=render_mode, render_config=cfg,
                    )
                except BaseException as exc:  # noqa: BLE001 - ghi lại mọi lỗi vào box
                    box["error"] = exc

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=90)
            if t.is_alive():
                render_engine.kill_all_render_processes()
                self.fail(
                    f"render_video('{render_mode}') với video câm + nhạc nền bị treo quá 90s "
                    "(P0 chưa được vá hoặc -shortest không có hiệu lực)."
                )
            if "error" in box:
                raise box["error"]
            out = box.get("output")
            self.assertTrue(out and Path(out).exists(), "Render không tạo được file output")
            dur = _probe_seconds(out)
            self.assertGreater(dur, 0.5, "Output rỗng/không đọc được thời lượng")
            self.assertLessEqual(
                dur, 4.0,
                f"Output dài {dur:.1f}s — âm thanh nhạc nền không bị cắt theo video (-shortest thiếu hiệu lực).",
            )
            self.assertLess(
                Path(out).stat().st_size, 30 * 1024 * 1024,
                "File output phình bất thường — dấu hiệu encode vô hạn.",
            )
        finally:
            render_engine.APP_OUTPUT_DIR = old_out
            render_engine.DHB_RENDER_DIR = old_dhb
            shutil.rmtree(tmp, ignore_errors=True)

    def test_silent_plus_bg_music_shorts_terminates(self):
        self._render_with_timeout("shorts_9_16")

    def test_silent_plus_bg_music_full_16_9_terminates(self):
        self._render_with_timeout("full_16_9")


class FFmpegWatchdogTests(unittest.TestCase):
    """Watchdog: FFmpeg im lặng bị tự hủy; Dừng có hiệu lực cả khi readline đang block."""

    SILENT_CHILD = "import time; time.sleep(30)"

    def test_watchdog_kills_silent_ffmpeg(self):
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            render_engine._run_ffmpeg_with_progress(
                [sys.executable, "-c", self.SILENT_CHILD],
                expected_duration=10.0,
                progress_callback=None,
                silence_timeout=2.5,
            )
        elapsed = time.monotonic() - started
        self.assertIn("treo", str(ctx.exception).lower())
        self.assertLess(elapsed, 20.0, f"Watchdog không kịp kill (mất {elapsed:.1f}s).")

    def test_stop_event_interrupts_blocked_readline(self):
        ev = threading.Event()
        threading.Timer(1.5, ev.set).start()
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            render_engine._run_ffmpeg_with_progress(
                [sys.executable, "-c", self.SILENT_CHILD],
                expected_duration=10.0,
                progress_callback=None,
                stop_event=ev,
                silence_timeout=60.0,
            )
        elapsed = time.monotonic() - started
        # Trước khi có watchdog: readline block tới khi tiến trình con thoát (~30s).
        self.assertLess(elapsed, 15.0, f"Dừng không có hiệu lực khi FFmpeg im lặng (mất {elapsed:.1f}s).")

    def test_normal_command_still_succeeds(self):
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('time=00:00:01.00\\n'); sys.stderr.flush()"]
        render_engine._run_ffmpeg_with_progress(
            cmd, expected_duration=1.0, progress_callback=None, silence_timeout=10.0,
        )


class DownloadStopTests(unittest.TestCase):
    """download_video phải nhận stop_event: dừng trước khi tải và dừng giữa chừng."""

    def test_stop_before_start(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(InterruptedError):
            scanner_downloader.download_video(
                "http://127.0.0.1:9/video.mp4", post_id="t", stop_event=ev,
            )

    def test_stop_during_download(self):
        tmp = Path(tempfile.mkdtemp(prefix="dhb_test_dl_"))
        old_temp = scanner_downloader.APP_TEMP_DIR
        scanner_downloader.APP_TEMP_DIR = tmp

        class _SlowHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(20 * 1024 * 1024))
                self.end_headers()

            def do_GET(self):
                self.do_HEAD()
                chunk = b"\x00" * 8192
                try:
                    for _ in range(2500):
                        self.wfile.write(chunk)
                        time.sleep(0.02)
                except Exception:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
        # yt-dlp ngắt kết nối đột ngột khi stop_event kích hoạt — ConnectionResetError
        # từ server thread là chuyện expected, nuốt để output test sạch.
        server.handle_error = lambda *args: None
        port = server.server_address[1]
        thread_srv = threading.Thread(target=server.serve_forever, daemon=True)
        thread_srv.start()

        ev = threading.Event()
        box = {}

        def _run():
            try:
                scanner_downloader.download_video(
                    f"http://127.0.0.1:{port}/video.mp4", post_id="slow", stop_event=ev,
                )
                box["error"] = None
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        try:
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            time.sleep(3.0)
            ev.set()
            t.join(timeout=40)
            self.assertFalse(t.is_alive(), "download_video không phản ứng với stop_event sau 40s.")
            self.assertIsInstance(box.get("error"), InterruptedError,
                                  f"Kỳ vọng InterruptedError, nhận: {box.get('error')!r}")
        finally:
            server.shutdown()
            server.server_close()
            scanner_downloader.APP_TEMP_DIR = old_temp
            shutil.rmtree(tmp, ignore_errors=True)


class ExcelImportWorkerTests(unittest.TestCase):
    """_excel_import_worker: cập nhật đúng dòng theo cột ID, giữ hành vi cũ."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dhb_test_excel_"))
        cls.old_db = database.APP_DB_PATH
        cls.old_data = database.APP_DATA_DIR
        cls.old_temp = database.APP_TEMP_DIR
        database.APP_DB_PATH = cls.tmp / "db.sqlite"
        database.APP_DATA_DIR = cls.tmp / "data"
        database.APP_TEMP_DIR = cls.tmp / "temp"
        # init_db() tường minh: nếu file test khác đã import app trước (cache module)
        # thì import lại đây không còn chạy init_db trên DB tạm mới của class này.
        database.init_db()
        import app  # noqa: F401 - các thread nền của app chạy trên DB tạm vừa patch

        cls.app = app
        conn = database.get_conn()
        conn.execute("INSERT OR IGNORE INTO projects (name) VALUES ('Dự án Test')")
        for title in ("Video A", "Video B"):
            conn.execute(
                """
                INSERT INTO video_queue (project_name, title, status)
                VALUES ('Dự án Test', ?, 'Pending')
                """,
                (title,),
            )
        conn.commit()
        cls.row_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM video_queue WHERE project_name = 'Dự án Test' ORDER BY id ASC"
            ).fetchall()
        ]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        database.APP_DB_PATH = cls.old_db
        database.APP_DATA_DIR = cls.old_data
        database.APP_TEMP_DIR = cls.old_temp
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build_xlsx(self) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["ID", "Tiêu đề", "Đặt lịch"])
        ws.append([self.row_ids[0], "Tiêu đề mới A", "2030-01-01 09:00"])
        ws.append([self.row_ids[1], "Tiêu đề mới B", "2030-01-01 10:00"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_worker_updates_rows_by_id(self):
        raw = self._build_xlsx()
        res = self.app._excel_import_worker(raw, "Dự án Test", [])
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("updated"), 2)

        conn = database.get_conn()
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM video_queue WHERE project_name = 'Dự án Test' ORDER BY id ASC"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(rows[self.row_ids[0]]["title"], "Tiêu đề mới A")
        self.assertEqual(rows[self.row_ids[1]]["title"], "Tiêu đề mới B")
        for rid in self.row_ids:
            self.assertEqual(rows[rid]["visibility"], "schedule")
            self.assertTrue(rows[rid]["schedule_time"])

    def test_worker_rejects_garbage_bytes(self):
        with self.assertRaises(Exception):
            self.app._excel_import_worker(b"khong-phai-excel", "Dự án Test", [])


if __name__ == "__main__":
    unittest.main()
