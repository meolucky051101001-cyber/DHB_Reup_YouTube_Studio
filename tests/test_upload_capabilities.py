"""Capability guards at real preview/SQLite boundaries; all upload IO is forbidden."""
import threading
import unittest
from concurrent.futures import Future
from contextlib import closing
from unittest import mock

import database
import monitor_policy
import pipeline_manager as pm
import test_p3_uploader as uploader_fixtures
import test_review_regressions as fixtures
import upload_capabilities as capabilities
import youtube_uploader as yu


CHANNEL_ID = 'UC' + 'a' * 22


class CapabilityGuardTests(unittest.TestCase):
    def test_legacy_defaults_remain_unconfirmed_and_do_not_request_language(self):
        for mode in ('youtube_api', 'playwright'):
            for value in (None, '', 'vi', 'en-US'):
                with self.subTest(mode=mode, language=value):
                    metadata = yu.build_final_metadata(
                        {'title': 'Fixture', 'visibility': 'private'},
                        {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'},
                        {'language': value, 'disable_shorts_remix': 0},
                    )
                    self.assertEqual(metadata['language_intent'], 'legacy_unconfirmed')
                    self.assertEqual(metadata['legacy_language'], value)
                    self.assertFalse(metadata['disable_shorts_remix'])
                    self.assertFalse(metadata.get('video_language'))
                    self.assertEqual(capabilities.capability_issues(mode, metadata), [])

    def test_false_values_are_not_restrictive_remix_requests(self):
        for value in (None, '', False, 0, '0', 'false'):
            with self.subTest(value=value):
                metadata = capabilities.legacy_option_requests({'disable_shorts_remix': value})
                metadata['studio_category_mode'] = 'keep'
                self.assertFalse(metadata['disable_shorts_remix'])
                self.assertEqual(capabilities.capability_issues('playwright', metadata), [])

    def test_raw_and_built_flags_agree_without_activating_retained_pin_text(self):
        for mode in ('youtube_api', 'playwright'):
            for value in (None, '', False, 0, '0', 'false', ' FALSE '):
                with self.subTest(mode=mode, value=value):
                    item = {'title': 'Fixture', 'visibility': 'private',
                            'age_restriction': value, 'is_premiere': value}
                    option = {'pin_comment_enabled': value, 'pin_comment_text': 'Retained comment',
                              'disable_shorts_remix': value}
                    original_option = dict(option)
                    channel = {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'}
                    raw = {**item, **capabilities.legacy_option_requests(option), 'studio_category_mode': 'keep'}
                    metadata = yu.build_final_metadata(item, channel, option)
                    self.assertEqual(capabilities.capability_issues(mode, raw), [])
                    self.assertEqual(capabilities.capability_issues(mode, metadata), [])
                    self.assertIs(metadata['age_restriction'], False)
                    self.assertIs(metadata['is_premiere'], False)
                    self.assertIs(metadata['pin_comment_requested'], False)
                    self.assertEqual(metadata['pin_comment'], '')
                    self.assertEqual(option, original_option)
                    with mock.patch.object(yu, 'get_oauth_token_local_status',
                                           return_value={'configured': True, 'error': ''}), \
                            mock.patch.object(yu, '_channel_antidetect_id', return_value=''), \
                            mock.patch.object(yu, '_parse_channel_proxy'):
                        self.assertEqual(yu.validate_upload_preflight(item, channel, option), metadata)

    def test_invalid_flags_have_same_rejection_before_preflight_or_adapter_io(self):
        fields = [('video', 'age_restriction', 'age_restriction'),
                  ('video', 'is_premiere', 'is_premiere'),
                  ('option', 'pin_comment_enabled', 'pin_comment_requested'),
                  ('option', 'disable_shorts_remix', 'disable_shorts_remix')]
        for mode in ('youtube_api', 'playwright'):
            for origin, field, metadata_field in fields:
                for value in (2, -1, 0.0, 1.0, [], {}, 'invalid-sensitive-value'):
                    with self.subTest(mode=mode, field=field, value=value):
                        item = {'title': 'Fixture', 'visibility': 'private'}
                        option = {}
                        (item if origin == 'video' else option)[field] = value
                        channel = {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'}
                        raw = {metadata_field: value, 'studio_category_mode': 'keep'}
                        expected = capabilities.capability_issues(mode, raw)[0]
                        self.assertEqual(expected['code'], 'UPLOAD_METADATA_FLAG_INVALID')
                        with mock.patch.object(yu, 'get_oauth_token_local_status') as oauth, \
                                mock.patch.object(yu, '_channel_antidetect_id') as profile, \
                                mock.patch.object(yu, 'probe_video_info') as probe, \
                                mock.patch.object(yu, '_http_session_for_channel') as session, \
                                mock.patch.object(yu, '_refresh_google_access_token') as refresh, \
                                mock.patch.object(yu, 'get_settings') as settings, \
                                mock.patch('playwright.sync_api.sync_playwright') as browser:
                            calls = [lambda: yu.validate_upload_preflight(item, channel, option),
                                     lambda: yu.upload_video_to_youtube(item, channel, option)]
                            if mode == 'youtube_api':
                                calls.extend([
                                    lambda: yu.upload_via_youtube_api('missing.mp4', raw, channel),
                                    lambda: yu._upload_via_youtube_api_with_session('missing.mp4', raw, channel),
                                ])
                            else:
                                calls.append(lambda: yu.upload_via_playwright_studio('missing.mp4', raw, channel))
                            for call in calls:
                                with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                                    call()
                                self.assertEqual(raised.exception.code, expected['code'])
                                self.assertNotIn('invalid-sensitive-value', str(raised.exception))
                            for boundary in (oauth, profile, probe, session, refresh, settings, browser):
                                boundary.assert_not_called()

    def test_unsupported_requests_share_codes_before_any_upload_side_effect(self):
        cases = [
            ('youtube_api', {'age_restriction': True}, None),
            ('playwright', {'age_restriction': True}, None),
            ('youtube_api', {'is_premiere': True}, None),
            ('playwright', {'is_premiere': True}, None),
            ('youtube_api', {}, {'disable_shorts_remix': 1}),
            ('playwright', {}, {'disable_shorts_remix': 1}),
            ('youtube_api', {}, {'pin_comment_enabled': 1, 'pin_comment_text': ''}),
            ('playwright', {}, {'pin_comment_enabled': 1, 'pin_comment_text': ''}),
        ]
        for mode, requested, option in cases:
            with self.subTest(mode=mode, requested=requested, option=option):
                item = {'title': 'Fixture', 'visibility': 'private', **requested}
                channel = {'channel_id': CHANNEL_ID, 'upload_mode': mode, 'studio_category_mode': 'keep'}
                metadata = yu.build_final_metadata(item, channel, option)
                issue = capabilities.capability_issues(mode, metadata)[0]
                with mock.patch.object(yu, 'get_oauth_token_local_status') as oauth, \
                        mock.patch.object(yu, '_channel_antidetect_id') as profile, \
                        mock.patch.object(yu, 'probe_video_info') as probe, \
                        mock.patch.object(yu, '_http_session_for_channel') as session, \
                        mock.patch.object(yu, 'get_settings') as settings, \
                        mock.patch('playwright.sync_api.sync_playwright') as browser:
                    calls = [
                        lambda: yu.validate_upload_preflight(item, channel, option),
                        lambda: yu.upload_video_to_youtube(item, channel, option),
                    ]
                    if mode == 'youtube_api':
                        calls.extend([
                            lambda: yu.upload_via_youtube_api('missing.mp4', metadata, channel),
                            lambda: yu._upload_via_youtube_api_with_session('missing.mp4', metadata, channel),
                        ])
                    else:
                        calls.append(lambda: yu.upload_via_playwright_studio('missing.mp4', metadata, channel))
                    for call in calls:
                        with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                            call()
                        self.assertEqual(raised.exception.code, issue['code'])
                        self.assertEqual(raised.exception.issues, [issue])
                    for boundary in (oauth, profile, probe, session, settings, browser):
                        boundary.assert_not_called()

    def test_unknown_backend_cannot_fall_back_to_studio(self):
        with mock.patch.object(yu, '_channel_antidetect_id') as profile, \
                mock.patch.object(yu, 'upload_via_playwright_studio') as studio:
            with self.assertRaises(capabilities.UploadCapabilityError):
                yu.upload_video_to_youtube(
                    {'title': 'Fixture'}, {'channel_id': CHANNEL_ID, 'upload_mode': 'studio-typo'})
            profile.assert_not_called()
            studio.assert_not_called()

    def test_age_reason_never_recommends_an_also_unsupported_backend(self):
        for mode in ('youtube_api', 'playwright'):
            with self.subTest(mode=mode):
                message = capabilities.capability_issues(mode, {'age_restriction': True})[0]['message']
                self.assertIn('18+', message)
                self.assertNotIn('chuyển sang YouTube Studio', message)
                self.assertNotIn('Hãy dùng phương thức Playwright', message)


class CapabilityIntegrationTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def seed_option(self, **values):
        with closing(database.get_conn()) as conn, conn:
            option_id = conn.execute("INSERT INTO upload_option_groups(name) VALUES('Capability fixture')").lastrowid
            if values:
                conn.execute('UPDATE upload_option_groups SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=?',
                             (*values.values(), option_id))
            conn.execute('UPDATE video_queue SET option_group_id=? WHERE id=901', (option_id,))
            return dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option_id,)).fetchone())

    def channel(self):
        with closing(database.get_conn()) as conn:
            return dict(conn.execute('SELECT * FROM youtube_channels WHERE id=901').fetchone())

    def test_capability_endpoint_preserves_legacy_keys_and_exposes_evidence_limits(self):
        response = self.client.get('/api/upload-capabilities', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        matrix = response.json()
        for mode in ('youtube_api', 'playwright'):
            row = matrix[mode]
            self.assertIs(row['pin_comment'], False)
            self.assertIs(row['thumbnail'], True)
            self.assertTrue(row['pin_comment_reason'])
            self.assertEqual(row['schema_version'], 1)
            self.assertTrue(row['revision'])
            for feature in ('category', 'video_language', 'remix', 'age_restriction', 'premiere', 'pin_comment', 'thumbnail'):
                detail = row['features'][feature]
                self.assertIn('can_set', detail)
                self.assertIn('readback', detail)
                self.assertIn('offline_verified', detail)
                self.assertIs(detail['live_verified'], False)
                self.assertTrue(detail['reason'])
                self.assertTrue(detail['evidence'])
        # G17 publishes the current implementation honestly; readback comes in G19.
        self.assertTrue(matrix['youtube_api']['features']['category']['can_set'])
        self.assertEqual(matrix['youtube_api']['features']['category']['readback'], 'none')
        for feature in ('category', 'video_language', 'remix'):
            self.assertFalse(matrix['playwright']['features'][feature]['enabled'])

    def test_preview_blocks_restrictive_remix_without_mutating_configuration_or_queue(self):
        option = self.seed_option(language='vi', disable_shorts_remix=1)
        before = self.row()
        expected = capabilities.capability_issues('playwright', capabilities.legacy_option_requests(option))[0]
        with mock.patch.object(yu, '_channel_antidetect_id') as profile:
            preview = self.manager.preview_distribution([901], [False, False, True])
            profile.assert_not_called()
        entry = preview['items'][0]
        self.assertTrue(entry['blocked'])
        self.assertIn(expected['code'], entry['reason_codes'])
        self.assertIn(expected['message'], entry['reasons'])
        self.assertFalse(preview['preview_token'])
        self.assertEqual(self.row(), before)
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option['id'],)).fetchone()), option)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_approvals').fetchone()[0], 0)

    def test_worker_rejects_before_download_render_reservation_or_attempt(self):
        option = self.seed_option(language='vi', disable_shorts_remix=1)
        expected = capabilities.capability_issues('playwright', capabilities.legacy_option_requests(option))[0]
        source_before = self.source.read_bytes()
        with mock.patch.object(self.manager, '_resolve_target_channel', return_value=self.channel()), \
                mock.patch.object(pm, 'download_video') as download, \
                mock.patch.object(pm.render_queue, 'submit') as render, \
                mock.patch.object(pm, 'upload_video_to_youtube') as upload:
            self.manager._process_single_item_safe(901, 0, True, True, True, threading.Semaphore(0), 'capability-test')
            download.assert_not_called()
            render.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.row()['status'], 'Error')
        self.assertIn(expected['message'], self.row()['error_message'])
        self.assertEqual(self.source.read_bytes(), source_before)
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM content_reservations').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_attempts').fetchone()[0], 0)
            self.assertEqual(dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option['id'],)).fetchone()), option)

    def test_render_only_does_not_require_upload_capabilities_or_channel_credentials(self):
        self.seed_option(disable_shorts_remix=1, pin_comment_enabled=1)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET channel_id='',upload_mode='youtube_api',oauth_token_json='' WHERE id=901")
        rendered = self.root / 'rendered.mp4'
        rendered.write_bytes(b'offline-render-result')
        future = Future()
        future.set_result(str(rendered))
        with mock.patch.object(pm, 'validate_upload_preflight', side_effect=AssertionError('Upload guard consulted')) as guard, \
                mock.patch.object(pm, 'upload_video_to_youtube') as upload, \
                mock.patch.object(pm.render_queue, 'submit', return_value=future) as render, \
                mock.patch.object(pm, 'generate_thumbnail_from_video', return_value=''), \
                mock.patch.object(pm, 'ensure_disk_space'), \
                mock.patch.object(pm, 'estimate_render_bytes', return_value=1):
            self.manager._process_single_item_safe(901, 0, False, True, False, threading.Semaphore(0), 'capability-render')
            guard.assert_not_called()
            upload.assert_not_called()
            render.assert_called_once()
        self.assertEqual(self.row()['status'], 'Rendered')
        self.assertEqual(self.row()['output_video_path'], str(rendered))
        self.assertTrue(self.source.is_file())

    def test_default_legacy_options_do_not_block_existing_studio_preview(self):
        option = self.seed_option(language='vi', disable_shorts_remix=0)
        preview = self.manager.preview_distribution([901], [False, False, True])
        self.assertFalse(preview['items'][0]['blocked'], preview)
        self.assertTrue(preview['preview_token'])
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option['id'],)).fetchone()), option)

    def test_api_payload_preserves_category_precedence_without_activating_legacy_language(self):
        channel = {'channel_id': CHANNEL_ID, 'upload_mode': 'youtube_api', 'default_category_id': '24',
                   'oauth_token_json': '{"refresh_token":"offline-fixture"}'}
        metadata = yu.build_final_metadata({'title': 'Fixture', 'visibility': 'private'}, channel,
                                           {'category_id': '27', 'language': 'vi', 'disable_shorts_remix': 0})
        session = uploader_fixtures._ResumableSession([
            uploader_fixtures._Response(200, body={'id': uploader_fixtures.VIDEO_ID})])
        with mock.patch.object(yu, '_refresh_google_access_token', return_value='fixture'), \
                mock.patch.object(yu, '_fetch_oauth_channel_identity', return_value={'channel_id': CHANNEL_ID, 'name': 'Fixture'}), \
                mock.patch.object(yu, 'get_settings', return_value={}), \
                mock.patch.object(yu, 'add_log'):
            result = yu._upload_via_youtube_api_with_session(str(self.source), metadata, channel, session=session)
        self.assertEqual(result['url'], f'https://youtu.be/{uploader_fixtures.VIDEO_ID}')
        self.assertEqual(len(session.put_calls), 1)
        snippet = session.post_calls[0][1]['json']['snippet']
        self.assertEqual(snippet['categoryId'], '27')
        self.assertNotIn('defaultAudioLanguage', snippet)
        self.assertNotIn('defaultLanguage', snippet)
        # Captures the G17 evidence limit instead of inferring readback from success.
        self.assertNotIn('snippet', session.get_calls[0][1]['params']['part'])

    def test_monitor_checks_all_group_candidates_and_cannot_confirm_blocked_options(self):
        option = self.seed_option(disable_shorts_remix=1)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Capability group')")
            conn.executemany('INSERT INTO channel_group_memberships(channel_id,group_name) VALUES(?,?)',
                             [(901, 'Capability group'), (902, 'Capability group')])
            conn.execute("UPDATE youtube_channels SET upload_mode='youtube_api' WHERE id=902")
            monitor_id = conn.execute(
                'INSERT INTO monitors(name,source_urls,target_group_name,option_group_id,metadata_policy_json) VALUES(?,?,?,?,?)',
                ('Capability monitor', 'https://www.youtube.com/@fixture', 'Capability group', option['id'],
                 monitor_policy.encode(monitor_policy.DEFAULT_POLICY))).lastrowid
            before = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
            preview = monitor_policy.preview(conn, before)
            self.assertTrue(preview['blocked'])
            self.assertEqual({entry['channel_id'] for entry in preview['channels']}, {901, 902})
            for entry in preview['channels']:
                self.assertTrue(entry['blocked'])
                self.assertTrue(entry['reason_codes'])
                self.assertTrue(entry['reasons'])
            with self.assertRaises(capabilities.UploadCapabilityError):
                monitor_policy.validate_session(conn, {**before, 'metadata_revision': preview['revision']})
        response = self.client.get(f'/api/monitors/{monitor_id}/metadata-preview', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['blocked'])
        response = self.client.post(f'/api/monitors/{monitor_id}/metadata-confirm', headers=self.headers,
                                    json=response.json())
        self.assertEqual(response.status_code, 409, response.text)
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone()), before)
        with mock.patch.object(pm.threading, 'Thread') as thread:
            self.assertFalse(self.manager.start_monitor(monitor_id)['ok'])
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
