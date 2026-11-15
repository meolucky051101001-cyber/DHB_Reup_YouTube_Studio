"""Regressions for upload approval, Antidetect readiness and global ownership."""
import tempfile
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import antidetect_client
import database
import pipeline_manager as pm


class UploadHardeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enter_db = mock.patch.object(database, "APP_DB_PATH", Path(temp.name) / "test.sqlite")
        self.enter_db.start()
        self.addCleanup(self.enter_db.stop)
        for name, value in (("_UPLOAD_GAP_STATE_BROKEN", False), ("_COOLDOWN_ANCHOR", None)):
            patcher = mock.patch.object(pm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        self.video = Path(temp.name) / "fixture.mp4"
        self.video.write_bytes(b"mock uploader fixture")
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO youtube_channels (id, name, channel_id,studio_category_mode) VALUES (901, 'First', ?,'keep')", ("UC" + "a" * 22,))
            conn.execute(
                "INSERT INTO video_queue (id, project_name, title, target_type, target_channel_id, source_type, input_video_path) "
                "VALUES (901, 'test', 'Video', 'CHANNEL', 901, 'file', ?)", (str(self.video),),
            )

    def test_start_rejects_changed_channel(self):
        manager = pm.PipelineManager()
        result = manager.preview_distribution([901])
        token = result.get("preview_token") if isinstance(result, dict) else None
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET channel_id = ? WHERE id = 901", ("UC" + "b" * 22,))
        with mock.patch.object(pm.threading, "Thread") as thread:
            if token:
                result = manager.start_batch([901], preview_token=token)
            else:
                result = manager.start_batch([901])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "PREVIEW_STALE")
        thread.assert_not_called()

    def test_same_start_replays_and_claim_blocks_rebinding(self):
        manager = pm.PipelineManager()
        token = manager.preview_distribution([901])["preview_token"]
        with mock.patch.object(pm.threading, "Thread") as thread:
            first = manager.start_batch([901], preview_token=token)
            second = manager.start_batch([901], preview_token=token)
        self.assertTrue(first["ok"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertTrue(second["replayed"])
        thread.assert_called_once()
        with closing(database.get_conn()) as conn, self.assertRaisesRegex(sqlite3.IntegrityError, "UPLOAD_BINDING_BUSY"):
            conn.execute("UPDATE youtube_channels SET channel_id = 'replacement' WHERE id = 901")

    def test_preview_reports_render_output_and_rejects_missing_file(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET output_video_path = ? WHERE id = 901", (str(self.video),))
        manager = pm.PipelineManager()
        options = [False, False, True]
        preview = manager.preview_distribution([901], options)
        self.assertEqual(preview["items"][0]["file_kind"], "bản render có sẵn")
        self.assertEqual(preview["items"][0]["file_name"], self.video.name)
        self.video.unlink()
        self.assertFalse(manager.start_batch([901], *options, preview_token=preview["preview_token"])["ok"])
        self.assertTrue(manager.preview_distribution([901], options)["items"][0]["blocked"])

    def test_preview_binds_upload_format_and_rechecks_recovery_state(self):
        manager = pm.PipelineManager()
        options = [False, False, True]
        preview = manager.preview_distribution([901], options)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET upload_to = 'Video' WHERE id = 901")
        self.assertEqual(manager.start_batch([901], *options, preview_token=preview["preview_token"])["code"], "PREVIEW_STALE")
        preview = manager.preview_distribution([901], options)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE upload_slot SET state = 'recovery_required' WHERE id = 1")
        self.assertEqual(manager.start_batch([901], *options, preview_token=preview["preview_token"])["code"], "PREVIEW_STALE")
        self.assertTrue(manager.preview_distribution([901], options)["items"][0]["blocked"])

    def test_failed_thread_launch_rolls_back_claim_and_can_retry(self):
        manager = pm.PipelineManager()
        token = manager.preview_distribution([901])["preview_token"]
        with mock.patch.object(pm.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("test launch failure")
            self.assertFalse(manager.start_batch([901], preview_token=token)["ok"])
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT claim_token FROM video_queue WHERE id=901").fetchone()[0], "")
        with mock.patch.object(pm.threading, "Thread"):
            self.assertTrue(manager.start_batch([901], preview_token=token)["ok"])

    def test_approved_worker_validates_before_uploader_and_records_result(self):
        manager = pm.PipelineManager()
        options = [False, False, True]
        token = manager.preview_distribution([901], options)["preview_token"]
        manager._freeze_batch_assignments([901], token, options, "approved-worker")

        def upload(**kwargs):
            kwargs["channel"]["_verify_approval"]()
            self.assertEqual(kwargs["channel"]["channel_id"], "UC" + "a" * 22)
            return {"url": "https://youtu.be/dQw4w9WgXcQ"}

        with mock.patch.object(pm, "upload_video_to_youtube", side_effect=upload) as uploader:
            manager._process_single_item_safe(901, 0, *options, threading.Semaphore(0), "approved-worker")
        uploader.assert_called_once()
        with closing(database.get_conn()) as conn:
            row = conn.execute("SELECT status, claim_token FROM video_queue WHERE id = 901").fetchone()
            self.assertEqual(tuple(row), ("Completed", ""))
        self.assertEqual(pm.upload_slot_recovery_status()["state"], "cooldown")

    def test_missing_baseline_is_not_sent_and_stops_the_batch(self):
        manager = pm.PipelineManager()
        options = [False, False, True]
        token = manager.preview_distribution([901], options)["preview_token"]
        manager._freeze_batch_assignments([901], token, options, "baseline-blocked")
        with mock.patch.object(pm, "upload_video_to_youtube", side_effect=pm.UploadNotSent("baseline unavailable")):
            manager._process_single_item_safe(901, 0, *options, threading.Semaphore(0), "baseline-blocked")
        with closing(database.get_conn()) as conn:
            self.assertEqual(tuple(conn.execute("SELECT status, claim_token FROM video_queue WHERE id=901").fetchone()), ("Error", ""))
            self.assertEqual(conn.execute("SELECT outcome FROM upload_attempts WHERE queue_id=901").fetchone()[0], "NotSent")
            self.assertEqual(conn.execute("SELECT count(*) FROM content_reservations WHERE queue_id=901").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM published_history").fetchone()[0], 0)
        self.assertTrue(manager._stop_event.is_set())
        self.assertEqual(pm.upload_slot_recovery_status()["state"], "cooldown")

    def test_unknown_uploader_error_still_requires_review(self):
        manager = pm.PipelineManager()
        options = [False, False, True]
        token = manager.preview_distribution([901], options)["preview_token"]
        manager._freeze_batch_assignments([901], token, options, "unknown-dispatch")
        with mock.patch.object(pm, "upload_video_to_youtube", side_effect=RuntimeError("connection lost")):
            manager._process_single_item_safe(901, 0, *options, threading.Semaphore(0), "unknown-dispatch")
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM video_queue WHERE id=901").fetchone()[0], "NeedsReview")
            self.assertEqual(conn.execute("SELECT state FROM content_reservations WHERE queue_id=901").fetchone()[0], "NeedsReview")

    def test_health_rechecks_launch_identity_before_tab_creation(self):
        client = antidetect_client.AntidetectClient()
        state = {"port": 12345, "status": "running", "cdp_health": {"state": "healthy", "launch_id": "new"}}
        context = mock.Mock()
        with mock.patch.object(client, "get_profile_state", return_value=state):
            tracked = antidetect_client._TrackedContext(context, [], lambda: client.require_ready("p", "old", 12345))
            with self.assertRaises(antidetect_client.AntidetectNotReady):
                tracked.new_page()
        context.new_page.assert_not_called()

    def test_degraded_start_is_rejected(self):
        client = antidetect_client.AntidetectClient()
        with mock.patch.object(client, "_request", return_value={
            "success": True, "port": 12345, "degraded": True, "cdp_ready": False,
        }):
            with self.assertRaises(antidetect_client.AntidetectError):
                client.start_profile("profile")

    def test_an_active_upload_survives_elapsed_gap(self):
        with mock.patch.object(pm.time, "time", return_value=100000):
            self.assertTrue(pm.reserve_upload_slot(threading.Event()))
        stop = threading.Event()
        with mock.patch.object(pm.time, "time", return_value=101801), mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            self.assertFalse(pm.reserve_upload_slot(stop))
