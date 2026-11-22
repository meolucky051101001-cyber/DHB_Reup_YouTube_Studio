"""Offline regression tests for scheduled upload source preflight."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import scheduled_upload_runner as schedule_cli
import scheduled_uploads as su


class SchedulePreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.directory = Path(fixture.name)
        self.sources = self.directory / "sources"
        self.sources.mkdir()
        self.due = datetime(2026, 11, 20, 16, 15, tzinfo=timezone.utc)
        self.clock = self.due - timedelta(hours=1)
        self.elapsed = 0
        self.sleep = mock.Mock(side_effect=self.advance)

        self.manifest = {
            "version": su.MANIFEST_VERSION,
            "runner_contract": su.CONTRACT_VERSION,
            "project": "Offline fixture",
            "channel": {"id": 1, "channel_id": "UC" + "a" * 22, "name": "Fixture channel"},
            "source_directory": str(self.sources),
            "cadence_hours": 72,
            "start_at": su.stamp(self.due - timedelta(minutes=5)),
            "jobs": [],
        }
        self.rows = {}
        self.paths = {}
        for queue_id in (239, 240, 241):
            path = self.sources / f"{queue_id}.mp4"
            path.write_bytes(f"fixture video {queue_id}".encode())
            self.paths[queue_id] = path
            self.manifest["jobs"].append({"queue_id": queue_id, "path": str(path), "sha256": su.sha256(path)})
            self.rows[queue_id] = {"id": queue_id, "status": "Pending", "input_video_path": str(path),
                                   "visibility": "private", "target_channel_id": 1}
        self.complete(239)
        prior = su.stamp(self.due - timedelta(hours=72))
        self.state = su.new_state(self.manifest)
        self.state.update(next_index=1, last_started_at=prior, last_completed_at=prior, status="waiting",
                          last_result={"queue_id": 239, "status": "completed", "finished_at": prior})
        package = Path(su.__file__).resolve().parent
        self.release = {
            "version": 1,
            "runner_contract": su.CONTRACT_VERSION,
            "runtime_root": str(self.directory.resolve()),
            "revision": "a" * 40,
            "build": "fixture-build",
            "runner_hashes": {name: su.sha256(package / name) for name in (
                "scheduled_uploads.py", "scheduled_upload_runner.py", "scheduled_upload_task.py",
            )},
        }
        su.save_json(self.directory / "manifest.json", self.manifest)
        su.save_json(self.directory / "state.json", self.state)
        su.save_json(self.directory / "release.json", self.release)

        self.diagnostics = {
            "version": self.release["build"],
            "runtime": {"revision": self.release["revision"], "restart_required": False},
            "upload_slot": {"available": True, "state": "idle"},
        }
        self.api = mock.Mock(name="fixture_api", session=None)
        self.api.diagnostics.return_value = self.diagnostics
        self.api.queue.return_value = self.rows
        self.api.preview.return_value = {"ids": [240], "preview_id": "fixture-preview"}
        self.factory = mock.Mock(return_value=self.api)
        for target in ("requests.sessions.Session.request", "scheduled_uploads.subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError(f"Unexpected call to {target}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)
        self.elapsed += seconds

    def complete(self, queue_id):
        self.rows[queue_id].update(status="Completed", published_url="https://youtu.be/fixturevid1")

    def mark_inflight(self, submission_state, status):
        self.state.update(inflight_queue_id=240, submission_state=submission_state, status=status,
                          submitted_at=su.stamp(self.due), last_started_at=su.stamp(self.due))
        su.save_json(self.directory / "state.json", self.state)

    def make_runner(self, **kwargs):
        return su.ScheduleRunner(self.directory, self.directory, now=lambda: self.clock,
                                 monotonic=lambda: self.elapsed, sleep=self.sleep,
                                 api_factory=kwargs.pop("api_factory", self.factory), **kwargs)

    def load_state(self):
        return su.load_json(self.directory / "state.json")

    def schedule_files(self):
        return {name: (self.directory / name).read_bytes() for name in (
            "manifest.json", "state.json", "release.json",
        )}

    def assert_source_changed(self, **kwargs):
        with self.assertRaises(su.ScheduleError) as error:
            self.make_runner().run(**kwargs)
        self.assertEqual(error.exception.code, "SOURCE_CHANGED")

    def assert_no_submission(self):
        self.api.preview.assert_not_called()
        self.api.start.assert_not_called()

    def assert_blocked_before_api(self):
        self.factory.assert_not_called()
        self.api.connect.assert_not_called()
        self.sleep.assert_not_called()
        self.assert_no_submission()
        state = self.load_state()
        for key in ("next_index", "last_started_at", "inflight_queue_id", "submission_state", "last_result"):
            self.assertEqual(state[key], self.state[key], key)
        self.assertEqual(state["error_code"], "SOURCE_CHANGED")
        status = su.read_schedule_status(self.directory, now=self.clock)
        self.assertTrue(status["action_required"])
        self.assertIn("Tệp nguồn của lượt kế tiếp", status["message"])
        self.assertNotIn(str(self.sources), status["message"])

    def test_runtime_lock_contention_stops_runner_before_the_schedule_lock(self):
        before = self.schedule_files()
        # Use the same runtime through an alias, as an independent caller can.
        runtime_alias = self.sources / ".."
        with ThreadPoolExecutor(max_workers=1) as worker, su.runtime_operation_lock(runtime_alias):
            run = worker.submit(self.make_runner().run)
            with self.assertRaises(su.ScheduleError) as error:
                run.result(timeout=5)
            self.assertEqual(error.exception.code, "RUNTIME_OPERATION_ACTIVE")
        self.assertFalse((self.directory / "schedule.lock").exists())
        self.assertEqual(self.schedule_files(), before)
        self.factory.assert_not_called()
        self.api.connect.assert_not_called()
        self.assert_no_submission()

    def test_runtime_lock_does_not_block_runner_on_a_separate_runtime(self):
        other_runtime = self.directory / "other-runtime"
        other_runtime.mkdir()
        with su.runtime_operation_lock(other_runtime):
            self.assertEqual(self.make_runner().run(), 0)
        for runtime in (self.directory, other_runtime):
            self.assertTrue((runtime / "data" / "schedule_operation.lock").is_file())
        self.api.connect.assert_called_once_with()
        self.assert_no_submission()

    def test_runtime_lock_is_released_if_connect_fails(self):
        self.api.connect.side_effect = su.ScheduleError("LOCAL_API_UNAVAILABLE")
        with self.assertRaises(su.ScheduleError) as error:
            self.make_runner().run()
        self.assertEqual(error.exception.code, "LOCAL_API_UNAVAILABLE")
        with su.runtime_operation_lock(self.directory), su.single_instance(self.directory):
            pass
        self.assert_no_submission()

    def test_busy_legacy_schedule_lock_also_releases_runtime_lock(self):
        with su.single_instance(self.directory):
            with self.assertRaises(su.ScheduleError) as error:
                self.make_runner().run()
            self.assertEqual(error.exception.code, "RUNNER_ALREADY_ACTIVE")
            with su.runtime_operation_lock(self.directory):
                pass
        self.factory.assert_not_called()
        self.assert_no_submission()

    def test_runtime_lock_wraps_schedule_lock_and_observer(self):
        self.mark_inflight("started", "running")
        self.rows[240].update(status="Uploading")
        schedule_lock = su.single_instance
        phases = []

        def assert_runtime_busy(phase):
            phases.append(phase)
            with self.assertRaises(su.ScheduleError) as error:
                with su.runtime_operation_lock(self.directory):
                    pass
            self.assertEqual(error.exception.code, "RUNTIME_OPERATION_ACTIVE")

        @contextmanager
        def check_lock_order(directory):
            assert_runtime_busy("before schedule lock")
            with schedule_lock(directory):
                yield
            assert_runtime_busy("after schedule unlock")

        def observe(seconds):
            assert_runtime_busy("polling inflight job")
            self.advance(seconds)
            self.complete(240)

        self.sleep.side_effect = observe
        with mock.patch.object(su, "single_instance", side_effect=check_lock_order):
            self.assertEqual(self.make_runner().run(observe_only=True), 0)
        self.assertEqual(phases, ["before schedule lock", "polling inflight job", "after schedule unlock"])
        self.assertEqual(self.load_state()["next_index"], 2)
        self.assert_no_submission()

    def test_read_schedule_status_does_not_take_or_create_a_lock(self):
        before = self.schedule_files()
        self.assertEqual(su.read_schedule_status(self.directory, now=self.clock)["status"], "waiting")
        self.assertFalse((self.directory / "data").exists())
        self.assertFalse((self.directory / "schedule.lock").exists())
        with su.runtime_operation_lock(self.directory):
            self.assertEqual(su.read_schedule_status(self.directory, now=self.clock)["status"], "waiting")
        self.assertEqual(self.schedule_files(), before)

    def test_missing_waiting_source_blocks_before_factory_or_connect(self):
        self.paths[240].unlink()
        self.assert_source_changed()
        self.assert_blocked_before_api()
        self.assertEqual(su.read_schedule_status(self.directory, now=self.clock)["folder_drift"], {
            "detected": True, "missing_future_count": 1, "new_files_count": 0, "next_source_exists": False,
        })

    def test_changed_waiting_hash_blocks_before_factory_or_connect(self):
        self.paths[240].write_bytes(b"different bytes")
        self.assert_source_changed()
        self.assert_blocked_before_api()

    def test_cli_returns_source_changed_before_local_api_creation(self):
        self.paths[240].unlink()
        stdout = io.StringIO()
        with redirect_stdout(stdout), mock.patch.object(su.LocalAPI, "__init__", autospec=True) as factory:
            exit_code = schedule_cli.main([
                "run", "--runtime-dir", str(self.directory), "--state-dir", str(self.directory),
            ])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": False, "code": "SOURCE_CHANGED"})
        factory.assert_not_called()

    def test_source_read_race_has_stable_error(self):
        hash_file = su.sha256

        def disappear(path):
            if Path(path) == self.paths[240]:
                raise FileNotFoundError(path)
            return hash_file(path)

        with mock.patch.object(su, "sha256", side_effect=disappear):
            self.assert_source_changed()
        self.assert_blocked_before_api()

    def test_valid_source_waits_for_cadence_and_starts_once(self):
        self.clock = self.due - timedelta(seconds=65)
        self.paths[239].unlink()
        self.paths[241].unlink()  # A later job is checked when it becomes next.

        def start(payload):
            self.assertEqual(payload, {"ids": [240], "preview_id": "fixture-preview"})
            self.assertEqual(self.clock, self.due)
            state = self.load_state()
            self.assertEqual(state["next_index"], 1)
            self.assertEqual(state["inflight_queue_id"], 240)
            self.assertEqual(state["submission_state"], "submitting")
            self.assertEqual(state["submitted_at"], su.stamp(self.due))
            self.assertEqual(state["last_result"], self.state["last_result"])
            self.complete(240)
            return {"ok": True}

        self.api.start.side_effect = start
        self.assertEqual(self.make_runner().run(), 0)
        self.assertEqual(self.sleep.call_args_list, [mock.call(30), mock.call(30), mock.call(5)])
        self.api.connect.assert_called_once_with()
        self.api.preview.assert_called_once_with(self.manifest, 240)
        self.api.start.assert_called_once()
        state = self.load_state()
        self.assertEqual((state["status"], state["next_index"], state["last_result"]["queue_id"]), ("completed", 2, 240))
        self.assertEqual(su.next_due(self.manifest, state), self.due + timedelta(hours=72))

    def test_valid_source_far_before_due_keeps_waiting(self):
        self.assertEqual(self.make_runner().run(), 0)
        self.api.connect.assert_called_once_with()
        self.sleep.assert_not_called()
        self.assert_no_submission()
        state = self.load_state()
        self.assertEqual((state["status"], state["next_index"], state["last_started_at"]),
                         ("waiting", 1, self.state["last_started_at"]))

    def test_rechecks_hash_after_wait_before_start(self):
        self.clock = self.due - timedelta(seconds=5)

        def replace_source(seconds):
            self.paths[240].write_bytes(b"replaced after the preflight")
            self.advance(seconds)

        self.sleep.side_effect = replace_source
        self.assert_source_changed()
        self.sleep.assert_called_once_with(5)
        self.api.connect.assert_called_once_with()
        self.assert_no_submission()
        state = self.load_state()
        self.assertEqual((state["next_index"], state["inflight_queue_id"], state["last_started_at"]),
                         (1, None, self.state["last_started_at"]))

    def test_rechecks_hash_after_preview_before_persisting_submission(self):
        self.clock = self.due

        def replace_source(_manifest, _queue_id):
            self.paths[240].write_bytes(b"replaced while preview was in flight")
            return {"ids": [240], "preview_id": "fixture-preview"}

        self.api.preview.side_effect = replace_source
        self.assert_source_changed()
        self.api.preview.assert_called_once_with(self.manifest, 240)
        self.api.start.assert_not_called()
        state = self.load_state()
        self.assertEqual(state["error_code"], "SOURCE_CHANGED")
        for key in ("next_index", "last_started_at", "inflight_queue_id", "submission_state", "last_result"):
            self.assertEqual(state[key], self.state[key], key)

    def test_inflight_completion_is_observed_with_cleaned_sources(self):
        self.mark_inflight("started", "running")
        self.complete(240)
        for path in self.paths.values():
            path.unlink()
        self.assertEqual(self.make_runner().run(observe_only=True), 0)
        self.api.connect.assert_called_once_with()
        self.assert_no_submission()
        state = self.load_state()
        self.assertEqual((state["next_index"], state["inflight_queue_id"], state["last_result"]["queue_id"]), (2, None, 240))

    def test_interrupted_start_is_observed_without_retry(self):
        self.clock = self.due
        self.api.start.side_effect = su.ScheduleError("LOCAL_API_UNAVAILABLE")
        runner = self.make_runner()
        with self.assertRaises(su.ScheduleError) as error:
            runner.run()
        self.assertEqual(error.exception.code, "LOCAL_API_UNAVAILABLE")
        uncertain = self.load_state()
        self.assertEqual((uncertain["next_index"], uncertain["inflight_queue_id"], uncertain["submission_state"]),
                         (1, 240, "submitting"))
        self.paths[240].unlink()
        self.assertEqual(runner.run(), 2)
        self.assertEqual(runner.run(), 2)
        self.api.start.assert_called_once()
        self.api.preview.assert_called_once()
        halted = self.load_state()
        self.assertEqual((halted["next_index"], halted["inflight_queue_id"], halted["submission_state"], halted["error_code"]),
                         (1, 240, "halted", "UPLOAD_RESULT_UNCERTAIN"))

    def test_finished_schedule_needs_no_source(self):
        for queue_id, path in self.paths.items():
            self.complete(queue_id)
            path.unlink()
        self.state.update(next_index=3, status="finished")
        su.save_json(self.directory / "state.json", self.state)
        self.assertEqual(self.make_runner().run(), 0)
        self.assert_no_submission()
        self.assertEqual((self.load_state()["next_index"], self.load_state()["status"]), (3, "finished"))

    def test_halted_schedule_keeps_reconciliation_required(self):
        self.paths[240].unlink()
        self.state.update(submission_state="halted", status="needs_review")
        su.save_json(self.directory / "state.json", self.state)
        before = self.schedule_files()
        self.assertEqual(self.make_runner().run(dry_run=True), 2)
        self.assertEqual(self.schedule_files(), before)
        self.assertEqual(self.make_runner().run(), 2)
        self.assert_no_submission()
        state = self.load_state()
        self.assertEqual((state["next_index"], state["submission_state"], state["error_code"]), (1, "halted", "SCHEDULE_HALTED"))

    def test_idle_observe_only_keeps_pending_source_unchecked(self):
        self.paths[240].unlink()
        before = self.schedule_files()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                self.assertEqual(self.make_runner().run(observe_only=True, dry_run=dry_run), 0)
                self.assertEqual(self.schedule_files(), before)
        self.assertEqual(self.api.connect.call_count, 2)
        self.assert_no_submission()

    def test_bad_or_missing_state_blocks_before_api_and_never_resets(self):
        path = self.directory / "state.json"
        path.unlink()
        self.paths[240].unlink()
        with self.assertRaises(FileNotFoundError):
            self.make_runner().run()
        self.assertFalse(path.exists())
        for contents, code in ((b"invalid json", "JSON_INVALID"), (b"{}", "STATE_INVALID")):
            with self.subTest(code=code):
                path.write_bytes(contents)
                with self.assertRaises(su.ScheduleError) as error:
                    self.make_runner().run()
                self.assertEqual(error.exception.code, code)
                self.assertEqual(path.read_bytes(), contents)
        self.factory.assert_not_called()
        self.api.connect.assert_not_called()
        self.assert_no_submission()

    def test_early_dry_run_failure_leaves_schedule_files_unchanged(self):
        self.paths[240].unlink()
        before = self.schedule_files()
        self.assert_source_changed(dry_run=True)
        self.assertEqual(self.schedule_files(), before)
        self.factory.assert_not_called()
        self.assert_no_submission()

    def test_dry_run_still_checks_other_pending_jobs(self):
        self.paths[241].unlink()
        before = self.schedule_files()
        self.assert_source_changed(dry_run=True)
        self.assertEqual(self.schedule_files(), before)
        self.assert_no_submission()

    def test_local_api_dry_run_uses_only_read_requests(self):
        payloads = {
            "/health/dhb": {"application": su.APPLICATION, "source_root": str(self.directory)},
            "/api/auth/token": {"token": "fixture-token"},
            "/api/diagnostics": self.diagnostics,
            "/api/channels": [{**self.manifest["channel"], "upload_mode": "youtube_api"}],
            "/api/queue": list(self.rows.values()),
        }

        def request(method, url, **_):
            self.assertEqual(method, "GET", url)
            path = url.removeprefix("http://127.0.0.1:8686")
            return mock.Mock(status_code=200, **{"json.return_value": payloads[path]})

        session = mock.Mock(headers={})
        session.request.side_effect = request
        before = self.schedule_files()
        with mock.patch.object(su.requests, "Session", return_value=session), mock.patch.object(
            su.LocalAPI, "_ensure_local_server", side_effect=AssertionError("Unexpected server startup path"),
        ):
            self.assertEqual(self.make_runner(api_factory=su.LocalAPI).run(dry_run=True), 0)
        self.assertEqual(self.schedule_files(), before)
        self.assertEqual(session.request.call_count, 6)
        session.close.assert_called_once_with()
        self.sleep.assert_not_called()
