"""Production Studio category stays gated despite synthetic contract coverage."""
from contextlib import closing, ExitStack
import copy
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


class StudioCategoryRouteTests(unittest.TestCase):
    def test_category_apply_is_blocked_on_every_direct_route_before_provider_or_file_io(self):
        item = {'title': 'Offline fixture', 'visibility': 'private'}
        for origin in ('channel', 'option'):
            for category_id in ('22', '999'):
                with self.subTest(origin=origin, category_id=category_id):
                    channel = {'channel_id': CHANNEL_ID, 'upload_mode': 'playwright',
                               'default_category_id': category_id,
                               'studio_category_mode': 'apply' if origin == 'channel' else 'keep'}
                    options = None if origin == 'channel' else {
                        'category_id': category_id, 'metadata_intent_json': confirmed(category='apply')}
                    before = copy.deepcopy((item, channel, options))
                    resolved = metadata_resolution.resolve_metadata(channel, options)
                    self.assertEqual(resolved['category']['value'], category_id)
                    self.assertEqual([issue['code'] for issue in resolved['issues']], ['UPLOAD_CATEGORY_UNSUPPORTED'])
                    metadata = yu.build_final_metadata(item, channel, options)
                    self.assertEqual(metadata['category_id'], category_id)
                    with ExitStack() as stack:
                        boundaries = [stack.enter_context(mock.patch.object(yu, name,
                                      side_effect=AssertionError('Provider/file boundary reached')))
                                      for name in ('get_settings', '_channel_antidetect_id', 'probe_video_info',
                                                   '_http_session_for_channel', '_StudioBrowserSession')]
                        network = stack.enter_context(mock.patch('requests.sessions.Session.request',
                            side_effect=AssertionError('External network forbidden')))
                        for route in (
                            lambda: yu.validate_upload_preflight(item, channel, options),
                            lambda: yu.upload_video_to_youtube(item, channel, options),
                            lambda: yu.upload_via_playwright_studio('missing.mp4', metadata, channel),
                        ):
                            with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                                route()
                            self.assertEqual(raised.exception.code, 'UPLOAD_CATEGORY_UNSUPPORTED')
                        for boundary in [*boundaries, network]:
                            boundary.assert_not_called()
                    self.assertEqual((item, channel, options), before)

    def test_keep_preserves_category_intent_and_dispatches_existing_studio_path(self):
        channel = {'channel_id': CHANNEL_ID, 'upload_mode': 'playwright',
                   'default_category_id': '24', 'studio_category_mode': 'apply'}
        options = {'category_id': '999', 'metadata_intent_json': confirmed(category='keep')}
        item = {'title': 'Keep fixture', 'visibility': 'private', 'input_video_path': 'fixture.mp4'}
        before = copy.deepcopy((item, channel, options))
        with mock.patch.object(yu, '_channel_antidetect_id', return_value=''), \
                mock.patch.object(yu, '_parse_channel_proxy'), \
                mock.patch.object(yu.os.path, 'isfile', return_value=True), \
                mock.patch.object(yu, 'probe_video_info'), \
                mock.patch.object(yu, 'upload_via_playwright_studio', return_value={'url': 'https://youtu.be/dQw4w9WgXcQ'}) as studio, \
                mock.patch.object(yu, 'upload_via_youtube_api') as api:
            metadata = yu.validate_upload_preflight(item, channel, options)
            result = yu.upload_video_to_youtube(item, channel, options)
        self.assertEqual(metadata['category_id'], '999')
        self.assertEqual(metadata['studio_category_mode'], 'keep')
        self.assertEqual(studio.call_args.kwargs['metadata'], metadata)
        self.assertEqual(result['url'], 'https://youtu.be/dQw4w9WgXcQ')
        studio.assert_called_once()
        api.assert_not_called()
        self.assertEqual((item, channel, options), before)


class StudioCategoryIntegrationTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        patch = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        patch.start()
        self.addCleanup(patch.stop)

    def apply_channel(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET studio_category_mode='apply',default_category_id='999' WHERE id=901")
            return dict(conn.execute('SELECT * FROM youtube_channels WHERE id=901').fetchone())

    def assert_no_upload_records(self):
        with closing(database.get_conn()) as conn:
            for table in ('upload_attempts', 'content_reservations', 'published_history'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_preview_reports_requested_category_and_start_cannot_bypass_gate(self):
        channel = self.apply_channel()
        before = self.row()
        preview = self.manager.preview_distribution([901], [False, False, True])
        self.assertTrue(preview['items'][0]['blocked'])
        self.assertEqual(preview['items'][0]['reason_codes'], ['UPLOAD_CATEGORY_UNSUPPORTED'])
        self.assertEqual(preview['items'][0]['metadata_effective']['category']['value'], '999')
        self.assertFalse(preview['preview_token'])
        with mock.patch.object(pm.threading, 'Thread') as thread:
            response = self.client.post('/api/pipeline/start', headers=self.headers,
                json={'ids': [901], 'do_download': False, 'do_render': False, 'do_upload': True})
        self.assertEqual(response.status_code, 409, response.text)
        thread.assert_not_called()
        self.assertEqual(self.row(), before)
        self.assert_no_upload_records()
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM youtube_channels WHERE id=901').fetchone()), channel)

    def test_worker_category_guard_precedes_download_render_and_upload_ownership(self):
        channel = self.apply_channel()
        source_before = self.source.read_bytes()
        with mock.patch.object(self.manager, '_resolve_target_channel', return_value=channel), \
                mock.patch.object(pm, 'download_video') as download, \
                mock.patch.object(pm.render_queue, 'submit') as render, \
                mock.patch.object(pm, 'upload_video_to_youtube') as upload, \
                mock.patch.object(yu, '_channel_antidetect_id') as profile:
            self.manager._process_single_item_safe(901, 0, True, True, True,
                                                  threading.Semaphore(0), 'studio-category-gate')
        for boundary in (download, render, upload, profile):
            boundary.assert_not_called()
        row = self.row()
        self.assertEqual(row['status'], 'Error')
        self.assertIn(capabilities.capability_matrix()['playwright']['features']['category']['reason'], row['error_message'])
        self.assertFalse(row['claim_token'])
        self.assertEqual(row['input_video_path'], str(self.source))
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assert_no_upload_records()

    def test_mixed_monitor_blocks_only_studio_apply_and_cannot_confirm_or_start(self):
        self.apply_channel()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Studio category gate')")
            conn.executemany('INSERT INTO channel_group_memberships(channel_id,group_name) VALUES(?,?)',
                             [(901, 'Studio category gate'), (902, 'Studio category gate')])
            conn.execute("UPDATE youtube_channels SET upload_mode='youtube_api',default_category_id='24' WHERE id=902")
            monitor_id = conn.execute(
                'INSERT INTO monitors(name,source_urls,target_group_name,metadata_policy_json) VALUES(?,?,?,?)',
                ('Studio category monitor', 'https://www.youtube.com/@fixture', 'Studio category gate',
                 monitor_policy.encode(monitor_policy.DEFAULT_POLICY))).lastrowid
            mon = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
            preview = monitor_policy.preview(conn, mon)
            # Even a revision supplied directly cannot authorize unsupported intent.
            conn.execute('UPDATE monitors SET metadata_revision=? WHERE id=?', (preview['revision'], monitor_id))
            before = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
            with self.assertRaises(capabilities.UploadCapabilityError) as raised:
                monitor_policy.validate_session(conn, before)
            self.assertEqual(raised.exception.code, 'UPLOAD_CATEGORY_UNSUPPORTED')
        rows = {row['channel_id']: row for row in preview['channels']}
        self.assertTrue(preview['blocked'])
        self.assertEqual(rows[901]['reason_codes'], ['UPLOAD_CATEGORY_UNSUPPORTED'])
        self.assertEqual(rows[901]['metadata_effective']['category']['value'], '999')
        self.assertFalse(rows[902]['blocked'])
        self.assertEqual(rows[902]['metadata_effective']['category']['value'], '24')
        response = self.client.get(f'/api/monitors/{monitor_id}/metadata-preview', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['blocked'])
        response = self.client.post(f'/api/monitors/{monitor_id}/metadata-confirm', headers=self.headers,
                                    json=response.json())
        self.assertEqual(response.status_code, 409, response.text)
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone()), before)
        with mock.patch.object(pm.threading, 'Thread') as thread, \
                mock.patch.object(pm, 'scan_social_urls') as scan:
            result = self.manager.start_monitor(monitor_id)
        self.assertFalse(result['ok'])
        thread.assert_not_called()
        scan.assert_not_called()
        self.assert_no_upload_records()
        with closing(database.get_conn()) as conn:
            after = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone())
        self.assertEqual(after['is_running'], 0)
        self.assertIn(capabilities.capability_matrix()['playwright']['features']['category']['reason'], after['status'])
        self.assertEqual({**after, 'status': before['status']}, before)


if __name__ == '__main__':
    unittest.main()
