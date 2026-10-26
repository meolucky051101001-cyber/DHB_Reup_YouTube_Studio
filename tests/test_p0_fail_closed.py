import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline_manager as pm
import youtube_uploader as yu

CHANNEL_ID = "UC1234567890abcdefghijkl"


class PublishedUrlTests(unittest.TestCase):
    def test_short_url(self):
        self.assertEqual(
            yu.normalize_published_video_url("https://youtu.be/dQw4w9WgXcQ"),
            "https://youtu.be/dQw4w9WgXcQ",
        )

    def test_watch_url_with_params(self):
        self.assertEqual(
            yu.normalize_published_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42"),
            "https://youtu.be/dQw4w9WgXcQ",
        )

    def test_shorts_url(self):
        self.assertEqual(
            yu.normalize_published_video_url("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share"),
            "https://youtu.be/dQw4w9WgXcQ",
        )

    def test_rejects_dashboard_url(self):
        self.assertEqual(yu.normalize_published_video_url("https://studio.youtube.com"), "")

    def test_rejects_short_video_id(self):
        self.assertEqual(yu.normalize_published_video_url("https://youtu.be/abc"), "")


class ChannelIdTests(unittest.TestCase):
    def test_extract_from_studio_url(self):
        self.assertEqual(
            yu._extract_channel_id(f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos"),
            CHANNEL_ID,
        )

    def test_extract_bare_id(self):
        self.assertEqual(yu._extract_channel_id(f"browseId={CHANNEL_ID}"), CHANNEL_ID)

    def test_extract_ignores_non_channel_page(self):
        self.assertEqual(yu._extract_channel_id("https://studio.youtube.com"), "")

    def test_verify_requires_expected_id(self):
        with self.assertRaises(RuntimeError) as ctx:
            yu._verify_studio_channel_id({"channel_id": CHANNEL_ID}, {"name": "Kênh A"})
        self.assertIn("Channel ID", str(ctx.exception))

    def test_verify_requires_actual_id(self):
        with self.assertRaises(RuntimeError) as ctx:
            yu._verify_studio_channel_id({"channel_id": ""}, {"channel_id": CHANNEL_ID})
        self.assertIn("Không đọc được Channel ID", str(ctx.exception))

    def test_verify_rejects_mismatch(self):
        other = "UCabcdefghijklmnopqrstuv"
        with self.assertRaises(RuntimeError) as ctx:
            yu._verify_studio_channel_id({"channel_id": other}, {"channel_id": CHANNEL_ID})
        self.assertIn("không khớp", str(ctx.exception))

    def test_verify_accepts_exact_match(self):
        yu._verify_studio_channel_id({"channel_id": CHANNEL_ID}, {"channel_id": CHANNEL_ID})

    def test_select_channel_fails_without_id(self):
        with self.assertRaises(RuntimeError) as ctx:
            yu.select_studio_channel(None, {"name": "Kênh A", "handle": "@kenha", "channel_id": ""})
        self.assertIn("Channel ID", str(ctx.exception))

    def test_upload_preflight_fails_before_opening_browser(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".mp4", dir=str(ROOT / "temp"))
        os.close(fd)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                yu.upload_video_to_youtube(
                    {"title": "Video test", "output_video_path": path},
                    {"name": "Kênh A", "handle": "@kenha", "channel_id": "", "upload_mode": "playwright"},
                )
            self.assertIn("Channel ID", str(ctx.exception))
        finally:
            os.remove(path)


class AgeRestrictionTests(unittest.TestCase):
    def test_kids_and_18_plus_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            yu.build_final_metadata(
                {"title": "Video", "made_for_kids": 1, "age_restriction": 1},
                {"name": "Kênh A"},
            )

    def test_api_mode_rejects_18_plus_before_touching_file(self):
        with self.assertRaises(RuntimeError) as ctx:
            yu._upload_via_youtube_api_with_session(
                video_path=str(ROOT / "khong-ton-tai.mp4"),
                metadata={"age_restriction": True},
                channel={"name": "Kênh A"},
            )
        self.assertIn("18+", str(ctx.exception))


class ChecksFailClosedTests(unittest.TestCase):
    def test_empty_and_unknown_checks_text_are_not_safe(self):
        self.assertFalse(yu.checks_report_safe(""))
        self.assertFalse(yu.checks_report_safe("Đang kiểm tra bản quyền..."))

    def test_safe_text_is_recognized(self):
        self.assertTrue(yu.checks_report_safe("No issues found"))
        self.assertTrue(yu.checks_report_safe("Checks complete. No issues found"))

    def test_violation_wins_over_safe_phrase(self):
        state = yu.classify_checks_text(
            "Checks complete. Copyright-protected content found. Blocked in some territories"
        )
        self.assertTrue(state["has_violation"])

    def test_negated_copyright_phrase_is_not_violation(self):
        state = yu.classify_checks_text("No copyright issues were found")
        self.assertFalse(state["has_violation"])


class FailClosedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uploader = (ROOT / "youtube_uploader.py").read_text(encoding="utf-8")
        cls.pipeline = (ROOT / "pipeline_manager.py").read_text(encoding="utf-8")

    def test_publish_requires_verified_url(self):
        self.assertNotIn('video_url or "https://studio.youtube.com"', self.uploader)
        self.assertIn("published_uncertain", self.uploader)
        self.assertIn("if not verified_url:", self.uploader)

    def test_no_draft_delete_after_publish_click(self):
        self.assertIn("publish_clicked = True", self.uploader)
        self.assertIn("if publish_clicked:", self.uploader)
        self.assertIn("elif draft_created and page is not None:", self.uploader)

    def test_checks_must_be_confirmed_safe(self):
        self.assertIn("if not checks_safe_seen:", self.uploader)
        self.assertIn("raise RuntimeError(checks_error)", self.uploader)

    def test_age_restriction_is_fail_closed(self):
        self.assertNotIn("VIDEO_AGE_RESTRICTION_SELF", self.uploader)
        self.assertIn('if metadata.get("age_restriction") and not age_confirmed:', self.uploader)
        self.assertIn("raise RuntimeError(age_error)", self.uploader)

    def test_pipeline_marks_uncertain_result_as_error(self):
        self.assertIn('if is_uncertain or not (pub_url or "").strip():', self.pipeline)
        self.assertIn("error_message=uncertain_msg", self.pipeline)


class StopSemanticsTests(unittest.TestCase):
    class _FakeConn:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append((sql, params))
            return self

        def commit(self):
            pass

        def close(self):
            pass

    def test_stop_keeps_uploading_rows_and_running_flag(self):
        manager = pm.PipelineManager.__new__(pm.PipelineManager)
        manager._stop_event = __import__("threading").Event()
        manager._is_running = True
        fake_conn = self._FakeConn()

        with mock.patch.object(pm, "get_conn", return_value=fake_conn), \
                mock.patch.object(pm, "add_log"), \
                mock.patch("render_engine.kill_all_render_processes", lambda: None):
            manager.stop_all()

        self.assertTrue(manager._is_running)
        self.assertTrue(manager._stop_event.is_set())
        self.assertEqual(len(fake_conn.statements), 1)
        sql = fake_conn.statements[0][0]
        self.assertNotIn("Uploading", sql)
        self.assertIn("Pending", sql)


if __name__ == "__main__":
    unittest.main()
