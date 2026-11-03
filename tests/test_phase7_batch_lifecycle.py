import sqlite3
import threading
import unittest
from unittest import mock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline_manager as pm


class _ThreadHandle:
    def __init__(self, thread, fail_start=False, start_failed=None):
        self.thread = thread
        self.fail_start = fail_start
        self.start_failed = start_failed

    @property
    def ident(self):
        return self.thread.ident

    def is_alive(self):
        return self.thread.is_alive()

    def start(self):
        if self.fail_start:
            if self.start_failed:
                self.start_failed.set()
            raise OSError('simulated thread creation failure')
        self.thread.start()

    def join(self, timeout=None):
        return self.thread.join(timeout)


class _TrackedSemaphore:
    def __init__(self, value, stop_event=None, stop_wait_seen=None, capacity_wait_started=None):
        self.semaphore = threading.Semaphore(value)
        self.stop_event = stop_event
        self.stop_wait_seen = stop_wait_seen
        self.capacity_wait_started = capacity_wait_started
        self.acquire_calls = 0

    def acquire(self, *args, **kwargs):
        self.acquire_calls += 1
        if self.acquire_calls >= 2 and self.capacity_wait_started:
            self.capacity_wait_started.set()
        acquired = self.semaphore.acquire(*args, **kwargs)
        if not acquired and self.stop_event and self.stop_event.is_set() and self.stop_wait_seen:
            self.stop_wait_seen.set()
        return acquired

    def release(self):
        return self.semaphore.release()


class _ThreadingProxy:
    def __init__(self, fail_start_at=None, stop_event=None):
        self.fail_start_at = fail_start_at
        self.stop_event = stop_event
        self.start_failed = threading.Event()
        self.stop_wait_seen = threading.Event()
        self.capacity_wait_started = threading.Event()
        self.start_calls = 0
        self.semaphores = []

    def Semaphore(self, value=1):
        semaphore = _TrackedSemaphore(value, self.stop_event, self.stop_wait_seen, self.capacity_wait_started)
        self.semaphores.append(semaphore)
        return semaphore

    def Thread(self, *args, **kwargs):
        self.start_calls += 1
        thread = threading.Thread(*args, **kwargs)
        return _ThreadHandle(
            thread,
            fail_start=self.start_calls == self.fail_start_at,
            start_failed=self.start_failed,
        )


class _ClearClaimsConnection:
    def __init__(self, cleanup_started):
        self.cleanup_started = cleanup_started
        self.sql = []

    def execute(self, statement, params=()):
        self.sql.append((statement, params))
        self.cleanup_started.set()
        return None

    def commit(self):
        pass

    def close(self):
        pass


