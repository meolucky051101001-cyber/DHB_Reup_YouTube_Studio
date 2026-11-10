"""Batch orchestration with real SQLite claims and a fake processing boundary."""
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import pipeline_manager as pm


class BatchRealClaimTests(unittest.TestCase):
    def setUp(self):
        self._old_paths = {
            "APP_DB_PATH": database.APP_DB_PATH,
            "APP_DATA_DIR": database.APP_DATA_DIR,
            "APP_TEMP_DIR": database.APP_TEMP_DIR,
            "APP_PROFILES_DIR": database.APP_PROFILES_DIR,
        }
        self.temp = Path(tempfile.mkdtemp(prefix="dhb_batch_claims_"))
        database.APP_DB_PATH = self.temp / "data" / "test.sqlite"
        database.APP_DATA_DIR = self.temp / "data"
        database.APP_TEMP_DIR = self.temp / "temp"
        database.APP_PROFILES_DIR = self.temp / "profiles"
        for folder in (database.APP_DATA_DIR, database.APP_TEMP_DIR, database.APP_PROFILES_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        database.init_db()
        conn = database.get_conn()
        conn.executemany(
            "INSERT INTO video_queue (project_name, title, status) VALUES (?, ?, 'Pending')",
            [("Batch claim integration", f"Item {index}") for index in range(100)],
        )
        conn.commit()
        self.item_ids = [int(row[0]) for row in conn.execute(
            "SELECT id FROM video_queue WHERE project_name = ? ORDER BY id", ("Batch claim integration",)
        )]
        conn.close()
        self.manager = pm.PipelineManager()
        self.manager._stop_event.clear()

    def tearDown(self):
        database.APP_DB_PATH = self._old_paths["APP_DB_PATH"]
        database.APP_DATA_DIR = self._old_paths["APP_DATA_DIR"]
        database.APP_TEMP_DIR = self._old_paths["APP_TEMP_DIR"]
        database.APP_PROFILES_DIR = self._old_paths["APP_PROFILES_DIR"]
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_one_hundred_items_use_real_claim_and_release_with_bounded_workers(self):
        barrier = threading.Barrier(8)
        active_lock = threading.Lock()
        active = 0
        peak_active = 0
        claimed_ids = set()
        failed_ids = {self.item_ids[index] for index in (4, 19, 37, 58, 79, 96)}

        def claim_then_process(item_id, _row_idx, *_args, claim_token="", **_kwargs):
            nonlocal active, peak_active
            conn = database.get_conn()
            cursor = conn.execute(
                "UPDATE video_queue SET claim_token = ? WHERE id = ? AND COALESCE(claim_token, '') = ''",
                (claim_token, item_id),
            )
            conn.commit()
            conn.close()
            if cursor.rowcount != 1:
                raise AssertionError(f"item {item_id} did not acquire its SQLite claim")
            with active_lock:
                claimed_ids.add(item_id)
                active += 1
                peak_active = max(peak_active, active)
                wait_for_gate = len(claimed_ids) <= 8
            try:
                if wait_for_gate:
                    barrier.wait(5)
                if item_id in failed_ids:
                    raise RuntimeError("simulated worker failure after acquiring claim")
            finally:
                with active_lock:
                    active -= 1

        self.manager.process_single_item = claim_then_process
        with mock.patch.object(pm, "get_settings", return_value={
            "max_concurrent_threads": 8,
            "upload_delay_seconds": 0,
        }), mock.patch.object(pm, "add_log"):
            self.manager._batch_worker_guarded(
                self.item_ids, False, False, False, "real-claim-batch", force_reupload=False,
            )

        conn = database.get_conn()
        rows = conn.execute(
            "SELECT id, claim_token FROM video_queue WHERE project_name = ? ORDER BY id",
            ("Batch claim integration",),
        ).fetchall()
        conn.close()
        remaining_claims = [int(row["id"]) for row in rows if row["claim_token"]]
        self.assertEqual(len(claimed_ids), 100)
        self.assertEqual(peak_active, 8)
        self.assertEqual(remaining_claims, [])
        self.assertTrue(failed_ids.issubset(claimed_ids))
        self.assertFalse(self.manager.is_running)

    def test_killed_child_process_recovers_safe_work_and_quarantines_upload(self):
        crash_root = self.temp / "process-crash"
        data_dir = crash_root / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "crash.sqlite"
        marker = crash_root / "ready.marker"
        child_code = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import database
root = Path(sys.argv[2])
database.APP_DB_PATH = root / 'data' / 'crash.sqlite'
database.APP_DATA_DIR = root / 'data'
database.APP_TEMP_DIR = root / 'temp'
database.APP_PROFILES_DIR = root / 'profiles'
database.APP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
database.APP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
database.init_db()
conn = database.get_conn()
conn.execute("INSERT INTO video_queue (project_name, title, status, claim_token) VALUES ('Crash', 'download', 'Downloading 35%', 'dead-download')")
conn.execute("INSERT INTO video_queue (project_name, title, status, claim_token) VALUES ('Crash', 'upload', 'Uploading -> Test', 'dead-upload')")
conn.commit()
conn.close()
Path(sys.argv[3]).write_text('ready', encoding='utf-8')
time.sleep(60)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(ROOT), str(crash_root), str(marker)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "Child process did not commit its claims in time")
            child.kill()
            child.wait(timeout=10)

            old_paths = {
                "APP_DB_PATH": database.APP_DB_PATH,
                "APP_DATA_DIR": database.APP_DATA_DIR,
                "APP_TEMP_DIR": database.APP_TEMP_DIR,
                "APP_PROFILES_DIR": database.APP_PROFILES_DIR,
            }
            database.APP_DB_PATH = db_path
            database.APP_DATA_DIR = data_dir
            database.APP_TEMP_DIR = crash_root / "temp"
            database.APP_PROFILES_DIR = crash_root / "profiles"
            try:
                pm.PipelineManager().clear_stale_claims()
                conn = database.get_conn()
                rows = {row["title"]: dict(row) for row in conn.execute(
                    "SELECT title, status, claim_token, error_message FROM video_queue WHERE project_name = 'Crash'"
                )}
                conn.close()
            finally:
                database.APP_DB_PATH = old_paths["APP_DB_PATH"]
                database.APP_DATA_DIR = old_paths["APP_DATA_DIR"]
                database.APP_TEMP_DIR = old_paths["APP_TEMP_DIR"]
                database.APP_PROFILES_DIR = old_paths["APP_PROFILES_DIR"]

            self.assertEqual(rows["download"]["status"], "Pending")
            self.assertEqual(rows["download"]["claim_token"], "")
            self.assertEqual(rows["upload"]["status"], "NeedsReview")
            self.assertEqual(rows["upload"]["claim_token"], "")
            self.assertIn("Studio", rows["upload"]["error_message"])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
