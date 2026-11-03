import os
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

import pipeline_manager
import youtube_uploader


class MonitorDedupeTests(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE published_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, source_post_id TEXT NOT NULL, "
            "channel_id INTEGER, published_url TEXT)"
        )
        conn.execute(
            "INSERT INTO published_history (source_post_id, channel_id) VALUES (?, ?)",
            ("POST-A", 1),
        )
        return conn

    def test_same_source_on_other_channel_is_not_blocked(self):
        conn = self._conn()
        self.assertTrue(pipeline_manager._monitor_source_already_published(conn, "POST-A", 1))
        self.assertFalse(pipeline_manager._monitor_source_already_published(conn, "POST-A", 2))
        self.assertFalse(pipeline_manager._monitor_source_already_published(conn, "POST-B", 1))

    def test_group_monitor_keeps_global_dedupe(self):
        conn = self._conn()
        self.assertTrue(pipeline_manager._monitor_source_already_published(conn, "POST-A", None))
        self.assertTrue(pipeline_manager._monitor_source_already_published(conn, "POST-A", ""))
        self.assertFalse(pipeline_manager._monitor_source_already_published(conn, "", None))


class ScannerTlsTests(unittest.TestCase):
    def test_no_tls_verification_bypass_remains(self):
        source = (ROOT / "scanner_downloader.py").read_text(encoding="utf-8")
        self.assertNotIn("nocheckcertificate", source)
        self.assertNotIn("--no-check-certificate", source)
        self.assertNotIn("verify=False", source)


class FrontendQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_toast_does_not_render_html(self):
        start = self.source.index("function showToast")
        body = self.source[start:start + 1400]
        self.assertNotIn("innerHTML", body)
        self.assertIn("textContent = String(msg", body)

    def test_direct_upload_sends_audience_fields(self):
        for field in ("made_for_kids", "age_restriction", "altered_content", "emulate_mobile"):
            self.assertIn(f"formData.append('{field}'", self.source)
        self.assertIn("Không thể vừa chọn \"Dành cho trẻ em\"", self.source)

    def test_polling_pauses_when_tab_hidden(self):
        self.assertIn("if (document.hidden || gLiveRefreshInFlight) return;", self.source)
        self.assertIn("setInterval(refreshLivePanels, 2500);", self.source)
        self.assertIn("document.addEventListener('visibilitychange'", self.source)


