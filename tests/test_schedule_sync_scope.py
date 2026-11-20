"""Offline fixtures for scheduled folder sync in a shared project."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import scheduled_uploads as su


class ScheduleSyncScopeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name)
        self.state_dir = self.runtime / "schedule"
        self.state_dir.mkdir()
        self.sources = self.runtime / "sources"
        self.sources.mkdir()
        self.rows = {}
        self.scan_rows = []
        self.manifest = {
            "version": su.MANIFEST_VERSION, "runner_contract": su.CONTRACT_VERSION,
            "project": "mixed project", "cadence_hours": 72,
            "start_at": "2026-11-20T23:15:00+07:00", "source_directory": str(self.sources),
            "channel": {"id": 3, "channel_id": "UC" + "a" * 22, "name": "Fixture"},
            "jobs": [self.add_owned(239, status="Completed", published_url="fixture-receipt"),
                     self.add_owned(244), self.add_owned(240)],
        }
        self.stale_row = dict(self.rows[240])
        self.state = su.new_state(self.manifest)
        self.state.update(next_index=1, status="completed", last_started_at="2026-11-17T16:15:00+00:00",
                          last_completed_at="2026-11-17T16:17:00+00:00",
                          last_result={"queue_id": 239, "status": "completed", "finished_at": "2026-11-17T16:17:00+00:00"})
        self.write_schedule()
        self.api = mock.Mock(session=None)
        self.api.project_rows.side_effect = lambda _: [dict(row) for row in self.rows.values()]
        self.api.scan_folder.side_effect = self.scan
        self.api.delete_rows.side_effect = self.delete
        self.api.cleanup_scan_rows.side_effect = self.cleanup
        self.factory = mock.Mock(return_value=self.api)
        for target in ("requests.sessions.Session.request", "scheduled_uploads.subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError(f"Unexpected call to {target}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_schedule(self):
        self.state["manifest_sha256"] = su.manifest_digest(self.manifest)
        su.save_json(self.state_dir / "manifest.json", self.manifest)
        su.save_json(self.state_dir / "state.json", self.state)

    def schedule_bytes(self):
        return tuple((self.state_dir / name).read_bytes() for name in ("manifest.json", "state.json"))

    def row(self, queue_id, *, folder=None, **updates):
        path = (self.sources if folder is None else folder) / f"{queue_id:04}.mp4"
        path.write_bytes(f"fixture {queue_id}".encode())
        return {"id": queue_id, "status": "Pending", "input_video_path": str(path),
                "visibility": "private", "schedule_time": "", "target_channel_id": 3, **updates}

    def add_owned(self, queue_id, **updates):
        row = self.rows[queue_id] = self.row(queue_id, **updates)
        return {"queue_id": queue_id, "path": row["input_video_path"], "sha256": su.sha256(row["input_video_path"])}

    def scan(self, _manifest):
        existing_paths = {row["input_video_path"] for row in self.rows.values()}
        added = [row for row in self.scan_rows if row["input_video_path"] not in existing_paths]
        self.rows.update((row["id"], dict(row)) for row in added)
        ids = [row["id"] for row in added]
        return {"ok": True, "added": len(ids), "added_ids": ids, "cleanup_receipts": self.receipts(ids)}

    @staticmethod
    def receipts(ids):
        return [{"queue_id": queue_id, "proof": f"{queue_id:064x}"} for queue_id in ids]

    def cleanup(self, receipts):
        ids = [receipt["queue_id"] for receipt in receipts]
        self.assertEqual(receipts, self.receipts(ids))
        expected = {row["id"]: row for row in self.scan_rows}
        deleted, retained = [], []
        for queue_id in ids:
            if self.rows[queue_id] == expected[queue_id] and not self.rows[queue_id].get("requires_review"):
                del self.rows[queue_id]
                deleted.append(queue_id)
            else:
                retained.append(queue_id)
        return {"ok": True, "deleted_ids": deleted, "retained_ids": retained}

    def delete(self, queue_ids, *, expected_rows=None):
        if expected_rows is not None:
            self.assertEqual([self.rows[queue_id] for queue_id in queue_ids], expected_rows)
        for queue_id in queue_ids:
            del self.rows[queue_id]
        return {"ok": True, "deleted": len(queue_ids)}

    def sync(self):
        return su.sync_schedule_folder(self.state_dir, self.runtime, api_factory=self.factory, check_release=False)

    def assert_sync_error(self, code):
        before = self.schedule_bytes()
        with self.assertRaises(su.ScheduleError) as error:
            self.sync()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.schedule_bytes(), before)
        self.api.delete_rows.assert_not_called()
        self.api.start.assert_not_called()

    def test_foreign_pending_outside_root_is_never_deleted(self):
        foreign = self.rows[900] = self.row(900, folder=self.runtime)
        self.sync()
        self.api.delete_rows.assert_not_called()
        self.assertEqual(self.rows[900], foreign)
        self.api.start.assert_not_called()

    def test_foreign_pending_inside_root_stops_sync_without_mutation(self):
        foreign = self.rows[900] = self.row(900)
        self.assert_sync_error("UNSCHEDULED_SOURCE_ITEMS")
        self.assertEqual(self.rows[900], foreign)
        self.api.scan_folder.assert_not_called()
        self.api.preview_batch.assert_not_called()
        self.api.cleanup_scan_rows.assert_not_called()

    def test_mixed_project_sync_preserves_foreign_rows_history_and_order(self):
        Path(self.rows[239]["input_video_path"]).unlink()
        Path(self.rows[240]["input_video_path"]).unlink()
        foreign = {
            900: self.row(900, folder=self.runtime),
            901: self.row(901, folder=self.runtime),
            902: self.row(902, status="Completed", published_url="other-receipt"),
            903: self.row(903, status="NeedsReview", requires_review=True),
        }
        self.rows.update(foreign)
        self.scan_rows = [self.row(250), self.row(241)]

        result = self.sync()

        self.api.delete_rows.assert_called_once_with([240], expected_rows=[self.stale_row])
        self.api.preview_batch.assert_called_once_with(self.manifest, [244, 241, 250])
        self.assertEqual({queue_id: self.rows[queue_id] for queue_id in foreign}, foreign)
        manifest = su.load_json(self.state_dir / "manifest.json")
        state = su.load_json(self.state_dir / "state.json")
        self.assertEqual([job["queue_id"] for job in manifest["jobs"]], [239, 244, 241, 250])
        self.assertEqual(manifest["jobs"][0], self.manifest["jobs"][0])
        self.assertEqual(su.next_due(manifest, state), su.next_due(self.manifest, self.state))
        for field in ("next_index", "last_started_at", "last_result", "last_completed_at"):
            self.assertEqual(state[field], self.state[field])
        self.assertEqual((result["removed_pending_count"], result["added_count"]), (1, 2))
        self.api.start.assert_not_called()

    def test_concurrent_project_insert_outside_root_is_ignored(self):
        concurrent = self.row(910, folder=self.runtime)

        def scan_and_competing_insert(manifest):
            self.rows[910] = concurrent
            return self.scan(manifest)

        self.scan_rows = [self.row(250)]
        self.api.scan_folder.side_effect = scan_and_competing_insert
        result = self.sync()
        self.api.preview_batch.assert_called_once_with(self.manifest, [244, 240, 250])
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(self.rows[910], concurrent)

    def test_owned_pending_rebound_row_aborts_before_scan_or_delete(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        outside_file = self.runtime / "manually-moved.mp4"
        outside_file.touch()
        for changes in (
            {"input_video_path": str(outside_file)},
            {"input_video_path": str(self.sources / "wrong-missing.mp4")},
            {"input_video_path": ""},
            {"visibility": "public"},
            {"target_channel_id": 9},
            {"schedule_time": "2026-12-01 12:00:00"},
        ):
            with self.subTest(changes=changes):
                self.rows[240] = {**self.stale_row, **changes}
                self.assert_sync_error("QUEUE_BINDING_CHANGED")
                self.api.scan_folder.assert_not_called()
                self.assertEqual(self.rows[240], {**self.stale_row, **changes})

    def test_scan_must_return_verified_new_ids_before_stale_deletion(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.rows[910] = self.row(910, folder=self.runtime)
        for response in (
            {"ok": True, "added": 1},
            {"ok": True, "added": 1, "added_ids": [910]},
            {"ok": True, "added": 1, "added_ids": [9999]},
            {"ok": True, "added": 2, "added_ids": [9999, 9999]},
            {"ok": True, "added": 0, "added_ids": [9999]},
            {"ok": True, "added": 1, "added_ids": [True]},
        ):
            with self.subTest(response=response):
                self.api.scan_folder.side_effect = None
                self.api.scan_folder.return_value = response
                self.assert_sync_error("QUEUE_SCAN_FAILED")
                self.api.preview_batch.assert_not_called()
                self.api.cleanup_scan_rows.assert_not_called()

    def test_scan_requires_bound_pending_local_files(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        invalid_rows = [
            self.row(250, folder=self.runtime), self.row(251, visibility="public"),
            self.row(252, target_channel_id=4), self.row(253, requires_review=True),
        ]
        for row in invalid_rows:
            with self.subTest(row=row["id"]):
                self.scan_rows = [row]
                error = "QUEUE_SCAN_CLEANUP_INCOMPLETE" if row.get("requires_review") else "QUEUE_BINDING_CHANGED"
                self.assert_sync_error(error)
                self.api.preview_batch.assert_not_called()

    def test_failed_preview_cleans_scan_rows_to_allow_retry_through_dedupe(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.rows[910] = self.row(910, folder=self.runtime)
        self.scan_rows = [self.row(250), self.row(910, folder=self.runtime)]
        self.api.preview_batch.side_effect = [su.ScheduleError("PREVIEW_BLOCKED"), None]
        self.assert_sync_error("PREVIEW_BLOCKED")
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250]))
        self.assertEqual(set(self.rows), {239, 240, 244, 910})

        result = self.sync()

        self.assertEqual(result["added_count"], 1)
        self.api.delete_rows.assert_called_once_with([240], expected_rows=[self.stale_row])
        manifest = su.load_json(self.state_dir / "manifest.json")
        self.assertEqual([job["queue_id"] for job in manifest["jobs"]], [239, 244, 250])
        self.assertEqual(set(self.rows), {239, 244, 250, 910})
        self.api.start.assert_not_called()

    def test_preview_failure_preserves_changed_scan_rows_for_review(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250), self.row(251)]

        def fail_preview(_manifest, _queue_ids):
            self.rows[251]["visibility"] = "public"
            raise su.ScheduleError("PREVIEW_BLOCKED")

        self.api.preview_batch.side_effect = fail_preview
        self.assert_sync_error("QUEUE_SCAN_CLEANUP_INCOMPLETE")
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250, 251]))
        self.assertEqual(set(self.rows), {239, 240, 244, 251})
        self.assertEqual(self.rows[251]["visibility"], "public")

    def test_json_size_failure_preserves_old_slots_and_cleans_scan_insert(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        fresh = self.row(250)
        unicode_path = self.sources / ("色" * 30 + ".mp4")
        Path(fresh["input_video_path"]).rename(unicode_path)
        fresh["input_video_path"] = str(unicode_path)
        self.scan_rows = [fresh]
        limit = max(map(len, self.schedule_bytes()))
        with mock.patch.object(su, "JSON_LIMIT", limit):
            self.assert_sync_error("JSON_TOO_LARGE")
        self.api.preview_batch.assert_not_called()
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250]))
        self.assertEqual(set(self.rows), {239, 240, 244})

    def test_state_json_size_failure_precedes_preview_and_stale_deletion(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]
        self.state["fixture_padding"] = "x" * 1500
        self.write_schedule()
        limit = (self.state_dir / "state.json").stat().st_size
        with mock.patch.object(su, "JSON_LIMIT", limit):
            self.assert_sync_error("JSON_TOO_LARGE")
        self.api.preview_batch.assert_not_called()
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250]))
        self.assertEqual(set(self.rows), {239, 240, 244})

    def test_foreign_pending_insert_inside_root_during_scan_stops_sync(self):
        competing = self.row(910)
        self.scan_rows = [self.row(250)]

        def scan_and_competing_insert(manifest):
            self.rows[910] = competing
            return self.scan(manifest)

        self.api.scan_folder.side_effect = scan_and_competing_insert
        self.assert_sync_error("UNSCHEDULED_SOURCE_ITEMS")
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250]))
        self.api.preview_batch.assert_not_called()
        self.assertEqual(self.rows[910], competing)

    def test_persistence_error_restores_old_json_and_keeps_old_queue(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]
        before = self.schedule_bytes()
        native_replace = su.os.replace

        def fail_state_publish(source, target):
            if target == self.state_dir / "state.json":
                raise OSError("fixture disk error")
            return native_replace(source, target)

        with mock.patch.object(su.os, "replace", side_effect=fail_state_publish):
            with self.assertRaisesRegex(OSError, "fixture disk error"):
                self.sync()
        self.assertEqual(self.schedule_bytes(), before)
        self.api.delete_rows.assert_not_called()
        self.api.cleanup_scan_rows.assert_called_once_with(self.receipts([250]))
        self.assertEqual(set(self.rows), {239, 240, 244})
        self.assertEqual(list(self.state_dir.glob("*.tmp")), [])
        self.assertEqual(list(self.state_dir.glob("sync-recovery-*")), [])

    def test_failed_json_restore_preserves_rows_and_returns_recovery_required(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]
        before = self.schedule_bytes()
        native_replace = su.os.replace
        calls = []

        def fail_publish_and_restore(source, target):
            calls.append(target.name)
            if len(calls) > 1:
                raise OSError("fixture disk error")
            return native_replace(source, target)

        with mock.patch.object(su.os, "replace", side_effect=fail_publish_and_restore):
            with self.assertRaises(su.ScheduleError) as error:
                self.sync()
        self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(calls, ["manifest.json", "state.json", "manifest.json"])
        self.assertNotEqual(self.schedule_bytes()[0], before[0])
        self.assertEqual(self.schedule_bytes()[1], before[1])
        self.assertEqual(set(self.rows), {239, 240, 244, 250})
        self.api.cleanup_scan_rows.assert_not_called()
        self.api.delete_rows.assert_not_called()
        recovery_directory, = self.state_dir.glob("sync-recovery-*")
        self.assertEqual((recovery_directory / "manifest.json").read_bytes(), before[0])
        self.assertEqual((recovery_directory / "state.json").read_bytes(), before[1])

    def test_recovery_directory_cleanup_error_cannot_undo_successful_publication(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]
        native_rmdir = Path.rmdir
        cleanup_attempts = []

        def fail_backup_cleanup(path):
            if path.parent == self.state_dir and path.name.startswith("sync-recovery-"):
                cleanup_attempts.append(path)
                raise OSError("fixture cleanup error")
            return native_rmdir(path)

        with mock.patch.object(Path, "rmdir", autospec=True, side_effect=fail_backup_cleanup):
            result = self.sync()

        self.assertEqual(len(cleanup_attempts), 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_pending_count"], 1)
        self.assertEqual(set(self.rows), {239, 244, 250})
        manifest = su.load_json(self.state_dir / "manifest.json")
        state = su.load_json(self.state_dir / "state.json")
        su.validate_state(state, manifest)
        self.assertEqual([job["queue_id"] for job in manifest["jobs"]], [239, 244, 250])
        self.api.cleanup_scan_rows.assert_not_called()

    def test_post_publication_stale_cleanup_failure_keeps_new_schedule(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]

        def refused_delete(*_args, **_kwargs):
            current = su.load_json(self.state_dir / "manifest.json")
            su.validate_state(su.load_json(self.state_dir / "state.json"), current)
            self.assertEqual([job["queue_id"] for job in current["jobs"]], [239, 244, 250])
            raise su.ScheduleError("QUEUE_DELETE_FAILED")

        self.api.delete_rows.side_effect = refused_delete
        result = self.sync()
        self.assertEqual(result["warning_code"], "STALE_CLEANUP_INCOMPLETE")
        self.assertEqual(result["removed_pending_count"], 0)
        self.assertEqual(set(self.rows), {239, 240, 244, 250})
        self.api.cleanup_scan_rows.assert_not_called()

    def test_stale_row_changed_during_preview_is_not_deleted(self):
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(250)]

        def claim(_manifest, _queue_ids):
            self.rows[240]["is_claimed"] = True

        self.api.preview_batch.side_effect = claim
        self.assert_sync_error("PENDING_ITEM_CHANGED")
        self.assertTrue(self.rows[240]["is_claimed"])
        self.assertNotIn(250, self.rows)

    def test_runtime_guard_blocks_sync_before_api_or_schedule_lock(self):
        with su.runtime_operation_lock(self.runtime):
            self.assert_sync_error("RUNTIME_OPERATION_ACTIVE")
        self.assertFalse((self.state_dir / "schedule.lock").exists())
        self.factory.assert_not_called()
        self.api.scan_folder.assert_not_called()

    def test_scan_limit_fails_before_scan_or_delete(self):
        for number in range(1000):
            (self.sources / f"new_{number}.mp4").touch()
        self.assert_sync_error("SOURCE_INVENTORY_LIMIT")
        self.api.scan_folder.assert_not_called()

    def test_combined_limit_accounts_for_completed_files_removed_from_disk(self):
        Path(self.rows[239]["input_video_path"]).unlink()
        for number in range(998):
            (self.sources / f"new_{number}.mp4").touch()
        self.assert_sync_error("SOURCE_INVENTORY_LIMIT")
        self.api.scan_folder.assert_not_called()

    def test_500_existing_jobs_and_501_imports_fit_after_one_stale_removal(self):
        self.manifest["jobs"].extend(self.add_owned(queue_id) for queue_id in range(1000, 1497))
        self.write_schedule()
        Path(self.rows[240]["input_video_path"]).unlink()
        self.scan_rows = [self.row(queue_id) for queue_id in range(2000, 2501)]
        result = self.sync()
        self.assertEqual(result["schedule"]["total_count"], 1000)
        self.assertEqual(result["added_count"], 501)
        self.api.delete_rows.assert_called_once_with([240], expected_rows=[self.stale_row])
        self.api.start.assert_not_called()

    def test_scan_request_uses_full_supported_batch(self):
        api = su.LocalAPI("http://127.0.0.1:8686", self.runtime, {}, session=mock.Mock(headers={}))
        with mock.patch.object(api, "request", return_value={"ok": True, "added": 0, "added_ids": []}) as request:
            api.scan_folder(self.manifest)
        self.assertEqual(request.call_args.kwargs["json"]["max_items"], 1000)
        self.assertIs(request.call_args.kwargs["json"]["schedule_sync"], True)

    def test_local_api_cleanup_and_preview_use_sync_contract(self):
        api = su.LocalAPI("http://127.0.0.1:8686", self.runtime, {}, session=mock.Mock(headers={}))
        receipts = self.receipts([250])
        with mock.patch.object(api, "request") as request:
            api.cleanup_scan_rows(receipts)
            request.assert_called_once_with("POST", "/api/queue/schedule-scan-cleanup",
                                            json={"receipts": receipts}, timeout=30)
            request.reset_mock()
            request.return_value = {"ok": True, "items": [{"channel_id": 3, "channel_name": "Fixture",
                "upload_mode": "youtube_api", "visibility": "private"}]}
            api.preview_batch(self.manifest, [250])
            self.assertIs(request.call_args.kwargs["json"]["schedule_sync"], True)
            request.reset_mock()
            request.return_value = {"ok": True, "deleted": 1}
            api.delete_rows([240], expected_rows=[self.stale_row])
            request.assert_called_once_with("POST", "/api/queue/delete", timeout=30,
                                            json={"ids": [240], "expected_rows": [self.stale_row]})

    def test_runner_queue_validates_owned_rows_and_ignores_foreign_rows(self):
        self.rows[900] = self.row(900, folder=self.runtime, status="NeedsReview", requires_review=True)
        api = su.LocalAPI("http://127.0.0.1:8686", self.runtime, {}, session=mock.Mock(headers={}))
        with mock.patch.object(api, "request", return_value=list(self.rows.values())) as request:
            self.assertEqual(set(api.queue(self.manifest)), {239, 240, 244})
            request.return_value.append(self.rows[244])
            with self.assertRaises(su.ScheduleError) as error:
                api.queue(self.manifest)
            self.assertEqual(error.exception.code, "QUEUE_SCOPE_CHANGED")
            request.return_value = [row for queue_id, row in self.rows.items() if queue_id != 244]
            with self.assertRaises(su.ScheduleError) as error:
                api.queue(self.manifest)
            self.assertEqual(error.exception.code, "QUEUE_SCOPE_CHANGED")
            self.rows[244]["visibility"] = "public"
            request.return_value = list(self.rows.values())
            with self.assertRaises(su.ScheduleError) as error:
                api.queue(self.manifest)
            self.assertEqual(error.exception.code, "QUEUE_BINDING_CHANGED")
