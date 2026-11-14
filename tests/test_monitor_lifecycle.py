"""Regression tests for monitor start/stop serialization and worker ownership."""
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import pipeline_manager as pm


class MonitorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._old_paths = {
            "APP_DB_PATH": database.APP_DB_PATH,
            "APP_DATA_DIR": database.APP_DATA_DIR,
            "APP_TEMP_DIR": database.APP_TEMP_DIR,
            "APP_PROFILES_DIR": database.APP_PROFILES_DIR,
        }
        self.temp = Path(tempfile.mkdtemp(prefix="dhb_monitor_lifecycle_"))
        database.APP_DB_PATH = self.temp / "data" / "test.sqlite"
        database.APP_DATA_DIR = self.temp / "data"
        database.APP_TEMP_DIR = self.temp / "temp"
        database.APP_PROFILES_DIR = self.temp / "profiles"
        for folder in (database.APP_DATA_DIR, database.APP_TEMP_DIR, database.APP_PROFILES_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        database.init_db()
        conn = database.get_conn()
        conn.execute("INSERT INTO monitors (name, source_urls) VALUES ('Lifecycle test', 'https://example.test/source')")
        conn.commit()
        self.monitor_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        from monitor_fixture import confirm_monitor
        confirm_monitor(conn, self.monitor_id)
        conn.commit()
        conn.close()

        self.manager = pm.PipelineManager()
        self.manager._start_monitor_heartbeat = lambda *args, **kwargs: None
        self.manager.start_monitor_supervisor = lambda: None
        self.log_patch = mock.patch.object(pm, "add_log")
        self.log_patch.start()
        self.release_first = threading.Event()
        self.worker_entered = threading.Event()
        self.active = 0
        self.max_active = 0
        self.active_lock = threading.Lock()
        self.run_count = 0

    def tearDown(self):
        self.release_first.set()
        with self.manager._monitor_lock:
            handles = [self.manager._monitor_workers.get(self.monitor_id),
                       self.manager._monitor_heartbeats.get(self.monitor_id)]
        self.manager.stop_monitor(self.monitor_id)
        for handle in handles:
            if handle is not None and handle.is_alive():
                handle.join(2)
        self.log_patch.stop()
        database.APP_DB_PATH = self._old_paths["APP_DB_PATH"]
        database.APP_DATA_DIR = self._old_paths["APP_DATA_DIR"]
        database.APP_TEMP_DIR = self._old_paths["APP_TEMP_DIR"]
        database.APP_PROFILES_DIR = self._old_paths["APP_PROFILES_DIR"]
        shutil.rmtree(self.temp, ignore_errors=True)

    def _controlled_loop(self, _monitor_id, stop_event):
        with self.active_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.run_count += 1
            run_number = self.run_count
        self.worker_entered.set()
        try:
            if run_number == 1:
                self.release_first.wait(4)
            else:
                stop_event.wait(4)
        finally:
            with self.active_lock:
                self.active -= 1

    def test_start_is_blocked_until_stopped_worker_and_heartbeat_exit(self):
        self.manager._monitor_loop = self._controlled_loop
        first = self.manager.start_monitor(self.monitor_id)
        self.assertTrue(first["ok"], first)
        self.assertTrue(self.worker_entered.wait(2))
        with self.manager._monitor_lock:
            old_worker = self.manager._monitor_workers[self.monitor_id]

        stopped = self.manager.stop_monitor(self.monitor_id)
        self.assertEqual(stopped["lifecycle_state"], "Stopping")
        self.assertTrue(self.manager._monitor_threads[self.monitor_id].is_set())
        self.assertTrue(old_worker.is_alive())
        self.manager._update_monitor_status(self.monitor_id, "worker overwrote stopping state")
        conn = database.get_conn()
        self.assertEqual(conn.execute("SELECT status FROM monitors WHERE id = ?", (self.monitor_id,)).fetchone()[0], "Stopping")
        conn.close()
        blocked = self.manager.start_monitor(self.monitor_id)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["lifecycle_state"], "Stopping")
        self.assertEqual(self.run_count, 1)
        self.assertFalse(self.manager.delete_monitor(self.monitor_id)["ok"])

        # A stale supervisor recovery request must respect the persisted Stop.
        self.manager._restart_dead_monitor(self.monitor_id, "Lifecycle test")
        self.manager._check_monitors_health()
        self.assertEqual(self.run_count, 1)
        self.release_first.set()
        old_worker.join(2)
        self.assertFalse(old_worker.is_alive())
        self.manager._check_monitors_health()
        self.assertEqual(self.manager.refresh_monitor_states([self.monitor_id])[self.monitor_id], "Stopped")

        second = self.manager.start_monitor(self.monitor_id)
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.run_count, 2)
        self.manager.stop_monitor(self.monitor_id)
        with self.manager._monitor_lock:
            new_worker = self.manager._monitor_workers[self.monitor_id]
        new_worker.join(2)
        self.assertFalse(new_worker.is_alive())
        self.manager.refresh_monitor_states([self.monitor_id])
        self.assertEqual(self.max_active, 1)
        self.assertTrue(self.manager.delete_monitor(self.monitor_id)["ok"])

    def test_worker_thread_start_failure_rolls_back_running_state(self):
        class FailingThread:
            ident = None
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                raise OSError("simulated thread start failure")
            def is_alive(self):
                return False

        proxy = SimpleNamespace(Event=threading.Event, Thread=FailingThread)
        with mock.patch.object(pm, "threading", proxy):
            result = self.manager.start_monitor(self.monitor_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["lifecycle_state"], "Error")
        self.assertFalse(self.manager._monitor_handles_alive(self.monitor_id))
        conn = database.get_conn()
        row = conn.execute("SELECT is_running, status FROM monitors WHERE id = ?", (self.monitor_id,)).fetchone()
        conn.close()
        self.assertEqual(row["is_running"], 0)
        self.assertEqual(row["status"], "Không thể tạo worker monitor.")

    def test_thread_that_starts_then_raises_remains_registered_until_exit(self):
        class PartialStartThread:
            def __init__(self, *args, **kwargs):
                self.thread = threading.Thread(*args, **kwargs)
            @property
            def ident(self):
                return self.thread.ident
            def start(self):
                self.thread.start()
                raise OSError("simulated late start failure")
            def is_alive(self):
                return self.thread.is_alive()
            def join(self, timeout=None):
                return self.thread.join(timeout)

        self.manager._monitor_loop = self._controlled_loop
        proxy = SimpleNamespace(Event=threading.Event, Thread=PartialStartThread)
        with mock.patch.object(pm, "threading", proxy):
            result = self.manager.start_monitor(self.monitor_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["lifecycle_state"], "Stopping")
        self.assertTrue(self.worker_entered.wait(2))
        with self.manager._monitor_lock:
            old_worker = self.manager._monitor_workers[self.monitor_id]
        self.assertTrue(old_worker.is_alive())
        self.assertFalse(self.manager.start_monitor(self.monitor_id)["ok"])
        self.assertEqual(self.run_count, 1)
        self.release_first.set()
        old_worker.join(2)
        self.assertFalse(old_worker.is_alive())
        self.assertEqual(self.manager.refresh_monitor_states([self.monitor_id])[self.monitor_id], "Stopped")

    def test_stop_db_failure_keeps_generation_blocked_until_stop_is_durable(self):
        self.manager._monitor_loop = self._controlled_loop
        started = self.manager.start_monitor(self.monitor_id)
        self.assertTrue(started["ok"], started)
        self.assertTrue(self.worker_entered.wait(2))
        with self.manager._monitor_lock:
            worker = self.manager._monitor_workers[self.monitor_id]

        with mock.patch.object(pm, "get_conn", side_effect=sqlite3.OperationalError("simulated database busy")):
            stopped = self.manager.stop_monitor(self.monitor_id)
            self.assertFalse(stopped["ok"])
            self.assertEqual(stopped["lifecycle_state"], "Stopping")
        self.release_first.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

        with mock.patch.object(pm, "get_conn", side_effect=sqlite3.OperationalError("simulated database busy")):
            self.assertEqual(self.manager.refresh_monitor_states([self.monitor_id])[self.monitor_id], "Stopping")
            self.assertFalse(self.manager.start_monitor(self.monitor_id)["ok"])

        conn = database.get_conn()
        self.assertEqual(conn.execute("SELECT is_running FROM monitors WHERE id = ?", (self.monitor_id,)).fetchone()[0], 1)
        conn.close()
        self.assertEqual(self.manager.refresh_monitor_states([self.monitor_id])[self.monitor_id], "Stopped")
        conn = database.get_conn()
        self.assertEqual(conn.execute("SELECT is_running FROM monitors WHERE id = ?", (self.monitor_id,)).fetchone()[0], 0)
        conn.close()

    def test_concurrent_start_requests_create_only_one_worker(self):
        self.manager._monitor_loop = self._controlled_loop
        gate = threading.Barrier(3)
        results = []

        def start():
            gate.wait()
            results.append(self.manager.start_monitor(self.monitor_id))

        callers = [threading.Thread(target=start), threading.Thread(target=start)]
        for caller in callers:
            caller.start()
        gate.wait()
        for caller in callers:
            caller.join(3)
            self.assertFalse(caller.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertTrue(self.worker_entered.wait(2))
        self.assertEqual(self.run_count, 1)
        self.assertEqual(self.max_active, 1)