class PublishedHistoryWritePathTests(unittest.TestCase):
    """A-9: lượt đăng Completed phải ghi published_history cho mọi nguồn.

    Trước đây chỉ Auto Monitor ghi bảng này, nên video từ link / kéo-thả /
    đường dẫn local không có lịch sử; dedupe chỉ còn dựa vào video_queue.
    """

    def _make_db(self, td):
        db_path = os.path.join(td, "test.sqlite")
        video_path = os.path.join(td, "input.mp4")
        Path(video_path).write_bytes(b"video")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE video_queue (
                id INTEGER PRIMARY KEY, title TEXT, status TEXT, published_url TEXT,
                claim_token TEXT, post_id TEXT, source_url TEXT, source_type TEXT,
                platform TEXT, input_video_path TEXT, output_video_path TEXT,
                thumbnail_path TEXT, target_channel_id INTEGER,
                download_progress INTEGER, upload_progress INTEGER, error_message TEXT,
                render_config_json TEXT, resolution TEXT, render_mode TEXT,
                cut_start TEXT, cut_end TEXT
            );
            CREATE TABLE youtube_channels (
                id INTEGER PRIMARY KEY, name TEXT, channel_id TEXT, handle TEXT,
                group_name TEXT, profile_name TEXT
            );
            CREATE TABLE published_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_post_id TEXT NOT NULL,
                source_url TEXT DEFAULT '',
                channel_id INTEGER DEFAULT NULL,
                published_url TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_post_id, channel_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO youtube_channels VALUES (7, 'Kênh A', 'UC123', '@a', 'Mặc định', '')"
        )
        conn.execute(
            "INSERT INTO video_queue (id, title, status, published_url, claim_token, post_id, "
            "source_url, source_type, platform, input_video_path, output_video_path, "
            "thumbnail_path, target_channel_id, download_progress, upload_progress, "
            "error_message, render_config_json, resolution, render_mode, cut_start, cut_end) "
            "VALUES (1, 'Video A', 'Pending', '', '', 'POST-1', 'https://src.example/v', "
            "'Link', '', ?, '', '', 7, 0, 0, '', '{}', '', 'shorts_9_16', '00:00:00', '00:00:00')",
            (video_path,),
        )
        conn.commit()
        conn.close()
        return db_path

    def _connect(self, db_path):
        def make_conn():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn
        return make_conn

    def test_completed_upload_from_link_writes_history(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            manager = pipeline_manager.PipelineManager()
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=self._connect(db_path)), \
                    mock.patch.object(pipeline_manager, "add_log"), \
                    mock.patch.object(pipeline_manager, "get_settings", return_value={}), \
                    mock.patch.object(pipeline_manager, "read_upload_gap_remaining", return_value=0), \
                    mock.patch.object(pipeline_manager, "reserve_upload_slot", return_value=True), \
                    mock.patch.object(
                        pipeline_manager, "upload_video_to_youtube",
                        return_value={"url": "https://youtu.be/abc123"},
                    ), \
                    mock.patch.object(pipeline_manager, "_record_upload_finished_safely"):
                manager.process_single_item(
                    1, do_download=False, do_render=False, do_upload=True,
                    stop_event=threading.Event(),
                )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM published_history").fetchone()
            queue_row = conn.execute("SELECT status FROM video_queue WHERE id = 1").fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["source_post_id"], "POST-1")
            self.assertEqual(row["source_url"], "https://src.example/v")
            self.assertEqual(row["channel_id"], 7)
            self.assertEqual(row["published_url"], "https://youtu.be/abc123")
            self.assertEqual(queue_row["status"], "Completed")

    def test_unverified_publish_requires_manual_review_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            manager = pipeline_manager.PipelineManager()
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=self._connect(db_path)), \
                    mock.patch.object(pipeline_manager, "add_log"), \
                    mock.patch.object(pipeline_manager, "get_settings", return_value={}), \
                    mock.patch.object(pipeline_manager, "read_upload_gap_remaining", return_value=0), \
                    mock.patch.object(pipeline_manager, "reserve_upload_slot", return_value=True), \
                    mock.patch.object(
                        pipeline_manager,
                        "upload_video_to_youtube",
                        return_value={
                            "url": "",
                            "published_uncertain": True,
                            "warning_message": "Không xác minh được URL",
                        },
                    ) as upload, \
                    mock.patch.object(pipeline_manager, "_record_upload_finished_safely"):
                manager.process_single_item(
                    1, do_download=False, do_render=False, do_upload=True,
                    stop_event=threading.Event(),
                )
                conn = sqlite3.connect(db_path)
                status = conn.execute("SELECT status FROM video_queue WHERE id = 1").fetchone()[0]
                conn.close()
                self.assertEqual(status, "NeedsReview")

                # An explicit force-reupload flag still cannot bypass unresolved
                # evidence that YouTube may have accepted the prior publish.
                manager.process_single_item(
                    1, do_download=False, do_render=False, do_upload=True,
                    force_reupload=True, stop_event=threading.Event(),
                )
                upload.assert_called_once()

    def test_unfinished_item_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE video_queue SET status = 'Error' WHERE id = 1")
            conn.commit()
            conn.close()
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=self._connect(db_path)):
                recorded = pipeline_manager.PipelineManager._record_published_history_for_item(
                    1, {"id": 7}
                )
            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM published_history").fetchone()[0]
            conn.close()
            self.assertFalse(recorded)
            self.assertEqual(count, 0)

    def test_empty_post_id_uses_queue_scoped_key_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE video_queue SET post_id = '', status = 'Completed', "
                "published_url = 'https://youtu.be/abc123' WHERE id = 1"
            )
            conn.commit()
            conn.close()
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=self._connect(db_path)):
                first = pipeline_manager.PipelineManager._record_published_history_for_item(1, {"id": 7})
                second = pipeline_manager.PipelineManager._record_published_history_for_item(1, {"id": 7})
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT source_post_id FROM published_history").fetchall()
            conn.close()
            self.assertTrue(first)
            self.assertTrue(second)
            self.assertEqual(rows, [("queue:1",)])


class UploaderLoggingTests(unittest.TestCase):
    def test_optional_step_failure_is_logged(self):
        channel = {"name": "Kênh A"}
        metadata = {"title": "Video A"}
        with mock.patch.object(youtube_uploader, "add_log") as add_log:
            youtube_uploader._log_optional_step_failure(
                "tải thumbnail", ValueError("boom"), channel, metadata
            )
        add_log.assert_called_once()
        args, kwargs = add_log.call_args
        self.assertEqual(args[0], "Upload")
        self.assertIn("boom", args[1])
        self.assertEqual(args[2], "WARNING")
        self.assertEqual(kwargs.get("channel_name"), "Kênh A")
        self.assertEqual(kwargs.get("video_title"), "Video A")


if __name__ == "__main__":
    unittest.main()
