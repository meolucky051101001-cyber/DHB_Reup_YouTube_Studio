"""Regression tests for F1-F5 upload verification, receipt binding, v2 evidence, and packaged scheduler."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import database
import scheduled_upload_runner as schedule_cli
import scheduled_upload_task as schedule_task
import scheduled_uploads as su
import test_api_metadata_evidence as api_fixtures
import test_review_regressions as review_fixtures
import upload_evidence as ue
import youtube_uploader as yu


CHANNEL_ID = api_fixtures.CHANNEL_ID
VIDEO_ID = api_fixtures.VIDEO_ID
VIDEO_URL = f"https://youtu.be/{VIDEO_ID}"


class UploadVerificationAndEvidenceV2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clip = Path(temp.name) / "sample.mp4"
        self.clip.write_bytes(b"synthetic-mp4-content")
        self.channel = {
            "id": 66,
            "name": "Chị Đẹp Biết Tuốt",
            "channel_id": CHANNEL_ID,
            "upload_mode": "youtube_api",
            "oauth_token_json": '{"refresh_token":"offline-fixture"}',
        }
        self.metadata = {
            "title": "Fixture Private",
            "description": "",
            "tags": [],
            "category_id": "22",
            "made_for_kids": False,
            "altered_content": False,
            "privacy_status": "private",
        }
        for target, attr, val in (
            (yu, "_refresh_google_access_token", "offline-token"),
            (yu, "_fetch_oauth_channel_identity", {"channel_id": CHANNEL_ID, "name": "Chị Đẹp Biết Tuốt"}),
            (yu, "get_settings", {"youtube_processing_timeout_seconds": 30}),
        ):
            p = mock.patch.object(target, attr, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        net = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("External network forbidden"))
        net.start()
        self.addCleanup(net.stop)

    def test_omitted_synthetic_media_when_false_completes_with_warning_and_partial_metadata_state(self):
        omitted = api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic="OMIT"))
        session = api_fixtures.MetadataSession(readbacks=[omitted, omitted, omitted])
        events = []
        with mock.patch.object(yu.time, "sleep"):
            result = yu._upload_via_youtube_api_with_session(
                str(self.clip), self.metadata, self.channel, session=session, evidence_sink=events.append
            )
        self.assertEqual(result["url"], VIDEO_URL)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "API_VIDEO_AI_OMITTED")
        self.assertEqual([e["verification_state"] for e in events], ["received", "verified"])
        normalized = ue._normalize_event(events[-1], version=2)
        self.assertEqual(normalized["upload_state"], "processed")
        self.assertEqual(normalized["metadata_state"], "partial")
        self.assertEqual(normalized["field_states"]["altered_content"], "missing")
        self.assertNotIn("altered_content", normalized["observed"])
        self.assertEqual(sum("uploadType=resumable" in u for u, _ in session.post_calls), 1)
        self.assertEqual(sum(bool(k.get("data")) for _, k in session.put_calls), 1)

    def test_omitted_null_invalid_and_mismatched_synthetic_media_fail_closed_when_required_or_wrong(self):
        cases = [
            ({**self.metadata, "altered_content": True}, [api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic="OMIT"))] * 3, "API_VIDEO_AI_MISSING", "unknown"),
            (self.metadata, [api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic=None))], "API_VIDEO_AI_INVALID", "unknown"),
            (self.metadata, [api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic="false"))], "API_VIDEO_AI_INVALID", "unknown"),
            (self.metadata, [api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic=True))], "API_VIDEO_AI_MISMATCH", "mismatch"),
            ({**self.metadata, "altered_content": True}, [api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic=False))], "API_VIDEO_AI_MISMATCH", "mismatch"),
        ]
        for meta, readbacks, expected_code, expected_state in cases:
            with self.subTest(code=expected_code, altered=meta["altered_content"]):
                session = api_fixtures.MetadataSession(readbacks=readbacks)
                events = []
                with mock.patch.object(yu.time, "sleep"):
                    with self.assertRaises(yu.UploadVerificationError) as ctx:
                        yu._upload_via_youtube_api_with_session(
                            str(self.clip), meta, self.channel, session=session, evidence_sink=events.append
                        )
                self.assertEqual(ctx.exception.code, expected_code)
                self.assertEqual([e["verification_state"] for e in events], ["received", expected_state])
                self.assertEqual(sum("uploadType=resumable" in u for u, _ in session.post_calls), 1)
                self.assertEqual(sum(bool(k.get("data")) for _, k in session.put_calls), 1)

    def test_historical_v1_evidence_json_from_live_attempt_33_reads_cleanly(self):
        raw_v1 = (
            '{"attempt_id":33,"backend":"youtube_api","channel_id":66,"code":"API_VIDEO_AI_MISSING",'
            '"expected":{"category_id":"22"},"observed":{"category_id":"22"},"queue_id":239,'
            '"received_at":"2026-11-17T16:13:40.591360Z","updated_at":"2026-11-17T16:14:41.327011Z",'
            '"verification_state":"unknown","version":1,"video_id":"m7vwyaBseyo",'
            '"video_url":"https://youtu.be/m7vwyaBseyo"}'
        )
        decoded = ue.read_evidence(raw_v1, 239, 33, 66)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["version"], 1)
        self.assertEqual(decoded["video_id"], "m7vwyaBseyo")
        self.assertEqual(decoded["video_url"], "https://youtu.be/m7vwyaBseyo")


class ManualReconcileAndReceiptRecheckTests(unittest.TestCase):
    row = review_fixtures.ReviewRegressions.row

    def setUp(self):
        review_fixtures.ReviewRegressions.setUp(self)
        net = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("External network forbidden"))
        net.start()
        self.addCleanup(net.stop)

    def seed_receipt_review(self, video_id=VIDEO_ID, *, with_evidence=True):
        with closing(database.get_conn()) as conn, conn:
            conn.execute(
                "UPDATE youtube_channels SET upload_mode='youtube_api', channel_id=?, oauth_token_json=? WHERE id=901",
                (CHANNEL_ID, '{"refresh_token":"offline-fixture"}'),
            )
            conn.execute(
                "UPDATE video_queue SET status='NeedsReview', visibility='private', target_channel_id=901, content_key='sha256:abc' WHERE id=901"
            )
            conn.execute(
                "INSERT INTO content_reservations(content_key,channel_id,queue_id,claim_token,state) VALUES('sha256:abc',901,901,'tok','NeedsReview')"
            )
            ev = "{}"
            if with_evidence:
                ev = json.dumps({
                    "attempt_id": 50,
                    "backend": "youtube_api",
                    "channel_id": 901,
                    "code": "API_VIDEO_AI_MISSING",
                    "expected": {
                        "altered_content": False,
                        "category_id": "22",
                        "made_for_kids": False,
                        "privacy_status": "private",
                        "publish_at": None,
                        "youtube_channel_id": CHANNEL_ID,
                    },
                    "field_states": {
                        "altered_content": "missing",
                        "category_id": "present",
                        "made_for_kids": "present",
                        "privacy_status": "present",
                        "processing_status": "present",
                        "publish_at": "missing",
                        "upload_status": "present",
                        "youtube_channel_id": "present",
                    },
                    "metadata_state": "unknown",
                    "observed": {
                        "category_id": "22",
                        "made_for_kids": False,
                        "privacy_status": "private",
                        "processing_status": "succeeded",
                        "publish_at": None,
                        "upload_status": "processed",
                        "youtube_channel_id": CHANNEL_ID,
                    },
                    "queue_id": 901,
                    "received_at": "2026-11-18T00:00:00.000000Z",
                    "updated_at": "2026-11-18T00:00:05.000000Z",
                    "upload_state": "processed",
                    "verification_state": "unknown",
                    "version": 2,
                    "video_id": video_id,
                    "video_url": f"https://youtu.be/{video_id}",
                }, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO upload_attempts(id,queue_id,attempt_no,attempt_type,content_key,channel_id,phase,outcome,evidence_json) "
                "VALUES(50,901,1,'upload','sha256:abc',901,'Finished','NeedsReview',?)",
                (ev,),
            )
            conn.execute("UPDATE video_queue SET last_attempt_id=50 WHERE id=901")

    def test_mismatched_url_is_rejected_and_exact_receipt_url_is_idempotent(self):
        self.seed_receipt_review()
        wrong = self.client.post(
            "/api/queue/901/manual-reupload",
            headers=self.headers,
            json={"action": "confirmed_published", "reason": "Wrong URL attempt", "published_url": "https://youtu.be/abcdefghijk"},
        )
        self.assertEqual(wrong.status_code, 409, wrong.text)
        self.assertEqual(wrong.json().get("code"), "UPLOAD_RECEIPT_URL_MISMATCH")
        self.assertEqual(self.row()["status"], "NeedsReview")

        right = self.client.post(
            "/api/queue/901/manual-reupload",
            headers=self.headers,
            json={"action": "confirmed_published", "reason": "Verified exact video ID", "published_url": VIDEO_URL},
        )
        self.assertEqual(right.status_code, 200, right.text)
        self.assertEqual(self.row()["status"], "Completed")
        self.assertEqual(self.row()["published_url"], VIDEO_URL)

        replay = self.client.post(
            "/api/queue/901/manual-reupload",
            headers=self.headers,
            json={"action": "confirmed_published", "reason": "Verified exact video ID", "published_url": VIDEO_URL},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json().get("replayed"))
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM upload_attempts WHERE queue_id=901").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM published_history WHERE channel_id=901").fetchone()[0], 1)

    def test_unreceipted_attempt_requires_explicit_legacy_acknowledgment(self):
        self.seed_receipt_review(with_evidence=False)
        blocked = self.client.post(
            "/api/queue/901/manual-reupload",
            headers=self.headers,
            json={"action": "confirmed_published", "reason": "Checked Studio manually", "published_url": VIDEO_URL},
        )
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertEqual(blocked.json().get("code"), "LEGACY_RECEIPT_ACK_REQUIRED")
        self.assertEqual(self.row()["status"], "NeedsReview")

        accepted = self.client.post(
            "/api/queue/901/manual-reupload",
            headers=self.headers,
            json={
                "action": "confirmed_published",
                "reason": "Checked Studio manually",
                "published_url": VIDEO_URL,
                "acknowledge_legacy_no_receipt": True,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.row()["status"], "Completed")

    def test_readonly_recheck_receipt_endpoint_verifies_without_mutating_queue(self):
        self.seed_receipt_review()
        fake_session = api_fixtures.MetadataSession(
            readbacks=[api_fixtures.JsonResponse(api_fixtures.video_payload(synthetic="OMIT"))]
        )
        with mock.patch.object(yu, "_http_session_for_channel", return_value=fake_session), \
                mock.patch.object(yu, "_refresh_google_access_token", return_value="offline-token"), \
                mock.patch.object(yu, "_fetch_oauth_channel_identity", return_value={"channel_id": CHANNEL_ID, "name": "Fixture"}):
            res = self.client.post("/api/queue/901/recheck-receipt", headers=self.headers, json={})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["found"])
        self.assertTrue(body["matches_expected"])
        self.assertTrue(body["ai_omitted_when_false"])
        self.assertEqual(body["upload_state"], "processed")
        self.assertEqual(self.row()["status"], "NeedsReview")
        self.assertEqual(fake_session.post_calls, [])
        self.assertEqual(fake_session.put_calls, [])

    def test_readonly_recheck_does_not_accept_terminal_provider_failures(self):
        self.seed_receipt_review()
        before = self.row()
        with closing(database.get_conn()) as conn:
            tables = ("upload_attempts", "published_history", "content_reservations")
            snapshot = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                        for table in tables}
        cases = [
            ("failed", "succeeded", ["upload_status"]),
            ("rejected", "succeeded", ["upload_status"]),
            ("deleted", "succeeded", ["upload_status"]),
            ("processed", "failed", ["processing_status"]),
            ("processed", "terminated", ["processing_status"]),
            ("rejected", "failed", ["upload_status", "processing_status"]),
        ]
        for upload_status, processing_status, mismatches in cases:
            with self.subTest(upload=upload_status, processing=processing_status):
                payload = api_fixtures.video_payload()
                payload["items"][0]["status"]["uploadStatus"] = upload_status
                payload["items"][0]["processingDetails"]["processingStatus"] = processing_status
                session = api_fixtures.MetadataSession(readbacks=[api_fixtures.JsonResponse(payload)])
                with mock.patch.object(yu, "_http_session_for_channel", return_value=session), \
                        mock.patch.object(yu, "_refresh_google_access_token", return_value="offline-token"), \
                        mock.patch.object(yu, "_fetch_oauth_channel_identity", return_value={"channel_id": CHANNEL_ID}):
                    response = self.client.post("/api/queue/901/recheck-receipt", headers=self.headers, json={})
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertTrue(report["found"])
                self.assertFalse(report["matches_expected"])
                self.assertEqual(report["upload_state"], "failed")
                self.assertEqual(report["mismatches"], mismatches)
                self.assertEqual(len(session.get_calls), 1)
                self.assertEqual(session.get_calls[0][1]["params"]["id"], VIDEO_ID)
                self.assertEqual(session.post_calls, [])
                self.assertEqual(session.put_calls, [])
                self.assertEqual(self.row(), before)
                with closing(database.get_conn()) as conn:
                    self.assertEqual(
                        {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                         for table in tables}, snapshot)


class ScheduledUploadCliEncodingTests(unittest.TestCase):
    def test_json_output_is_safe_for_legacy_windows_console_encoding(self):
        report = {"configured": True, "status": "waiting",
                  "message": "Đang chờ đủ khoảng cách giữa hai lượt đăng.", "video": "视频"}
        with tempfile.TemporaryDirectory() as directory:
            for command in ("status", "sync-folder", "run", "observe"):
                with self.subTest(command=command):
                    arguments = [command, "--state-dir", directory]
                    if command != "status":
                        arguments += ["--runtime-dir", directory]
                    buffer = io.BytesIO()
                    output = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", write_through=True)
                    try:
                        with mock.patch("sys.stdout", output), \
                                mock.patch.object(schedule_cli, "read_schedule_status", return_value=report), \
                                mock.patch.object(schedule_cli, "sync_schedule_folder", return_value=report), \
                                mock.patch.object(schedule_cli, "ScheduleRunner") as runner:
                            runner.return_value.run.return_value = 0
                            result = schedule_cli.main(arguments)
                        output.flush()
                        self.assertEqual(result, 0)
                        self.assertEqual(json.loads(buffer.getvalue().decode("cp1252")), report)
                    finally:
                        output.close()

    def test_runner_failure_exit_codes_survive_legacy_console_output(self):
        report = {"configured": True, "status": "needs_review", "message": "Cần đối soát lượt đăng."}
        with tempfile.TemporaryDirectory() as directory:
            for command in ("run", "observe"):
                for code in (1, 2, 3):
                    with self.subTest(command=command, code=code):
                        buffer = io.BytesIO()
                        output = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", write_through=True)
                        try:
                            with mock.patch("sys.stdout", output), \
                                    mock.patch.object(schedule_cli, "read_schedule_status", return_value=report), \
                                    mock.patch.object(schedule_cli, "ScheduleRunner") as runner:
                                runner.return_value.run.return_value = code
                                result = schedule_cli.main([command, "--state-dir", directory,
                                                            "--runtime-dir", directory])
                            output.flush()
                            self.assertEqual(result, code)
                            self.assertEqual(json.loads(buffer.getvalue().decode("cp1252")), report)
                        finally:
                            output.close()


class PackagedScheduleAndTaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sources = self.root / "banve"
        self.sources.mkdir()
        self.file0 = self.sources / "done.mp4"
        self.file1 = self.sources / "next.mp4"
        self.file2 = self.sources / "later.mp4"
        for idx, p in enumerate((self.file0, self.file1, self.file2)):
            p.write_bytes(f"video-{idx}".encode("utf-8"))
        self.legacy_dir = self.root / "legacy"
        self.legacy_dir.mkdir()
        self.legacy_manifest = {
            "revision": "18c0a3691f4b36063a373d9fb87bb36e735257b2",
            "build": "2026.11.17-publish",
            "project": "DHB Banve Private 3d 20261117",
            "channel": {"id": 66, "channel_id": CHANNEL_ID, "name": "Chị Đẹp Biết Tuốt"},
            "source_directory": str(self.sources),
            "cadence_hours": 72,
            "jobs": [
                {"queue_id": 239, "path": str(self.file0), "sha256": hashlib.sha256(self.file0.read_bytes()).hexdigest()},
                {"queue_id": 240, "path": str(self.file1), "sha256": hashlib.sha256(self.file1.read_bytes()).hexdigest()},
                {"queue_id": 241, "path": str(self.file2), "sha256": hashlib.sha256(self.file2.read_bytes()).hexdigest()},
            ],
        }
        self.legacy_state = {
            "version": 1,
            "next_index": 1,
            "last_started_at": "2026-11-17T16:13:10+00:00",
            "inflight_queue_id": None,
            "submission_state": "ready",
            "submitted_at": None,
        }
        su.save_json(self.legacy_dir / "private-upload-manifest.json", self.legacy_manifest)
        su.save_json(self.legacy_dir / "private-upload-state.json", self.legacy_state)

        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()
        self.release_file = self.root / "release.json"
        self.state_dir = self.root / "state"
        self.revision = "a" * 40
        self.build = "2026.11.18-hardening"
        self.assertEqual(
            schedule_cli.main([
                "prepare-release",
                "--runtime-dir", str(self.runtime_dir),
                "--revision", self.revision,
                "--build", self.build,
                "--output", str(self.release_file),
            ]),
            0,
        )
        self.assertEqual(
            schedule_cli.main([
                "migrate-legacy",
                "--legacy-dir", str(self.legacy_dir),
                "--start-at", "2026-11-20T23:15:00+07:00",
                "--release", str(self.release_file),
                "--state-dir", str(self.state_dir),
            ]),
            0,
        )

    def make_fake_api(self, rows, *, on_start=None):
        release_pin = su.load_json(self.release_file)
        manifest = su.load_json(self.state_dir / "manifest.json")

        class FakeAPI:
            def __init__(self_api, _base, _rt, _rel):
                self_api.started_payloads = []
                self_api.preview_calls = []

            def connect(self_api):
                return None

            def diagnostics(self_api, *, idle=False):
                return {
                    "version": release_pin["build"],
                    "runtime": {"revision": release_pin["revision"], "restart_required": False},
                    "workers": {"pipeline_running": False, "active_batch_threads": 0, "monitor_workers": 0},
                    "upload_slot": {"available": True, "state": "idle", "remaining_seconds": 0},
                }

            def verify_channel(self_api, _m):
                return None

            def queue(self_api, _m):
                return rows()

            def preview(self_api, _m, qid):
                self_api.preview_calls.append(qid)
                return {"ids": [qid], "do_download": False, "do_render": False, "do_upload": True, "preview_id": f"tok-{qid}"}

            def start(self_api, payload):
                self_api.started_payloads.append(payload)
                if on_start:
                    return on_start(payload)
                return {"ok": True}

        return FakeAPI, manifest

    def base_rows(self, statuses=None):
        statuses = statuses or {239: ("Completed", "https://youtu.be/m7vwyaBseyo"), 240: ("Pending", ""), 241: ("Pending", "")}
        paths = {239: str(self.file0), 240: str(self.file1), 241: str(self.file2)}
        return {
            qid: {
                "id": qid,
                "status": statuses[qid][0],
                "published_url": statuses[qid][1],
                "input_video_path": paths[qid],
                "visibility": "private",
                "schedule_time": "",
                "target_channel_id": 66,
                "is_claimed": False,
                "requires_review": statuses[qid][0] == "NeedsReview",
            }
            for qid in (239, 240, 241)
        }

    def test_migrated_state_allows_cleaned_completed_file_and_dry_run_catches_missing_future_file(self):
        # Delete already-completed #239 file: must NOT break dry-run or next run.
        self.file0.unlink()
        FakeAPI, _ = self.make_fake_api(lambda: self.base_rows())
        early_now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        runner = su.ScheduleRunner(self.state_dir, self.runtime_dir, api_factory=FakeAPI, now=lambda: early_now)
        self.assertEqual(runner.run(dry_run=True), 0)
        self.assertEqual(runner.api.preview_calls, [240])
        self.assertEqual(runner.api.started_payloads, [])

        # Delete future pending #241 file: dry-run must fail immediately with SOURCE_CHANGED without mutating state.json!
        before_state = (self.state_dir / "state.json").read_text(encoding="utf-8")
        self.file2.unlink()
        with self.assertRaises(su.ScheduleError) as ctx:
            su.ScheduleRunner(self.state_dir, self.runtime_dir, api_factory=FakeAPI, now=lambda: early_now).run(dry_run=True)
        self.assertEqual(ctx.exception.code, "SOURCE_CHANGED")
        self.assertEqual((self.state_dir / "state.json").read_text(encoding="utf-8"), before_state)

    def test_drift_wait_observer_completion_and_needs_review_halt(self):
        current_rows = self.base_rows()
        clock = [datetime(2026, 11, 20, 16, 14, 55, tzinfo=timezone.utc)]  # 5s before 23:15+07:00 start_at
        mono = [100.0]

        def sleep_fn(sec):
            clock[0] += timedelta(seconds=sec)
            mono[0] += sec

        def on_start(payload):
            qid = payload["ids"][0]
            current_rows[qid]["status"] = "Uploading"
            current_rows[qid]["is_claimed"] = True
            return {"ok": True}

        polls = [0]

        def rows_provider():
            if current_rows[240]["status"] == "Uploading":
                polls[0] += 1
                if polls[0] >= 2:
                    current_rows[240]["status"] = "Completed"
                    current_rows[240]["published_url"] = VIDEO_URL
                    current_rows[240]["is_claimed"] = False
            return current_rows

        FakeAPI, _ = self.make_fake_api(rows_provider, on_start=on_start)
        runner = su.ScheduleRunner(
            self.state_dir,
            self.runtime_dir,
            api_factory=FakeAPI,
            now=lambda: clock[0],
            monotonic=lambda: mono[0],
            sleep=sleep_fn,
            poll_seconds=1,
        )
        self.assertEqual(runner.run(), 0)
        status = su.read_schedule_status(self.state_dir, now=clock[0])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["next_queue_id"], 241)

        # Advance 72h and simulate NeedsReview on final item #241 -> halts with exit 2 and action_required=True.
        clock[0] += timedelta(hours=72, seconds=5)
        def on_start_fail(payload):
            qid = payload["ids"][0]
            current_rows[qid]["status"] = "NeedsReview"
            current_rows[qid]["requires_review"] = True
            return {"ok": True}

        FakeAPI2, _ = self.make_fake_api(lambda: current_rows, on_start=on_start_fail)
        runner2 = su.ScheduleRunner(
            self.state_dir,
            self.runtime_dir,
            api_factory=FakeAPI2,
            now=lambda: clock[0],
            monotonic=lambda: mono[0],
            sleep=sleep_fn,
        )
        self.assertEqual(runner2.run(), 2)
        halted = su.read_schedule_status(self.state_dir, now=clock[0])
        self.assertEqual(halted["status"], "needs_review")
        self.assertTrue(halted["action_required"])

    def test_task_xml_and_plan_contract(self):
        plan_dir = self.root / "task-plan"
        py_exe = self.root / "python.exe"
        py_exe.write_bytes(b"stub")
        plan = schedule_task.prepare_plan(
            task_name="DHB-Banve-Private-72h",
            python=py_exe,
            runtime_dir=self.runtime_dir,
            state_dir=self.state_dir,
            output_dir=plan_dir,
        )
        self.assertEqual(plan["task_name"], "DHB-Banve-Private-72h")
        xml_text = (plan_dir / "task.xml").read_text(encoding="utf-8")
        self.assertIn("InteractiveToken", xml_text)
        self.assertIn("<DaysInterval>3</DaysInterval>", xml_text)
        self.assertIn("scheduled_upload_runner.py", xml_text)

    def test_diagnostics_and_schedule_status_endpoint_expose_safe_summary(self):
        import app as studio_app
        from fastapi.testclient import TestClient

        with mock.patch.dict(os.environ, {"DHB_SCHEDULE_STATE_DIR": str(self.state_dir)}):
            client = TestClient(studio_app.app, base_url="http://127.0.0.1:8686")
            token = client.get("/api/auth/token").json()["token"]
            headers = {"X-DHB-Token": token, "Origin": "http://127.0.0.1:8686"}
            sched_res = client.get("/api/scheduled-uploads/status", headers=headers)
            self.assertEqual(sched_res.status_code, 200, sched_res.text)
            data = sched_res.json()
            self.assertTrue(data["configured"])
            self.assertEqual(data["completed_count"], 1)
            self.assertEqual(data["total_count"], 3)
            self.assertFalse(data["folder_drift"]["detected"])
            self.assertNotIn(str(self.sources), json.dumps(data))

    def test_folder_drift_detection_and_sync_folder_preserves_cursor(self):
        self.file2.unlink()
        file3 = self.sources / "brand_new.mp4"
        file3.write_bytes(b"video-brand-new")

        drift_status = su.read_schedule_status(self.state_dir)
        self.assertTrue(drift_status["folder_drift"]["detected"])
        self.assertEqual(drift_status["folder_drift"]["missing_future_count"], 1)
        self.assertEqual(drift_status["folder_drift"]["new_files_count"], 1)
        self.assertTrue(drift_status["folder_drift"]["next_source_exists"])
        self.assertNotIn(str(self.sources), json.dumps(drift_status))

        db_rows = list(self.base_rows().values())
        deleted_batches = []
        previewed_batches = []

        class FakeSyncAPI:
            def __init__(self_api, _base, _rt, _rel):
                self_api.session = None

            def connect(self_api):
                return None

            def diagnostics(self_api, *, idle=False):
                return {"workers": {"pipeline_running": False, "active_batch_threads": 0, "monitor_workers": 0}}

            def verify_channel(self_api, _m):
                return None

            def project_rows(self_api, _project):
                return [dict(r) for r in db_rows]

            def delete_rows(self_api, ids, *, expected_rows=None):
                self.assertEqual([row["id"] for row in expected_rows or []], list(ids))
                deleted_batches.append(list(ids))
                db_rows[:] = [r for r in db_rows if r["id"] not in set(ids)]
                return {"ok": True, "deleted": len(ids)}

            def scan_folder(self_api, _manifest):
                db_rows.append({
                    "id": 242,
                    "status": "Pending",
                    "published_url": "",
                    "input_video_path": str(file3),
                    "visibility": "private",
                    "schedule_time": "",
                    "target_channel_id": 66,
                    "is_claimed": False,
                    "requires_review": False,
                })
                return {"ok": True, "added": 1, "skipped": 1, "added_ids": [242],
                        "cleanup_receipts": [{"queue_id": 242, "proof": "f" * 64}]}

            def preview_batch(self_api, _manifest, queue_ids):
                previewed_batches.append(list(queue_ids))

        res = su.sync_schedule_folder(self.state_dir, self.runtime_dir, api_factory=FakeSyncAPI)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed_pending_count"], 1)
        self.assertEqual(res["added_count"], 1)
        self.assertEqual(deleted_batches, [[241]])
        self.assertEqual(previewed_batches, [[240, 242]])

        after = res["schedule"]
        self.assertFalse(after["folder_drift"]["detected"])
        self.assertEqual(after["completed_count"], 1)
        self.assertEqual(after["total_count"], 3)
        self.assertEqual(after["next_queue_id"], 240)
        self.assertEqual(after["last_started_at"], "2026-11-17T16:13:10+00:00")
        self.assertNotIn(str(self.sources), json.dumps(res))

    def test_webserver_autostart_plan_xml_contract(self):
        plan_dir = self.root / "webserver-plan"
        py_exe = self.root / "python.exe"
        py_exe.write_bytes(b"stub")
        (self.runtime_dir / "app.py").write_text("# stub\n", encoding="utf-8")
        plan = schedule_task.prepare_webserver_plan(
            task_name="DHB-Studio-WebServer-Autostart",
            python=py_exe,
            runtime_dir=self.runtime_dir,
            output_dir=plan_dir,
            port=8686,
        )
        self.assertEqual(plan["kind"], "webserver_autostart")
        xml_text = (plan_dir / "task.xml").read_text(encoding="utf-8")
        self.assertIn('encoding="UTF-16"', xml_text)
        self.assertIn("<LogonTrigger>", xml_text)
        self.assertIn("<Hidden>true</Hidden>", xml_text)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml_text)
        self.assertIn("-m uvicorn app:app --host 127.0.0.1 --port 8686 --no-access-log", xml_text)

    def test_clean_raw_video_stem_strips_dubbed_hex_noise(self):
        import ai_engine

        self.assertEqual(
            ai_engine.clean_raw_video_stem("Dubbed_telegram_15230fc0090b4d6ea7cab7cc409dbb84_紧凑版卡套我向你道歉"),
            "紧凑版卡套我向你道歉",
        )
        self.assertEqual(
            ai_engine.clean_raw_video_stem("Dubbed_telegram_47d82bcb6911484990e8f10cd691a5a5_1200军装开箱试穿_93c6a186"),
            "1200军装开箱试穿",
        )
        self.assertEqual(
            ai_engine.clean_raw_video_stem("Dubbed_corrected_telegram_154f505728f44833b720ff6895b8379a"),
            "Dubbed_corrected_telegram_154f505728f44833b720ff6895b8379a",
        )
        self.assertEqual(
            ai_engine.clean_raw_video_stem("Xem chi tiết tại https://example.com/item_12345678"),
            "Xem chi tiết tại https://example.com/item_12345678",
        )

    def test_gemini_3_flash_preserved_and_retries_next_candidate_on_503_or_404(self):
        import ai_engine

        self.assertEqual(ai_engine._normalize_gemini_model("gemini-3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(ai_engine._normalize_gemini_model("models/gemini-3-flash-preview"), "gemini-3-flash-preview")

        called_urls = []

        class FakeResp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        def fake_post(url, **_kwargs):
            called_urls.append(url)
            if "gemini-3.8-flash" in url:
                return FakeResp(503, {"error": {"code": 503}})
            return FakeResp(200, {
                "candidates": [{
                    "content": {
                        "parts": [
                            {"thought": True, "text": "internal reasoning"},
                            {"text": '{"title":"Tiêu đề chuẩn #shorts","description":"Mô tả","tags":"shorts, meovat"}'},
                        ]
                    }
                }]
            })

        with mock.patch.object(ai_engine, "get_settings", return_value={"gemini_api_key": "AQ.test-key", "gemini_model": "gemini-3.8-flash"}), \
                mock.patch.object(ai_engine.requests, "post", side_effect=fake_post), \
                mock.patch.object(ai_engine, "add_log"):
            out = ai_engine.ai_optimize_youtube_metadata("Dubbed_telegram_15230fc0090b4d6ea7cab7cc409dbb84_紧凑版卡套", provider="gemini")
        self.assertFalse(out["fallback"])
        self.assertEqual(out["provider_used"], "gemini")
        self.assertEqual(out["title"], "Tiêu đề chuẩn #shorts")
        self.assertEqual(len(called_urls), 2)
        self.assertIn("gemini-3.8-flash", called_urls[0])
        self.assertIn("gemini-3-flash-preview", called_urls[1])


if __name__ == "__main__":
    unittest.main()
