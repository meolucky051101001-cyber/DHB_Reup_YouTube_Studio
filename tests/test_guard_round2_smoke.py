"""Test cho đợt sửa round-2 (2026-11-02):

- Chặn xóa video đang tải/render/đăng hoặc bị worker giữ (claim_token) → 409.
- Dedupe Monitor nguyên tử: insert_monitor_queue_item chỉ chèn khi chưa có dòng
  trùng nguồn cùng đích đăng (chống 2 monitor cùng đích chen nhau → đăng trùng).
- Dedupe kéo-thả theo nội dung: cùng video upload 2 lần chỉ tạo 1 hàng.
- Smoke test: app khởi động được qua TestClient (bắt lỗi import-time mà py_compile không thấy).

Toàn bộ chạy trên DB tạm riêng; không chạm dữ liệu thật.
"""
import io
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import pipeline_manager as pm
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import StreamingResponse

_OLD_DB = database.APP_DB_PATH
_OLD_DATA = database.APP_DATA_DIR
_OLD_TEMP = database.APP_TEMP_DIR
_TMP = None
app_module = None
client = None


def setUpModule():
    global _TMP, app_module, client
    _TMP = Path(tempfile.mkdtemp(prefix="dhb_test_guard_"))
    database.APP_DB_PATH = _TMP / "db.sqlite"
    database.APP_DATA_DIR = _TMP / "data"
    database.APP_TEMP_DIR = _TMP / "temp"
    # init_db() tường minh (import app có thể đã bị cache bởi file test khác).
    database.init_db()
    import app
    app_module = app
    app_module.APP_TEMP_DIR = _TMP / "temp"  # endpoint upload ghi file vào đây
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8686")


def tearDownModule():
    database.APP_DB_PATH = _OLD_DB
    database.APP_DATA_DIR = _OLD_DATA
    database.APP_TEMP_DIR = _OLD_TEMP
    shutil.rmtree(_TMP, ignore_errors=True)


def _mon(channel_id, group=""):
    return {
        "target_channel_id": channel_id,
        "target_group_name": group,
        "upload_to": "Shorts",
        "option_group_id": None,
        "render_mode": "shorts_9_16",
        "render_config_json": "{}",
    }


def _item(pid):
    return {
        "source_type": "URL",
        "platform": "TikTok",
        "post_id": pid,
        "source_url": f"https://example.com/v/{pid}",
        "input_video_path": "",
        "duration": "00:00:30",
        "view_count": 100,
        "like_count": 10,
    }


class MonitorDedupeTests(unittest.TestCase):
    """insert_monitor_queue_item: nguồn + cùng đích chỉ được vào hàng đợi đúng 1 lần."""

    def test_same_target_blocked_cross_monitor(self):
        conn = database.get_conn()
        try:
            first = pm.insert_monitor_queue_item(
                conn, "Monitor - A", _mon(5), _item("dup1"), "T", "D", "tags")
            self.assertIsNotNone(first)
            # Monitor B (project khác) cùng kênh đích 5 → bị chặn.
            second = pm.insert_monitor_queue_item(
                conn, "Monitor - B", _mon(5), _item("dup1"), "T", "D", "tags")
            self.assertIsNone(second)
            # Đích khác (kênh 7) → cho phép (history cũng cho phép theo từng kênh).
            other = pm.insert_monitor_queue_item(
                conn, "Monitor - B", _mon(7), _item("dup1"), "T", "D", "tags")
            self.assertIsNotNone(other)
            # Pre-check nhất quán với insert guard (grp được helper tự chuẩn hóa "Mặc định").
            self.assertTrue(pm._monitor_source_queued(conn, "dup1", "Monitor - X", 5, ""))
            self.assertFalse(pm._monitor_source_queued(conn, "dup1", "Monitor - X", 9, ""))
        finally:
            conn.close()

    def test_same_group_blocked_cross_monitor(self):
        conn = database.get_conn()
        try:
            first = pm.insert_monitor_queue_item(
                conn, "Monitor - G1", _mon(None, "Mặc định"), _item("dup2"), "T", "D", "tags")
            self.assertIsNotNone(first)
            second = pm.insert_monitor_queue_item(
                conn, "Monitor - G2", _mon(None, "Mặc định"), _item("dup2"), "T", "D", "tags")
            self.assertIsNone(second, "Cùng nhóm = cùng vòng xoay kênh → phải chặn")
        finally:
            conn.close()

    def test_manual_project_rows_do_not_block_monitor(self):
        conn = database.get_conn()
        try:
            conn.execute(
                "INSERT INTO video_queue (project_name, post_id, status) VALUES ('Dự án Tay', 'dup3', 'Pending')")
            conn.commit()
            res = pm.insert_monitor_queue_item(
                conn, "Monitor - M", _mon(3), _item("dup3"), "T", "D", "tags")
            self.assertIsNotNone(res, "Video do người dùng thêm tay không được chặn monitor khác đích")
        finally:
            conn.close()


