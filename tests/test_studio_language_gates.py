"""Exact video-language intent remains blocked before unsupported provider writes."""
from contextlib import closing, ExitStack
import copy
import json
import threading
import unittest
from unittest import mock

import database
import metadata_resolution
import monitor_policy
import pipeline_manager as pm
import test_review_regressions as fixtures
import upload_capabilities as capabilities
import youtube_uploader as yu
from test_metadata_resolution import confirmed


CHANNEL_ID = 'UC' + 'a' * 22
LANGUAGES = ('vi', 'en', 'en-US')


class VideoLanguageRouteTests(unittest.TestCase):
    def test_exact_language_intent_is_blocked_before_all_direct_adapter_boundaries(self):
        item = {'title': 'Language fixture', 'visibility': 'private'}
        for mode in ('playwright', 'youtube_api'):
            for language in LANGUAGES:
                with self.subTest(mode=mode, language=language):
                    channel = {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'}
                    option = {'language': language, 'metadata_intent_json': confirmed(
                        category='keep', language={'mode': 'apply', 'value': language})}
                    before = copy.deepcopy((item, channel, option))
                    resolved = metadata_resolution.resolve_metadata(channel, option)
                    self.assertEqual(resolved['video_language'], {'mode': 'apply', 'value': language, 'source': 'option'})
                    self.assertEqual([issue['code'] for issue in resolved['issues']], ['UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED'])
                    metadata = yu.build_final_metadata(item, channel, option)
                    self.assertEqual(metadata['video_language'], language)
                    with ExitStack() as stack:
                        boundaries = [stack.enter_context(mock.patch.object(yu, name,
                                      side_effect=AssertionError('Provider boundary reached')))
                                      for name in ('get_settings', 'get_oauth_token_local_status',
                                                   '_channel_antidetect_id', 'probe_video_info',
                                                   '_http_session_for_channel', '_refresh_google_access_token',
                                                   '_StudioBrowserSession')]
                        network = stack.enter_context(mock.patch('requests.sessions.Session.request',
                            side_effect=AssertionError('External network forbidden')))
                        routes = [lambda: yu.validate_upload_preflight(item, channel, option),
                                  lambda: yu.upload_video_to_youtube(item, channel, option)]
                        if mode == 'playwright':
                            routes.append(lambda: yu.upload_via_playwright_studio('missing.mp4', metadata, channel))
                        else:
                            routes.extend((lambda: yu.upload_via_youtube_api('missing.mp4', metadata, channel),
                                           lambda: yu._upload_via_youtube_api_with_session('missing.mp4', metadata, channel)))
                        for route in routes:
                            with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                                route()
                            self.assertEqual(raised.exception.code, 'UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED')
                        for boundary in [*boundaries, network]:
                            boundary.assert_not_called()
                    self.assertEqual((item, channel, option), before)

    def test_keep_and_legacy_language_never_become_a_language_write_at_dispatch(self):
        item = {'title': 'Keep fixture', 'visibility': 'private', 'input_video_path': 'fixture.mp4'}
        for mode in ('playwright', 'youtube_api'):
            for canonical in (False, True):
                with self.subTest(mode=mode, canonical=canonical):
                    channel = {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'}
                    option = {'language': 'vi', 'disable_shorts_remix': 0}
                    if canonical:
                        option['metadata_intent_json'] = confirmed(category='keep')
                    before = copy.deepcopy(option)
                    with mock.patch.object(yu, 'get_oauth_token_local_status',
                                           return_value={'configured': True, 'error': ''}), \
                            mock.patch.object(yu, '_channel_antidetect_id', return_value=''), \
                            mock.patch.object(yu, '_parse_channel_proxy'), \
                            mock.patch.object(yu.os.path, 'isfile', return_value=True), \
                            mock.patch.object(yu, 'probe_video_info'), \
                            mock.patch.object(yu, 'upload_via_playwright_studio', return_value={'url': 'fixture'}) as studio, \
                            mock.patch.object(yu, 'upload_via_youtube_api', return_value={'url': 'fixture'}) as api, \
                            mock.patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden')) as network:
                        yu.upload_video_to_youtube(item, channel, option)
                    selected, unused = (studio, api) if mode == 'playwright' else (api, studio)
                    selected.assert_called_once()
                    unused.assert_not_called()
                    network.assert_not_called()
                    metadata = selected.call_args.kwargs['metadata']
                    self.assertIsNone(metadata['video_language'])
                    self.assertEqual(metadata['legacy_language'], 'vi')
                    self.assertEqual(metadata['language_intent'], 'keep' if canonical else 'legacy_unconfirmed')
                    for field in ('defaultLanguage', 'defaultAudioLanguage', 'subtitle_language', 'caption_certification'):
                        self.assertNotIn(field, metadata)
                    self.assertEqual(option, before)


class VideoLanguageIntegrationTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        patch = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        patch.start()
        self.addCleanup(patch.stop)
        self.option_id = None

    def save_language(self, language):
        payload = {'metadata_intent_json': confirmed(category='keep',
                   language={'mode': 'apply', 'value': language})}
        if self.option_id is None:
            response = self.client.post('/api/options', headers=self.headers,
                                        json={'name': 'Language gate fixture', **payload})
        else:
            response = self.client.put(f'/api/options/{self.option_id}', headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        with closing(database.get_conn()) as conn, conn:
            if self.option_id is None:
                self.option_id = conn.execute("SELECT id FROM upload_option_groups WHERE name='Language gate fixture'").fetchone()[0]
                conn.execute('UPDATE video_queue SET option_group_id=? WHERE id=901', (self.option_id,))
            return dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (self.option_id,)).fetchone())

    def assert_no_upload_records(self):
        with closing(database.get_conn()) as conn:
            for table in ('upload_attempts', 'content_reservations', 'published_history'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_saving_exact_tags_has_no_online_effect_and_preview_worker_cannot_bypass_gate(self):
        source_before = self.source.read_bytes()
        with closing(database.get_conn()) as conn:
            channel = dict(conn.execute('SELECT * FROM youtube_channels WHERE id=901').fetchone())
        for language in LANGUAGES:
            with self.subTest(language=language):
                with mock.patch.object(yu, '_StudioBrowserSession') as browser, \
                        mock.patch.object(yu, '_refresh_google_access_token') as refresh:
                    stored = self.save_language(language)
                browser.assert_not_called()
                refresh.assert_not_called()
                self.assertEqual(stored['language'], language)
                self.assertEqual(json.loads(stored['metadata_intent_json'])['video_language'],
                                 {'mode': 'apply', 'value': language})
                before = self.row()
                preview = self.manager.preview_distribution([901], [False, False, True])
                entry = preview['items'][0]
                self.assertTrue(entry['blocked'])
                self.assertEqual(entry['reason_codes'], ['UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED'])
                self.assertEqual(entry['metadata_effective']['video_language']['value'], language)
                self.assertFalse(preview['preview_token'])
                with mock.patch.object(pm.threading, 'Thread') as thread:
                    response = self.client.post('/api/pipeline/start', headers=self.headers,
                        json={'ids': [901], 'do_download': False, 'do_render': False, 'do_upload': True})
                self.assertEqual(response.status_code, 409, response.text)
                thread.assert_not_called()
                self.assertEqual(self.row(), before)
                with mock.patch.object(self.manager, '_resolve_target_channel', return_value=channel), \
                        mock.patch.object(pm, 'download_video') as download, \
                        mock.patch.object(pm.render_queue, 'submit') as render, \
                        mock.patch.object(pm, 'upload_video_to_youtube') as upload, \
                        mock.patch.object(yu, '_channel_antidetect_id') as profile:
                    self.manager._process_single_item_safe(901, 0, True, True, True,
                                                          threading.Semaphore(0), 'studio-language-gate')
                for boundary in (download, render, upload, profile):
                    boundary.assert_not_called()
                row = self.row()
                self.assertEqual(row['status'], 'Error')
                self.assertIn(capabilities.capability_matrix()['playwright']['features']['video_language']['reason'], row['error_message'])
                self.assertFalse(row['claim_token'])
                self.assertEqual(row['input_video_path'], str(self.source))
                self.assertEqual(self.source.read_bytes(), source_before)
                self.assert_no_upload_records()
                with closing(database.get_conn()) as conn:
                    self.assertEqual(dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?',
                                                       (self.option_id,)).fetchone()), stored)

    def test_mixed_monitor_preserves_each_requested_tag_and_blocks_both_backends(self):
        self.save_language('vi')
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Language gate group')")
            conn.executemany('INSERT INTO channel_group_memberships(channel_id,group_name) VALUES(?,?)',
                             [(901, 'Language gate group'), (902, 'Language gate group')])
            conn.execute("UPDATE youtube_channels SET upload_mode='youtube_api' WHERE id=902")
            monitor_id = conn.execute(
                'INSERT INTO monitors(name,source_urls,target_group_name,option_group_id,metadata_policy_json) VALUES(?,?,?,?,?)',
                ('Language gate monitor', 'https://www.youtube.com/@fixture', 'Language gate group', self.option_id,
                 monitor_policy.encode(monitor_policy.DEFAULT_POLICY))).lastrowid
        for language in LANGUAGES:
            with self.subTest(language=language):
                stored = self.save_language(language)
                with closing(database.get_conn()) as conn, conn:
                    mon = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
                    preview = monitor_policy.preview(conn, mon)
                    conn.execute('UPDATE monitors SET metadata_revision=? WHERE id=?', (preview['revision'], monitor_id))
                    before = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
                    with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                        monitor_policy.validate_session(conn, before)
                    self.assertEqual(raised.exception.code, 'UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED')
                self.assertTrue(preview['blocked'])
                self.assertEqual({row['channel_id'] for row in preview['channels']}, {901, 902})
                for row in preview['channels']:
                    self.assertTrue(row['blocked'])
                    self.assertEqual(row['reason_codes'], ['UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED'])
                    self.assertEqual(row['metadata_effective']['video_language']['value'], language)
                response = self.client.post(f'/api/monitors/{monitor_id}/metadata-confirm', headers=self.headers, json=preview)
                self.assertEqual(response.status_code, 409, response.text)
                with mock.patch.object(pm.threading, 'Thread') as thread, \
                        mock.patch.object(pm, 'scan_social_urls') as scan:
                    self.assertFalse(self.manager.start_monitor(monitor_id)['ok'])
                thread.assert_not_called()
                scan.assert_not_called()
                self.assert_no_upload_records()
                with closing(database.get_conn()) as conn:
                    after = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
                    self.assertEqual(dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?',
                                                       (self.option_id,)).fetchone()), stored)
                self.assertEqual(after['is_running'], 0)
                self.assertEqual({**after, 'status': before['status']}, before)


if __name__ == '__main__':
    unittest.main()
