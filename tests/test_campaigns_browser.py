"""Exercise the campaign dashboard with intercepted HTTP requests."""

import json
import os
import unittest
from unittest import mock

from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get("DHB_BROWSER_TESTS") == "1", "Run in browser CI")
class CampaignBrowserTests(unittest.TestCase):
    def test_preview_enable_pause_and_render_progress(self):
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        with mock.patch("ui_browser_fixture.BASE", "http://127.0.0.1:9"):
            browser = playwright.chromium.launch(headless=True)
            self.addCleanup(browser.close)
            fixture = UIPage(browser)
            self.addCleanup(fixture.close)
            page = fixture.page
            fixture.responses["/api/channels"] = [
                {"id": 1, "name": "Kênh mẫu", "channel_id": "UC" + "a" * 22, "profile_name": "Mẫu"}
            ]
            data = {"lists": [{"id": 1, "name": "Nguồn", "queue_ids": [12]}], "runs": [],
                    "upload_gap_minutes": 30, "campaigns": [{
                        "id": 2, "state": "draft", "name": "Lịch thử", "channel_id": 1, "list_id": 1,
                        "visibility": "private", "content_order": "sequential", "start_date": None,
                        "times": ["09:00", "16:00"], "utc_offset_minutes": 420, "render": False,
                    }]}
            fixture.responses["/api/campaigns"] = data
            fixture.responses["/api/campaigns/2/preview"] = {
                "revision": "fixture-revision", "problems": [],
                "items": [{"id": 12, "title": "<Video fixture>", "youtube_channel_id": "UC" + "a" * 22,
                           "visibility": "private", "file_kind": "file nguồn"}],
            }

            def route_action(route, parsed):
                if parsed.path == "/api/campaigns/2/enable":
                    self.assertEqual(json.loads(route.request.post_data)["revision"], "fixture-revision")
                    data["campaigns"][0]["state"] = "enabled"
                    data["runs"] = [{"id": 7, "campaign_id": 2, "state": "completed", "queue_id": 12,
                                     "local_day": "2026-11-26", "local_time": "09:00"}]
                elif parsed.path == "/api/campaigns/2/pause":
                    data["campaigns"][0]["state"] = "paused"
                elif parsed.path == "/api/campaigns/runs/7/logs":
                    self.assertEqual(route.request.headers["x-dhb-token"], "offline-ui-token")
                    line = json.dumps({"created_at": "09:00", "status": "OK", "message": "<Mock upload>"})
                    route.fulfill(body=f"id: 1\ndata: {line}\n\n", content_type="text/event-stream")
                    return True
                elif parsed.path == "/api/channel-workspace/1/analytics/realtime":
                    self.assertEqual(route.request.method, "POST")
                    route.fulfill(json={"views_48h": 42, "updated_at": "2026-11-27T00:00:00+07:00"})
                    return True
                else:
                    return False
                route.fulfill(json={"id": 2, "state": data["campaigns"][0]["state"]})
                return True

            fixture.handler = route_action
            page.locator('[data-tab="tab-campaigns"]').click()
            page.locator("#campaign-edit").select_option("2")
            page.locator("#campaign-preview").click()
            page.locator("#campaign-preview-items li").get_by_text("<Video fixture>", exact=False).wait_for()
            page.locator("#campaign-enable").click()
            page.get_by_text("Đã bật chiến dịch.", exact=True).wait_for()
            page.locator("#campaign-run-rows").get_by_text("Đã đăng").wait_for()
            page.get_by_role("button", name="Xem log").click()
            page.locator("#campaign-log-lines").get_by_text("<Mock upload>", exact=False).wait_for()
            page.get_by_role("button", name="Đóng nhật ký").click()
            page.locator("#campaign-pause").click()
            page.get_by_text("Đã tạm dừng nhận lượt mới.", exact=True).wait_for()
            page.locator("#campaign-realtime").click()
            page.locator("#campaign-realtime-result").get_by_text("42 lượt xem trong 48 giờ", exact=False).wait_for()
            form = page.locator("#campaign-simulation-form")
            form.locator("[name=activity]").select_option("reply")
            form.locator("[name=outcome]").select_option("error")
            form.locator("[name=slots]").fill("16:00, 09:00")
            form.get_by_role("button", name="Chạy mô phỏng").click()
            page.locator("#campaign-simulation-next").click()
            page.locator("#campaign-simulation-runs").get_by_text("09:00 · Mẫu · Bình luận · Mock lỗi dịch vụ").wait_for()
            page.locator("#campaign-simulation-next").click()
            page.locator("#campaign-simulation-status").get_by_text("Mô phỏng đã hoàn tất.").wait_for()
            self.assertEqual(fixture.external, [])
            self.assertEqual(fixture.errors, [])

    def test_studio_realtime_only_accepts_the_visible_48_hour_card_of_requested_channel(self):
        from playwright.sync_api import sync_playwright
        from studio_realtime import _read_page

        channel = "UC" + "a" * 22
        other_channel = "UC" + "b" * 22
        opened = []
        body = ['<style>ytcp-realtime-metrics,yta-realtime-card { display:block }</style>'
                '<ytcp-realtime-metrics><div>1.234</div><div>Lượt xem · 48 giờ qua</div>'
                '<div>0</div><div>Views · Last 60 minutes</div></ytcp-realtime-metrics>']
        allowed = f"https://studio.youtube.com/channel/{channel}/analytics/tab-overview/period-default"
        mismatch = allowed.replace(channel, other_channel)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def route_studio(route):
                    opened.append(route.request.url)
                    if route.request.url in (allowed, mismatch):
                        route.fulfill(body=body[0], content_type="text/html; charset=utf-8")
                    else:
                        route.abort()

                page.route("**/*", route_studio)
                self.assertEqual(_read_page(page, channel), 1234)
                body[0] += '<yta-realtime-card><div>16</div><div>Views · Last 48 hours</div></yta-realtime-card>'
                with self.assertRaises(ValueError):
                    _read_page(page, channel)
                # Model an in-page channel switch. A synthetic HTTP redirect may escape
                # Playwright interception for the redirected request.
                body[0] += f'<script>history.replaceState(null,"",{json.dumps(mismatch)})</script>'
                with self.assertRaisesRegex(ValueError, "đúng kênh"):
                    _read_page(page, channel)
                self.assertEqual(set(opened), {allowed})
            finally:
                browser.close()
