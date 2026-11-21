import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline_manager as pm


class BatchPrepareDuringGapTests(unittest.TestCase):
    def _run_batch(self, manager, item_ids, workers=1):
        patches = (
            mock.patch.object(pm, 'get_settings', return_value={
                'max_concurrent_threads': workers, 'upload_delay_seconds': 0,
            }),
            mock.patch.object(pm, 'add_log'),
            mock.patch.object(pm, 'release_item_claim'),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        runner = threading.Thread(
            target=manager._batch_worker, args=(item_ids, True, True, True, 'gap-batch'), daemon=True,
        )
        runner.start()
        return runner

    def test_next_item_prepares_while_previous_waits_for_upload_gap(self):
        manager = pm.PipelineManager()
        manager._stop_event.clear()
        started = {i: threading.Event() for i in (1, 2, 3)}
        handed_off = {i: threading.Event() for i in (1, 2, 3)}
        upload_done = {i: threading.Event() for i in (1, 2, 3)}

        def fake_process(item_id, *args, capacity=None, **kwargs):
            started[item_id].set()
            self.assertTrue(capacity.hand_off())
            handed_off[item_id].set()
            upload_done[item_id].wait(5)

        manager.process_single_item = fake_process
        runner = self._run_batch(manager, [1, 2, 3], workers=1)

        self.assertTrue(handed_off[1].wait(5))
        # Item 1 waits for the upload slot without holding the prepare slot.
        self.assertTrue(started[2].wait(5), 'item 2 was not prepared during the upload gap')
        # Only one item may wait ready, so item 2 keeps its prepare slot and item 3 stays queued.
        self.assertFalse(handed_off[2].wait(0.5))
        self.assertFalse(started[3].is_set())

        upload_done[1].set()
        self.assertTrue(handed_off[2].wait(5))
        self.assertTrue(started[3].wait(5))
        upload_done[2].set()
        upload_done[3].set()
        runner.join(5)
        self.assertFalse(runner.is_alive())

    def test_stop_while_waiting_for_ready_slot_releases_prepare_slot(self):
        prepare = threading.Semaphore(1)
        ready = threading.Semaphore(0)
        stop = threading.Event()
        prepare.acquire()
        capacity = pm._WorkerCapacity(prepare, ready, stop)
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertFalse(capacity.hand_off())
        capacity.release()
        self.assertTrue(prepare.acquire(timeout=0))
        self.assertFalse(ready.acquire(timeout=0))

    def test_handed_off_worker_releases_ready_slot(self):
        prepare = threading.Semaphore(1)
        ready = threading.Semaphore(1)
        prepare.acquire()
        capacity = pm._WorkerCapacity(prepare, ready, threading.Event())
        self.assertTrue(capacity.hand_off())
        self.assertTrue(capacity.hand_off())
        self.assertTrue(prepare.acquire(timeout=0))
        self.assertFalse(ready.acquire(timeout=0))
        capacity.release()
        self.assertTrue(ready.acquire(timeout=0))


if __name__ == '__main__':
    unittest.main()
