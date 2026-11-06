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

# Import app trong file này sau khi đã tắt worker nền + lock thật (xem app.py).
os.environ["DHB_DISABLE_BACKGROUND_TASKS"] = "1"

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
        old_db_path = database.APP_DB_PATH
        old_data_dir = database.APP_DATA_DIR
        render_engine.APP_OUTPUT_DIR = tmp
        render_engine.DHB_RENDER_DIR = tmp
        database.APP_DATA_DIR = tmp / "data"
        database.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        database.APP_DB_PATH = database.APP_DATA_DIR / "test.sqlite"
        database.init_db()
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
            database.APP_DB_PATH = old_db_path
            database.APP_DATA_DIR = old_data_dir
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
            with mock.patch.object(scanner_downloader, "get_settings", return_value={}):
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
    """Legacy Excel imports and preview/revision-gated description updates on fixture data."""

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
        conn.execute("INSERT INTO projects (name) VALUES ('Dự án khác')")
        cls.other_id = conn.execute(
            "INSERT INTO video_queue (project_name, title, status) VALUES (?, ?, ?)",
            ("Dự án khác", "Video C", "Pending"),
        ).lastrowid
        cls.fixture_ids = cls.row_ids + [cls.other_id]
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        database.APP_DB_PATH = cls.old_db
        database.APP_DATA_DIR = cls.old_data
        database.APP_TEMP_DIR = cls.old_temp
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from fastapi.testclient import TestClient

        conn = database.get_conn()
        try:
            for index, row_id in enumerate(self.fixture_ids):
                conn.execute("DELETE FROM content_reservations WHERE queue_id = ?", (row_id,))
                conn.execute(
                    "UPDATE video_queue SET title = ?, description = ?, status = 'Pending', "
                    "claim_token = '', tags = 'fixture,keep', visibility = 'private', "
                    "schedule_time = '2030-02-01 12:00:00', render_mode = 'full_16_9', "
                    "render_config_json = '{\"resolution\":\"720p\"}', "
                    "source_url = ?, input_video_path = ? WHERE id = ?",
                    (f"Video {'ABC'[index]}", f"Mô tả cũ {'ABC'[index]}",
                     f"https://example.invalid/fixture/{row_id}",
                     str(self.tmp / f"fixture-{row_id}.mp4"), row_id),
                )
            conn.commit()
        finally:
            conn.close()
        # No lifespan/startup context: requests only exercise these two endpoints.
        self.client = TestClient(
            self.app.app, base_url="http://127.0.0.1:8686",
            headers={"Origin": "http://127.0.0.1:8686", "X-DHB-Token": self.app.API_TOKEN},
        )
        self.addCleanup(self.client.close)

    def _queue_rows(self):
        conn = database.get_conn()
        try:
            return {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM video_queue WHERE id IN (?, ?, ?)", self.fixture_ids,
                )
            }
        finally:
            conn.close()

    def _update_fixture(self, row_id, **changes):
        self.assertIn(row_id, self.fixture_ids)
        self.assertTrue(set(changes) <= set(self._queue_rows()[row_id]))
        conn = database.get_conn()
        try:
            columns = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE video_queue SET {columns} WHERE id = ?",
                [*changes.values(), row_id],
            )
            conn.commit()
        finally:
            conn.close()

    def _set_reservation(self, row_id, present=True, state="Active"):
        self.assertIn(row_id, self.fixture_ids)
        conn = database.get_conn()
        try:
            conn.execute("DELETE FROM content_reservations WHERE queue_id = ?", (row_id,))
            if present:
                conn.execute(
                    "INSERT INTO content_reservations "
                    "(content_key, channel_id, queue_id, claim_token, state) VALUES (?, ?, ?, ?, ?)",
                    (f"description-fixture-{row_id}", row_id, row_id, "fixture-reservation", state),
                )
            conn.commit()
        finally:
            conn.close()

    def _assert_descriptions_only(self, before, changes):
        expected = {row_id: dict(row) for row_id, row in before.items()}
        for row_id, description in changes.items():
            expected[row_id]["description"] = description
        # Includes scheduling, rendering, paths, destinations, claims and all other columns.
        self.assertEqual(self._queue_rows(), expected)

    def _preview_descriptions(self, entries, selected_ids=None):
        before = self._queue_rows()
        result = self.app._bulk_description_worker(
            entries, "Dự án Test", [] if selected_ids is None else selected_ids,
        )
        self.assertTrue(result["preview_only"])
        self.assertEqual(result["updated"], 0)
        self.assertRegex(result["preview_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(self._queue_rows(), before)
        return result

    def _apply_descriptions(self, entries, preview, selected_ids=None):
        result = self.app._bulk_description_worker(
            entries, "Dự án Test", [] if selected_ids is None else selected_ids,
            preview_only=False, preview_revision=preview["preview_revision"],
        )
        self.assertFalse(result["preview_only"])
        self.assertEqual(result["preview_revision"], preview["preview_revision"])
        return result

    def _assert_rejected(self, status, call, *args, **kwargs):
        before = self._queue_rows()
        with self.assertRaises(self.app.HTTPException) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status)
        self.assertEqual(self._queue_rows(), before)

    def _post_descriptions(self, entries, **overrides):
        payload = {"items": entries, "project_name": "Dự án Test"}
        payload.update(overrides)
        return self.client.post("/api/queue/bulk-descriptions", json=payload)

    def _post_description_excel(self, raw, filename="descriptions.xlsx", **overrides):
        form = {"project_name": "Dự án Test"}
        form.update(overrides)
        return self.client.post(
            "/api/queue/import-descriptions", data=form,
            files={"file": (filename, raw, "application/octet-stream")},
        )

    def _description_workbook(self, rows, headers=("ID", "Mô tả"), guide=False, macro=False):
        from openpyxl import Workbook

        wb = Workbook()
        try:
            ws = wb.active
            ws.title = "Fixture data"
            ws.append(list(headers))
            for row in rows:
                ws.append(row)
            if guide:
                guide_ws = wb.create_sheet("Guide", 0)
                guide_ws.append(["ID", "Description"])
                guide_ws.append([self.row_ids[0], "Guide must not be imported"])
                guide_ws.append(["=1+1", "#DIV/0!"])
            buf = io.BytesIO()
            wb.save(buf)
        finally:
            wb.close()
        if not macro:
            return buf.getvalue()

        # Genuine macro-enabled workbook content type, without executable VBA.
        from zipfile import ZipFile

        macro_buf = io.BytesIO()
        with ZipFile(io.BytesIO(buf.getvalue())) as source, ZipFile(macro_buf, "w") as target:
            for info in source.infolist():
                content = source.read(info.filename)
                if info.filename == "[Content_Types].xml":
                    content = content.replace(
                        b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                        b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                    )
                target.writestr(info, content)
        return macro_buf.getvalue()

    def test_bulk_descriptions_preview_and_apply(self):
        first_id, second_id = self.row_ids
        before = self._queue_rows()
        entries = [
            {"id": first_id, "description": "Dòng một\nDòng hai"},
            {"id": second_id, "description": "Mô tả cũ B"},
            {"id": 999999, "description": "Không thuộc dự án"},
        ]
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["unchanged"], 1)
        self.assertEqual(preview["skipped"], 1)
        self.assertEqual([row["state"] for row in preview["rows"]], ["ready", "unchanged", "skipped"])
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["error_count"], 0)
        self._assert_descriptions_only(before, {first_id: "Dòng một\nDòng hai"})

    def test_description_excel_ignores_other_columns_and_duplicate_ids(self):
        first_id, second_id = self.row_ids
        before = self._queue_rows()
        raw = self._description_workbook([
            [first_id, "Mô tả từ Excel", "Không được đổi tiêu đề", "public"],
            [f"#{first_id}", "Dòng lặp", "Vẫn không đổi", "schedule"],
            [second_id, "Mô tả mới B", "Tiêu đề B mới", "public"],
        ], headers=["ID", "Mô tả", "Tiêu đề", "Visibility"])
        entries = self.app._description_excel_entries(raw)
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["error_count"], 2)
        self.assertEqual([row["state"] for row in preview["rows"]], ["error", "error", "ready"])
        self.assertEqual([row["queue_id"] for row in preview["rows"][:2]], [first_id, first_id])
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual([row["state"] for row in result["rows"]], ["error", "error", "updated"])
        self._assert_descriptions_only(before, {second_id: "Mô tả mới B"})

    def test_bulk_descriptions_never_update_claimed_video(self):
        first_id = self.row_ids[0]
        self._update_fixture(first_id, claim_token="worker-test")
        before = self._queue_rows()
        entries = [{"id": first_id, "description": "Không được ghi"}]
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["error_count"], 1)
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_apply_requires_valid_preview_revision(self):
        entries = [{"id": self.row_ids[0], "description": "New description"}]
        self._assert_rejected(
            400, self.app._bulk_description_worker, entries, "Dự án Test", [], preview_only=False,
        )
        for revision in (None, 123, "", "a" * 63, "A" * 64, "g" * 64):
            with self.subTest(revision=revision):
                self._assert_rejected(
                    400, self.app._bulk_description_worker, entries, "Dự án Test", [],
                    preview_only=False, preview_revision=revision,
                )
        self._assert_rejected(
            409, self.app._bulk_description_worker, entries, "Dự án Test", [],
            preview_only=False, preview_revision="0" * 64,
        )

    def test_bulk_descriptions_stale_unchanged_row_prevents_all_writes(self):
        first_id, second_id = self.row_ids
        entries = [
            {"id": first_id, "description": "New A"},
            {"id": second_id, "description": "Mô tả cũ B"},
        ]
        before = self._queue_rows()
        response = self._post_descriptions(entries)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["preview_only"])
        self.assertEqual(preview["updated"], 0)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["unchanged"], 1)
        self.assertEqual(self._queue_rows(), before)
        self._update_fixture(second_id, description="Changed after preview")
        before_apply = self._queue_rows()
        response = self._post_descriptions(
            entries, preview_only=False, preview_revision=preview["preview_revision"],
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self._queue_rows(), before_apply)

        # A fresh preview is required to intentionally replace that external edit.
        fresh = self._post_descriptions(entries).json()
        self.assertNotEqual(fresh["preview_revision"], preview["preview_revision"])
        response = self._post_descriptions(
            entries, preview_only=False, preview_revision=fresh["preview_revision"],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        self._assert_descriptions_only(before_apply, {first_id: "New A", second_id: "Mô tả cũ B"})

    def test_bulk_descriptions_claim_changes_after_preview_return_409(self):
        first_id, second_id = self.row_ids
        entries = [
            {"id": first_id, "description": "Must not be written"},
            {"id": second_id, "description": "Mô tả cũ B"},
        ]
        for initial, changed in (("", "fixture-claim"), ("fixture-claim", "")):
            with self.subTest(initial_claim=initial):
                self._update_fixture(second_id, claim_token=initial)
                preview = self._preview_descriptions(entries)
                self.assertEqual(preview["pending"], 1)
                self._update_fixture(second_id, claim_token=changed)
                before = self._queue_rows()
                response = self._post_descriptions(
                    entries, preview_only=False, preview_revision=preview["preview_revision"],
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_reservation_changes_after_preview_return_409(self):
        first_id, second_id = self.row_ids
        entries = [
            {"id": first_id, "description": "Must not be written"},
            {"id": second_id, "description": "Mô tả cũ B"},
        ]
        for initially_reserved in (False, True):
            with self.subTest(initially_reserved=initially_reserved):
                self._set_reservation(second_id, initially_reserved)
                preview = self._preview_descriptions(entries)
                self.assertEqual(preview["pending"], 1)
                self._set_reservation(second_id, not initially_reserved)
                before = self._queue_rows()
                response = self._post_descriptions(
                    entries, preview_only=False, preview_revision=preview["preview_revision"],
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self._queue_rows(), before)
                conn = database.get_conn()
                try:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM content_reservations WHERE queue_id = ?", (second_id,),
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(count, int(not initially_reserved))

    def test_bulk_descriptions_status_changes_after_preview_return_409(self):
        first_id, second_id = self.row_ids
        entries = [
            {"id": first_id, "description": "Must not be written"},
            {"id": second_id, "description": "Mô tả cũ B"},
        ]
        for status in ("NeedsReview", "Rendering 25%", "Ready"):
            with self.subTest(status=status):
                self._update_fixture(second_id, status="Pending")
                preview = self._preview_descriptions(entries)
                self._update_fixture(second_id, status=status)
                before = self._queue_rows()
                response = self._post_descriptions(
                    entries, preview_only=False, preview_revision=preview["preview_revision"],
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_blocks_needs_review_and_busy_statuses(self):
        first_id, second_id = self.row_ids
        statuses = (
            "NeedsReview", "Uploading", "Uploading 25%", "Đang đăng 25%",
            "Rendering", "Rendering 25%", "Đang Render 25%",
            "Downloading", "Downloading 25%", "Đang tải 25%", "Chờ giãn cách 30s",
        )
        for status in statuses:
            with self.subTest(status=status):
                self._update_fixture(first_id, status=status)
                before = self._queue_rows()
                description = f"Allowed idle row: {status}"
                entries = [
                    {"id": first_id, "description": "Blocked"},
                    {"id": second_id, "description": description},
                ]
                preview = self._preview_descriptions(entries)
                self.assertEqual(preview["pending"], 1)
                self.assertEqual(preview["error_count"], 1)
                self.assertEqual(preview["rows"][0]["state"], "error")
                result = self._apply_descriptions(entries, preview)
                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["error_count"], 1)
                self._assert_descriptions_only(before, {second_id: description})

    def test_bulk_descriptions_reservation_presence_blocks_pending_unclaimed_row(self):
        first_id = self.row_ids[0]
        entries = [{"id": first_id, "description": "Blocked by reservation alone"}]
        for state in ("Active", "NeedsReview", ""):
            with self.subTest(reservation_state=state):
                self._set_reservation(first_id, state=state)
                before = self._queue_rows()
                self.assertEqual(before[first_id]["status"], "Pending")
                self.assertEqual(before[first_id]["claim_token"], "")
                preview = self._preview_descriptions(entries)
                self.assertEqual(preview["pending"], 0)
                self.assertEqual(preview["error_count"], 1)
                result = self._apply_descriptions(entries, preview)
                self.assertEqual(result["updated"], 0)
                self.assertEqual(result["error_count"], 1)
                self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_selection_and_project_scoping(self):
        first_id, second_id = self.row_ids
        before = self._queue_rows()
        entries = [
            {"id": first_id, "description": "Selected"},
            {"id": second_id, "description": "Not selected"},
            {"id": self.other_id, "description": "Other project"},
            {"id": 999999, "description": "Unknown ID"},
        ]
        selected = [first_id, self.other_id, 999999]
        preview = self._preview_descriptions(entries, selected)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["skipped"], 3)
        result = self._apply_descriptions(entries, preview, selected)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 3)
        self._assert_descriptions_only(before, {first_id: "Selected"})
        for selected in ([self.other_id], [999999]):
            with self.subTest(selected=selected):
                self._assert_rejected(
                    400, self.app._bulk_description_worker, entries, "Dự án Test", selected,
                )

    def test_bulk_descriptions_revision_ignores_out_of_scope_mutations(self):
        first_id, second_id = self.row_ids
        for selected in ([], [first_id]):
            with self.subTest(selected=selected):
                description = f"Scoped update {selected}"
                entries = [{"id": first_id, "description": description}]
                if selected:
                    entries.append({"id": second_id, "description": "Excluded input"})
                preview = self._preview_descriptions(entries, selected)
                self._update_fixture(
                    second_id, description=f"External edit {selected}",
                    status="NeedsReview", claim_token="external-fixture-claim",
                )
                self._set_reservation(second_id)
                self._update_fixture(self.other_id, description=f"Other project edit {selected}")
                before = self._queue_rows()
                result = self._apply_descriptions(entries, preview, selected)
                self.assertEqual(result["updated"], 1)
                self._assert_descriptions_only(before, {first_id: description})

    def test_bulk_descriptions_revision_binds_input_and_selection(self):
        first_id, second_id = self.row_ids
        entries = [{"id": first_id, "description": "Previewed", "line": 2}]
        preview = self._preview_descriptions(entries)
        cases = (
            ([{"id": first_id, "description": "Changed", "line": 2}], []),
            ([{"id": second_id, "description": "Previewed", "line": 2}], []),
            ([{"id": first_id, "description": "Previewed", "line": 3}], []),
            (entries, [first_id]),
        )
        for changed_entries, selected in cases:
            with self.subTest(entries=changed_entries, selected=selected):
                self._assert_rejected(
                    409, self.app._bulk_description_worker, changed_entries, "Dự án Test", selected,
                    preview_only=False, preview_revision=preview["preview_revision"],
                )

    def test_bulk_descriptions_revision_uses_normalized_input(self):
        first_id, second_id = self.row_ids
        entries = [
            {"id": f" #{first_id} ", "description": "  Dòng một\nDòng hai \n"},
            {"id": float(second_id), "description": " Mô tả cũ B "},
        ]
        preview = self._preview_descriptions(entries, [second_id, first_id])
        normalized = [
            {"id": first_id, "description": "Dòng một\nDòng hai"},
            {"id": str(second_id), "description": "Mô tả cũ B"},
        ]
        # Non-description metadata is not in the revision baseline and must survive.
        self._update_fixture(first_id, title="External title edit", tags="external,keep")
        before = self._queue_rows()
        result = self._apply_descriptions(normalized, preview, [first_id, second_id])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unchanged"], 1)
        self._assert_descriptions_only(before, {first_id: "Dòng một\nDòng hai"})

    def test_bulk_descriptions_blank_descriptions_are_skipped(self):
        before = self._queue_rows()
        entries = [
            {"id": self.row_ids[0], "description": ""},
            {"id": self.row_ids[1], "description": " \t\r\n "},
        ]
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["skipped"], 2)
        self.assertEqual(preview["pending"], 0)
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_description_length_boundary(self):
        first_id, second_id = self.row_ids
        before = self._queue_rows()
        entries = [
            {"id": first_id, "description": "á" * 5000},
            {"id": second_id, "description": "á" * 5001},
        ]
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["error_count"], 1)
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["error_count"], 1)
        self._assert_descriptions_only(before, {first_id: "á" * 5000})

    def test_bulk_descriptions_invalid_ids_are_per_row_errors(self):
        invalid_ids = (
            None, True, False, 0, -1, 1.5, 0.0, -2.0, float("nan"), float("inf"),
            "", "abc", "1.5", "-1", "#0", "0", [], {}, 2 ** 63, str(2 ** 63),
        )
        entries = [{"id": item_id, "description": "Invalid ID"} for item_id in invalid_ids]
        entries.append({"description": "Missing ID"})
        entries.append({"id": self.row_ids[1], "description": "Valid row"})
        before = self._queue_rows()
        preview = self._preview_descriptions(entries)
        self.assertEqual(preview["error_count"], len(invalid_ids) + 1)
        self.assertEqual(preview["pending"], 1)
        for row in preview["rows"][:-1]:
            self.assertEqual(row["state"], "error")
            self.assertIsNone(row["queue_id"])
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["error_count"], len(invalid_ids) + 1)
        self._assert_descriptions_only(before, {self.row_ids[1]: "Valid row"})

    def test_bulk_descriptions_malformed_entries_return_400(self):
        valid = {"id": self.row_ids[0], "description": "Must not be written"}
        malformed = [None, [], {}, "text", [None], ["text"], [123], [[1, "text"]]]
        malformed.extend(
            [valid, {"id": self.row_ids[1], "description": value}]
            for value in (None, True, 123, 1.5, [], {})
        )
        malformed.append([valid, {"id": self.row_ids[1]}])
        malformed.extend(
            [dict(valid, line=line)] for line in (None, True, 0, -1, 1.5, "2")
        )
        before = self._queue_rows()
        for entries in malformed:
            with self.subTest(entries=entries):
                response = self._post_descriptions(entries)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)
        response = self.client.post("/api/queue/bulk-descriptions", json={"project_name": "Dự án Test"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_malformed_scope_and_flags_return_400(self):
        entries = [{"id": self.row_ids[0], "description": "Must not be written"}]
        cases = [{"project_name": value} for value in (None, "", "  ", 123, [], {})]
        cases += [{"preview_only": value} for value in (None, "false", 0, 1, [], {})]
        cases += [{"ids": value} for value in (
            None, "1", 1, {}, [True], [False], [0], [-1], [1.0], [1.5],
            [str(self.row_ids[0])], [None], [[]], [2 ** 63],
        )]
        before = self._queue_rows()
        for overrides in cases:
            with self.subTest(overrides=overrides):
                response = self._post_descriptions(entries, **overrides)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)
        response = self.client.post("/api/queue/bulk-descriptions", json={"items": entries})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._queue_rows(), before)

    def test_bulk_descriptions_maximum_entry_and_selection_counts(self):
        first_id = self.row_ids[0]
        entries = [{"id": first_id, "description": "Last row must not be truncated"}]
        entries = [
            {"id": 100000 + index, "description": "Outside fixture project"} for index in range(4999)
        ] + entries
        selected = [entry["id"] for entry in entries]
        before = self._queue_rows()
        preview = self._preview_descriptions(entries, selected)
        self.assertEqual(preview["total_rows"], 5000)
        self.assertEqual(preview["pending"], 1)
        self.assertEqual(preview["skipped"], 4999)
        result = self._apply_descriptions(entries, preview, selected)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 4999)
        self._assert_descriptions_only(before, {first_id: entries[-1]["description"]})

        before = self._queue_rows()
        response = self._post_descriptions(entries + [{"id": self.row_ids[1], "description": "Overflow"}])
        self.assertEqual(response.status_code, 400, response.text)
        response = self._post_descriptions(entries[-1:], ids=selected + [self.row_ids[1]])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._queue_rows(), before)

    def test_description_excel_reordered_ids_headers_and_guide_sheet(self):
        from openpyxl import load_workbook

        first_id, second_id = self.row_ids
        raw = self._description_workbook([
            ["=1+1", "Excel B", second_id, "Uploading", "new,tags"],
            ["#DIV/0!", " Excel A\nSecond line ", first_id, "NeedsReview", "other,tags"],
        ], headers=["Tiêu đề", "Description", "ID", "Status", "Tags"], guide=True)
        before = self._queue_rows()
        with mock.patch("openpyxl.load_workbook", wraps=load_workbook) as loader:
            entries = self.app._description_excel_entries(raw)
        self.assertTrue(loader.call_args.kwargs["read_only"])
        self.assertFalse(loader.call_args.kwargs["data_only"])
        self.assertEqual([entry["id"] for entry in entries], [second_id, first_id])
        self.assertEqual([entry["line"] for entry in entries], [2, 3])
        preview = self._preview_descriptions(entries)
        result = self._apply_descriptions(entries, preview)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["error_count"], 0)
        self._assert_descriptions_only(before, {first_id: "Excel A\nSecond line", second_id: "Excel B"})

    def test_description_excel_requires_id_and_description_headers(self):
        for headers in (("ID", "Title"), ("Title", "Description"), ("Title", "Tags")):
            with self.subTest(headers=headers):
                raw = self._description_workbook([[self.row_ids[0], "Text"]], headers=headers)
                self._assert_rejected(400, self.app._description_excel_entries, raw)
        self._assert_rejected(400, self.app._description_excel_entries, self._description_workbook([]))

        from openpyxl import Workbook

        wb = Workbook()
        try:
            wb.active.title = "Hướng dẫn"
            wb.active.append(["ID", "Description"])
            wb.active.append([self.row_ids[0], "Guide only"])
            buf = io.BytesIO()
            wb.save(buf)
        finally:
            wb.close()
        self._assert_rejected(400, self.app._description_excel_entries, buf.getvalue())

    def test_description_excel_rejects_formulas_and_cell_errors(self):
        before = self._queue_rows()
        for column in (0, 1):
            for value in ("=1+1", "#DIV/0!", "#VALUE!"):
                with self.subTest(column=column, value=value):
                    bad_row = [self.row_ids[1], "Bad row"]
                    bad_row[column] = value
                    raw = self._description_workbook([[self.row_ids[0], "Valid first row"], bad_row])
                    self._assert_rejected(400, self.app._description_excel_entries, raw)
                    response = self._post_description_excel(raw)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(self._queue_rows(), before)

    def test_description_excel_counts_nonempty_rows_and_rejects_overflow(self):
        rows = []
        for index in range(5000):
            rows.extend(([None, None, "Only unrelated metadata"], [self.row_ids[0], f"Row {index}"]))
        headers = ["ID", "Description", "Title"]
        raw = self._description_workbook(rows, headers=headers)
        entries = self.app._description_excel_entries(raw)
        self.assertEqual(len(entries), 5000)
        self.assertEqual(entries[0]["line"], 3)
        self.assertEqual(entries[-1]["line"], 10001)
        self.assertEqual(entries[-1]["description"], "Row 4999")
        # The extra nonempty row must cause rejection, not a partial 5000-row import.
        raw = self._description_workbook(rows + [[], [self.row_ids[1], "Overflow"]], headers=headers)
        self._assert_rejected(400, self.app._description_excel_entries, raw)
        response = self._post_description_excel(raw)
        self.assertEqual(response.status_code, 400, response.text)

    def test_description_excel_rejects_malformed_workbooks(self):
        from zipfile import ZipFile

        buf = io.BytesIO()
        with ZipFile(buf, "w") as archive:
            archive.writestr("fixture.txt", "Not an Excel workbook")
        before = self._queue_rows()
        for raw in (b"", b"not-excel", b"ID,Description\n1,Text", buf.getvalue(), b"PK\x03\x04broken"):
            with self.subTest(raw=raw[:30]):
                self._assert_rejected(400, self.app._description_excel_entries, raw)
                response = self._post_description_excel(raw)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)

    def test_import_descriptions_xlsx_and_xlsm_preview_apply(self):
        first_id, second_id = self.row_ids
        for extension in ("xlsx", "xlsm"):
            with self.subTest(extension=extension):
                raw = self._description_workbook([
                    [second_id, f"From {extension} B"],
                    [first_id, f"From {extension} A"],
                ], macro=extension == "xlsm")
                filename = f"fixture.{extension}"
                before = self._queue_rows()
                response = self._post_description_excel(raw, filename, ids=f"[{first_id}]")
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                self.assertTrue(preview["preview_only"])
                self.assertEqual(preview["updated"], 0)
                self.assertEqual(preview["pending"], 1)
                self.assertEqual(preview["skipped"], 1)
                self.assertRegex(preview["preview_revision"], r"^[0-9a-f]{64}$")
                self.assertEqual(self._queue_rows(), before)
                response = self._post_description_excel(
                    raw, filename, ids=f"[{first_id}]", preview_only="false",
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)
                response = self._post_description_excel(
                    raw, filename, ids=f"[{first_id}]", preview_only="false",
                    preview_revision=preview["preview_revision"],
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["preview_only"])
                self.assertEqual(response.json()["updated"], 1)
                self.assertEqual(response.json()["skipped"], 1)
                self._assert_descriptions_only(before, {first_id: f"From {extension} A"})

    def test_import_descriptions_changed_excel_rejects_revision_atomically(self):
        first_id, second_id = self.row_ids
        rows = [[first_id, "Previewed A"], [second_id, "Previewed B"]]
        raw = self._description_workbook(rows)
        response = self._post_description_excel(raw)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        changed = self._description_workbook([rows[0], [second_id, "Changed B"]])
        before = self._queue_rows()
        response = self._post_description_excel(
            changed, preview_only="false", preview_revision=preview["preview_revision"],
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self._queue_rows(), before)

    def test_import_descriptions_invalid_selections_and_extensions_return_400(self):
        raw = self._description_workbook([[self.row_ids[0], "Must not be written"]])
        before = self._queue_rows()
        for ids in ("bad-json", "null", "{}", '"1"', "[true]", "[0]", "[-1]", "[1.5]", '["1"]'):
            with self.subTest(ids=ids):
                response = self._post_description_excel(raw, ids=ids)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)
        for filename in ("fixture.xls", "fixture.csv", "fixture.zip"):
            with self.subTest(filename=filename):
                response = self._post_description_excel(raw, filename)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self._queue_rows(), before)

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
