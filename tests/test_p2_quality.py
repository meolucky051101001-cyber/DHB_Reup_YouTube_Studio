import sqlite3
import sys
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
