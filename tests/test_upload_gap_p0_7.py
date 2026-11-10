"""Persistent upload owner, cooldown and interrupted-session recovery."""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import database
import pipeline_manager as pm


class UploadGapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(database, "APP_DB_PATH", Path(temp.name) / "test.sqlite").start()
        mock.patch.object(pm, "_COOLDOWN_ANCHOR", None).start()
        mock.patch.object(pm, "_UPLOAD_GAP_STATE_BROKEN", False).start()
        database.init_db()
        self.wall = 100000
        self.monotonic = 50
        mock.patch.object(pm.time, "time", side_effect=lambda: self.wall).start()
        mock.patch.object(pm.time, "monotonic", side_effect=lambda: self.monotonic).start()

    def stop_after_wait(self):
        stop = threading.Event()
        stop.wait = mock.Mock(side_effect=lambda _: stop.set())
        return stop

    def test_active_owner_cannot_expire_and_cooldown_counts_from_finish(self):
        owner = pm.reserve_upload_slot(threading.Event(), 101)
        pm._mark_slot_entered(owner, 201)
        self.wall += 1900
        self.monotonic += 1900
        self.assertFalse(pm.reserve_upload_slot(self.stop_after_wait(), 102))
        pm._record_upload_finished_safely("A", owner, entered_uploader=True)
        self.wall += 5000
        self.monotonic += 1799
        self.assertFalse(pm.reserve_upload_slot(self.stop_after_wait(), 102))
        self.monotonic += 1
        newer = pm.reserve_upload_slot(threading.Event(), 102)
        self.assertNotEqual(newer, owner)
        pm._record_upload_finished_safely("old callback", owner, entered_uploader=True)
        self.assertEqual(pm.upload_slot_recovery_status()["queue_id"], 102)
        self.assertEqual(pm.upload_slot_recovery_status()["state"], "active")

    def test_concurrent_workers_only_one_owner(self):
        barrier = threading.Barrier(2)
        owners = []

        def reserve(queue_id):
            barrier.wait(2)
            owners.append(pm.reserve_upload_slot(self.stop_after_wait(), queue_id))

        threads = [threading.Thread(target=reserve, args=(idx,)) for idx in (101, 102)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(len(owners), 2)
        self.assertEqual(sum(bool(owner) for owner in owners), 1)

    def test_recovery_requires_acknowledgment_and_keeps_cooldown(self):
        owner = pm.reserve_upload_slot(threading.Event(), 101)
        pm._mark_slot_entered(owner, 201)
        with closing(database.get_conn()) as conn, conn:
            pm._recover_upload_slot(conn)
        with self.assertRaises(pm.UploadGapStateError):
            pm.reserve_upload_slot(threading.Event())
        status = pm.upload_slot_recovery_status()
        self.assertEqual(status["attempt_id"], 201)
        self.assertFalse(pm.acknowledge_upload_slot_recovery("old-approval"))
        self.assertTrue(pm.acknowledge_upload_slot_recovery(status["recovery_id"]))
        self.assertFalse(pm.reserve_upload_slot(self.stop_after_wait()))

    def test_legacy_markers_preserved_on_repeat_migration(self):
        database.save_settings({pm._LAST_UPLOAD_SETTING_KEY: "99900", pm._LAST_UPLOAD_FINISHED_SETTING_KEY: "99990"})
        for _ in range(2):
            database.init_db()
            with closing(database.get_conn()) as conn, conn:
                pm._recover_upload_slot(conn)
        self.assertEqual(pm.upload_slot_recovery_status()["cooldown_until"], 101790)
        self.assertFalse(pm.reserve_upload_slot(self.stop_after_wait()))

    def test_legacy_nonfinite_marker_is_invalid(self):
        for value in ("nan", "inf", "-1", "invalid"):
            with self.subTest(value=value), self.assertRaises(pm.UploadGapStateError):
                pm._parse_upload_epoch(value, pm._LAST_UPLOAD_SETTING_KEY)

    def test_db_failure_never_admits_and_failed_finish_keeps_owner(self):
        with mock.patch.object(pm, "_slot_connection", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(pm.UploadGapStateError):
                pm.reserve_upload_slot(threading.Event())
        owner = pm.reserve_upload_slot(threading.Event())
        pm._mark_slot_entered(owner, 201)
        with mock.patch.object(pm, "_slot_connection", side_effect=sqlite3.OperationalError("disk full")):
            pm._record_upload_finished_safely("A", owner, entered_uploader=True)
        self.assertEqual(pm.upload_slot_recovery_status()["state"], "active")
        with self.assertRaises(pm.UploadGapStateError):
            pm.reserve_upload_slot(threading.Event())

    def test_stopped_worker_cannot_claim_and_preflight_cancel_does_not_hold_slot(self):
        stop = threading.Event()
        stop.set()
        self.assertFalse(pm.reserve_upload_slot(stop))
        owner = pm.reserve_upload_slot(threading.Event())
        pm._record_upload_finished_safely("A", owner, entered_uploader=False)
        self.assertEqual(pm.upload_slot_recovery_status()["state"], "idle")
