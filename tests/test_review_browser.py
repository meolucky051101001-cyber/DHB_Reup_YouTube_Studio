"""Real DOM regressions. CI's browser job sets DHB_BROWSER_TESTS=1."""
import os
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class ProfileDomTests(unittest.TestCase):
    def test_profile_handlers_treat_names_as_data_after_rerender(self):
        from playwright.sync_api import sync_playwright
        source = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text(encoding='utf-8')
        names = ["&#39;);globalThis.reviewMarker=1;//", 'Việt & <tag> " \' \\ &#34;']
        actions = ['profileOpenBrowser', 'profileCheckLogin', 'profileRefreshChannels', 'profileEditProxy', 'profileDelete']
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', lambda route: route.abort())
            page.set_content('<table><tbody id="profiles-tbody"></tbody></table>')
            page.add_script_tag(content=source)
            page.evaluate("""names => {
                globalThis.reviewMarker = 0; globalThis.calls = [];
                profileOpenBrowser = name => calls.push(['profileOpenBrowser', name]);
                profileCheckLogin = name => calls.push(['profileCheckLogin', name]);
                profileRefreshChannels = name => calls.push(['profileRefreshChannels', name]);
                profileEditProxy = name => calls.push(['profileEditProxy', name]);
                profileDelete = name => calls.push(['profileDelete', name]);
                gProfiles = names.map(name => ({ name, source: 'Local' }));
                renderProfilesTable(); renderProfilesTable(); renderProfilesTable();
            }""", names)
            for index, name in enumerate(names):
                self.assertEqual(page.locator('tbody tr').nth(index).locator('td b').first.inner_text(), name)
                for action in actions:
                    page.locator(f'[data-profile-index="{index}"][data-profile-action="{action}"]').click()
            self.assertEqual(page.evaluate('reviewMarker'), 0)
            self.assertEqual(page.evaluate('calls'), [[action,name] for name in names for action in actions])
            self.assertEqual(page.locator('tbody [onclick]').count(), 0)
            browser.close()


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class PreparationBrowserTests(unittest.TestCase):
    def test_bulk_group_edit_and_excel_download_import_use_real_api(self):
        import io
        import re
        from contextlib import closing
        from urllib.parse import urlsplit
        from openpyxl import load_workbook
        from playwright.sync_api import sync_playwright
        import database
        from test_review_regressions import ReviewRegressions
        fixture = ReviewRegressions()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Browser group')")
            conn.execute("INSERT INTO channel_group_memberships(group_name,channel_id) VALUES('Browser group',901)")
        root = Path(__file__).resolve().parents[1]
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', (root/'static/index.html').read_text(encoding='utf-8'), flags=re.S)
        requests = []
        def dispatch(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != '127.0.0.1:8686':
                route.abort()
                return
            if parsed.path == '/':
                route.fulfill(status=200,content_type='text/html',body=html)
                return
            if not parsed.path.startswith(('/api/queue','/api/auth','/api/groups','/api/projects','/api/channels','/static/')):
                route.abort()
                return
            requests.append((request.method, parsed.path))
            response = fixture.client.request(request.method, parsed.path + ('?'+parsed.query if parsed.query else ''),
                                              headers=dict(request.headers), content=request.post_data_buffer)
            route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(accept_downloads=True)
            page.route('**/*',dispatch)
            dialogs=[]
            page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.accept()))
            page.goto('http://127.0.0.1:8686/')
            page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
            page.add_style_tag(content='.tab-pane,.subtab-pane {display:block!important}')
            page.evaluate("async () => { await loadProjects(); document.getElementById('sel-project').value='review'; await loadGroups(); await loadChannels(); await loadQueue(); gSelectedIds=new Set([901]); }")
            page.select_option('#bulk-group',label='Browser group')
            page.fill('#bulk-title','Tiêu đề từ trình duyệt')
            page.locator('[onclick="applyBulkConfig()"]').click(force=True)
            page.wait_for_function("gQueueItems.some(v=>v.title==='Tiêu đề từ trình duyệt')")
            self.assertEqual(fixture.row()['target_group_name'],'Browser group')
            with page.expect_download() as download:
                page.locator('[onclick="exportExcel()"]').first.click(force=True)
            raw=Path(download.value.path()).read_bytes()
            workbook=load_workbook(io.BytesIO(raw))
            fields={field:index for index,(field,_,_) in enumerate(fixture.app._EXCEL_COLUMN_DEFS,1)}
            workbook['Danh sách'].cell(2,fields['description'],'Mô tả nhập lại từ Excel')
            output=io.BytesIO()
            workbook.save(output)
            workbook.close()
            page.set_input_files('#file-import-excel',{'name':'edited.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':output.getvalue()})
            page.wait_for_function("gQueueItems.some(v=>v.description==='Mô tả nhập lại từ Excel')")
            self.assertEqual(fixture.row()['description'],'Mô tả nhập lại từ Excel')
            self.assertEqual(requests.count(('POST','/api/queue/import-excel')),2)
            self.assertFalse(any('Lỗi ' in message for message in dialogs),dialogs)
            browser.close()
