"""Browser tests for v2 upload evidence, receipt-bound manual reconciliation, and scheduled upload status UI."""
from __future__ import annotations

import json
import os
import unittest

from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get("DHB_BROWSER_TESTS") == "1", "Run in the browser CI job")
class UploadWorkflowHardeningBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.fixture = UIPage(self.browser)
        self.page = self.fixture.page
        self.addCleanup(self.fixture.close)

    def test_v2_evidence_dialog_and_receipt_bound_reconciliation_with_readonly_recheck(self):
        self.fixture.seed_queue([
            {
                "id": 239,
                "title": "Video Banve #239",
                "status": "NeedsReview",
                "requires_review": 1,
                "target_channel_id": 1,
                "visibility": "private",
            }
        ])
        v2_evidence = {
            "version": 2,
            "backend": "youtube_api",
            "channel_id": 1,
            "queue_id": 239,
            "attempt_id": 33,
            "video_id": "m7vwyaBseyo",
            "video_url": "https://youtu.be/m7vwyaBseyo",
            "verification_state": "verified",
            "upload_state": "processed",
            "metadata_state": "partial",
            "code": "API_VIDEO_AI_OMITTED",
            "received_at": "2026-11-17T16:13:40Z",
            "updated_at": "2026-11-17T16:14:41Z",
            "expected": {
                "category_id": "22",
                "privacy_status": "private",
                "made_for_kids": False,
                "altered_content": False,
                "publish_at": None,
                "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
            },
            "observed": {
                "category_id": "22",
                "privacy_status": "private",
                "made_for_kids": False,
                "publish_at": None,
                "upload_status": "processed",
                "processing_status": "succeeded",
                "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
            },
            "field_states": {
                "category_id": "present",
                "privacy_status": "present",
                "made_for_kids": "present",
                "altered_content": "missing",
                "publish_at": "missing",
                "upload_status": "present",
                "processing_status": "present",
                "youtube_channel_id": "present",
            },
        }
        self.fixture.responses["/api/queue/239/upload-evidence"] = {
            "queue_id": 239,
            "attempts": [{"id": 33, "attempt_no": 1, "outcome": "NeedsReview", "evidence": v2_evidence, "evidence_error": False}],
        }
        self.fixture.responses["/api/queue/239/recheck-receipt"] = {
            "ok": True,
            "found": True,
            "video_id": "m7vwyaBseyo",
            "video_url": "https://youtu.be/m7vwyaBseyo",
            "upload_state": "processed",
            "observed": {"privacy_status": "private", "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"},
            "matches_expected": True,
            "ai_omitted_when_false": True,
            "mismatches": [],
        }
        self.fixture.responses["/api/queue/239/manual-reupload"] = {"ok": True, "message": "Đã xác nhận"}

        # 1. Verify v2 evidence modal rendering
        self.page.evaluate("openUploadEvidence(239)")
        ev_dialog = self.page.locator(".upload-evidence-dialog")
        ev_dialog.wait_for(state="visible")
        self.page.wait_for_function("document.querySelector('.upload-evidence-dialog').textContent.includes('YouTube lược bỏ trường')")
        text = ev_dialog.inner_text()
        self.assertIn("YouTube đã xử lý xong", text)
        self.assertIn("Đã xác minh (YouTube lược bỏ nhãn AI)", text)
        self.assertIn("UCaaaaaaaaaaaaaaaaaaaaaa", text)
        ev_dialog.get_by_role("button", name="Đóng", exact=True).click()
        ev_dialog.wait_for(state="detached")

        # 2. Verify receipt-bound manual reconciliation dialog + read-only API recheck
        self.page.evaluate("manualReuploadQueueItem(239, 'confirmed_published')")
        rec_dialog = self.page.locator(".reconcile-dialog")
        rec_dialog.wait_for(state="visible")
        url_input = rec_dialog.locator("#reconcile-url-input")
        self.assertEqual(url_input.input_value(), "https://youtu.be/m7vwyaBseyo")
        self.assertIsNotNone(url_input.get_attribute("readonly"))

        rec_dialog.locator("[data-recheck-receipt]").click()
        self.page.wait_for_function(
            "document.querySelector('[data-recheck-status]') && !document.querySelector('[data-recheck-status]').hidden && document.querySelector('[data-recheck-status]').textContent.includes('Đọc lại khớp yêu cầu')"
        )
        self.assertIn("m7vwyaBseyo", rec_dialog.locator("#reconcile-reason-input").input_value())

        with self.page.expect_response("**/api/queue/239/manual-reupload"):
            rec_dialog.locator("[data-reconcile-submit]").click()
        rec_dialog.wait_for(state="detached")
        post_calls = [r for r in self.fixture.requests if r["path"] == "/api/queue/239/manual-reupload"]
        self.assertEqual(len(post_calls), 1)
        sent = json.loads(post_calls[0]["body"])
        self.assertEqual(sent["published_url"], "https://youtu.be/m7vwyaBseyo")
        self.assertEqual(self.fixture.errors, [])
        self.assertEqual(self.fixture.external, [])

    def test_unreceipted_attempt_reconcile_requires_legacy_checkbox(self):
        self.fixture.seed_queue([
            {"id": 212, "title": "Legacy #212", "status": "NeedsReview", "requires_review": 1, "target_channel_id": 1}
        ])
        self.fixture.responses["/api/queue/212/upload-evidence"] = {
            "queue_id": 212,
            "attempts": [{"id": 10, "attempt_no": 1, "outcome": "NeedsReview", "evidence": None, "evidence_error": False}],
        }
        self.fixture.responses["/api/queue/212/manual-reupload"] = {"ok": True}

        self.page.evaluate("manualReuploadQueueItem(212, 'confirmed_published')")
        rec_dialog = self.page.locator(".reconcile-dialog")
        rec_dialog.wait_for(state="visible")
        rec_dialog.locator("#reconcile-url-input").fill("https://youtu.be/dQw4w9WgXcQ")
        rec_dialog.locator("#reconcile-reason-input").fill("Đã đối chiếu thủ công trên Studio")

        # Submitting without checking legacy-ack must show inline error and not POST
        rec_dialog.locator("[data-reconcile-submit]").click()
        self.assertFalse(rec_dialog.locator("[data-reconcile-error]").is_hidden())
        self.assertEqual([r for r in self.fixture.requests if r["path"] == "/api/queue/212/manual-reupload"], [])

        rec_dialog.locator("#reconcile-legacy-ack").check()
        with self.page.expect_response("**/api/queue/212/manual-reupload"):
            rec_dialog.locator("[data-reconcile-submit]").click()
        rec_dialog.wait_for(state="detached")
        sent = json.loads([r for r in self.fixture.requests if r["path"] == "/api/queue/212/manual-reupload"][0]["body"])
        self.assertTrue(sent["acknowledge_legacy_no_receipt"])
        self.assertEqual(self.fixture.errors, [])

    def test_schedule_status_banner_in_publish_hub_and_admin_diagnostics(self):
        sched_payload = {
            "configured": True,
            "status": "waiting",
            "message": "Đang chờ đủ khoảng cách giữa hai lượt đăng.",
            "completed_count": 1,
            "total_count": 21,
            "next_queue_id": 240,
            "next_due_at": "2026-11-20T16:15:00+00:00",
            "last_result": {"queue_id": 239, "status": "completed"},
            "task_session_required": True,
            "action_required": False,
            "task_disable_recommended": False,
        }
        self.fixture.responses["/api/scheduled-uploads/status"] = sched_payload
        self.page.locator("#publish-workflow-open").click()
        banner = self.page.locator("#publish-schedule-status")
        banner.wait_for(state="visible")
        self.assertIn("1/21 video", banner.inner_text())
        self.assertIn("#240", banner.inner_text())
        self.page.keyboard.press("Escape")

        self.page.evaluate(
            "payload => window.StudioAdmin.onDiagnosticsLoaded({version: '2026.11.18-ops', database: {available: true}, scheduled_uploads: payload})",
            sched_payload,
        )
        diag_text = self.page.locator("#admin-diagnostics-cards").inner_text()
        self.assertIn("Lịch đăng Private", diag_text)
        self.assertIn("1/21 video", diag_text)
        self.assertEqual(self.fixture.errors, [])

    def test_schedule_folder_drift_warning_and_sync_button(self):
        self.fixture.seed_queue([])
        drift_payload = {
            "configured": True,
            "status": "waiting",
            "message": "Đang chờ đủ khoảng cách giữa hai lượt đăng.",
            "completed_count": 1,
            "total_count": 25,
            "next_queue_id": 240,
            "next_due_at": "2026-11-20T16:15:00+00:00",
            "folder_drift": {
                "detected": True,
                "missing_future_count": 1,
                "new_files_count": 2,
                "next_source_exists": True,
            },
            "task_session_required": True,
            "action_required": False,
            "task_disable_recommended": False,
        }
        synced_payload = {
            **drift_payload,
            "total_count": 26,
            "folder_drift": {
                "detected": False,
                "missing_future_count": 0,
                "new_files_count": 0,
                "next_source_exists": True,
            },
        }
        self.fixture.responses["/api/scheduled-uploads/status"] = drift_payload
        self.fixture.responses["/api/scheduled-uploads/sync"] = {
            "ok": True,
            "removed_pending_count": 1,
            "added_count": 2,
            "schedule": synced_payload,
        }
        self.page.locator("#publish-workflow-open").click()
        banner = self.page.locator("#publish-schedule-status")
        banner.wait_for(state="visible")
        self.assertEqual(banner.get_attribute("data-state"), "warning")
        self.assertIn("1 file chờ không còn trên đĩa", banner.inner_text())
        self.assertIn("2 file mới chưa nạp vào lịch", banner.inner_text())

        with self.page.expect_response("**/api/scheduled-uploads/sync"):
            banner.locator("#publish-schedule-sync").click()
        self.page.wait_for_function("document.getElementById('publish-schedule-status')?.dataset?.state === 'ready'")
        self.assertIn("1/26 video", banner.inner_text())
        self.assertEqual(self.fixture.errors, [])


if __name__ == "__main__":
    unittest.main()
