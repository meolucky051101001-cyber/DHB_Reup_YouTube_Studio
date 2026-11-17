"""Administrative UI regressions with a full page and isolated HTTP fixtures."""
import json
import os
import unittest

from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in browser CI')
class AdminBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.ui = UIPage(self.browser)
        self.addCleanup(self.ui.close)
        self.page = self.ui.page

    def assert_no_browser_errors(self):
        self.assertEqual(self.ui.errors, [])
        self.assertEqual(self.ui.external, [])

    def open_system(self, panel='settings'):
        self.page.locator('[data-tab="tab-settings"]').click()
        self.page.evaluate('key => StudioAdmin.showPanel("tab-settings", key)', panel)

    def test_subviews_keyboard_and_create_forms_preserve_fields_without_requests(self):
        page = self.page
        page.locator('[data-tab="tab-channels"]').click()
        self.assertEqual(page.locator('#tab-channels [role=tab]').count(), 4)
        self.assertFalse(page.locator('#admin-channel-editor').evaluate('node=>node.open'))
        page.get_by_role('tab', name='Kênh', exact=True).press('ArrowRight')
        self.assertTrue(page.locator('#tab-channels-profiles').is_visible())
        self.assertEqual(page.get_by_role('tab', name='Profile', exact=True).get_attribute('aria-selected'), 'true')
        page.get_by_role('tab', name='Profile', exact=True).press('End')
        self.assertTrue(page.locator('#tab-channels-capabilities').is_visible())
        self.assertTrue(page.locator('#tab-channels-capabilities details').evaluate('node=>node.open'))
        page.get_by_role('tab', name='Khả năng đăng', exact=True).press('Home')
        page.get_by_role('button', name='+ Thêm kênh', exact=True).click()
        page.locator('#ch-name').fill('Kênh đang soạn')
        page.select_option('#ch-mode', 'youtube_api')
        self.assertTrue(page.locator('#ch-oauth').is_visible())
        page.locator('#ch-oauth').fill('fixture-only-secret')
        page.select_option('#ch-mode', 'playwright')
        self.assertFalse(page.locator('#ch-oauth').is_visible())
        self.assertEqual(page.input_value('#ch-oauth'), 'fixture-only-secret')
        page.get_by_role('tab', name='Profile', exact=True).click()
        page.get_by_role('tab', name='Kênh', exact=True).click()
        self.assertEqual(page.input_value('#ch-name'), 'Kênh đang soạn')
        page.locator('[data-tab="tab-monitor"]').click()
        page.get_by_role('button', name='+ Tạo luồng', exact=True).click()
        page.locator('#mon-name').fill('Luồng đang soạn')
        self.open_system()
        self.assertEqual(page.locator('#tab-settings [role=tab]').count(), 4)
        page.locator('[data-tab="tab-monitor"]').click()
        self.assertEqual(page.input_value('#mon-name'), 'Luồng đang soạn')
        self.assertEqual(self.ui.requests, [])
        self.assert_no_browser_errors()

    def test_settings_fetch_cannot_replace_a_draft_entered_while_waiting(self):
        self.open_system()
        page = self.page
        page.evaluate("""() => {
          window.savedApiFetch = apiFetch;
          apiFetch = (url, options) => url === '/api/settings'
            ? new Promise(resolve => window.resolveSettings = resolve)
            : savedApiFetch(url, options);
          window.pendingSettings = loadAppSettings();
        }""")
        page.locator('#set-gemini-model').fill('draft-during-fetch')
        page.locator('#set-openai-key').fill('fixture-new-key')
        page.evaluate("resolveSettings({gemini_model:'stale-model',openai_api_key_configured:true})")
        page.evaluate('pendingSettings')
        self.assertEqual(page.input_value('#set-gemini-model'), 'draft-during-fetch')
        self.assertEqual(page.input_value('#set-openai-key'), 'fixture-new-key')
        self.assertTrue(page.evaluate('StudioAdmin.hasUnsavedSettings()'))
        self.assertIn('chưa lưu', page.locator('#admin-settings-status').inner_text())
        self.assert_no_browser_errors()

    def test_settings_blank_secret_is_keep_and_partial_save_retains_draft(self):
        self.open_system()
        page = self.page
        self.ui.responses['/api/settings'] = {'gemini_model': 'stored-model', 'openai_api_key_configured': True}
        self.ui.responses['/api/storage/overview'] = {}
        self.ui.responses['/api/diagnostics'] = {'version': 'fixture', 'database': {'available': True}}
        page.evaluate('loadAppSettings()')
        self.assertEqual(page.input_value('#set-openai-key'), '')
        self.assertIn('Đã cấu hình', page.locator('#set-openai-key-state').inner_text())
        page.locator('#set-gemini-model').fill('saved-model')
        page.evaluate('saveAppSettings()')
        self.assertFalse(any(request['path'] == '/api/settings/secrets' for request in self.ui.requests))
        writes = [request for request in self.ui.requests if request['path'] == '/api/settings' and request['method'] == 'POST']
        self.assertEqual(json.loads(writes[-1]['body'])['gemini_model'], 'saved-model')
        self.assertFalse(page.evaluate('StudioAdmin.hasUnsavedSettings()'))

        def reject_secret(route, parsed):
            if parsed.path == '/api/settings/secrets':
                route.fulfill(status=503, content_type='application/json', body='{"detail":"fixture secret failure"}')
                return True
            return False

        self.ui.handler = reject_secret
        page.locator('#set-gemini-model').fill('partial-model')
        page.locator('#set-openai-key').fill('fixture-new-key')
        page.evaluate('saveAppSettings()')
        self.assertEqual(page.input_value('#set-gemini-model'), 'partial-model')
        self.assertEqual(page.input_value('#set-openai-key'), 'fixture-new-key')
        self.assertTrue(page.locator('#admin-settings-save').is_enabled())
        self.assertTrue(page.evaluate('StudioAdmin.hasUnsavedSettings()'))
        self.assertIn('Một phần', page.locator('#admin-settings-status').inner_text())
        self.assertEqual(page.locator('#admin-settings-status').get_attribute('data-state'), 'error')
        self.assertEqual(page.evaluate('localStorage.length'), 0)
        self.assert_no_browser_errors()

    def test_logs_pause_stale_response_and_cancel_delete(self):
        self.open_system('activity')
        page = self.page
        self.ui.responses['/api/logs'] = [{'created_at': '2026-11-17', 'module': 'Test', 'status': 'INFO', 'message': 'known-log'}]
        page.evaluate('loadLogs()')
        page.locator('#admin-logs-pause').click()
        before = len(self.ui.requests)
        page.evaluate('loadLogs()')
        self.assertEqual(len(self.ui.requests), before)
        self.assertIn('tạm dừng', page.locator('#admin-log-status').inner_text())
        page.evaluate('loadLogs(true)')
        self.assertGreater(len(self.ui.requests), before)
        page.locator('#admin-logs-pause').click()
        page.wait_for_function('document.getElementById("admin-logs-pause").getAttribute("aria-pressed")==="false"')
        page.evaluate("""() => {
          window.pendingLogCalls = [];
          window.savedApiFetch = apiFetch;
          apiFetch = (url, options) => url.startsWith('/api/logs?')
            ? new Promise((resolve,reject) => pendingLogCalls.push({resolve,reject,url}))
            : savedApiFetch(url, options);
          document.getElementById('log-filter-q').value='first';
          window.firstLog=loadLogs();
          document.getElementById('log-filter-q').value='second';
          window.secondLog=loadLogs();
          pendingLogCalls[1].resolve([{message:'newest-result',module:'Test',status:'INFO'}]);
        }""")
        page.evaluate('secondLog')
        page.evaluate("pendingLogCalls[0].resolve([{message:'stale-result',module:'Test',status:'INFO'}])")
        page.evaluate('firstLog')
        self.assertIn('newest-result', page.locator('#logs-tbody').inner_text())
        self.assertNotIn('stale-result', page.locator('#logs-tbody').inner_text())
        page.evaluate("window.failedLog=loadLogs();pendingLogCalls[2].reject(new Error('offline fixture'))")
        page.evaluate('failedLog')
        self.assertIn('cũ', page.locator('#admin-log-status').inner_text())
        self.assertIn('newest-result', page.locator('#logs-tbody').inner_text())
        page.evaluate('() => { window.clearResult=clearLogs(); }')
        dialog = page.locator('#admin-clear-logs-dialog')
        dialog.wait_for(state='visible')
        self.assertIn('ngoài bộ lọc', dialog.inner_text())
        dialog.get_by_role('button', name='Giữ nhật ký', exact=True).click()
        page.evaluate('clearResult')
        self.assertFalse(any(request['method'] == 'DELETE' for request in self.ui.requests))
        self.assert_no_browser_errors()

    def test_monitor_lifecycle_and_stale_snapshot_are_distinct(self):
        page = self.page
        page.locator('[data-tab="tab-monitor"]').click()
        monitor = {'id': 1, 'name': 'Luồng mẫu', 'source_urls': 'https://example.test/source',
                   'target_channel_id': 1, 'target_group_name': 'wrong-group', 'check_interval_minutes': 30,
                   'status': 'Stopping', 'lifecycle_state': 'Stopping', 'is_running': 1, 'can_delete': False}
        self.ui.responses['/api/monitors'] = [monitor]
        page.evaluate('loadMonitors()')
        page.evaluate('clearTimeout(gMonitorLifecycleTimer);gMonitorLifecycleTimer=null')
        self.assertIn('Kênh mẫu', page.locator('#monitors-tbody').inner_text())
        self.assertNotIn('wrong-group', page.locator('#monitors-tbody').inner_text())
        self.assertTrue(page.locator('#monitors-tbody button').nth(0).is_disabled())
        self.assertTrue(page.locator('#monitors-tbody button').nth(1).is_disabled())

        def reject_monitor(route, parsed):
            if parsed.path == '/api/monitors':
                route.fulfill(status=503, content_type='application/json', body='{"detail":"offline fixture"}')
                return True
            return False

        self.ui.handler = reject_monitor
        page.evaluate('loadMonitors().catch(()=>{})')
        self.assertEqual(page.locator('#admin-monitor-summary').get_attribute('data-state'), 'error')
        self.assertIn('cũ', page.locator('#admin-monitor-summary').inner_text())
        self.assertIn('Stopping', page.locator('#monitors-tbody').inner_text())
        self.assertTrue(page.locator('#admin-monitor-retry').is_visible())
        self.ui.handler = None
        monitor.update(status='Stopped', lifecycle_state='Stopped', is_running=0, can_delete=True)
        page.locator('#admin-monitor-retry').click()
        page.wait_for_function('document.getElementById("admin-monitor-summary").dataset.state==="ready"')
        self.assertTrue(page.locator('#monitors-tbody button').nth(1).is_enabled())
        self.assertFalse(page.locator('#admin-monitor-retry').is_visible())
        self.assertFalse(any(request['method'] != 'GET' for request in self.ui.requests))
        self.assert_no_browser_errors()
