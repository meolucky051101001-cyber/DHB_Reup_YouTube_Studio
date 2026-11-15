"""Effective metadata and approval invalidation against temporary production SQLite."""
import json
import threading
import unittest
from contextlib import closing
from unittest import mock

import database
import metadata_resolution
import pipeline_manager as pm
import test_review_regressions as fixtures
import upload_capabilities
from test_metadata_resolution import confirmed
from upload_approval import PreviewStale


class MetadataApprovalTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def set_option(self, intent):
        with closing(database.get_conn()) as conn, conn:
            option_id = conn.execute(
                'INSERT INTO upload_option_groups(name,category_id,metadata_intent_json) VALUES(?,?,?)',
                ('Confirmed fixture', '27', json.dumps(intent))).lastrowid
            conn.execute('UPDATE video_queue SET option_group_id=? WHERE id=901', (option_id,))
        return option_id

    def preview(self):
        result = self.manager.preview_distribution([901], [False, False, True])
        self.assertFalse(result['items'][0]['blocked'], result)
        self.assertTrue(result['preview_token'])
        return result

    def assert_no_upload_ownership(self):
        self.assertFalse(self.row()['claim_token'])
        self.assertIsNone(self.row()['assigned_channel_id'])
        with closing(database.get_conn()) as conn:
            for table in ('content_reservations', 'upload_attempts', 'published_history'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_optionless_legacy_preview_requires_explicit_channel_choice_without_rewriting_queue(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET studio_category_mode='legacy_unconfirmed',default_category_id='24' WHERE id=901")
        before = self.row()
        result = self.manager.preview_distribution([901], [False, False, True])
        entry = result['items'][0]
        self.assertTrue(entry['blocked'])
        self.assertIn('UPLOAD_CATEGORY_CONFIRMATION_REQUIRED', entry['reason_codes'])
        self.assertEqual(entry['metadata_effective']['category'], {
            'value': '24', 'source': 'channel', 'mode': 'legacy_unconfirmed', 'intent_source': 'channel'})
        self.assertFalse(result['preview_token'])
        response = self.client.put('/api/channels/901', headers=self.headers, json={'studio_category_mode': 'keep'})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.preview()
        self.assertEqual(result['items'][0]['metadata_effective']['category']['mode'], 'keep')
        self.assertEqual(self.row(), before)
        self.assert_no_upload_ownership()

    def test_snapshot_records_effective_metadata_and_canonical_change_invalidates_atomically(self):
        option_id = self.set_option(confirmed())
        before = self.row()
        result = self.preview()
        with closing(database.get_conn()) as conn:
            snapshot = json.loads(conn.execute('SELECT snapshot_json FROM upload_approval_items WHERE token=?',
                                              (result['preview_token'],)).fetchone()[0])
        self.assertEqual(snapshot['schema_version'], 3)
        self.assertEqual(snapshot['metadata_effective'], result['items'][0]['metadata_effective'])
        self.assertEqual(snapshot['metadata_effective']['category']['value'], '27')
        response = self.client.put(f'/api/options/{option_id}', headers=self.headers,
                                   json={'metadata_intent_json': confirmed(language={'mode': 'apply', 'value': 'en-US'})})
        self.assertEqual(response.status_code, 200, response.text)
        with self.assertRaises(PreviewStale):
            self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'stale-intent')
        self.assertEqual(self.row(), before)
        self.assert_no_upload_ownership()
        fresh = self.manager.preview_distribution([901], [False, False, True])
        self.assertTrue(fresh['items'][0]['blocked'])
        self.assertIn('UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED', fresh['items'][0]['reason_codes'])

    def test_channel_category_mode_change_invalidates_existing_approval(self):
        result = self.preview()
        response = self.client.put('/api/channels/901', headers=self.headers, json={'studio_category_mode': 'apply'})
        self.assertEqual(response.status_code, 200, response.text)
        with self.assertRaises(PreviewStale):
            self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'stale-category')
        self.assert_no_upload_ownership()

    def test_capability_revision_change_invalidates_before_freeze_and_before_worker_upload(self):
        result = self.preview()
        matrix = upload_capabilities.capability_matrix()
        matrix['playwright']['revision'] = 'changed-build-gate-revision'
        with mock.patch.object(metadata_resolution, 'capability_matrix', return_value=matrix):
            with self.assertRaises(PreviewStale):
                self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'stale-gate')
        self.assert_no_upload_ownership()
        # A change after approval was accepted must be rejected by the worker too.
        self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'accepted-before-gate')
        with mock.patch.object(metadata_resolution, 'capability_matrix', return_value=matrix), \
                mock.patch.object(pm, 'upload_video_to_youtube') as upload:
            self.manager._process_single_item_safe(901, 0, False, False, True,
                                                  threading.Semaphore(0), 'accepted-before-gate')
            upload.assert_not_called()
        self.assertEqual(self.row()['status'], 'Error')
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM content_reservations').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_attempts').fetchone()[0], 0)

    def test_old_snapshot_schema_cannot_be_silently_accepted_as_current(self):
        result = self.preview()
        with closing(database.get_conn()) as conn, conn:
            row = conn.execute('SELECT snapshot_json FROM upload_approval_items WHERE token=?',
                               (result['preview_token'],)).fetchone()
            old = json.loads(row[0])
            old['schema_version'] = 2
            old.pop('metadata_effective')
            conn.execute('UPDATE upload_approval_items SET snapshot_json=? WHERE token=?',
                         (json.dumps(old), result['preview_token']))
        with self.assertRaises(PreviewStale):
            self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'old-schema')
        self.assert_no_upload_ownership()

    def test_channel_display_note_does_not_invalidate_metadata_approval(self):
        result = self.preview()
        response = self.client.put('/api/channels/901', headers=self.headers, json={'note': 'Display note only'})
        self.assertEqual(response.status_code, 200, response.text)
        self.manager._freeze_batch_assignments([901], result['preview_token'], [False, False, True], 'same-metadata')
        self.assertEqual(self.row()['claim_token'], 'same-metadata')


if __name__ == '__main__':
    unittest.main()
