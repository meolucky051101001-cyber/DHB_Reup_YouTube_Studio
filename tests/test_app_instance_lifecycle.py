import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import pipeline_manager
from app_instance_lock import AppInstanceAlreadyRunning, acquire_app_instance_lock


class AppInstanceLockTests(unittest.TestCase):
    def test_one_process_cannot_acquire_the_same_data_lock_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "app.lock"
            first = acquire_app_instance_lock(lock_path)
            try:
                with self.assertRaises(AppInstanceAlreadyRunning):
                    acquire_app_instance_lock(lock_path)
            finally:
                first.release()

            second = acquire_app_instance_lock(lock_path)
            second.release()

    def test_second_process_is_rejected_while_first_holds_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "app.lock"
            first = acquire_app_instance_lock(lock_path)
            code = """import sys
from pathlib import Path
from app_instance_lock import acquire_app_instance_lock, AppInstanceAlreadyRunning
try:
    acquire_app_instance_lock(Path(sys.argv[1]))
except AppInstanceAlreadyRunning:
    raise SystemExit(23)
raise SystemExit(0)
"""
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            try:
                result = subprocess.run(
                    [sys.executable, "-c", code, str(lock_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=env,
                    check=False,
                )
                self.assertEqual(result.returncode, 23, result.stderr)
            finally:
                first.release()


class InterruptedJobRecoveryTests(unittest.TestCase):
    def test_uploads_need_review_and_preupload_jobs_return_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE video_queue (
                    id INTEGER PRIMARY KEY,
                    status TEXT,
                    error_message TEXT,
                    claim_token TEXT,
                    published_url TEXT DEFAULT ''
                )
            """)
            conn.executemany(
                "INSERT INTO video_queue (id, status, error_message, claim_token, published_url) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "Uploading -> Channel A", "", "claim-1", ""),
                    (2, "Rendering...", "", "claim-2", ""),
                    (3, "Completed", "", "claim-3", "https://youtu.be/verified"),
                    (4, "Đã bấm đăng nhưng CHƯA xác minh URL video!", "", "claim-4", ""),
                ],
            )
            conn.commit()
            conn.close()

            with mock.patch.object(database, "APP_DB_PATH", db_path), \
                    mock.patch.object(pipeline_manager, "add_log"):
                pipeline_manager.PipelineManager().clear_stale_claims()

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT id, status, error_message, claim_token FROM video_queue ORDER BY id"
            ).fetchall()
            conn.close()

            self.assertEqual(rows[0][1], "NeedsReview")
            self.assertIn("YouTube Studio", rows[0][2])
            self.assertEqual(rows[1][1], "Pending")
            self.assertEqual(rows[1][2], "Tiến trình cũ bị ngắt trước bước đăng; có thể tiếp tục xử lý.")
            self.assertEqual(rows[2][1], "Completed")
            self.assertEqual(rows[3][1], "NeedsReview")
            self.assertTrue(all(row[3] == "" for row in rows))


if __name__ == "__main__":
    unittest.main()
