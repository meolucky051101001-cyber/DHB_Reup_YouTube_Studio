"""Schedule imports use the full scanner capacity and report only inserted IDs."""
from contextlib import closing
import unittest
from unittest import mock

import database
import runtime_identity
import scanner_downloader as scanner
from scheduled_uploads import ScheduleError
import test_review_regressions as fixtures


class ScheduleScanCapacityTests(unittest.TestCase):
    def setUp(self):
        # Reuse a local SQLite/ASGI fixture without starting the runtime or probing Git.
        with mock.patch.object(runtime_identity, "git_output", return_value=""):
            fixtures.ReviewRegressions.setUp(self)
        patch = mock.patch.object(self.app, "collect_secret_values", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def assert_scan_imports_last_file(self, existing_count):
        source = self.root / "sources"
        source.mkdir()
        for n in range(existing_count):
            (source / f"clip-{n:04}.mp4").touch()
        project = "schedule-capacity"
        manifest = {"source_directory": str(source), "project": project, "channel": {"id": 901}}
        adapter = self.app._InProcessScheduleAPI(None, None, None)

        def fake_probe(paths):
            return {path: {"duration": "00:10", "seconds": 10, "width": 100, "height": 200}
                    for path in paths}

        with mock.patch.object(scanner, "probe_video_infos", side_effect=fake_probe) as probe, \
                mock.patch.object(scanner, "_scan_remote_source", side_effect=AssertionError("No network")):
            response = self.client.post("/api/queue/scan", headers=self.headers, json={
                "urls": str(source), "project_name": project, "target_channel_id": 901,
                "visibility": "private", "max_items": existing_count,
            })
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
            self.assertEqual(first["added"], existing_count)
            self.assertEqual(first["skipped"], 0)

            last = source / f"clip-{existing_count:04}.mp4"
            last.touch()
            result = adapter.scan_folder(manifest)
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["skipped"], existing_count)
            self.assertEqual(len(probe.call_args.args[0]), existing_count + 1)
            self.assertEqual(probe.call_args.args[0][-1], str(last))
            self.assertEqual(adapter.scan_folder(manifest), {
                "ok": True, "added": 0, "skipped": existing_count + 1, "added_ids": [], "cleanup_receipts": [],
            })

        with closing(database.get_conn()) as conn:
            rows = conn.execute(
                "SELECT id, input_video_path FROM video_queue WHERE project_name=? ORDER BY id", (project,)
            ).fetchall()
        self.assertEqual(len(rows), existing_count + 1)
        self.assertEqual(first["added_ids"], [row["id"] for row in rows[:-1]])
        self.assertEqual(result["added_ids"], [rows[-1]["id"]])
        self.assertEqual(rows[-1]["input_video_path"], str(last))

    def test_imports_501st_file_after_first_500_already_exist(self):
        self.assert_scan_imports_last_file(500)

    def test_imports_1000th_file_at_manifest_capacity(self):
        self.assert_scan_imports_last_file(999)

    def _scan_http(self, source, *, schedule_sync=True):
        def fake_probe(paths):
            return {path: {"duration": "00:10", "seconds": 10, "width": 100, "height": 200}
                    for path in paths}

        with mock.patch.object(scanner, "probe_video_infos", side_effect=fake_probe), \
                mock.patch.object(scanner, "_scan_remote_source", side_effect=AssertionError("No network")):
            response = self.client.post("/api/queue/scan", headers=self.headers, json={
                "urls": str(source), "project_name": "review", "target_channel_id": 901,
                "visibility": "private", "max_items": 1000, "schedule_sync": schedule_sync,
            })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _cleanup_http(self, receipts):
        return self.client.post("/api/queue/schedule-scan-cleanup", headers=self.headers,
                                json={"receipts": receipts})

    def _source_directory(self, names):
        source = self.root / "cleanup-sources"
        source.mkdir()
        for name in names:
            (source / name).write_bytes(b"offline fixture")
        return source

    def _queue_ids(self):
        with closing(database.get_conn()) as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM video_queue")}

    def test_cleanup_after_rejected_preview_allows_same_sources_to_import_again(self):
        source = self._source_directory(["existing.mp4"])
        earlier = self._scan_http(source, schedule_sync=False)
        self.assertNotIn("cleanup_receipts", earlier)
        (source / "new.mp4").write_bytes(b"leave source alone")
        added = self._scan_http(source)
        new_id, = added["added_ids"]
        receipt, = added["cleanup_receipts"]
        self.assertEqual(receipt["queue_id"], new_id)
        self.assertRegex(receipt["proof"], r"^[0-9a-f]{64}$")
        self.assertEqual(added["skipped"], 1)

        # Generic preview accepts an Upload API item, but scheduler will reject its
        # drifted destination. Use a real provisional approval row, without any provider.
        def preview(_ids, _options):
            with closing(database.get_conn()) as conn, conn:
                conn.execute("INSERT INTO upload_approvals(token,created_at,state,options_json) "
                             "VALUES ('sync-fixture',1,'preview','[]')")
                conn.execute("INSERT INTO upload_approval_items VALUES ('sync-fixture',?,'{}')", (new_id,))
            return {"items": [{"id": new_id, "channel_id": 902}], "preview_token": "sync-fixture"}

        with mock.patch.object(self.app.pipeline_manager, "preview_distribution", side_effect=preview):
            response = self.client.post("/api/queue/preview-distribution", headers=self.headers,
                                        json={"ids": [new_id], "schedule_sync": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["items"][0]["channel_id"], 902)
        self.assertEqual(response.json()["preview_id"], "")
        with closing(database.get_conn()) as conn:
            self.assertIsNone(conn.execute("SELECT token FROM upload_approvals WHERE token='sync-fixture'").fetchone())
            self.assertIsNone(conn.execute("SELECT token FROM upload_approval_items WHERE token='sync-fixture'").fetchone())

        self.assertEqual(self._scan_http(source)["added_ids"], [])  # Ordinary dedupe before rollback.
        response = self._cleanup_http(added["cleanup_receipts"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "deleted_ids": [new_id], "retained_ids": []})
        self.assertNotIn(new_id, self._queue_ids())
        self.assertTrue(set(earlier["added_ids"]) <= self._queue_ids())
        self.assertEqual((source / "new.mp4").read_bytes(), b"leave source alone")
        retried = self._scan_http(source)
        self.assertEqual(retried["added"], 1)
        self.assertNotEqual(retried["added_ids"], added["added_ids"])

    def test_cleanup_refuses_forged_foreign_and_nonpristine_rows_atomically(self):
        source = self._source_directory(["a.mp4", "b.mp4"])
        added = self._scan_http(source)
        receipts = added["cleanup_receipts"]
        protected_id = receipts[1]["queue_id"]
        before = self._queue_ids()
        conflicts = [
            ("invalid-proof", [receipts[0], {**receipts[1], "proof": "0" * 64}]),
            ("foreign-row", [receipts[0], {**receipts[1], "queue_id": 901}]),
        ]
        for name, invalid in conflicts:
            with self.subTest(guard=name):
                response = self._cleanup_http(invalid)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self._queue_ids(), before)

        # Each mutation is independently restored after verifying the whole batch
        # remains, even though its first row was unchanged and eligible.
        changes = [
            ("edit", "UPDATE video_queue SET title='operator edit' WHERE id=?",
             "UPDATE video_queue SET title='b' WHERE id=?"),
            ("claim", "UPDATE video_queue SET claim_token='worker' WHERE id=?",
             "UPDATE video_queue SET claim_token='' WHERE id=?"),
            ("approval", "INSERT INTO upload_approval_items VALUES ('manual-fixture',?,'{}')",
             "DELETE FROM upload_approval_items WHERE queue_id=?"),
            ("review", "INSERT INTO content_reservations(content_key,channel_id,queue_id,claim_token,state) "
             "VALUES ('fixture',901,?,'worker','NeedsReview')", "DELETE FROM content_reservations WHERE queue_id=?"),
            ("attempt", "INSERT INTO upload_attempts(queue_id,attempt_no) VALUES (?,1)",
             "DELETE FROM upload_attempts WHERE queue_id=?"),
            ("history", "INSERT INTO published_history(source_post_id,channel_id) "
             "SELECT post_id,target_channel_id FROM video_queue WHERE id=?",
             "DELETE FROM published_history WHERE source_post_id=(SELECT post_id FROM video_queue WHERE id=?)"),
            ("artifact", "INSERT INTO managed_artifacts(path,kind,owner_type,owner_id) "
             "VALUES ('fixture-path','render_output','queue',?)", "DELETE FROM managed_artifacts WHERE owner_id=?"),
        ]
        for name, mutate, restore in changes:
            with self.subTest(guard=name):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute(mutate, (protected_id,))
                response = self._cleanup_http(receipts)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "SCHEDULE_SCAN_CLEANUP_CONFLICT")
                self.assertEqual(self._queue_ids(), before)
                with closing(database.get_conn()) as conn, conn:
                    conn.execute(restore, (protected_id,))
        result = self._cleanup_http(receipts)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["deleted_ids"], added["added_ids"])
        self.assertEqual(self._queue_ids(), {901, 902})

    def test_real_blocked_preview_cleanup_and_retry(self):
        source = self._source_directory(["blocked.mp4"])
        added = self._scan_http(source)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET upload_mode='youtube_api',oauth_token_json='' WHERE id=901")
        response = self.client.post("/api/queue/preview-distribution", headers=self.headers, json={
            "ids": added["added_ids"], "do_download": False, "do_render": False, "do_upload": True,
            "schedule_sync": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
        self.assertTrue(response.json()["items"][0]["blocked"])
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM upload_approval_items").fetchone()[0], 0)
        response = self._cleanup_http(added["cleanup_receipts"])
        self.assertEqual(response.status_code, 200, response.text)
        retry = self._scan_http(source)
        self.assertEqual(retry["added"], 1)
        self.assertEqual(retry["skipped"], 0)
        self.assertTrue((source / "blocked.mp4").is_file())

    def test_guarded_delete_http_snapshot_and_concurrent_edit_are_atomic(self):
        source = self._source_directory(["a.mp4", "b.mp4"])
        added = self._scan_http(source)
        ids = added["added_ids"]
        snapshots = self.client.get("/api/queue", params={"project": "review"}, headers=self.headers).json()
        snapshots = [row for row in snapshots if row["id"] in ids]
        self.assertTrue(all(row["claim_token"] == "***" for row in snapshots))
        payload = {"ids": ids, "expected_rows": snapshots}
        before = self._queue_ids()
        # A source that is still present must protect the row even with an exact snapshot.
        result = self.client.post("/api/queue/delete", headers=self.headers, json=payload)
        self.assertEqual(result.status_code, 409, result.text)
        for path in source.iterdir():
            path.unlink()  # Fixture only: simulate the user's previously removed input files.
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET title='operator edit' WHERE id=?", (ids[-1],))
        result = self.client.post("/api/queue/delete", headers=self.headers, json=payload)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self._queue_ids(), before)
        refreshed = self.client.get("/api/queue", params={"project": "review"}, headers=self.headers).json()
        refreshed = [row for row in refreshed if row["id"] in ids]
        # Guarded internal callers get a scheduler error rather than raw FastAPI HTTPException.
        adapter = self.app._InProcessScheduleAPI(None, None, None)
        with self.assertRaises(ScheduleError) as error:
            adapter.delete_rows(ids, expected_rows=snapshots)
        self.assertEqual(error.exception.code, "QUEUE_DELETE_FAILED")
        result = self.client.post("/api/queue/delete", headers=self.headers,
                                  json={"ids": ids, "expected_rows": refreshed})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"ok": True, "deleted": len(ids)})
        self.assertEqual(self._queue_ids(), {901, 902})
        with self.assertRaises(ScheduleError) as error:
            adapter.cleanup_scan_rows(added["cleanup_receipts"])
        self.assertEqual(error.exception.code, "QUEUE_SCAN_CLEANUP_INCOMPLETE")
