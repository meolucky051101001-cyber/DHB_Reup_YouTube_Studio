"""Publishing workflows against real UI assets with isolated provider boundaries."""
import json
import os
import unittest

from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class PublishWorkflowBrowserTests(unittest.TestCase):
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
        self.fixture.responses['/api/pipeline/start'] = {'ok': True}
        self.seed_ready()

    def seed_ready(self):
        self.fixture.seed_queue([
            {'id': 1, 'title': 'Alpha', 'status': 'Rendered', 'target_channel_id': 1, 'visibility': 'private'},
            {'id': 2, 'title': 'Beta', 'status': 'Rendered', 'target_channel_id': 1, 'visibility': 'private'},
            {'id': 3, 'title': 'Gamma', 'status': 'Pending', 'target_channel_id': 1, 'visibility': 'unlisted'},
        ])

    def requests(self, path):
        return [row for row in self.fixture.requests if row['path'] == path]

    def mutations(self):
        return [row for row in self.fixture.requests if row['method'] not in ('GET', 'HEAD')]

    def assert_isolated(self):
        self.assertEqual(self.fixture.errors, [])
        self.assertEqual(self.fixture.external, [])

    def select(self, *ids):
        self.page.evaluate('ids => {gSelectedIds = new Set(ids); updateQueueSelectionSummary();}', list(ids))

    def open_workflow(self):
        self.page.locator('#publish-workflow-open').click()
        self.page.locator('#publish-workflow-dialog').wait_for(state='visible')

    def prepare_preview(self, *, blocked=False):
        def handler(route, parsed):
            if parsed.path != '/api/queue/preview-distribution':
                return False
            payload = json.loads(route.request.post_data)
            items = [
                {'id': item_id, 'title': f'Video {item_id}', 'visibility': 'private',
                 'channel_name': 'Kênh mẫu', 'profile_name': 'Profile mẫu',
                 'youtube_channel_id': 'UCaaaaaaaaaaaaaaaaaaaaaa', 'upload_mode': 'playwright',
                 'file_name': f'video-{item_id}.mp4', 'file_kind': 'File có sẵn',
                 'blocked': blocked,
                 'reasons': ['<img src=x onerror=window.BAD=true> Không tìm thấy file'] if blocked else [],
                 'warnings': []}
                for item_id in payload['ids']
            ]
            route.fulfill(json={'items': items, 'blocked': len(items) if blocked else 0,
                                'preview_id': '' if blocked else 'approved-preview-token'})
            return True
        self.fixture.handler = handler

    def test_open_cancel_and_escape_are_readonly_and_restore_focus(self):
        self.select(1)
        opener = self.page.locator('#publish-workflow-open')
        opener.focus()
        self.open_workflow()
        self.assertTrue(self.page.get_by_role('heading', name='Quy trình đăng tự động', exact=True).is_visible())
        self.assertTrue(self.page.evaluate('document.activeElement.closest("#publish-workflow-dialog") !== null'))
        self.page.keyboard.press('Escape')
        self.page.locator('#publish-workflow-dialog').wait_for(state='hidden')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'publish-workflow-open')
        self.assertEqual(self.mutations(), [])
        self.assert_isolated()

    def test_hidden_selection_and_later_selection_change_keep_frozen_preview_ids(self):
        self.select(1, 2)
        self.page.fill('#queue-search', 'Alpha')
        self.prepare_preview()
        self.open_workflow()
        self.assertIn('2', self.page.inner_text('#publish-scope-summary'))
        self.assertIn('1', self.page.inner_text('#publish-scope-summary'))
        self.select(3)
        self.page.locator('#publish-continue').click()
        self.page.locator('.upload-preview-dialog').wait_for(state='visible')
        payload = json.loads(self.requests('/api/queue/preview-distribution')[-1]['body'])
        self.assertEqual(payload['ids'], [1, 2])
        self.assertEqual((payload['do_download'], payload['do_render'], payload['do_upload']), (False, False, True))
        self.page.locator('.upload-preview-dialog').get_by_role('button', name='Đóng', exact=True).click()
        self.page.locator('.upload-preview-dialog').wait_for(state='detached')
        self.assertEqual(self.requests('/api/pipeline/start'), [])
        self.assert_isolated()

    def test_empty_review_claimed_and_completed_scopes_do_not_reach_preview(self):
        cases = [
            [],
            [{'id': 1, 'title': 'Review', 'status': 'NeedsReview', 'requires_review': 1}],
            [{'id': 1, 'title': 'Claimed', 'status': 'Rendered', 'is_claimed': 1}],
            [{'id': 1, 'title': 'Sent', 'status': 'Completed', 'published_url': 'https://youtu.be/dQw4w9WgXcQ'}],
            [{'id': 1, 'title': 'Busy', 'status': 'Uploading'}],
        ]
        for items in cases:
            with self.subTest(items=items):
                self.fixture.seed_queue(items)
                self.open_workflow()
                self.assertTrue(self.page.locator('#publish-continue').is_disabled())
                self.page.keyboard.press('Escape')
                self.page.locator('#publish-workflow-dialog').wait_for(state='hidden')
        self.assertEqual(self.requests('/api/queue/preview-distribution'), [])
        self.assertEqual(self.mutations(), [])
        self.assert_isolated()

    def test_project_change_and_new_claim_block_a_previously_ready_scope(self):
        self.select(1)
        self.open_workflow()
        self.assertFalse(self.page.locator('#publish-continue').is_disabled())
        self.page.evaluate("document.getElementById('sel-project').add(new Option('Other', 'other')); document.getElementById('sel-project').value='other'; StudioQueue.updateScope()")
        self.page.wait_for_function("document.getElementById('publish-continue').disabled")
        self.page.keyboard.press('Escape')
        self.page.evaluate("document.getElementById('sel-project').value='fixture'; StudioQueue.updateScope()")
        self.open_workflow()
        self.page.evaluate('gQueueItems[0].is_claimed=1; StudioQueue.render(gQueueItems)')
        self.page.wait_for_function("document.getElementById('publish-continue').disabled")
        self.assertEqual(self.requests('/api/queue/preview-distribution'), [])
        self.page.keyboard.press('Escape')
        self.assertEqual(self.mutations(), [])
        self.assert_isolated()

    def test_explicit_scope_refresh_and_full_mode_retain_approval_contract(self):
        self.select(1)
        self.prepare_preview()
        self.open_workflow()
        self.select(3)
        self.page.locator('#publish-refresh-scope').click()
        self.page.locator('#publish-mode-full').check()
        self.page.locator('#publish-continue').click()
        dialog = self.page.locator('.upload-preview-dialog')
        dialog.wait_for(state='visible')
        self.assertEqual(self.requests('/api/pipeline/start'), [])
        preview = json.loads(self.requests('/api/queue/preview-distribution')[-1]['body'])
        self.assertEqual(preview, {'ids': [3], 'do_download': True, 'do_render': True, 'do_upload': True})
        with self.page.expect_response('**/api/pipeline/start') as response_info:
            dialog.get_by_role('button', name='Xác nhận và chạy', exact=True).click()
        self.assertEqual(response_info.value.status, 200)
        starts = self.requests('/api/pipeline/start')
        self.assertEqual(len(starts), 1)
        self.assertEqual(json.loads(starts[0]['body']), {
            'ids': [3], 'do_download': True, 'do_render': True, 'do_upload': True,
            'force_reupload': False, 'preview_id': 'approved-preview-token',
        })
        self.assert_isolated()

    def test_duplicate_pipeline_clicks_wait_for_one_preview_and_cancel_unlocks(self):
        self.select(1)
        self.prepare_preview()
        self.page.evaluate("""() => {
            window.publishOriginalFetch = apiFetch;
            window.publishPendingPreviews = 0;
            apiFetch = (url, options) => {
                if (url !== '/api/queue/preview-distribution') return publishOriginalFetch(url, options);
                publishPendingPreviews++;
                return new Promise(resolve => {
                    window.publishReleasePreview = () => resolve(publishOriginalFetch(url, options));
                });
            };
            void startPipeline(false, false, true);
            void startPipeline(false, false, true);
        }""")
        self.assertEqual(self.page.evaluate('publishPendingPreviews'), 1)
        self.assertEqual(self.requests('/api/pipeline/start'), [])
        self.page.evaluate('publishReleasePreview()')
        self.page.locator('.upload-preview-dialog').wait_for(state='visible')
        self.page.locator('.upload-preview-dialog').get_by_role('button', name='Đóng', exact=True).click()
        self.page.wait_for_function('!gPipelineStartPending')
        self.assertEqual(len(self.requests('/api/queue/preview-distribution')), 1)
        self.assertEqual(self.requests('/api/pipeline/start'), [])
        self.assert_isolated()

    def test_server_blocked_preview_has_no_run_action_and_renders_reason_as_text(self):
        self.select(1)
        self.prepare_preview(blocked=True)
        self.open_workflow()
        self.page.locator('#publish-continue').click()
        dialog = self.page.locator('.upload-preview-dialog')
        dialog.wait_for(state='visible')
        self.assertIn('Không tìm thấy file', dialog.inner_text())
        self.assertEqual(dialog.locator('img,script').count(), 0)
        self.assertEqual(dialog.get_by_role('button', name='Xác nhận và chạy', exact=True).count(), 0)
        dialog.get_by_role('button', name='Đóng', exact=True).click()
        self.assertEqual(self.requests('/api/pipeline/start'), [])
        self.assertFalse(self.page.evaluate('Boolean(window.BAD)'))
        self.assert_isolated()

    def test_mobile_monitor_guidance_navigation_does_not_enable_a_source(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open_workflow()
        self.page.locator('#publish-flow-monitor').check()
        dialog = self.page.locator('#publish-workflow-dialog')
        self.assertTrue(self.page.locator('#publish-go-monitor').is_visible())
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertLessEqual(dialog.evaluate('el => el.getBoundingClientRect().right'), 391)
        self.page.locator('#publish-go-monitor').click()
        self.page.locator('#tab-monitor').wait_for(state='visible')
        self.assertEqual(self.mutations(), [])
        self.assert_isolated()

    def prepare_monitor(self, *, running=False, blocked=False):
        monitor = {'id': 7, 'name': 'Nguồn mẫu', 'source_urls': 'https://example.test/source',
                   'target_channel_id': 1, 'target_group_name': 'Mặc định', 'check_interval_minutes': 30,
                   'max_videos_per_scan': 3, 'render_mode': 'none', 'ai_Action': 'none',
                   'is_running': running, 'lifecycle_state': 'Running' if running else 'Stopped',
                   'status': 'Running' if running else 'Stopped', 'can_delete': not running}
        preview = {
            'policy': {'version': 1, 'visibility': 'private', 'made_for_kids': False, 'altered_content': False},
            'revision': 'a' * 64, 'blocked': blocked,
            'channels': [{'channel_id': 1, 'channel_name': 'Kênh mẫu',
                          'visibility': 'private', 'made_for_kids': 0, 'altered_content': 0,
                          'sources': {'visibility': 'monitor', 'made_for_kids': 'monitor', 'altered_content': 'monitor'},
                          'blocked': blocked, 'reasons': ['Danh mục chưa được hỗ trợ'] if blocked else [],
                          'reason_codes': ['STUDIO_CATEGORY_DISABLED'] if blocked else []}],
        }
        self.fixture.responses.update({
            '/api/monitors': [monitor], '/api/monitors/7/metadata-preview': preview,
            '/api/monitors/7/metadata-confirm': {'ok': True},
            '/api/monitors/7/toggle': {'ok': True, 'message': 'Đã nhận yêu cầu'},
        })
        return preview

    def open_monitor_review(self, *, twice=False):
        self.page.evaluate('void toggleMonitor(7); void toggleMonitor(7)' if twice else 'void toggleMonitor(7)')
        self.page.locator('.monitor-start-dialog').wait_for(state='visible')

    def test_monitor_cancel_does_not_confirm_or_toggle(self):
        self.prepare_monitor()
        self.open_monitor_review()
        dialog = self.page.locator('.monitor-start-dialog')
        self.assertTrue(dialog.get_by_role('heading', name='Xem trước nguồn tự động', exact=True).is_visible())
        self.assertIn('Riêng tư', dialog.inner_text())
        dialog.get_by_role('button', name='Đóng', exact=True).click()
        self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(self.requests('/api/monitors/7/metadata-confirm'), [])
        self.assertEqual(self.requests('/api/monitors/7/toggle'), [])
        self.assert_isolated()

    def test_monitor_cancel_returns_focus_after_list_poll_replaces_the_opener(self):
        self.prepare_monitor()
        self.page.locator('[data-tab="tab-monitor"]').click()
        self.page.evaluate('loadMonitors()')
        self.page.locator('[data-monitor-toggle="7"]').click()
        dialog = self.page.locator('.monitor-start-dialog')
        dialog.wait_for(state='visible')
        self.page.evaluate('loadMonitors()')
        self.assertTrue(self.page.evaluate('Boolean(document.activeElement.closest(".monitor-start-dialog"))'))
        dialog.get_by_role('button', name='Đóng', exact=True).click()
        self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(self.page.evaluate('document.activeElement.dataset.monitorToggle'), '7')
        self.assertEqual(self.requests('/api/monitors/7/metadata-confirm'), [])
        self.assertEqual(self.requests('/api/monitors/7/toggle'), [])
        self.assert_isolated()

    def test_monitor_double_invocation_confirms_canonical_policy_only_once(self):
        preview = self.prepare_monitor()
        self.open_monitor_review(twice=True)
        self.assertEqual(len(self.requests('/api/monitors/7/metadata-preview')), 1)
        self.assertEqual(self.requests('/api/monitors/7/metadata-confirm'), [])
        self.page.locator('.monitor-start-dialog').get_by_role('button', name='Xác nhận và bật nguồn', exact=True).click()
        self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(len(self.requests('/api/monitors/7/toggle')), 1)
        confirmations = self.requests('/api/monitors/7/metadata-confirm')
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(json.loads(confirmations[0]['body']), {'policy': preview['policy'], 'revision': preview['revision']})
        self.assert_isolated()

    def test_monitor_blocked_or_missing_revision_never_enables_source(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                preview = self.prepare_monitor(blocked=not malformed)
                if malformed:
                    preview.pop('revision')
                self.page.evaluate('void toggleMonitor(7)')
                dialog = self.page.locator('.monitor-start-dialog')
                dialog.wait_for(state='visible')
                if not malformed:
                    self.assertIn('Danh mục chưa được hỗ trợ', dialog.inner_text())
                else:
                    self.assertIn('Chưa đủ dữ liệu xác nhận', dialog.inner_text())
                button = dialog.get_by_role('button', name='Xác nhận và bật nguồn', exact=True)
                self.assertTrue(button.count() == 0 or button.is_disabled())
                dialog.get_by_role('button', name='Đóng', exact=True).click()
                self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(self.requests('/api/monitors/7/metadata-confirm'), [])
        self.assertEqual(self.requests('/api/monitors/7/toggle'), [])
        self.assert_isolated()

    def test_monitor_stale_policy_failure_does_not_toggle(self):
        self.prepare_monitor()

        def fail_confirmation(route, parsed):
            if parsed.path == '/api/monitors/7/metadata-confirm':
                route.fulfill(status=409, json={'detail': 'Cấu hình đã đổi. Xem trước lại.'})
                return True
            return False
        self.fixture.handler = fail_confirmation
        self.open_monitor_review()
        self.page.locator('.monitor-start-dialog').get_by_role('button', name='Xác nhận và bật nguồn', exact=True).click()
        self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(len(self.requests('/api/monitors/7/metadata-confirm')), 1)
        self.assertEqual(self.requests('/api/monitors/7/toggle'), [])
        self.assert_isolated()

    def test_stopping_running_monitor_does_not_issue_a_new_metadata_approval(self):
        self.prepare_monitor(running=True)
        self.page.evaluate('void toggleMonitor(7)')
        self.page.wait_for_function('gMonitorTogglePending.size === 0')
        self.assertEqual(len(self.requests('/api/monitors/7/toggle')), 1)
        self.assertEqual(self.requests('/api/monitors/7/metadata-preview'), [])
        self.assertEqual(self.requests('/api/monitors/7/metadata-confirm'), [])
        self.assertEqual(self.page.locator('.monitor-start-dialog').count(), 0)
        self.assert_isolated()
