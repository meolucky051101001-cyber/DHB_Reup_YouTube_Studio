"""Regression coverage for strict probes, render output contracts and artifact cleanup."""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import render_engine
import scanner_downloader
import storage_manager


class StrictProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dhb_phase5_probe_"))
        self.video = self.tmp / "video test.mp4"
        self.video.write_bytes(b"fixture")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _result(self, payload, returncode=0, stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr=stderr)

    def test_probe_rejects_file_without_video_stream(self):
        with mock.patch.object(scanner_downloader.subprocess, "run", return_value=self._result({"streams": []})) as run:
            with self.assertRaises(scanner_downloader.VideoProbeError):
                scanner_downloader.probe_video_info(str(self.video))
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_probe_rejects_unknown_duration(self):
        payload = {"streams": [{"codec_type": "video", "width": 1280, "height": 720}]}
        with mock.patch.object(scanner_downloader.subprocess, "run", return_value=self._result(payload)):
            with self.assertRaisesRegex(scanner_downloader.VideoProbeError, "thời lượng"):
                scanner_downloader.probe_video_info(str(self.video))

    def test_probe_reports_valid_video_dimensions_and_audio(self):
        payload = {
            "format": {"duration": "2.5"},
            "streams": [
                {"codec_type": "video", "width": 1280, "height": 720},
                {"codec_type": "audio"},
            ],
        }
        with mock.patch.object(scanner_downloader.subprocess, "run", return_value=self._result(payload)):
            result = scanner_downloader.probe_video_info(str(self.video))
        self.assertEqual((result["seconds"], result["width"], result["height"]), (2.5, 1280, 720))
        self.assertTrue(result["has_video"])
        self.assertTrue(result["has_audio"])


class RenderContractTests(unittest.TestCase):
    def test_speed_limits_are_finite_and_shared(self):
        for speed in (0.5, 1.0, 2.0):
            self.assertEqual(render_engine.normalize_render_speed(speed), speed)
        for speed in (0.49, 2.01, float("nan"), "fast"):
            with self.assertRaises(ValueError):
                render_engine.normalize_render_speed(speed)

    def test_manifest_and_log_failure_do_not_fail_a_valid_render_output(self):
        with tempfile.TemporaryDirectory(prefix="dhb_phase5_finalize_") as td:
            output = Path(td) / "valid output.mp4"
            output.write_bytes(b"video")
            probe = {"width": 1920, "height": 1080, "seconds": 2.0, "has_audio": True}
            with mock.patch.object(render_engine, "probe_video_info", return_value=probe), \
                    mock.patch("database.register_managed_artifact", side_effect=sqlite3.OperationalError("db down")), \
                    mock.patch("database.add_log", side_effect=sqlite3.OperationalError("logs unavailable")):
                result = render_engine._finalize_render_output(
                    str(output), expected_width=1920, expected_height=1080,
                    expected_duration=2.0, require_audio=True,
                )
            self.assertEqual(result, probe)
            self.assertTrue(output.is_file())


class StorageOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dhb_phase5_storage_"))
        self.old_db_path, self.old_data_dir = database.APP_DB_PATH, database.APP_DATA_DIR
        self.old_temp_dir, self.old_output_dir = storage_manager.APP_TEMP_DIR, storage_manager.APP_OUTPUT_DIR
        self.data_dir = self.tmp / "data"
        self.temp_dir = self.tmp / "temp"
        self.output_dir = self.tmp / "output"
        self.outside_dir = self.tmp / "temp_neighbor"
        for path in (self.data_dir, self.temp_dir, self.output_dir, self.outside_dir):
            path.mkdir(parents=True, exist_ok=True)
        database.APP_DATA_DIR = self.data_dir
        database.APP_DB_PATH = self.data_dir / "test.sqlite"
        storage_manager.APP_TEMP_DIR = self.temp_dir
        storage_manager.APP_OUTPUT_DIR = self.output_dir
        database.init_db()

    def tearDown(self):
        database.APP_DB_PATH, database.APP_DATA_DIR = self.old_db_path, self.old_data_dir
        storage_manager.APP_TEMP_DIR, storage_manager.APP_OUTPUT_DIR = self.old_temp_dir, self.old_output_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_old(self, path, kind="render_output", owner_id=None):
        artifact_id = database.register_managed_artifact(str(path), kind, "queue", owner_id)
        conn = database.get_conn()
        try:
            conn.execute(
                "UPDATE managed_artifacts SET registered_at = datetime('now', '-2 days') WHERE id = ?",
                (artifact_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return artifact_id

    def test_cleanup_deletes_selected_owned_file_and_rejects_neighbor_path(self):
        owned = self.temp_dir / "owned.bin"
        neighbor = self.outside_dir / "keep.bin"
        owned.write_bytes(b"owned")
        neighbor.write_bytes(b"keep")
        owned_id = self._register_old(owned)
        neighbor_id = self._register_old(neighbor)

        result = storage_manager.cleanup_managed_artifacts([owned_id, neighbor_id], retention_days=1)

        self.assertFalse(owned.exists())
        self.assertTrue(neighbor.exists())
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["skipped"][0]["id"], neighbor_id)

    def test_needs_review_queue_reference_is_protected(self):
        source = self.temp_dir / "review source.mp4"
        source.write_bytes(b"source")
        conn = database.get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO video_queue (project_name, title, status, input_video_path) VALUES (?, ?, ?, ?)",
                ("Phase 5", "Needs review", "NeedsReview", str(source)),
            )
            queue_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        artifact_id = self._register_old(source, owner_id=queue_id)

        result = storage_manager.cleanup_managed_artifacts([artifact_id], retention_days=1)

        self.assertTrue(source.exists())
        self.assertEqual(result["deleted_count"], 0)
        self.assertIn("NeedsReview", result["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