class DeleteGuardTests(unittest.TestCase):
    """Không xóa được video đang xử lý hoặc đang bị worker giữ (409)."""

    def _seed(self, status="", claim=""):
        conn = database.get_conn()
        cur = conn.execute(
            "INSERT INTO video_queue (project_name, title, status, claim_token) "
            "VALUES ('Dự án Xóa', 'v', ?, ?)",
            (status, claim),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def test_delete_blocked_for_claimed_row(self):
        rid = self._seed(status="Pending", claim="worker-token")
        with self.assertRaises(HTTPException) as ctx:
            app_module.api_delete_queue_items({"ids": [rid]})
        self.assertEqual(ctx.exception.status_code, 409)
        conn = database.get_conn()
        still = conn.execute("SELECT id FROM video_queue WHERE id = ?", (rid,)).fetchone()
        conn.close()
        self.assertIsNotNone(still, "Row bị claim phải còn nguyên sau khi bị chặn xóa")

    def test_delete_blocked_for_uploading_row(self):
        rid = self._seed(status="Uploading -> Kênh A")
        with self.assertRaises(HTTPException) as ctx:
            app_module.api_delete_queue_items({"ids": [rid]})
        self.assertEqual(ctx.exception.status_code, 409)

    def test_delete_blocked_for_needs_review_reservation(self):
        rid = self._seed(status="NeedsReview")
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO content_reservations (content_key, channel_id, queue_id, claim_token, state) "
            "VALUES (?, 7, ?, 'old-token', 'NeedsReview')",
            (f"source:youtube:review-{rid}", rid),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(HTTPException) as ctx:
            app_module.api_delete_queue_items({"ids": [rid]})
        self.assertEqual(ctx.exception.status_code, 409)
        conn = database.get_conn()
        still = conn.execute("SELECT id FROM video_queue WHERE id = ?", (rid,)).fetchone()
        conn.close()
        self.assertIsNotNone(still)

    def test_delete_ok_for_idle_rows(self):
        rid1 = self._seed(status="Pending")
        rid2 = self._seed(status="Error")
        res = app_module.api_delete_queue_items({"ids": [rid1, rid2]})
        self.assertEqual(res["deleted"], 2)


class UploadDedupeTests(unittest.TestCase):
    """Kéo-thả cùng nội dung 2 lần chỉ tạo 1 hàng; file tạm của bản trùng được dọn."""

    CONTENT = b"FAKE_MP4_ftyp" + os.urandom(4096)

    def _upload(self, filename):
        return UploadFile(file=io.BytesIO(self.CONTENT), filename=filename)

    def test_duplicate_content_skipped(self):
        valid_probe = {"duration": "00:00:02", "seconds": 2.0, "width": 320, "height": 240,
                       "has_video": True, "has_audio": False}
        with mock.patch.object(app_module, "probe_video_info", return_value=valid_probe):
            res1 = app_module.api_upload_video_files(
                project_name="Dự án Upload Test", upload_to="Shorts", render_mode="shorts_9_16",
                resolution="1080p", visibility="public", schedule_time="", schedule_interval_minutes=60,
                is_premiere=0, made_for_kids=0, age_restriction=0,
                altered_content=0, emulate_mobile=0,
                target_group_names="",
                files=[self._upload("clip1.mp4"), self._upload("clip2.mp4")],
            )
        self.assertEqual(res1["added"], 1)
        self.assertEqual(res1["skipped"], 1)

        with mock.patch.object(app_module, "probe_video_info", return_value=valid_probe):
            res2 = app_module.api_upload_video_files(
                project_name="Dự án Upload Test", upload_to="Shorts", render_mode="shorts_9_16",
                resolution="1080p", visibility="public", schedule_time="", schedule_interval_minutes=60,
                is_premiere=0, made_for_kids=0, age_restriction=0,
                altered_content=0, emulate_mobile=0,
                target_group_names="",
                files=[self._upload("clip3.mp4")],
            )
        self.assertEqual(res2["added"], 0)
        self.assertEqual(res2["skipped"], 1)

        uploads_dir = app_module.APP_TEMP_DIR / "uploads"
        leftovers = list(uploads_dir.glob("clip*.mp4")) if uploads_dir.exists() else []
        self.assertEqual(len(leftovers), 1, "Chỉ đúng 1 file được giữ lại, bản trùng phải bị dọn")


class ManualUploadDecisionTests(unittest.TestCase):
    def test_completed_group_item_without_known_channel_cannot_be_reuploaded(self):
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO video_queue (project_name, title, status, post_id, content_key, "
            "target_type, target_group_name) VALUES (?, ?, 'Completed', ?, ?, 'GROUP', ?)",
            ("Manual retry group", "Clip", "POST-MANUAL-GROUP", "sha256:manual-group", "Mặc định"),
        )
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        with self.assertRaises(HTTPException) as raised:
            app_module.api_manual_reupload(
                item_id,
                {"action": "reupload", "reason": "Kiểm tra và yêu cầu đăng lại"},
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("kênh đích", raised.exception.detail)

    def test_confirmed_published_rejects_non_youtube_url(self):
        with self.assertRaises(HTTPException) as raised:
            app_module.api_manual_reupload(
                999999,
                {
                    "action": "confirmed_published",
                    "reason": "Đã đối chiếu video trong Studio",
                    "published_url": "https://example.com/watch?v=dQw4w9WgXcQ",
                },
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_completed_item_requires_one_item_reason_to_reupload(self):
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO video_queue (project_name, title, status, post_id, content_key, "
            "target_channel_id, published_url) VALUES (?, ?, 'Completed', ?, ?, ?, ?)",
            ("Manual retry", "Clip", "POST-MANUAL", "sha256:manual", 7, "https://youtu.be/old"),
        )
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        result = app_module.api_manual_reupload(
            item_id,
            {"action": "reupload", "reason": "Kiểm tra và yêu cầu đăng lại", "confirm_not_published": False},
        )
        conn = database.get_conn()
        row = conn.execute(
            "SELECT status, manual_reupload_authorized, manual_reupload_reason, last_attempt_id "
            "FROM video_queue WHERE id = ?",
            (item_id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT attempt_type, outcome, reason FROM upload_attempts WHERE id = ?",
            (row["last_attempt_id"],),
        ).fetchone()
        conn.close()
        self.assertTrue(result["ok"])
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["manual_reupload_authorized"], 1)
        self.assertEqual(attempt["attempt_type"], "manual_reupload")
        self.assertEqual(attempt["outcome"], "Authorized")
        self.assertEqual(attempt["reason"], "Kiểm tra và yêu cầu đăng lại")

    def test_needs_review_can_be_resolved_as_published_with_history(self):
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO video_queue (project_name, title, status, post_id, source_url, "
            "content_key, target_channel_id, error_message) VALUES (?, ?, 'NeedsReview', ?, ?, ?, ?, ?)",
            (
                "Manual review", "Clip", "POST-REVIEW", "https://source.example/clip",
                "sha256:review", 7, "Missing URL",
            ),
        )
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO content_reservations (content_key, channel_id, queue_id, claim_token, state) "
            "VALUES (?, 7, ?, 'old-token', 'NeedsReview')",
            ("sha256:review", item_id),
        )
        conn.commit()
        conn.close()

        result = app_module.api_manual_reupload(
            item_id,
            {
                "action": "confirmed_published",
                "reason": "Đã đối chiếu video trong Studio",
                "published_url": "https://youtu.be/review12345",
            },
        )
        conn = database.get_conn()
        queue = conn.execute("SELECT status, published_url FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        history = conn.execute(
            "SELECT source_post_id, channel_id, content_key, published_url FROM published_history WHERE source_post_id = ?",
            ("POST-REVIEW",),
        ).fetchone()
        held = conn.execute("SELECT COUNT(*) FROM content_reservations WHERE queue_id = ?", (item_id,)).fetchone()[0]
        conn.close()
        self.assertTrue(result["ok"])
        self.assertEqual(queue["status"], "Completed")
        self.assertEqual(queue["published_url"], "https://youtu.be/review12345")
        self.assertEqual(tuple(history), ("POST-REVIEW", 7, "sha256:review", "https://youtu.be/review12345"))
        self.assertEqual(held, 0)


class CsvImportSafetyTests(unittest.TestCase):
    def test_csv_import_skips_bom_blank_and_duplicate_sources(self):
        csv_text = (
            "\ufeffSource_URL,Title\n"
            "https://youtu.be/abc?b=2&a=1,Video A\n"
            "https://youtu.be/abc?a=1&b=2,Video A trùng\n"
            ",Không có nguồn\n"
        )
        result = app_module.api_import_csv({"project_name": "CSV Safety", "csv_text": csv_text})
        self.assertEqual((result["ok"], result["added"], result["skipped"]), (True, 1, 2))
        self.assertEqual((result["duplicate_count"], result["error_count"]), (1, 1))
        self.assertEqual((result["total_rows"], result["valid_count"]), (3, 1))

        repeated = app_module.api_import_csv({"project_name": "CSV Safety", "csv_text": csv_text})
        self.assertEqual((repeated["ok"], repeated["added"], repeated["skipped"]), (True, 0, 3))
        self.assertEqual((repeated["duplicate_count"], repeated["error_count"]), (2, 1))

        conn = database.get_conn()
        rows = conn.execute(
            "SELECT COUNT(*) FROM video_queue WHERE project_name = 'CSV Safety'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(rows, 1)

        conn = database.get_conn()
        conn.execute(
            "INSERT INTO video_queue (project_name, title, status) VALUES (?, ?, ?)",
            ("CSV Safety", "Chờ đối soát", "NeedsReview"),
        )
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        with self.assertRaises(HTTPException) as raised:
            app_module.api_start_pipeline({
                "ids": [item_id], "do_download": False, "do_render": False,
                "do_upload": True, "force_reupload": True,
            })
        self.assertIn("đối soát", str(raised.exception.detail))


class SmokeTests(unittest.TestCase):
    """App khởi động thật (middleware, token, endpoint nền) — bắt lỗi import-time."""

    def test_index_served(self):
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("app.js", res.text)

    def test_token_endpoint_public(self):
        res = client.get("/api/auth/token")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("token"))

    def test_api_requires_token(self):
        self.assertEqual(client.get("/api/overview").status_code, 401)

    def test_overview_with_token(self):
        token = client.get("/api/auth/token").json()["token"]
        res = client.get("/api/overview", headers={"x-dhb-token": token})
        self.assertEqual(res.status_code, 200)
        self.assertIn("pipeline_running", res.json())

    def test_state_changing_requires_same_origin_and_token(self):
        # Thiếu Origin bị chặn trước khi xác thực token; không gọi tới pipeline thật.
        self.assertEqual(client.post("/api/pipeline/stop").status_code, 403)

        token = client.get("/api/auth/token").json()["token"]
        headers = {"Origin": "http://127.0.0.1:8686", "Host": "127.0.0.1:8686"}
        probe = "/api/__phase6_guard_probe"
        self.assertEqual(client.post(probe, headers=headers).status_code, 401)

        headers["X-DHB-Token"] = token
        headers["Origin"] = "http://evil.example"
        self.assertEqual(client.post(probe, headers=headers).status_code, 403)

        headers["Origin"] = "http://127.0.0.1:8686"
        self.assertEqual(client.post(probe, headers=headers).status_code, 404)


class JsonResponseRedactionTests(unittest.TestCase):
    def _request(self):
        return Request({
            "type": "http", "method": "GET", "path": "/acceptance-json",
            "headers": [(b"host", b"127.0.0.1:8686")], "scheme": "http",
            "server": ("127.0.0.1", 8686), "query_string": b"",
        })

    def test_redacts_json_values_without_changing_structure_or_headers(self):
        payload = {
            "message": "Cookie: SID=sample",
            "text": '"quoted"\\line\nnext',
            "authorization": "Bearer sample-token",
            "cookie": {"SID": "structured-cookie"},
            "nested": [{"api_key": "sample-key", "refreshToken": "sample-refresh", "ok": True}],
            "count": 7,
        }

        async def call_next(_request):
            async def chunks():
                yield json.dumps(payload).encode("utf-8")
            response = StreamingResponse(chunks(), media_type="application/json")
            response.raw_headers.append((b"set-cookie", b"a=1; HttpOnly"))
            response.raw_headers.append((b"set-cookie", b"b=2; HttpOnly"))
            return response

        async def run():
            with mock.patch.object(app_module, "collect_secret_values", return_value=[]):
                return await app_module.guard_local_api(self._request(), call_next)

        response = asyncio.run(run())
        result = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["message"], "Cookie: ***")
        self.assertEqual(result["text"], '"quoted"\\line\nnext')
        self.assertEqual(result["authorization"], "***")
        self.assertEqual(result["cookie"], "***")
        self.assertEqual(result["nested"][0]["api_key"], "***")
        self.assertEqual(result["nested"][0]["refreshToken"], "***")
        self.assertIs(result["nested"][0]["ok"], True)
        self.assertEqual(result["count"], 7)
        self.assertEqual(int(response.headers["content-length"]), len(response.body))
        self.assertEqual(sum(1 for key, _ in response.raw_headers if key.lower() == b"set-cookie"), 2)

    def test_invalid_json_fails_closed_without_echoing_body(self):
        async def call_next(_request):
            async def chunks():
                yield b'{"cookie": "Cookie: SID=secret"'
            return StreamingResponse(chunks(), media_type="application/json")

        async def run():
            with mock.patch.object(app_module, "collect_secret_values", return_value=[]):
                return await app_module.guard_local_api(self._request(), call_next)

        response = asyncio.run(run())
        result = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("SID=secret", response.body.decode("utf-8"))
        self.assertIn("detail", result)


class AiBatchLinkLimitTests(unittest.TestCase):
    def test_bulk_ai_skips_too_long_item_and_preserves_its_original_metadata(self):
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO video_queue (project_name, title, description, tags, status) VALUES (?, ?, ?, ?, ?)",
            ("AI link limit", "Too long", "original long description", "old tags", "Pending"),
        )
        too_long_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO video_queue (project_name, title, description, tags, status) VALUES (?, ?, ?, ?, ?)",
            ("AI link limit", "Good", "original short description", "old tags", "Pending"),
        )
        good_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        conn.close()

        def optimize(title, **_kwargs):
            if title == "Too long":
                raise app_module.SourceLinksTooLongError("source links exceed description limit")
            return {"title": "Updated", "description": "updated description", "tags": "new tags",
                    "provider_used": "gemini", "fallback": False, "links_preserved": 0}

        with mock.patch.object(app_module, "ai_optimize_youtube_metadata", side_effect=optimize):
            result = app_module.api_ai_process_queue({
                "ids": [too_long_id, good_id], "mode": "rewrite_seo", "provider": "gemini",
            })

        self.assertFalse(result["ok"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual([item["id"] for item in result["failed"]], [too_long_id])
        conn = database.get_conn()
        too_long = conn.execute("SELECT title, description, tags FROM video_queue WHERE id = ?", (too_long_id,)).fetchone()
        good = conn.execute("SELECT title, description, tags FROM video_queue WHERE id = ?", (good_id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(too_long), ("Too long", "original long description", "old tags"))
        self.assertEqual(tuple(good), ("Updated", "updated description", "new tags"))


class DiagnosticsProbeTests(unittest.TestCase):
    def test_each_tool_receives_its_own_version_argument(self):
        cases = [
            ("FFmpeg", "C:/Tools/ffmpeg.exe", ["-version"]),
            ("FFprobe", "C:/Tools/ffprobe.exe", ["-version"]),
            ("yt-dlp", "C:/Tools with spaces/yt-dlp.exe", ["--version"]),
        ]
        for label, executable, args in cases:
            with self.subTest(label=label), mock.patch.object(
                app_module.subprocess, "run",
                return_value=mock.Mock(returncode=0, stdout="2026.07.04\n", stderr=""),
            ) as run:
                result = app_module._probe_local_tool(label, executable, args)
            self.assertTrue(result["available"])
            self.assertEqual(result["version"], "2026.07.04")
            self.assertEqual(run.call_args.args[0], [executable, *args])

    def test_probe_reports_nonzero_missing_and_timeout(self):
        with mock.patch.object(app_module.subprocess, "run", return_value=mock.Mock(
            returncode=2, stdout="", stderr="tool error",
        )) as run:
            failed = app_module._probe_local_tool("yt-dlp", "yt-dlp", ["--version"])
        self.assertFalse(failed["available"])
        self.assertTrue(failed["error"])
        self.assertEqual(run.call_args.args[0], ["yt-dlp", "--version"])

        with mock.patch.object(app_module.subprocess, "run", side_effect=FileNotFoundError):
            missing = app_module._probe_local_tool("yt-dlp", "missing-yt-dlp", ["--version"])
        self.assertFalse(missing["available"])

        with mock.patch.object(app_module.subprocess, "run", side_effect=app_module.subprocess.TimeoutExpired("yt-dlp", 6)):
            timed_out = app_module._probe_local_tool("yt-dlp", "yt-dlp", ["--version"])
        self.assertFalse(timed_out["available"])


class MonitorLifecycleApiTests(unittest.TestCase):
    def test_list_uses_durable_stopped_state_after_sqlite_lock_is_released(self):
        conn = database.get_conn()
        monitor_id = conn.execute("INSERT INTO monitors (name, source_urls) VALUES ('Stop DB lock', 'https://example.test/source')").lastrowid
        conn.commit()
        conn.close()
        manager = pm.PipelineManager()
        manager.start_monitor_supervisor = lambda: None
        entered, release = threading.Event(), threading.Event()
        def loop(_id, _stop):
            entered.set()
            release.wait(5)
        manager._monitor_loop = loop
        def short_connection():
            conn = sqlite3.connect(str(database.APP_DB_PATH), timeout=0.05, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        blocker = None
        handles = []
        original_manager = app_module.pipeline_manager
        try:
            with mock.patch.object(original_manager, '_check_monitors_health'), mock.patch.object(pm, 'add_log'), mock.patch.object(app_module, 'pipeline_manager', manager):
                self.assertTrue(manager.start_monitor(monitor_id)['ok'])
                self.assertTrue(entered.wait(2))
                handles = [manager._monitor_workers[monitor_id], manager._monitor_heartbeats[monitor_id]]
                blocker = database.get_conn()
                blocker.execute('BEGIN IMMEDIATE')
                with mock.patch.object(pm, 'get_conn', side_effect=short_connection):
                    result = manager.stop_monitor(monitor_id)
                self.assertFalse(result['ok'])
                self.assertEqual(result['lifecycle_state'], 'Stopping')
                blocker.rollback()
                blocker.close()
                blocker = None
                release.set()
                for handle in handles:
                    handle.join(2)
                    self.assertFalse(handle.is_alive())
                token = client.get('/api/auth/token').json()['token']
                headers = {'x-dhb-token': token, 'Origin': 'http://127.0.0.1:8686'}
                response = client.get('/api/monitors', headers=headers)
                self.assertEqual(response.status_code, 200)
                item = next(item for item in response.json() if item['id'] == monitor_id)
                self.assertEqual(item['is_running'], 0)
                self.assertEqual(item['status'], 'Stopped')
                self.assertEqual(item['lifecycle_state'], 'Stopped')
                self.assertTrue(item['can_start'])
                self.assertTrue(item['can_delete'])
                self.assertEqual(client.delete(f'/api/monitors/{monitor_id}', headers=headers).status_code, 200)
        finally:
            if blocker is not None:
                blocker.rollback()
                blocker.close()
            release.set()
            manager.stop_monitor(monitor_id)
            for handle in handles:
                handle.join(2)
                self.assertFalse(handle.is_alive())

    def test_api_surfaces_stopping_and_rejects_restart_or_delete_until_exit(self):
        conn = database.get_conn()
        conn.execute("INSERT INTO monitors (name, source_urls) VALUES ('API lifecycle', 'https://example.test/source')")
        monitor_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        conn.close()

        manager = pm.PipelineManager()
        entered = threading.Event()
        release = threading.Event()
        runs = []
        def fake_loop(_monitor_id, _stop_event):
            runs.append(1)
            entered.set()
            release.wait(4)
        manager._monitor_loop = fake_loop
        manager._start_monitor_heartbeat = lambda *args, **kwargs: None
        manager.start_monitor_supervisor = lambda: None
        original_manager = app_module.pipeline_manager
        original_check = original_manager._check_monitors_health
        original_manager._check_monitors_health = lambda: None
        token = client.get("/api/auth/token").json()["token"]
        headers = {"x-dhb-token": token, "Origin": "http://127.0.0.1:8686"}

        try:
            with mock.patch.object(app_module, "pipeline_manager", manager):
                started = client.post(f"/api/monitors/{monitor_id}/toggle", headers=headers)
                self.assertEqual(started.status_code, 200, started.text)
                self.assertTrue(entered.wait(2))

                listed = client.get("/api/monitors", headers={"x-dhb-token": token}).json()[0]
                self.assertEqual(listed["lifecycle_state"], "Running")
                self.assertFalse(listed["can_delete"])

                stopped = client.post(f"/api/monitors/{monitor_id}/toggle", headers=headers)
                self.assertEqual(stopped.status_code, 200, stopped.text)
                self.assertEqual(stopped.json()["lifecycle_state"], "Stopping")
                self.assertEqual(client.post(f"/api/monitors/{monitor_id}/toggle", headers=headers).status_code, 409)
                self.assertEqual(client.delete(f"/api/monitors/{monitor_id}", headers=headers).status_code, 409)
                self.assertEqual(len(runs), 1)

                with manager._monitor_lock:
                    worker = manager._monitor_workers[monitor_id]
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                listed = client.get("/api/monitors", headers={"x-dhb-token": token}).json()[0]
                self.assertEqual(listed["lifecycle_state"], "Stopped")
                self.assertTrue(listed["can_delete"])
                deleted = client.delete(f"/api/monitors/{monitor_id}", headers=headers)
                self.assertEqual(deleted.status_code, 200, deleted.text)
        finally:
            release.set()
            manager.stop_monitor(monitor_id)
            with manager._monitor_lock:
                worker = manager._monitor_workers.get(monitor_id)
            if worker is not None and worker.is_alive():
                worker.join(2)
            original_manager._check_monitors_health = original_check


if __name__ == "__main__":
    unittest.main()
