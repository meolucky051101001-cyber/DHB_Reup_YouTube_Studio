import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline_manager as pm


class FakeSettingsStore:
    def __init__(self, data=None, fail_read=False, fail_write=False):
        self.data = dict(data or {})
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.writes = []

    def get_settings(self):
        if self.fail_read:
            raise RuntimeError("database is locked")
        return dict(self.data)

    def save_settings(self, new_settings):
        if self.fail_write:
            raise RuntimeError("disk full")
        self.writes.append(dict(new_settings))
        self.data.update(new_settings)


class UploadGapTests(unittest.TestCase):
    def setUp(self):
        pm._UPLOAD_GAP_STATE_BROKEN = False
        self.store = FakeSettingsStore()
        self.patches = [
            mock.patch.object(pm, "get_settings", self.store.get_settings),
            mock.patch.object(pm, "save_settings", self.store.save_settings),
            mock.patch.object(pm, "add_log", lambda *args, **kwargs: None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        pm._UPLOAD_GAP_STATE_BROKEN = False

    def test_read_returns_zero_when_never_uploaded(self):
        self.assertEqual(pm.read_last_upload_epoch(), 0.0)

    def test_read_raises_when_settings_unreadable(self):
        self.store.fail_read = True
        with self.assertRaises(pm.UploadGapStateError):
            pm.read_last_upload_epoch()

    def test_read_raises_on_malformed_marker(self):
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = "not-a-number"
        with self.assertRaises(pm.UploadGapStateError):
            pm.read_last_upload_epoch()

    def test_read_uses_later_of_start_and_finish(self):
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = "1000"
        self.store.data[pm._LAST_UPLOAD_FINISHED_SETTING_KEY] = "2500"
        self.assertEqual(pm.read_last_upload_epoch(), 2500.0)

    def test_gap_counts_from_finish(self):
        now = time.time()
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = str(now - 40 * 60)
        self.store.data[pm._LAST_UPLOAD_FINISHED_SETTING_KEY] = str(now - 5 * 60)
        remaining = pm.read_upload_gap_remaining(now)
        self.assertAlmostEqual(remaining, 25 * 60, delta=2)

    def test_reserve_fails_closed_when_settings_unreadable(self):
        self.store.fail_read = True
        with self.assertRaises(pm.UploadGapStateError):
            pm.reserve_upload_slot(threading.Event())
        self.assertEqual(self.store.writes, [])

    def test_reserve_refuses_when_write_of_start_marker_fails(self):
        self.store.fail_write = True
        with self.assertRaises(pm.UploadGapStateError):
            pm.reserve_upload_slot(threading.Event())
        self.assertEqual(self.store.writes, [])

    def test_reserve_respects_finish_marker(self):
        now = time.time()
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = str(now - 40 * 60)
        self.store.data[pm._LAST_UPLOAD_FINISHED_SETTING_KEY] = str(now - 2 * 60)
        stop = threading.Event()
        stop.set()
        self.assertFalse(pm.reserve_upload_slot(stop))
        self.assertEqual(self.store.writes, [])

    def test_reserve_allows_when_gap_elapsed_and_records_start(self):
        now = time.time()
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = str(now - 40 * 60)
        self.store.data[pm._LAST_UPLOAD_FINISHED_SETTING_KEY] = str(now - 30 * 60)
        self.assertTrue(pm.reserve_upload_slot(threading.Event()))
        self.assertEqual(len(self.store.writes), 1)
        self.assertIn(pm._LAST_UPLOAD_SETTING_KEY, self.store.writes[0])
        self.assertGreaterEqual(float(self.store.writes[0][pm._LAST_UPLOAD_SETTING_KEY]), now)

    def test_mark_finished_writes_marker(self):
        pm.mark_upload_finished(when=1234.5)
        self.assertEqual(self.store.data[pm._LAST_UPLOAD_FINISHED_SETTING_KEY], "1234.5")

    def test_mark_finished_failure_raises(self):
        self.store.fail_write = True
        with self.assertRaises(pm.UploadGapStateError):
            pm.mark_upload_finished(when=1234.5)

    def test_finish_write_failure_blocks_later_uploads(self):
        self.store.fail_write = True
        pm._record_upload_finished_safely("video test")
        self.assertTrue(pm._UPLOAD_GAP_STATE_BROKEN)
        with self.assertRaises(pm.UploadGapStateError):
            pm.read_last_upload_epoch()

    def test_reserve_waits_then_stops(self):
        self.store.data[pm._LAST_UPLOAD_SETTING_KEY] = str(time.time() - 29 * 60)
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.time()
        self.assertFalse(pm.reserve_upload_slot(stop))
        self.assertLess(time.time() - started, 5)


if __name__ == "__main__":
    unittest.main()
