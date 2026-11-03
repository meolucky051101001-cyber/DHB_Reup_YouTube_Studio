"""Test cho đợt sửa round-2 (2026-11-02):

- Chặn xóa video đang tải/render/đăng hoặc bị worker giữ (claim_token) → 409.
- Dedupe Monitor nguyên tử: insert_monitor_queue_item chỉ chèn khi chưa có dòng
  trùng nguồn cùng đích đăng (chống 2 monitor cùng đích chen nhau → đăng trùng).
- Dedupe kéo-thả theo nội dung: cùng video upload 2 lần chỉ tạo 1 hàng.
- Smoke test: app khởi động được qua TestClient (bắt lỗi import-time mà py_compile không thấy).

Toàn bộ chạy trên DB tạm riêng; không chạm dữ liệu thật.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import pipeline_manager as pm
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

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
        res1 = app_module.api_upload_video_files(
            project_name="Dự án Upload Test", upload_to="Shorts", render_mode="shorts_9_16",
            resolution="1080p", visibility="public", made_for_kids=0, age_restriction=0,
            altered_content=0, emulate_mobile=0,
            files=[self._upload("clip1.mp4"), self._upload("clip2.mp4")],
        )
        self.assertEqual(res1["added"], 1)
        self.assertEqual(res1["skipped"], 1)

        res2 = app_module.api_upload_video_files(
            project_name="Dự án Upload Test", upload_to="Shorts", render_mode="shorts_9_16",
            resolution="1080p", visibility="public", made_for_kids=0, age_restriction=0,
            altered_content=0, emulate_mobile=0,
            files=[self._upload("clip3.mp4")],
        )
        self.assertEqual(res2["added"], 0)
        self.assertEqual(res2["skipped"], 1)

        uploads_dir = app_module.APP_TEMP_DIR / "uploads"
        leftovers = list(uploads_dir.glob("clip*.mp4")) if uploads_dir.exists() else []
        self.assertEqual(len(leftovers), 1, "Chỉ đúng 1 file được giữ lại, bản trùng phải bị dọn")


class CsvImportSafetyTests(unittest.TestCase):
    def test_csv_import_skips_bom_blank_and_duplicate_sources(self):
        csv_text = (
            "\ufeffSource_URL,Title\n"
            "https://youtu.be/abc?b=2&a=1,Video A\n"
            "https://youtu.be/abc?a=1&b=2,Video A trùng\n"
            ",Không có nguồn\n"
        )
        result = app_module.api_import_csv({"project_name": "CSV Safety", "csv_text": csv_text})
        self.assertEqual(result, {"ok": True, "added": 1, "skipped": 2})

        repeated = app_module.api_import_csv({"project_name": "CSV Safety", "csv_text": csv_text})
        self.assertEqual(repeated, {"ok": True, "added": 0, "skipped": 3})

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

    def test_state_changing_blocked_without_token(self):
        # 401 ở middleware — KHÔNG đụng vào pipeline thật.
        self.assertEqual(client.post("/api/pipeline/stop").status_code, 401)


if __name__ == "__main__":
    unittest.main()
