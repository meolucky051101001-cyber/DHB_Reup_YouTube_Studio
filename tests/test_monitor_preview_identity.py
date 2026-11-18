"""The Monitor approval view identifies destinations without exposing credentials."""
from contextlib import closing
import unittest
from unittest import mock

import database
import monitor_policy
import test_review_regressions as review_fixture


class MonitorPreviewIdentityTests(unittest.TestCase):
    setUp = review_fixture.ReviewRegressions.setUp

    def test_readonly_preview_shows_exact_channel_backend_and_profile_without_secrets(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute(
                "UPDATE youtube_channels SET name=?,profile_name=?,upload_mode=?,oauth_token_json=? WHERE id=901",
                ('Fixture destination', 'Fixture profile', 'youtube_api', 'private-oauth-fixture'),
            )
            monitor_id = conn.execute(
                'INSERT INTO monitors(name,source_urls,target_channel_id,metadata_policy_json) VALUES(?,?,?,?)',
                ('Identity fixture', 'https://example.test/source', 901, monitor_policy.encode(monitor_policy.DEFAULT_POLICY)),
            ).lastrowid
            before = list(conn.execute('SELECT * FROM monitors'))
        with mock.patch.object(self.manager, 'start_monitor') as start:
            response = self.client.get(f'/api/monitors/{monitor_id}/metadata-preview', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()['channels'][0]
        self.assertEqual(row['channel_id'], 901)
        self.assertEqual(row['youtube_channel_id'], 'UC' + 'a' * 22)
        self.assertEqual(row['profile_name'], 'Fixture profile')
        self.assertEqual(row['upload_mode'], 'youtube_api')
        self.assertNotIn('private-oauth-fixture', response.text)
        self.assertNotIn('oauth_token_json', row)
        start.assert_not_called()
        with closing(database.get_conn()) as conn:
            self.assertEqual(list(conn.execute('SELECT * FROM monitors')), before)
