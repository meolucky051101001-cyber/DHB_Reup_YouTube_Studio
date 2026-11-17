"""Full-page queue UI: selection scope, guarded actions and polling stability."""
import json
import os
import unittest

from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class QueueUIBrowserTests(unittest.TestCase):
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
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.addCleanup(self.fixture.close)

    def seed(self):
        self.fixture.seed_queue([
            {'id': 1, 'title': 'Alpha', 'description': 'Nội dung thứ nhất', 'status': 'Rendered',
             'target_channel_id': 1, 'render_progress': 100, 'render_mode': 'shorts_9_16'},
            {'id': 2, 'title': 'Beta', 'status': 'Rendering', 'render_progress': 42, 'is_claimed': 1},
            {'id': 3, 'title': 'Gamma', 'status': 'NeedsReview', 'requires_review': 1,
             'error_message': 'Cần đối chiếu kết quả', 'upload_progress': 83},
            {'id': 4, 'title': 'Delta', 'status': 'Completed', 'upload_progress': 100,
             'published_url': 'https://youtu.be/dQw4w9WgXcQ'},
            {'id': 5, 'title': 'Epsilon', 'status': 'Error', 'error_message': 'Không đọc được nguồn'},
        ])

    def assert_isolated(self):
        self.assertEqual(self.fixture.errors, [])
        self.assertEqual(self.fixture.external, [])

    def starts(self):
        return [request for request in self.fixture.requests if request['path'] == '/api/pipeline/start']

    def test_hidden_selection_controls_real_payload_and_zero_scope_does_not_start(self):
        self.seed()
        self.fixture.responses['/api/pipeline/start'] = {'ok': True}
        self.page.locator('.row-chk[data-id="1"]').check()
        self.page.locator('.row-chk[data-id="5"]').check()
        self.page.select_option('#queue-status-filter', 'rendered')
        self.assertIn('1 đang ẩn', self.page.inner_text('#queue-selection-summary'))
        self.assertEqual(self.page.evaluate('getTargetIdsOrAlert()'), [1, 5])
        self.page.evaluate('startPipeline(true, false, false)')
        self.assertEqual(json.loads(self.starts()[-1]['body'])['ids'], [1, 5])
        self.page.evaluate('toggleSelectAll(false)')
        self.assertEqual(self.page.evaluate('getTargetIdsOrAlert()'), [5])
        self.page.get_by_text('Lựa chọn', exact=True).click()
        self.page.get_by_role('button', name='Bỏ toàn bộ lựa chọn', exact=True).click()
        self.assertEqual(self.page.evaluate('getTargetIdsOrAlert()'), [1])
        self.page.fill('#queue-search', 'no matching rows')
        self.assertEqual(self.page.evaluate('getTargetIdsOrAlert()'), [])
        self.assertTrue(self.page.get_by_role('button', name='Xem trước & đăng', exact=True).is_disabled())
        before = len(self.starts())
        self.page.evaluate('startPipeline(true, false, false)')
        self.assertEqual(len(self.starts()), before)
        self.assert_isolated()

    def test_poll_preserves_focus_scroll_selection_and_source_draft(self):
        self.seed()
        self.page.locator('.row-chk[data-id="1"]').check()
        self.page.locator('tr[data-queue-id="1"] .uq-title').focus()
        self.page.evaluate("gQueueItems[0].title='Alpha updated'; StudioQueue.render(gQueueItems); StudioQueue.render(gQueueItems)")
        self.assertEqual(self.page.evaluate('document.activeElement.dataset.queueAction'), 'details')
        self.assertTrue(self.page.locator('.row-chk[data-id="1"]').is_checked())
        self.page.get_by_role('button', name='+ Thêm video', exact=True).click()
        self.page.get_by_role('button', name='Liên kết', exact=True).click()
        self.page.fill('#scan-urls', 'https://example.test/source')
        self.page.evaluate('StudioQueue.render(gQueueItems); StudioQueue.render(gQueueItems)')
        self.assertEqual(self.page.input_value('#scan-urls'), 'https://example.test/source')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'scan-urls')
        self.assertTrue(self.page.locator('#queue-source-panel').evaluate('(node) => node.open'))
        self.assert_isolated()

    def test_review_details_are_readonly_and_untrusted_values_are_text(self):
        self.fixture.seed_queue([
            {'id': 9, 'title': '<img src=x onerror=window.BAD=true>', 'description': '<script>window.BAD=true</script>',
             'status': 'NeedsReview', 'requires_review': 1, 'published_url': 'javascript:window.BAD=true',
             'target_profile_name': '<svg/onload=window.BAD=true>', 'render_mode': 'custom_ffmpeg'},
        ])
        self.fixture.responses['/api/queue/9/upload-evidence'] = {'queue_id': 9, 'attempts': []}
        self.assertEqual(self.page.locator('#queue-tbody img,#queue-tbody svg,#queue-tbody a').count(), 0)
        self.assertIn('Profile:', self.page.inner_text('.uq-channel'))
        self.assertIn('FFmpeg tùy chỉnh', self.page.inner_text('.uq-config'))
        self.page.get_by_role('button', name='Đối soát', exact=True).click()
        self.assertTrue(self.page.locator('.uq-details-dialog').is_visible())
        self.assertTrue(self.page.get_by_role('button', name='Xác nhận đã đăng', exact=True).is_visible())
        self.assertTrue(self.page.get_by_role('button', name='Xác nhận chưa đăng', exact=True).is_visible())
        self.assertEqual(self.page.locator('.uq-details-dialog img,.uq-details-dialog script').count(), 0)
        self.page.get_by_role('button', name='Xem bằng chứng', exact=True).click()
        self.page.get_by_text('Chưa có lượt đăng được ghi nhận cho video này.', exact=True).wait_for()
        self.assertTrue(self.page.evaluate('Boolean(document.activeElement.closest(".upload-evidence-dialog"))'))
        mutations = [request for request in self.fixture.requests if request['method'] not in ('GET', 'HEAD')]
        self.assertEqual(mutations, [])
        self.assertFalse(self.page.evaluate('Boolean(window.BAD)'))
        self.assert_isolated()

    def test_excel_csv_scope_labels_and_completed_safe_link(self):
        self.seed()
        self.page.fill('#queue-search', 'Alpha')
        self.page.get_by_text('Nhập / xuất', exact=True).click()
        self.assertTrue(self.page.get_by_role('button', name='Excel: xuất toàn bộ dự án', exact=True).is_visible())
        self.assertTrue(self.page.get_by_role('button', name='CSV: xuất toàn dự án', exact=True).is_visible())
        self.page.locator('.row-chk[data-id="1"]').check()
        self.assertTrue(self.page.get_by_role('button', name='Excel: xuất 1 dòng đã chọn', exact=True).is_visible())
        self.assertTrue(self.page.get_by_role('button', name='CSV: xuất toàn dự án', exact=True).is_visible())
        self.page.evaluate('StudioQueue.resetFilters()')
        self.assertEqual(self.page.locator('tr[data-queue-id="4"] a').get_attribute('href'), 'https://youtu.be/dQw4w9WgXcQ')
        self.assert_isolated()

    def test_filter_and_stop_states_do_not_claim_unknown_data_is_ready(self):
        self.seed()
        self.page.select_option('#queue-channel-filter', '1')
        self.assertEqual(self.page.evaluate('getVisibleQueueIds()'), [1])
        self.page.evaluate('StudioQueue.updateRunning({pipeline_running:true,batch_state:"stopping"})')
        self.assertTrue(self.page.get_by_role('button', name='Đang dừng toàn hệ thống…', exact=True).is_disabled())
        self.page.evaluate('StudioQueue.onLoadError(new Error("offline")); StudioQueue.onOverviewError(new Error("offline"))')
        self.assertIn('Không cập nhật được hàng đợi', self.page.inner_text('#queue-freshness'))
        self.assertEqual(self.page.locator('#uq-count-total').inner_text(), '5')
        self.assertTrue(self.page.get_by_role('button', name='Đang dừng toàn hệ thống…', exact=True).is_visible())
        self.assert_isolated()
