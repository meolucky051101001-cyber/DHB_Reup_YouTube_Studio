"""Full static UI regressions for content scope, AI results and render presets."""
import json
import os
import unittest
from html.parser import HTMLParser

from ui_browser_fixture import ROOT, UIPage


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class ContentUIBrowserTests(unittest.TestCase):
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

    def assert_isolated(self):
        self.assertEqual(self.fixture.external, [])
        self.assertEqual(self.fixture.errors, [])

    @staticmethod
    def item(item_id, title):
        return {'id': item_id, 'title': title, 'description': '', 'status': 'Pending',
                'project_name': 'fixture', 'platform': 'Local', 'upload_to': 'Video',
                'visibility': 'private', 'render_mode': 'none', 'target_channel_id': 1,
                'download_progress': 0, 'render_progress': 0, 'upload_progress': 0}

    def test_empty_scope_does_not_fall_back_to_project_and_hidden_selection_is_explicit(self):
        self.fixture.seed_queue([])
        self.page.locator('[data-tab="tab-ai"]').click()
        self.page.evaluate("runAIProcess('ai')")
        self.assertFalse(any(request['path'] == '/api/queue/ai-process' for request in self.fixture.requests))
        self.assertIn('0 video', self.page.locator('#tab-ai .content-scope-summary').inner_text())

        items = [self.item(71, 'Hiện'), self.item(72, 'Ẩn A'), self.item(73, 'Ẩn B')]
        self.fixture.seed_queue(items)
        self.page.evaluate("""() => {
            gSelectedIds = new Set([71,72,73]);
            filterQueueTable('Hiện');
            StudioContent.updateScope();
        }""")
        self.assertIn('Đã chọn 3 video', self.page.locator('#tab-ai .content-scope-summary').inner_text())
        self.assertIn('2 đang ẩn', self.page.locator('#tab-ai .content-scope-summary').inner_text())
        self.fixture.responses['/api/queue/ai-process'] = {'updated': 3, 'ok': True, 'failed': []}
        self.page.evaluate("runAIProcess('batch_edit')")
        request = next(request for request in self.fixture.requests if request['path'] == '/api/queue/ai-process')
        self.assertEqual(json.loads(request['body'])['ids'], [71, 72, 73])
        self.assert_isolated()

    def test_partial_ai_results_survive_navigation_and_later_failure(self):
        self.fixture.seed_queue([self.item(71, 'A'), self.item(72, 'B'), self.item(73, 'C')])
        self.fixture.responses['/api/queue/ai-process'] = {
            'updated': 2, 'ok': False,
            'failed': [{'id': 73, 'code': 'stale_snapshot', 'error': '<script>changed</script>'}],
            'provider_requested': 'gemini', 'provider_counts': {'gemini': 1, 'google_translate': 1},
            'fallback_count': 1, 'links_preserved': 2,
        }
        self.page.locator('[data-tab="tab-ai"]').click()
        self.page.evaluate("runAIProcess('ai')")
        result = self.page.locator('#content-ai-results')
        self.assertIn('2 đã cập nhật', result.inner_text())
        self.assertIn('1 không cập nhật', result.inner_text())
        self.assertIn('Google Translate: 1', result.inner_text())
        self.assertIn('thay thế nhà cung cấp', result.inner_text())
        self.assertEqual(result.locator('tbody tr').count(), 1)
        self.assertEqual(result.locator('script').count(), 0)
        self.assertIn('<script>changed</script>', result.inner_text())
        self.page.locator('[data-tab="tab-render"]').click()
        self.page.locator('[data-tab="tab-ai"]').click()
        self.assertIn('stale_snapshot', result.inner_text())

        def fail(route, parsed):
            if parsed.path == '/api/queue/ai-process':
                route.fulfill(status=409, content_type='application/json', body='{"detail":"State changed"}')
                return True
            return False
        self.fixture.handler = fail
        self.page.evaluate("runAIProcess('ai')")
        self.assertIn('State changed', result.locator('.content-inline-error').inner_text())
        self.assertIn('2 đã cập nhật', result.inner_text())
        self.assertFalse(self.page.locator('[onclick="runAIProcess(\'ai\')"]').is_disabled())
        self.assertEqual(self.page.locator('#tab-ai').get_attribute('aria-busy'), 'false')
        self.assert_isolated()

    def test_saved_edit_reports_refresh_failure_without_claiming_save_failed(self):
        self.fixture.seed_queue([self.item(71, 'Tiêu đề cũ')])
        self.fixture.responses['/api/queue/edit-metadata'] = {'ok': True}

        def fail_queue_refresh(route, parsed):
            if parsed.path == '/api/queue' and route.request.method == 'GET':
                route.fulfill(status=503, content_type='application/json',
                              body='{"detail":"Queue refresh unavailable"}')
                return True
            return False

        self.fixture.handler = fail_queue_refresh
        self.page.evaluate('openEditModal(71)')
        self.page.fill('#ed-vid-title', 'Tiêu đề đã lưu')
        self.page.evaluate('saveEditModal()')
        self.assertFalse(self.page.locator('#modal-edit-video').evaluate('node => node.open'))
        warning = self.page.locator('#toast-container .ui-toast-warning')
        self.assertTrue(warning.is_visible())
        self.assertIn('Đã lưu video', warning.inner_text())
        self.assertIn('chưa tải lại được hàng đợi', warning.inner_text())
        self.assertNotIn('Không lưu được video', self.page.locator('#modal-edit-video').text_content())
        requests = [request for request in self.fixture.requests
                    if request['path'] == '/api/queue/edit-metadata']
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0]['body'])['title'], 'Tiêu đề đã lưu')
        self.assert_isolated()

    def test_failed_logs_refresh_marks_header_stale_and_recovers(self):
        self.fixture.seed_queue([])
        self.fixture.responses['/api/overview'] = {
            'pipeline_running': False, 'batch_state': 'idle', 'dhb_connected': True,
            'channels_count': 1, 'total_videos': 0, 'completed_videos': 0,
            'monitors_running': 0,
        }
        self.fixture.responses['/api/monitors'] = []
        self.fixture.responses['/api/logs'] = []

        def fail_logs(route, parsed):
            if parsed.path == '/api/logs':
                route.fulfill(status=503, content_type='application/json',
                              body='{"detail":"Logs unavailable"}')
                return True
            return False

        self.fixture.handler = fail_logs
        self.page.evaluate('refreshLivePanels()')
        self.assertIn('1 phần chưa cập nhật được', self.page.locator('#studio-update-status').inner_text())
        self.assertIn('Logs unavailable', self.page.locator('#admin-log-status').text_content())
        self.fixture.handler = None
        self.page.evaluate('refreshLivePanels()')
        self.assertIn('Cập nhật lúc', self.page.locator('#studio-update-status').inner_text())
        self.assertNotIn('chưa cập nhật được', self.page.locator('#studio-update-status').inner_text())
        self.assertEqual(self.page.locator('#admin-log-status').get_attribute('data-state'), 'ready')
        self.assert_isolated()

    def test_render_reorganization_preserves_fields_unknown_preset_data_and_dirty_state(self):
        class FieldIds(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                field_id = attrs.get('id', '')
                if tag in ('input', 'select', 'textarea') and field_id.startswith(('rc-', 'rp-')):
                    self.ids.append(field_id)

        parser = FieldIds()
        parser.feed((ROOT / 'static/index.html').read_text(encoding='utf-8'))
        for field_id in parser.ids:
            self.assertEqual(self.page.locator(f'#{field_id}').count(), 1, field_id)

        preset = {'id': 91, 'name': 'Mẫu kiểm thử', 'note': 'Ghi chú', 'revision': 'r1',
                  'render_mode': 'shorts_9_16', 'valid': True,
                  'config': {'speed': 1.2, 'framerate': 60, 'shorts_visual_pop': False,
                             'bg_music_name': 'missing-track.mp3', 'future_setting': {'kept': True}}}
        self.fixture.responses['/api/render-presets'] = [preset]
        self.page.evaluate('loadRenderPresets()')
        self.page.locator('[data-tab="tab-render"]').click()
        self.page.locator('[onclick="loadPresetToForm()"]').click()
        self.assertFalse(self.page.is_checked('#rc-shorts-visual'))
        self.assertEqual(self.page.input_value('#rc-bg-music'), 'missing-track.mp3')
        self.assertNotIn('is-dirty', self.page.locator('#content-render-dirty').get_attribute('class'))
        self.page.fill('#rc-speed', '1.5')
        self.assertIn('is-dirty', self.page.locator('#content-render-dirty').get_attribute('class'))
        self.assertIn('1.5', self.page.locator('#content-render-summary').inner_text())

        def save(route, parsed):
            if parsed.path == '/api/render-presets/91' and route.request.method == 'PUT':
                payload = route.request.post_data_json
                latest = {**preset, **payload, 'revision': 'r2'}
                self.fixture.responses['/api/render-presets'] = [latest]
                route.fulfill(content_type='application/json', body=json.dumps(latest))
                return True
            return False
        self.fixture.handler = save
        self.page.evaluate('saveRenderPreset(true)')
        request = next(request for request in self.fixture.requests if request['path'] == '/api/render-presets/91')
        payload = json.loads(request['body'])
        self.assertEqual(payload['expected_revision'], 'r1')
        self.assertEqual(payload['config']['framerate'], 60)
        self.assertEqual(payload['config']['future_setting'], {'kept': True})
        self.assertEqual(payload['config']['bg_music_name'], 'missing-track.mp3')
        self.assertIs(payload['config']['shorts_visual_pop'], False)
        self.assertEqual(payload['config']['speed'], 1.5)
        self.assertNotIn('is-dirty', self.page.locator('#content-render-dirty').get_attribute('class'))

        self.page.locator('#content-render-0').focus()
        self.page.keyboard.press('ArrowRight')
        self.assertEqual(self.page.locator('#content-render-1').get_attribute('aria-selected'), 'true')
        self.assertTrue(self.page.locator('#rc-crop-size').is_visible())
        self.assertTrue(self.page.locator('#content-render-1').evaluate('(el) => el === document.activeElement'))
        self.assert_isolated()

    def test_resources_filter_without_external_requests_and_mobile_content_does_not_overflow(self):
        self.fixture.responses['/api/render-resources'] = {'resources': {
            'background_music': [{'name': 'Music sample.mp3'}],
            'background_video': [{'name': 'Video sample.mp4'}], 'fonts': [{'name': 'Segoe.ttf'}],
        }}
        self.page.evaluate('loadRenderResources()')
        self.page.locator('[data-tab="tab-render"]').click()
        self.page.locator('#content-resource-search').fill('Music')
        self.assertEqual(self.page.locator('#dhb-resources-summary .badge:visible').count(), 1)
        self.page.locator('#content-resource-type').select_option('1')
        self.assertEqual(self.page.locator('#dhb-resources-summary .badge:visible').count(), 0)
        self.assertIn('Không có tài nguyên', self.page.locator('#content-resource-status').inner_text())
        def fail_resources(route, parsed):
            if parsed.path == '/api/render-resources':
                route.fulfill(status=503, content_type='application/json', body='{"detail":"Unavailable"}')
                return True
            return False
        self.fixture.handler = fail_resources
        self.page.get_by_role('button', name='Tải lại tài nguyên', exact=True).click()
        self.page.wait_for_function("document.getElementById('content-resource-status').textContent.includes('Không tải được')")
        self.assertEqual(self.page.locator('#dhb-resources-summary .badge').count(), 3)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        for tab in ('tab-ai', 'tab-render'):
            self.page.evaluate("tab => switchTab(tab,document.querySelector(`[data-tab=\"${tab}\"]`))", tab)
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"), tab)
        self.assertFalse(any(request['method'] != 'GET' for request in self.fixture.requests))
        self.assert_isolated()
