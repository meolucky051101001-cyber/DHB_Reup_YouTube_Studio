"""Real DOM regressions. CI's browser job sets DHB_BROWSER_TESTS=1."""
import os
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class ConfigBrowserTests(unittest.TestCase):
    def test_configuration_forms_keep_values_and_validate_monitor_limits(self):
        import re
        from contextlib import closing
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        import database
        from test_review_regressions import ReviewRegressions
        fixture = ReviewRegressions()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(__file__).resolve().parents[1]
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', (root/'static/index.html').read_text(encoding='utf-8'), flags=re.S)
        def dispatch(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != '127.0.0.1:8686':
                route.abort()
                return
            if parsed.path == '/':
                route.fulfill(status=200, content_type='text/html', body=html)
                return
            if not parsed.path.startswith(('/api/auth','/api/channels','/api/groups','/api/options','/api/profiles','/api/overview','/api/monitors','/static/')):
                route.abort()
                return
            response = fixture.client.request(request.method, parsed.path + ('?'+parsed.query if parsed.query else ''),
                                              headers=dict(request.headers), content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', dispatch)
            dialogs = []
            page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
            page.goto('http://127.0.0.1:8686/')
            page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
            page.add_style_tag(content='.tab-pane,.subtab-pane {display:block!important}')
            page.evaluate('async () => { await loadGroups(); await loadChannels(); await loadOptions(); }')
            page.fill('#ch-name','Kênh Việt G10')
            page.locator('[onclick="addChannel()"]').click(force=True)
            page.wait_for_function("gChannels.some(c => c.name === 'Kênh Việt G10')")
            channel_id = page.evaluate("gChannels.find(c => c.name === 'Kênh Việt G10').id")
            page.evaluate('id => openChannelEdit(id)', channel_id)
            page.select_option('#ec-privacy','private')
            page.select_option('#ec-made-for-kids','0')
            page.select_option('#ec-altered-content','1')
            page.locator('[onclick="saveChannelEdit()"]').click()
            page.wait_for_function("gChannels.some(c => c.name === 'Kênh Việt G10' && c.altered_content === 1)")
            with closing(database.get_conn()) as conn:
                row = conn.execute('SELECT default_privacy,made_for_kids,altered_content FROM youtube_channels WHERE id=?',(channel_id,)).fetchone()
            self.assertEqual(tuple(row),('private',0,1))
            page.fill('#opt-name','Tùy chọn Việt G10')
            page.fill('#opt-hashtags','')
            page.fill('#opt-affiliate-cta','')
            page.check('#opt-ai-label')
            page.locator('[onclick="saveOptionGroup()"]').click(force=True)
            page.wait_for_function("gOptions.some(o => o.name === 'Tùy chọn Việt G10')")
            option_id = page.evaluate("gOptions.find(o => o.name === 'Tùy chọn Việt G10').id")
            page.evaluate('id => editOptionGroup(id)', option_id)
            self.assertEqual(page.input_value('#opt-hashtags'),'')
            self.assertEqual(page.input_value('#opt-affiliate-cta'),'')
            page.uncheck('#opt-ai-label')
            page.locator('[onclick="saveOptionGroup()"]').click(force=True)
            page.wait_for_function("gOptions.some(o => o.name === 'Tùy chọn Việt G10' && o.ai_label === 0)")
            with closing(database.get_conn()) as conn:
                row = conn.execute('SELECT append_hashtags,affiliate_CTA,made_for_kids,ai_label FROM upload_option_groups WHERE id=?',(option_id,)).fetchone()
            self.assertEqual(tuple(row),('','',0,0))
            page.fill('#mon-name','Monitor Việt G11')
            page.fill('#mon-urls','https://www.youtube.com/@fixture')
            page.select_option('#mon-channel',str(channel_id))
            page.fill('#mon-interval','5.5')
            page.locator('[onclick="addMonitor()"]').click(force=True)
            self.assertFalse(page.locator('#mon-interval').evaluate('input => input.validity.valid'))
            with closing(database.get_conn()) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM monitors').fetchone()[0],0)
            page.fill('#mon-interval','5')
            page.fill('#mon-max','1000')
            page.locator('[onclick="addMonitor()"]').click(force=True)
            page.wait_for_function("document.getElementById('mon-name').value === ''")
            with closing(database.get_conn()) as conn:
                row = conn.execute("SELECT check_interval_minutes,max_videos_per_scan,is_running,metadata_revision FROM monitors WHERE name='Monitor Việt G11'").fetchone()
            self.assertEqual(tuple(row),(5,1000,0,''))
            self.assertEqual(dialogs,[])
            browser.close()


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
            import json
            warning='<img src=x onerror="globalThis.warningExecuted=1"> thumbnail failed'
            with closing(database.get_conn()) as conn, conn:
                conn.execute("UPDATE video_queue SET status='Completed',post_upload_warnings_json=? WHERE id=901",
                             (json.dumps([{'step':'thumbnail','code':'HTTP_403','message':warning}]),))
            page.evaluate('async () => { globalThis.warningExecuted=0; await loadQueue(true); }')
            self.assertIn('cảnh báo bước phụ',page.locator('#queue-tbody').inner_text())
            page.locator('#queue-tbody details summary').click()
            self.assertIn(warning,page.locator('#queue-tbody').inner_text())
            self.assertEqual(page.evaluate('warningExecuted'),0)
            fixture.reserve_review('Completed')
            page.evaluate('loadQueue(true)')
            self.assertIn('Cần đối soát',page.locator('#queue-tbody').inner_text())
            browser.close()
