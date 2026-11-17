"""Real DOM regressions. CI's browser job sets DHB_BROWSER_TESTS=1."""
import os
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('DHB_BROWSER_TESTS') == '1', 'Run in the browser CI job')
class ConfigBrowserTests(unittest.TestCase):
    def test_studio_spoken_language_synthetic_contract_and_aggregate_evidence(self):
        from playwright.sync_api import sync_playwright
        from studio_metadata_contract import StudioMetadataError, verify_saved_metadata
        from test_studio_metadata_contract import SyntheticCategoryBrowserAdapter, contract as category_contract
        from test_studio_language_contract import (
            FIXTURE_ROOT, LANGUAGE_MAP, VIDEO_ID, SyntheticLanguageBrowserAdapter, language_contract,
        )
        from upload_evidence import _normalize_event
        events, requests, unexpected, receipts = [], [], [], []
        saved = {'locale': 'en', 'video_id': VIDEO_ID, 'language': 'spoken-en-us', 'category': 'entertainment', 'abort': False}

        def dispatch(route):
            url = route.request.url
            if url != f'http://127.0.0.1:8687/video/{VIDEO_ID}':
                unexpected.append(url); route.abort(); return
            requests.append(url)
            receipts.append([event['verification_state'] for event in events] == ['received'])
            if saved['abort']: route.abort(); return
            route.fulfill(status=200, content_type='text/html', body=(
                f'<html lang="{saved["locale"]}"><article data-video-id="{saved["video_id"]}">'
                f'<output data-category-value="{saved["category"]}"></output>'
                f'<output data-video-language-value="{saved["language"]}"></output></article></html>'))

        def sink(event): events.append(_normalize_event(event))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(service_workers='block')
                context.route('**/*', dispatch)
                page, category_page = context.new_page(), context.new_page()

                def reset(locale='en'):
                    page.set_content((FIXTURE_ROOT / f'language_{locale}.html').read_text(encoding='utf-8'))
                    self.assertFalse(page.locator('[data-details]').is_visible())
                    events.clear()
                    saved.update(locale=locale, video_id=VIDEO_ID, language='spoken-en-us', category='entertainment', abort=False)
                    return language_contract(SyntheticLanguageBrowserAdapter(page))

                c = reset()
                self.assertIsNone(c.prepare('invalid legacy', mode='keep'))
                c.recheck(None)
                self.assertIsNone(c.verify_saved(None, VIDEO_ID, evidence_sink=sink))
                self.assertFalse(page.locator('[data-details]').is_visible())
                self.assertEqual((events, requests), ([], []))

                for locale in ('vi', 'en'):
                    for tag, value in LANGUAGE_MAP[locale].items():
                        with self.subTest(locale=locale, tag=tag):
                            c = reset(locale); saved['language'] = value
                            prepared = c.prepare(tag)
                            self.assertTrue(page.locator('[data-details]').is_visible())
                            self.assertEqual(page.input_value('#video-language'), value)
                            self.assertEqual(page.input_value('[data-semantic="caption-certification"]'), 'not-applicable')
                            self.assertEqual(page.input_value('[data-semantic="title-language"]'), 'title-language-en')
                            c.recheck(prepared, verify_binding=lambda: True, verify_approval=lambda: True)
                            c.verify_saved(prepared, VIDEO_ID, evidence_sink=sink)
                            self.assertEqual([event['verification_state'] for event in events], ['received', 'verified'])
                            self.assertEqual(events[-1]['expected'], {'video_language': tag})
                            self.assertEqual(events[-1]['observed'], {'video_language': tag})

                c = reset()
                page.locator('#video-language option').evaluate_all('(options) => options.forEach(option => option.textContent="Same label")')
                self.assertEqual(c.prepare('en-US').value, 'spoken-en-us')
                for script, selector, code in (
                    ('(node) => node.dataset.studioSection="caption_certification"', 'section[data-studio-section="video_language"]', 'SCOPE_AMBIGUOUS'),
                    ('(node) => node.dataset.studioSection="title_language"', 'section[data-studio-section="video_language"]', 'SCOPE_AMBIGUOUS'),
                    ('(node) => node.remove()', 'section[data-studio-section="video_language"]', 'SCOPE_AMBIGUOUS'),
                    ('(node) => node.after(node.cloneNode(true))', 'section[data-studio-section="video_language"]', 'SCOPE_AMBIGUOUS'),
                    ('(node) => node.after(node.cloneNode(true))', '#video-language', 'SCOPE_AMBIGUOUS'),
                    ('(node) => node.disabled=true', '#video-language', 'CONTROL_DISABLED'),
                    ('(node) => node.disabled=true', '#video-language option[value="spoken-en-us"]', 'OPTION_DISABLED'),
                    ('(node) => node.after(node.cloneNode(true))', '#video-language option[value="spoken-en-us"]', 'OPTION_AMBIGUOUS'),
                    ('(node) => node.dataset.noChange="1"', '#video-language', 'SET_MISMATCH'),
                    ('(node) => node.lang="fr"', 'html', 'LOCALE_UNSUPPORTED'),
                ):
                    with self.subTest(prepare=code, selector=selector):
                        c = reset(); page.locator(selector).evaluate(script)
                        with self.assertRaisesRegex(StudioMetadataError, code): c.prepare('en-US')
                        self.assertEqual(events, [])

                for script, code in (('(node) => node.value="spoken-en"', 'CHANGED_BEFORE_PUBLISH'),
                                     ('(node) => node.remove()', 'SCOPE_AMBIGUOUS')):
                    c = reset(); prepared = c.prepare('en-US')
                    page.locator('#video-language').evaluate(script)
                    with self.assertRaisesRegex(StudioMetadataError, code):
                        c.recheck(prepared, verify_binding=lambda: True, verify_approval=lambda: True)

                for changed, expected_state, code in (
                    ({}, 'verified', None),
                    ({'language': 'spoken-en'}, 'mismatch', 'STUDIO_LANGUAGE_READBACK_MISMATCH'),
                    ({'video_id': 'otherID1234'}, 'unknown', 'READBACK_ID_MISMATCH'),
                    ({'language': 'unsupported'}, 'unknown', 'STUDIO_LANGUAGE_READBACK_UNMAPPED'),
                    ({'abort': True}, 'unknown', 'READBACK_FAILED'),
                ):
                    with self.subTest(combined=expected_state, code=code):
                        lang = reset(); pl = lang.prepare('en-US')
                        category_page.set_content((FIXTURE_ROOT / 'category_en.html').read_text(encoding='utf-8'))
                        cat = category_contract(SyntheticCategoryBrowserAdapter(category_page)); pc = cat.prepare('24')
                        saved.update(changed)
                        call = lambda: verify_saved_metadata(VIDEO_ID, category=(cat, pc), language=(lang, pl), evidence_sink=sink)
                        if code:
                            with self.assertRaisesRegex(StudioMetadataError, code) as raised: call()
                            self.assertTrue(raised.exception.after_send)
                        else:
                            self.assertEqual(set(call()), {'category_id', 'video_language'})
                        self.assertEqual([event['verification_state'] for event in events], ['received', expected_state])
                        self.assertEqual(events[0]['expected'], {'category_id': '24', 'video_language': 'en-US'})
                        if expected_state == 'mismatch':
                            self.assertEqual(events[-1]['observed'], {'category_id': '24', 'video_language': 'en'})
                self.assertEqual(len(requests), 16)
                self.assertTrue(all(receipts))
                self.assertEqual(unexpected, [])
            finally:
                browser.close()

    def test_studio_category_synthetic_contract_scopes_rechecks_and_exact_id_readback(self):
        from playwright.sync_api import sync_playwright
        from studio_metadata_contract import StudioMetadataError
        from test_studio_metadata_contract import FIXTURE_ROOT, SyntheticCategoryBrowserAdapter, contract
        video_id = 'dQw4w9WgXcQ'
        events, requests, unexpected, receipt_before_readback = [], [], [], []
        saved = {'locale': 'en', 'video_id': video_id, 'value': 'entertainment', 'abort': False}

        def dispatch(route):
            url = route.request.url
            if url != f'http://127.0.0.1:8687/video/{video_id}':
                unexpected.append(url)
                route.abort()
                return
            requests.append(url)
            receipt_before_readback.append(bool(events and events[0]['verification_state'] == 'received'))
            if saved['abort']:
                route.abort()
                return
            route.fulfill(status=200, content_type='text/html', body=(
                f'<html lang="{saved["locale"]}"><article data-video-id="{saved["video_id"]}">'
                f'<output data-category-value="{saved["value"]}"></output></article></html>'))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(service_workers='block')
                context.route('**/*', dispatch)
                page = context.new_page()

                def reset(locale='en'):
                    page.set_content((FIXTURE_ROOT / f'category_{locale}.html').read_text(encoding='utf-8'))
                    self.assertFalse(page.locator('[data-details]').is_visible())
                    events.clear()
                    saved.update(locale=locale, video_id=video_id, value='entertainment', abort=False)
                    return contract(SyntheticCategoryBrowserAdapter(page))

                for locale in ('vi', 'en'):
                    with self.subTest(locale=locale):
                        c = reset(locale)
                        page.locator('[data-semantic="unrelated"] option').evaluate('(option) => option.value="audience-only"')
                        prepared = c.prepare('24')
                        self.assertTrue(page.locator('[data-details]').is_visible())
                        self.assertEqual(page.input_value('#category'), 'entertainment')
                        # The unrelated section has the same label and must remain untouched.
                        self.assertEqual(page.input_value('[data-semantic="unrelated"]'), 'audience-only')
                        guards = []
                        c.recheck(prepared, verify_binding=lambda: guards.append('binding'),
                                  verify_approval=lambda: guards.append('approval'))
                        self.assertEqual(guards, ['binding', 'approval'])
                        result = c.verify_saved(prepared, video_id, evidence_sink=events.append)
                        self.assertEqual(result.video_id, video_id)
                        self.assertEqual([event['verification_state'] for event in events], ['received', 'verified'])
                        self.assertEqual(events[-1]['observed'], {'category_id': '24'})

                c = reset()
                page.locator('#category option').evaluate_all('(options) => options.forEach(option => option.textContent="Same label")')
                self.assertEqual(c.prepare('24').value, 'entertainment')
                self.assertEqual(page.input_value('#category'), 'entertainment')

                prepare_failures = (
                    ('duplicate section', '(section) => section.after(section.cloneNode(true))', 'section[data-studio-section="category"]', 'SCOPE_AMBIGUOUS'),
                    ('duplicate control', '(control) => control.after(control.cloneNode(true))', '#category', 'SCOPE_AMBIGUOUS'),
                    ('missing section', '(section) => section.remove()', 'section[data-studio-section="category"]', 'SCOPE_AMBIGUOUS'),
                    ('wrong section', '(section) => section.dataset.studioSection="audience"', 'section[data-studio-section="category"]', 'SCOPE_AMBIGUOUS'),
                    ('disabled control', '(control) => control.disabled=true', '#category', 'CONTROL_DISABLED'),
                    ('disabled option', '(option) => option.disabled=true', '#category option[value="entertainment"]', 'OPTION_DISABLED'),
                    ('duplicate value', '(option) => option.after(option.cloneNode(true))', '#category option[value="entertainment"]', 'OPTION_AMBIGUOUS'),
                    ('no change', '(control) => control.dataset.noChange="1"', '#category', 'SET_MISMATCH'),
                    ('unknown locale', '(html) => html.lang="fr"', 'html', 'LOCALE_UNSUPPORTED'),
                    ('missing reveal', '(button) => button.remove()', '[data-action="show-more"]', 'REVEAL_FAILED'),
                )
                for label, script, selector, code in prepare_failures:
                    with self.subTest(case=label):
                        c = reset()
                        page.locator(selector).evaluate(script)
                        with self.assertRaisesRegex(StudioMetadataError, code) as raised:
                            c.prepare('24')
                        self.assertFalse(raised.exception.after_send)
                        self.assertEqual(events, [])

                for action, code in (
                    ('(control) => control.value="people-blogs"', 'CHANGED_BEFORE_PUBLISH'),
                    ('(control) => control.remove()', 'SCOPE_AMBIGUOUS'),
                ):
                    with self.subTest(recheck=code):
                        c = reset(); prepared = c.prepare('24')
                        page.locator('#category').evaluate(action)
                        with self.assertRaisesRegex(StudioMetadataError, code):
                            c.recheck(prepared, verify_binding=lambda: None, verify_approval=lambda: None)

                for binding, approval, code in ((False, True, 'BINDING_CHANGED'), (True, False, 'APPROVAL_STALE')):
                    with self.subTest(guard=code):
                        c = reset(); prepared = c.prepare('24')
                        with self.assertRaisesRegex(StudioMetadataError, code):
                            c.recheck(prepared, verify_binding=lambda: binding, verify_approval=lambda: approval)

                for changed, code, state in (
                    ({'value': 'people-blogs'}, 'READBACK_MISMATCH', 'mismatch'),
                    ({'video_id': 'otherID1234'}, 'READBACK_ID_MISMATCH', 'unknown'),
                    ({'locale': 'vi'}, 'READBACK_LOCALE_CHANGED', 'unknown'),
                    ({'value': 'unmapped'}, 'READBACK_UNMAPPED', 'unknown'),
                    ({'abort': True}, 'READBACK_FAILED', 'unknown'),
                ):
                    with self.subTest(readback=code):
                        c = reset(); prepared = c.prepare('24'); saved.update(changed)
                        with self.assertRaisesRegex(StudioMetadataError, code) as raised:
                            c.verify_saved(prepared, video_id, evidence_sink=events.append)
                        self.assertTrue(raised.exception.after_send)
                        self.assertEqual([event['verification_state'] for event in events], ['received', state])
                        self.assertEqual(events[0]['video_id'], video_id)
                self.assertEqual(len(requests), 7)
                self.assertTrue(all(receipt_before_readback))
                self.assertEqual(unexpected, [])
            finally:
                browser.close()

    def test_metadata_choices_require_confirmation_and_preview_actual_values(self):
        import json
        import re
        from contextlib import closing
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        import database
        import monitor_policy
        from test_review_regressions import ReviewRegressions
        fixture = ReviewRegressions()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO chrome_profiles(name,path) VALUES('Offline metadata fixture','')")
            option_id = conn.execute("INSERT INTO upload_option_groups(name,category_id,language,disable_shorts_remix) VALUES('Legacy metadata','27','en-US',1)").lastrowid
            conn.execute('UPDATE video_queue SET option_group_id=? WHERE id=901', (option_id,))
            conn.execute("UPDATE youtube_channels SET studio_category_mode='legacy_unconfirmed',profile_name='Offline metadata fixture' WHERE id=901")
            monitor_id = conn.execute('INSERT INTO monitors(name,source_urls,target_channel_id,option_group_id,metadata_policy_json) VALUES(?,?,?,?,?)',
                ('Metadata monitor', 'https://example.test/source', 901, option_id, monitor_policy.encode(monitor_policy.DEFAULT_POLICY))).lastrowid
        def saved_option():
            with closing(database.get_conn()) as conn:
                return dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option_id,)).fetchone())
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
                route.fulfill(status=200, content_type='text/html', body=html)
                return
            allowed = ('/api/auth', '/api/channels', '/api/groups', '/api/options', '/api/upload-capabilities', '/api/queue/preview-distribution', '/api/monitors', '/api/overview', '/static/')
            if not parsed.path.startswith(allowed) or any(suffix in parsed.path for suffix in ('open-browser', 'check-oauth', '/toggle', 'metadata-confirm')):
                route.abort()
                return
            requests.append((request.method, parsed.path, request.post_data))
            response = fixture.client.request(request.method, parsed.path,
                headers=dict(request.headers), content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', dispatch)
                dialogs = []
                page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
                page.goto('http://127.0.0.1:8686/')
                page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
                page.add_style_tag(content='.tab-pane {display:block!important}')
                page.evaluate('async () => { await loadGroups(); await loadChannels(); await loadOptions(); }')
                page.evaluate('editOptionGroup', option_id)
                self.assertFalse(page.is_checked('#opt-metadata-confirm'))
                self.assertTrue(page.locator('#opt-remix').is_disabled())
                self.assertEqual(page.input_value('#opt-category'), '27')
                page.fill('#opt-name', 'Legacy renamed')
                page.evaluate('saveOptionGroup()')
                original = saved_option()
                self.assertEqual((original['metadata_intent_json'], original['language'], original['disable_shorts_remix']), ('{}', 'en-US', 1))
                mutation = [json.loads(body) for method, path, body in requests if method == 'PUT' and path == f'/api/options/{option_id}'][-1]
                self.assertNotIn('metadata_intent_json', mutation)
                page.evaluate('toggleMonitor', monitor_id)
                self.assertTrue(any('Chưa thể bật Monitor' in message and 'UPLOAD_REMIX_UNSUPPORTED' in message for message in dialogs), dialogs)
                self.assertFalse(any('metadata-confirm' in path for _, path, _ in requests))
                page.evaluate('editOptionGroup', option_id)
                page.check('#opt-metadata-confirm')
                page.select_option('#opt-studio-category', 'inherit')
                page.select_option('#opt-video-language-mode', 'keep')
                page.select_option('#opt-remix', 'keep')
                page.evaluate('saveOptionGroup()')
                confirmed = saved_option()
                self.assertEqual(json.loads(confirmed['metadata_intent_json']), {'version': 1, 'studio_category_mode': 'inherit', 'video_language': {'mode': 'keep'}, 'remix': 'keep'})
                self.assertEqual(confirmed['language'], 'en-US')
                self.assertEqual(confirmed['disable_shorts_remix'], 0)
                page.evaluate('editOptionGroup', option_id)
                page.fill('#opt-name', 'Confirmed renamed')
                page.evaluate('saveOptionGroup()')
                self.assertEqual(saved_option()['metadata_intent_json'], confirmed['metadata_intent_json'])
                page.evaluate('openChannelEdit(901)')
                self.assertEqual(page.input_value('#ec-studio-category'), 'legacy_unconfirmed')
                page.select_option('#ec-studio-category', 'keep')
                page.evaluate('saveChannelEdit()')
                page.evaluate("async () => { const p = await apiFetch('/api/queue/preview-distribution', {method:'POST',body:JSON.stringify({ids:[901],do_download:false,do_render:false,do_upload:true})}); window.metadataPreview=p; void showDistributionDialog(p,'Kiểm tra metadata',true); }")
                self.assertFalse(page.evaluate('metadataPreview.items[0].blocked'), {'preview': page.evaluate('metadataPreview'), 'dialogs': dialogs})
                preview_text = page.locator('.upload-preview-dialog').inner_text()
                self.assertIn('Danh mục: Giữ thiết lập bên YouTube · 27', preview_text)
                self.assertIn('giá trị từ bộ tùy chọn; lựa chọn từ kênh', preview_text)
                self.assertIn('Ngôn ngữ video: Giữ thiết lập bên YouTube', preview_text)
                page.get_by_role('button', name='Đóng', exact=True).click()
                page.evaluate('editOptionGroup', option_id)
                page.check('#opt-metadata-confirm')
                page.select_option('#opt-video-language-mode', 'apply')
                page.fill('#opt-video-language', 'vi')
                page.evaluate('saveOptionGroup()')
                self.assertEqual(saved_option()['language'], 'vi')
                page.evaluate("async () => { const p = await apiFetch('/api/queue/preview-distribution', {method:'POST',body:JSON.stringify({ids:[901],do_download:false,do_render:false,do_upload:true})}); window.metadataPreview=p; void showDistributionDialog(p,'Metadata chưa hỗ trợ',true); }")
                self.assertTrue(page.evaluate('metadataPreview.items[0].blocked'))
                self.assertIn('Ngôn ngữ video: Yêu cầu áp dụng · vi', page.locator('.upload-preview-dialog').inner_text())
                self.assertEqual(page.locator('.upload-preview-dialog button[value="start"]').count(), 0)
                page.get_by_role('button', name='Đóng', exact=True).click()
                self.assertFalse(any('Lỗi lưu' in message for message in dialogs), dialogs)
                page.evaluate('openChannelEdit(901)')
                self.assertEqual(page.input_value('#ec-studio-category'), 'keep')
                response = fixture.client.put('/api/channels/901', headers=fixture.headers, json={'studio_category_mode': 'apply'})
                self.assertEqual(response.status_code, 200, response.text)
                page.fill('#ec-note', 'Unrelated stale note')
                page.evaluate('saveChannelEdit()')
                with closing(database.get_conn()) as conn:
                    channel_mode = conn.execute('SELECT studio_category_mode FROM youtube_channels WHERE id=901').fetchone()[0]
                self.assertEqual(channel_mode, 'apply')
                self.assertTrue(any('Lỗi lưu' in message for message in dialogs), dialogs)
                channel_mutation = [json.loads(body) for method, path, body in requests if method == 'PUT' and path == '/api/channels/901'][-1]
                self.assertTrue(channel_mutation['expected_metadata_revision'])
                self.assertNotIn('studio_category_mode', channel_mutation)
                page.evaluate('closeChannelEdit()')
                page.evaluate('editOptionGroup', option_id)
                loaded_revision = page.locator('#opt-id').get_attribute('data-metadata-revision')
                restrictive = {'version': 1, 'studio_category_mode': 'inherit', 'video_language': {'mode': 'keep'}, 'remix': 'disable'}
                response = fixture.client.put(f'/api/options/{option_id}', headers=fixture.headers, json={'metadata_intent_json': restrictive})
                self.assertEqual(response.status_code, 200, response.text)
                page.evaluate('loadOptions()')  # Refresh list must not update the open form's revision.
                self.assertEqual(page.locator('#opt-id').get_attribute('data-metadata-revision'), loaded_revision)
                page.check('#opt-metadata-confirm')
                page.select_option('#opt-video-language-mode', 'keep')
                page.select_option('#opt-remix', 'keep')
                page.evaluate('saveOptionGroup()')
                self.assertEqual(json.loads(saved_option()['metadata_intent_json']), restrictive)
                self.assertIn('Lỗi lưu', dialogs[-1])
                self.assertEqual(page.input_value('#ch-studio-category'), 'legacy_unconfirmed')
                with closing(database.get_conn()) as conn, conn:
                    conn.execute("UPDATE upload_option_groups SET metadata_intent_json='null' WHERE id=?", (option_id,))
                page.evaluate('loadOptions()')
                page.evaluate('editOptionGroup', option_id)
                self.assertTrue(page.locator('#opt-metadata-confirm').is_disabled())
                self.assertIn('Metadata đã lưu không hợp lệ', page.locator('#opt-metadata-state').inner_text())
                calls_before = len([r for r in requests if r[0] == 'PUT'])
                page.evaluate('saveOptionGroup()')
                self.assertEqual(len([r for r in requests if r[0] == 'PUT']), calls_before)
                self.assertEqual(saved_option()['metadata_intent_json'], 'null')
            finally:
                browser.close()

    def test_upload_capabilities_show_limits_and_clear_stale_support_on_failure(self):
        import copy
        import json
        import re
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        from test_review_regressions import ReviewRegressions
        fixture = ReviewRegressions()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(__file__).resolve().parents[1]
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', (root/'static/index.html').read_text(encoding='utf-8'), flags=re.S)
        response_mode = {'value': 'real'}
        requests = []
        marker = '<img src=x onerror="globalThis.capabilityExecuted=1"> & tiếng Việt'

        def dispatch(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != '127.0.0.1:8686':
                route.abort()
                return
            if parsed.path == '/':
                route.fulfill(status=200, content_type='text/html', body=html)
                return
            if not parsed.path.startswith(('/api/auth', '/api/options', '/api/upload-capabilities', '/static/')):
                route.abort()
                return
            requests.append((request.method, parsed.path))
            if parsed.path == '/api/upload-capabilities' and response_mode['value'] == 'unavailable':
                route.fulfill(status=503, content_type='application/json', body='{"detail":"Unavailable"}')
                return
            response = fixture.client.request(request.method, parsed.path,
                headers=dict(request.headers), content=request.post_data_buffer)
            if parsed.path == '/api/upload-capabilities' and response_mode['value'] != 'real':
                data = copy.deepcopy(response.json())
                if response_mode['value'] == 'legacy':
                    data = {mode: {'pin_comment': False, 'thumbnail': True} for mode in ('youtube_api', 'playwright')}
                elif response_mode['value'] == 'malformed':
                    data['youtube_api']['features']['category']['enabled'] = 'yes'
                else:
                    data['youtube_api']['features']['category']['label'] = marker
                    data['playwright']['features']['category']['reason'] = marker
                route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
                return
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', dispatch)
                page.goto('http://127.0.0.1:8686/')
                page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
                page.add_style_tag(content='.tab-pane {display:block!important}')
                page.evaluate('loadOptions()')
                page.locator('section[aria-labelledby="upload-capabilities-title"] summary').click()
                rows = page.locator('#upload-capabilities-tbody tr')
                self.assertEqual(rows.count(), 7)
                category = page.locator('[data-capability="category"] td')
                self.assertIn('Áp dụng: Có', category.nth(0).inner_text())
                self.assertIn('Xác minh: Kiểm tra phản hồi sau khi gửi', category.nth(0).inner_text())
                self.assertIn('Chưa bật', category.nth(1).inner_text())
                for key in ('video_language', 'remix', 'age_restriction', 'pin_comment'):
                    for cell in page.locator(f'[data-capability="{key}"] td').all():
                        self.assertIn('Chưa bật', cell.inner_text())
                        self.assertIn('Nghiệm thu thực tế: Chưa có', cell.inner_text())
                self.assertEqual(page.locator('#opt-category').input_value(), '22')
                self.assertFalse(page.locator('#opt-pin-enabled').is_checked())
                for mode in ('unavailable', 'legacy', 'malformed'):
                    response_mode['value'] = mode
                    page.get_by_role('button', name='Tải lại khả năng đăng', exact=True).click()
                    page.wait_for_function("document.getElementById('upload-capabilities-status').textContent.startsWith('Chưa xác định')")
                    self.assertEqual(rows.count(), 0)
                response_mode['value'] = 'escaped'
                page.evaluate('globalThis.capabilityExecuted=0')
                page.get_by_role('button', name='Tải lại khả năng đăng', exact=True).click()
                page.wait_for_function("document.querySelectorAll('#upload-capabilities-tbody tr').length === 7")
                self.assertIn(marker, rows.first.inner_text())
                self.assertEqual(page.locator('#upload-capabilities-tbody img').count(), 0)
                self.assertEqual(page.evaluate('capabilityExecuted'), 0)
                self.assertTrue(all(method == 'GET' for method, _ in requests), requests)
            finally:
                browser.close()

    def test_preset_roundtrip_keeps_hidden_fields_and_rejects_stale_edit(self):
        import json
        import re
        from contextlib import closing
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        import database
        from test_render_preset_crud import RenderPresetCrudTests
        fixture = RenderPresetCrudTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        preset = fixture.create(config={'speed':1.2,'framerate':60,'bitrate':'7000k',
            'logo_video_similarity':.6,'text_scroll_height':90,'bg_music_name':'missing-local.mp3',
            'bg_music_volume':.35,'shorts_vibrant_colors':False},note='original note')
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
            if not parsed.path.startswith(('/api/auth','/api/render-presets','/api/queue','/api/projects','/static/')):
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
            page.on('dialog',lambda dialog: (dialogs.append(dialog.message),dialog.accept()))
            page.goto('http://127.0.0.1:8686/')
            page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
            page.add_style_tag(content='.tab-pane {display:block!important}')
            page.evaluate('async () => { await loadRenderPresets(); await loadProjects(); }')
            page.locator('[onclick="loadPresetToForm()"]').click(force=True)
            self.assertEqual(page.input_value('#rc-bg-music'),'missing-local.mp3')
            self.assertEqual(page.input_value('#rp-note'),'original note')
            self.assertFalse(page.is_checked('#rc-shorts-visual'))
            page.fill('#rc-speed','1.5')
            page.fill('#rp-name','Preset đã sửa')
            page.fill('#rp-note','new note')
            page.locator('[onclick="saveRenderPreset(true)"]').click(force=True)
            page.wait_for_function("gRenderPresets.some(p => p.name === 'Preset đã sửa')")
            saved = fixture.request('GET').json()[0]
            self.assertEqual(saved['id'],preset['id'])
            for key in ('framerate','bitrate','logo_video_similarity','text_scroll_height','bg_music_name'):
                self.assertEqual(saved['config'][key],preset['config'][key])
            self.assertEqual(saved['config']['bg_music_percent'],35)
            self.assertIs(saved['config']['shorts_visual_pop'],False)
            self.assertEqual(saved['config']['speed'],1.5)
            page.select_option('#sel-project','review')
            page.evaluate('loadQueue()')
            page.evaluate('applyRenderStudioToSelected()')
            with closing(database.get_conn()) as conn:
                queue_copy = conn.execute('SELECT render_config_json FROM video_queue WHERE id=901').fetchone()[0]
            self.assertEqual(json.loads(queue_copy)['framerate'],60)
            latest = fixture.request('PUT',f"/{preset['id']}",{'note':'other window','expected_revision':saved['revision']}).json()
            page.fill('#rp-note','stale edit')
            page.evaluate('saveRenderPreset(true)')
            self.assertTrue(any('Preset đã thay đổi' in message for message in dialogs))
            self.assertEqual(fixture.request('GET').json()[0]['note'],'other window')
            page.evaluate('async () => { await loadRenderPresets(); await deleteRenderPreset(); }')
            self.assertEqual(fixture.request('GET').json(),[])
            with closing(database.get_conn()) as conn:
                self.assertEqual(conn.execute('SELECT render_config_json FROM video_queue WHERE id=901').fetchone()[0],queue_copy)
            self.assertNotEqual(latest['revision'],saved['revision'])
            browser.close()

    def test_local_profile_create_note_and_delete_use_real_api(self):
        import re
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        from test_profile_crud import ProfileCrudTests
        fixture = ProfileCrudTests()
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
            if parsed.path.startswith('/api/antidetect'):
                route.fulfill(status=200, content_type='application/json', body='{"connected":false}')
                return
            response = fixture.client.request(request.method, parsed.path + ('?'+parsed.query if parsed.query else ''),
                                              headers=dict(request.headers), content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/*', dispatch)
            prompts = iter(['Profile local Việt', 'Ghi chú <script> & chữ Việt', 'Profile local Việt'])
            dialogs = []
            def on_dialog(dialog):
                dialogs.append(dialog.message)
                dialog.accept(next(prompts)) if dialog.type == 'prompt' else dialog.accept()
            page.on('dialog', on_dialog)
            page.goto('http://127.0.0.1:8686/')
            page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
            page.add_style_tag(content='.tab-pane {display:block!important}')
            page.locator('[onclick="createLocalProfile()"]').click(force=True)
            page.wait_for_function("gProfiles.some(p => p.name === 'Profile local Việt')")
            index = page.evaluate("gProfiles.findIndex(p => p.name === 'Profile local Việt')")
            page.locator(f'[data-profile-index="{index}"][data-profile-action="profileEditNote"]').click(force=True)
            page.wait_for_function("gProfiles.some(p => p.note === 'Ghi chú <script> & chữ Việt')")
            page.evaluate('createLocalProfile()')
            self.assertTrue(any('đã có cấu hình' in message for message in dialogs))
            cookie = fixture.profiles/'Profile local Việt'/'Cookies'
            cookie.write_bytes(b'keep')
            page.locator(f'[data-profile-index="{index}"][data-profile-action="profileDelete"]').click(force=True)
            page.wait_for_function("!gProfiles.some(p => p.name === 'Profile local Việt')")
            self.assertTrue(cookie.is_file())
            browser.close()

    def test_project_actions_keep_selection_and_report_blocked_delete(self):
        import re
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
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
            response = fixture.client.request(request.method, parsed.path + ('?'+parsed.query if parsed.query else ''),
                                              headers=dict(request.headers), content=request.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            self.addCleanup(browser.close)
            page = browser.new_page()
            page.route('**/*', dispatch)
            prompts = iter(['Dự án Việt / thử', 'Đã đổi tên', 'Đã đổi tên'])
            dialogs = []
            def dialog_handler(dialog):
                dialogs.append(dialog.message)
                dialog.accept(next(prompts)) if dialog.type == 'prompt' else dialog.accept()
            page.on('dialog', dialog_handler)
            page.goto('http://127.0.0.1:8686/')
            page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
            page.evaluate('loadProjects()')
            page.locator('[onclick="createNewProject()"]').click()
            page.wait_for_function("document.getElementById('sel-project').value === 'Dự án Việt / thử'")
            page.locator('[onclick="renameCurrentProject()"]').click()
            page.wait_for_function("document.getElementById('sel-project').value === 'Đã đổi tên'")
            # The option count already equals three before the duplicate request;
            # wait for its server error instead of racing the alert handler.
            with page.expect_event('dialog', predicate=lambda dialog: dialog.type == 'alert'):
                page.locator('[onclick="createNewProject()"]').click()
            page.wait_for_function("document.getElementById('sel-project').options.length === 3")
            self.assertTrue(any('đã tồn tại' in message for message in dialogs))
            page.locator('[onclick="deleteCurrentProject()"]').click()
            page.wait_for_function("document.getElementById('sel-project').options.length === 2")
            page.select_option('#sel-project', 'review')
            page.evaluate('deleteCurrentProject()')
            self.assertTrue(any('còn video' in message for message in dialogs))
            self.assertEqual(page.input_value('#sel-project'), 'review')
            browser.close()
            self._cleanups.pop()  # Browser is closed before Playwright exits.

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
        actions = ['profileOpenBrowser', 'profileCheckLogin', 'profileRefreshChannels', 'profileEditProxy', 'profileEditNote', 'profileDelete']
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
                profileEditNote = name => calls.push(['profileEditNote', name]);
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
    def test_upload_evidence_is_readonly_and_does_not_claim_received_id_is_published(self):
        import json
        import re
        from contextlib import closing
        from urllib.parse import urlsplit
        from playwright.sync_api import sync_playwright
        import database
        from test_review_regressions import ReviewRegressions
        fixture = ReviewRegressions()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        secret = 'PRIVATE_EVIDENCE_SENTINEL'
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET status='NeedsReview',published_url='' WHERE id=901")
            attempt_id = conn.execute("INSERT INTO upload_attempts(queue_id,attempt_no,channel_id,outcome,error_message) VALUES(901,1,901,'NeedsReview',?)", (secret,)).lastrowid
            evidence = {'version': 1, 'queue_id': 901, 'attempt_id': attempt_id, 'channel_id': 901,
                'backend': 'youtube_api', 'video_id': 'dQw4w9WgXcQ', 'video_url': 'https://youtu.be/dQw4w9WgXcQ',
                'received_at': '2026-11-15T03:00:00.000000Z', 'updated_at': '2026-11-15T03:00:00.000000Z',
                'verification_state': 'received', 'expected': {'category_id': '27', 'video_language': 'en-US'},
                'observed': {}, 'code': 'EVIDENCE_RECEIVED'}
            conn.execute('UPDATE upload_attempts SET evidence_json=? WHERE id=?', (json.dumps(evidence), attempt_id))
        before = fixture.row()
        root = Path(__file__).resolve().parents[1]
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', (root/'static/index.html').read_text(encoding='utf-8'), flags=re.S)
        requests, outbound = [], []
        failures = {'value': False}
        def dispatch(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != '127.0.0.1:8686':
                outbound.append(request.url)
                route.abort()
                return
            if parsed.path == '/':
                route.fulfill(status=200, content_type='text/html', body=html)
                return
            if request.method != 'GET' or not parsed.path.startswith(('/api/auth', '/api/queue', '/api/projects', '/static/')):
                route.abort()
                return
            requests.append((request.method, parsed.path))
            if failures['value'] and parsed.path.endswith('/upload-evidence'):
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': secret}))
                return
            response = fixture.client.request(request.method, parsed.path + ('?' + parsed.query if parsed.query else ''), headers=dict(request.headers))
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', dispatch)
                page.goto('http://127.0.0.1:8686/')
                page.add_script_tag(content=(root/'static/app.js').read_text(encoding='utf-8'))
                page.add_style_tag(content='.tab-pane {display:block!important}')
                page.evaluate("async () => { await loadProjects(); document.getElementById('sel-project').value='review'; await loadQueue(); }")
                page.get_by_role('button', name='Bằng chứng', exact=True).click()
                page.wait_for_function("document.querySelector('.upload-evidence-dialog').textContent.includes('dQw4w9WgXcQ')")
                modal = page.locator('.upload-evidence-dialog')
                text = modal.inner_text()
                self.assertIn('Video ID đã nhận không có nghĩa video đã đăng thành công', text)
                self.assertIn('Đã nhận video ID; chưa xác minh', text)
                self.assertIn('27', text)
                self.assertIn('en-US', text)
                self.assertIn('Không có bằng chứng', text)
                self.assertIn('2026-11-15T03:00:00.000000Z', text)
                self.assertNotIn(secret, text)
                self.assertEqual(modal.locator('a').get_attribute('href'), 'https://youtu.be/dQw4w9WgXcQ')
                self.assertEqual(modal.locator('button').count(), 1)
                self.assertEqual(modal.locator('input').count(), 0)
                self.assertEqual(fixture.row(), before)
                modal.get_by_role('button', name='Đóng', exact=True).click()
                with closing(database.get_conn()) as conn, conn:
                    conn.execute('UPDATE upload_attempts SET evidence_json=? WHERE id=?', ('{"oauth_token":"' + secret, attempt_id))
                page.get_by_role('button', name='Bằng chứng', exact=True).click()
                page.wait_for_function("document.querySelector('.upload-evidence-dialog').textContent.includes('Không đọc được bằng chứng hợp lệ')")
                self.assertNotIn(secret, page.locator('.upload-evidence-dialog').inner_text())
                self.assertEqual(page.locator('.upload-evidence-dialog a').count(), 0)
                page.locator('.upload-evidence-dialog').get_by_role('button', name='Đóng', exact=True).click()
                failures['value'] = True
                page.get_by_role('button', name='Bằng chứng', exact=True).click()
                page.wait_for_function("document.querySelector('.upload-evidence-dialog').textContent.includes('Không tải được bằng chứng')")
                self.assertNotIn(secret, page.locator('.upload-evidence-dialog').inner_text())
                self.assertEqual(fixture.row(), before)
                self.assertTrue(all(method == 'GET' for method, _ in requests), requests)
                self.assertEqual(outbound, [])
            finally:
                browser.close()

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
