"""Responsive navigation, native dialog and polling contracts for the full UI."""
import os
import unittest
from ui_browser_fixture import UIPage


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in browser CI')
class ShellBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.ui = UIPage(self.browser)
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)
        self.addCleanup(self.ui.close)

    def test_all_six_views_reflow_and_keep_forms_without_network_side_effects(self):
        page = self.ui.page
        page.evaluate("document.getElementById('ai-prompt').value='Bản nháp đang soạn'")
        for width, height in [(1920,1080),(1440,900),(1366,768),(1024,768),(768,1024),(390,844)]:
            page.set_viewport_size({'width':width,'height':height})
            for tab in ('workspace','ai','render','channels','monitor','settings'):
                if width < 1024:
                    page.locator('#navigation-toggle').click()
                link = page.locator(f'[data-tab="tab-{tab}"]')
                link.click()
                self.assertTrue(page.locator('#tab-'+tab).is_visible())
                self.assertEqual(link.get_attribute('aria-current'), 'page')
                self.assertEqual(page.locator('[aria-current=page]').count(), 1)
                size = page.evaluate('({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})')
                self.assertLessEqual(size['scroll'],size['client']+1,(width,tab,size))
        self.assertEqual(page.input_value('#ai-prompt'),'Bản nháp đang soạn')
        self.assertEqual(self.ui.requests, [])
        self.assertEqual(self.ui.errors, [])
        self.assertEqual(self.ui.external, [])

    def test_edit_drawer_keeps_draft_blocks_busy_close_and_restores_focus(self):
        page=self.ui.page
        self.ui.seed_queue([{'id':1,'title':'Video mẫu','status':'Pending','visibility':'private','render_mode':'none'}])
        page.locator('#queue-search').focus()
        page.evaluate('openEditModal(1)')
        self.assertTrue(page.locator('#modal-edit-video').evaluate('node=>node.open'))
        page.locator('#ed-vid-title').fill('Tiêu đề đang gõ')
        page.evaluate('async()=>{await loadQueue(true);await loadQueue(true)}')
        self.assertEqual(page.input_value('#ed-vid-title'),'Tiêu đề đang gõ')
        page.evaluate("StudioUI.setBusy('modal-edit-video',true)")
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#modal-edit-video').evaluate('node=>node.open'))
        page.evaluate("StudioUI.setBusy('modal-edit-video',false)")
        page.once('dialog',lambda dialog:dialog.dismiss())
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#modal-edit-video').evaluate('node=>node.open'))
        page.once('dialog',lambda dialog:dialog.accept())
        page.keyboard.press('Escape')
        page.wait_for_function("!document.getElementById('modal-edit-video').open")
        page.wait_for_function("document.activeElement.id==='queue-search'")
        self.assertFalse(any(r['method']!='GET' for r in self.ui.requests))
        self.assertEqual(self.ui.errors,[])

    def test_import_confirmation_cancel_never_applies(self):
        page=self.ui.page
        self.ui.responses['/api/queue/import-csv']={'valid_count':2,'duplicate_count':1,'error_count':1,'rows':[{'line':2,'state':'error','reasons':['Thiếu nguồn']} ]}
        page.locator('#file-import-csv').set_input_files({'name':'test.csv','mimeType':'text/csv','buffer':b'title,source_url\nA,https://example.test/a'})
        dialog=page.locator('.ui-message-dialog')
        dialog.wait_for(state='visible')
        self.assertIn('không giới hạn',dialog.inner_text())
        dialog.get_by_role('button',name='Hủy',exact=True).click()
        import json
        requests=[r for r in self.ui.requests if r['path']=='/api/queue/import-csv']
        self.assertEqual(len(requests),1)
        self.assertTrue(json.loads(requests[0]['body'])['preview'])
        self.assertEqual(self.ui.errors,[])

    def test_native_dialog_contains_keyboard_focus_and_bulk_busy_cannot_close(self):
        page=self.ui.page
        self.ui.seed_queue([{'id':1,'title':'Video mẫu','status':'Pending','visibility':'private','render_mode':'none'}])
        page.evaluate('openBulkDescriptionModal();setBulkDescriptionBusy(true)')
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#modal-bulk-description').evaluate('node=>node.open'))
        page.evaluate('setBulkDescriptionBusy(false)')
        for _ in range(12):
            page.keyboard.press('Tab')
            self.assertTrue(page.evaluate("document.activeElement===document.body || document.getElementById('modal-bulk-description').contains(document.activeElement)"))
        page.evaluate('closeBulkDescriptionModal(true)')
        self.assertFalse(page.locator('#modal-bulk-description').evaluate('node=>node.open'))
        self.assertEqual(self.ui.errors,[])

    def test_closed_mobile_navigation_is_not_in_keyboard_path(self):
        page = self.ui.page
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#navigation-toggle').focus()
        for _ in range(18):
            page.keyboard.press('Tab')
            self.assertFalse(page.evaluate("document.querySelector('.app-sidebar').contains(document.activeElement)"))
        page.locator('#navigation-toggle').click()
        page.locator('[data-tab="tab-ai"]').wait_for(state='visible')
        page.keyboard.press('Escape')
        page.locator('[data-tab="tab-ai"]').wait_for(state='hidden')
        self.assertTrue(page.locator('#navigation-toggle').evaluate('node=>node===document.activeElement'))

    def test_schedule_duplicate_submit_is_locked_and_failure_keeps_draft(self):
        page = self.ui.page
        self.ui.seed_queue([{'id':1,'title':'Video mẫu','status':'Pending','visibility':'private','render_mode':'none'}])
        page.evaluate("""() => {
            promptAutoSchedule();
            window.scheduleRequests = 0;
            window.originalApiFetch = apiFetch;
            apiFetch = (url, options) => {
                if (url !== '/api/queue/auto-schedule') return originalApiFetch(url, options);
                scheduleRequests++;
                return new Promise((resolve, reject) => window.rejectSchedule = reject);
            };
            applyAutoSchedule(); applyAutoSchedule();
        }""")
        self.assertEqual(page.evaluate('scheduleRequests'),1)
        page.keyboard.press('Escape')
        self.assertTrue(page.locator('#modal-auto-schedule').evaluate('node=>node.open'))
        page.evaluate("rejectSchedule(new Error('Dữ liệu đã thay đổi'))")
        page.locator('#modal-auto-schedule .ui-form-error').wait_for(state='visible')
        self.assertTrue(page.locator('#modal-auto-schedule').evaluate('node=>node.open'))
        self.assertEqual(page.locator('#modal-auto-schedule').get_attribute('aria-busy'),'false')
        self.assertFalse(page.locator('#as-interval').is_disabled())
        self.assertEqual(self.ui.errors,[])