class Phase7BatchLifecycleTests(unittest.TestCase):
    def test_failed_child_start_waits_for_live_worker_and_releases_capacity(self):
        manager = pm.PipelineManager()
        manager._stop_event.clear()
        worker_entered = threading.Event()
        release_worker = threading.Event()
        batch_done = threading.Event()
        cleanup_started = threading.Event()
        released_claims = []
        logs = []
        proxy = _ThreadingProxy(fail_start_at=2)

        def fake_process(item_id, *args, **kwargs):
            worker_entered.set()
            if not release_worker.wait(5):
                raise TimeoutError('test gate timed out')

        fake_conn = _ClearClaimsConnection(cleanup_started)
        manager.process_single_item = fake_process
        patches = [
            mock.patch.object(pm, 'threading', proxy),
            mock.patch.object(pm, 'get_settings', return_value={
                'max_concurrent_threads': 2, 'upload_delay_seconds': 0,
            }),
            mock.patch.object(pm, 'add_log', side_effect=lambda *args, **kwargs: logs.append((args, kwargs))),
            mock.patch.object(pm, 'release_item_claim', side_effect=lambda item_id, token: released_claims.append(item_id)),
            mock.patch.object(pm, 'get_conn', return_value=fake_conn),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            runner = threading.Thread(
                target=lambda: (manager._batch_worker_guarded([1, 2, 3], False, False, False, 'batch-x'), batch_done.set()),
                daemon=True,
            )
            runner.start()
            self.assertTrue(worker_entered.wait(5), 'worker đầu không khởi chạy')
            self.assertTrue(proxy.start_failed.wait(5), 'không giả lập được lỗi Thread.start')
            self.assertFalse(cleanup_started.is_set(), 'claim batch bị dọn khi worker đầu còn sống')
            self.assertEqual(released_claims, [], 'claim worker đang chạy bị nhả sớm')
            release_worker.set()
            self.assertTrue(batch_done.wait(5), 'batch không kết thúc sau khi worker hoàn tất')
            runner.join(5)

        self.assertFalse(runner.is_alive())
        self.assertEqual(released_claims, [1])
        self.assertTrue(cleanup_started.is_set())
        self.assertFalse(manager.is_running)
        self.assertTrue(any(kwargs.get('error_code') == 'ERR_WORKER_START' for _, kwargs in logs))
        semaphore = proxy.semaphores[0]
        self.assertTrue(semaphore.acquire(blocking=False))
        self.assertTrue(semaphore.acquire(blocking=False))
        self.assertFalse(semaphore.acquire(blocking=False), 'semaphore còn dư slot sau khi kiểm tra capacity')
        semaphore.release(); semaphore.release()

    def test_100_item_failure_injection_bounds_workers_and_releases_all_claims(self):
        manager = pm.PipelineManager()
        manager._stop_event.clear()
        worker_limit = 8
        barrier = threading.Barrier(worker_limit)
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        started = []
        released = []
        logs = []
        proxy = _ThreadingProxy()
        failures = {
            12: ConnectionError('simulated network drop'),
            24: TimeoutError('simulated timeout'),
            36: sqlite3.OperationalError('database is locked'),
            48: RuntimeError('simulated worker crash'),
            60: FileNotFoundError('simulated missing media'),
            72: OSError(28, 'simulated disk full'),
        }
        observed_failures = {}

        def fake_process(item_id, *args, **kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                started.append(item_id)
            try:
                if item_id <= worker_limit:
                    barrier.wait(timeout=5)
                if item_id in failures:
                    try:
                        raise failures[item_id]
                    except Exception as exc:
                        with lock:
                            observed_failures[item_id] = type(exc).__name__
                        raise
            finally:
                with lock:
                    active -= 1

        manager.process_single_item = fake_process
        fake_conn = _ClearClaimsConnection(threading.Event())
        patches = [
            mock.patch.object(pm, 'threading', proxy),
            mock.patch.object(pm, 'get_settings', return_value={
                'max_concurrent_threads': worker_limit, 'upload_delay_seconds': 0,
            }),
            mock.patch.object(pm, 'add_log', side_effect=lambda *args, **kwargs: logs.append((args, kwargs))),
            mock.patch.object(pm, 'release_item_claim', side_effect=lambda item_id, token: released.append(item_id)),
            mock.patch.object(pm, 'get_conn', return_value=fake_conn),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            manager._batch_worker_guarded(list(range(1, 101)), False, False, False, 'batch-100')

        self.assertEqual(set(started), set(range(1, 101)))
        self.assertEqual(len(started), 100)
        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, worker_limit)
        self.assertEqual(set(released), set(range(1, 101)))
        self.assertEqual(len(released), 100)
        self.assertEqual(set(observed_failures), set(failures))
        worker_errors = [kwargs for _, kwargs in logs if kwargs.get('error_code') == 'ERR_WORKER']
        self.assertEqual(len(worker_errors), len(failures))
        semaphore = proxy.semaphores[0]
        acquired_slots = sum(bool(semaphore.acquire(blocking=False)) for _ in range(worker_limit))
        self.assertEqual(acquired_slots, worker_limit)
        self.assertFalse(semaphore.acquire(blocking=False))
        for _ in range(acquired_slots):
            semaphore.release()

    def test_stop_interrupts_wait_for_worker_capacity(self):
        manager = pm.PipelineManager()
        manager._stop_event.clear()
        worker_entered = threading.Event()
        release_worker = threading.Event()
        batch_done = threading.Event()
        started = []
        proxy = _ThreadingProxy(stop_event=manager._stop_event)

        def fake_process(item_id, *args, **kwargs):
            started.append(item_id)
            worker_entered.set()
            if not release_worker.wait(5):
                raise TimeoutError('test gate timed out')

        manager.process_single_item = fake_process
        patches = [
            mock.patch.object(pm, 'threading', proxy),
            mock.patch.object(pm, 'get_settings', return_value={
                'max_concurrent_threads': 1, 'upload_delay_seconds': 0,
            }),
            mock.patch.object(pm, 'add_log'),
            mock.patch.object(pm, 'release_item_claim'),
            mock.patch.object(pm, 'get_conn', return_value=_ClearClaimsConnection(threading.Event())),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            runner = threading.Thread(
                target=lambda: (manager._batch_worker_guarded([1, 2], False, False, False, 'batch-stop'), batch_done.set()),
                daemon=True,
            )
            runner.start()
            self.assertTrue(worker_entered.wait(5))
            self.assertTrue(proxy.capacity_wait_started.wait(5), 'batch chưa vào lần chờ slot thứ hai')
            manager._stop_event.set()
            self.assertTrue(proxy.stop_wait_seen.wait(5), 'batch không poll được stop khi chờ semaphore')
            self.assertEqual(started, [1])
            release_worker.set()
            self.assertTrue(batch_done.wait(5))
            runner.join(5)

        self.assertFalse(runner.is_alive())
        self.assertEqual(started, [1])


if __name__ == '__main__':
    unittest.main()
