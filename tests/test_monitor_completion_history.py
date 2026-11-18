"""Monitor observes owned completion without writing global history after release."""
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import database
import pipeline_manager as pm


class MonitorCompletionHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dhb_monitor_history_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (
            ('APP_DB_PATH', self.root / 'db.sqlite'), ('APP_DATA_DIR', self.root),
            ('APP_TEMP_DIR', self.root / 'temp'), ('APP_PROFILES_DIR', self.root / 'profiles'),
            ('DHB_PROFILE_CHROME_DIR', self.root / 'no-profiles'), ('load_dhb_settings', lambda: {}),
        ):
            patch = mock.patch.object(database, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        database.init_db()
        self.manager = pm.PipelineManager()
        self.token = 'fixture-owned-token'
        self.url = 'https://youtu.be/dQw4w9WgXcQ'
        self.item = {'post_id': 'POST-A', 'source_url': 'https://example.invalid/post-a',
                     'content_key': 'fixture-content-key'}
        with closing(database.get_conn()) as conn, conn:
            for channel_id in (7, 8):
                conn.execute('INSERT INTO youtube_channels(id,name) VALUES(?,?)', (channel_id, str(channel_id)))
            conn.execute("INSERT INTO video_queue(id,title,target_type,assigned_channel_id,post_id,source_url,"
                         "content_key,claim_token,status) VALUES(901,'Fixture','GROUP',7,?,?,?,?,'Uploading')",
                         (*self.item.values(), self.token))
            conn.execute("INSERT INTO content_reservations(content_key,channel_id,queue_id,claim_token,state) "
                         "VALUES(?,7,901,?,'Active')", (self.item['content_key'], self.token))
        self.attempt_id = pm._start_upload_attempt(901, self.token, self.item, 7)

    def complete(self, warnings=None):
        pm._complete_upload_attempt(901, self.token, self.attempt_id, {'id': 7}, self.url,
                                    warnings or [], backend='playwright', expected_metadata={})
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET claim_token='' WHERE id=901")

    def history(self):
        with closing(database.get_conn()) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM published_history ORDER BY id')]

    def observe(self, target_channel=None):
        # A monitor's current configuration is not completion evidence.
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE video_queue SET target_channel_id=? WHERE id=901', (target_channel,))
        return self.manager._monitor_upload_completed(901)

    def test_group_completion_observation_does_not_create_null_history(self):
        self.complete()
        before = self.history()
        self.assertTrue(self.observe())
        self.assertEqual(self.history(), before)
        self.assertEqual([row['channel_id'] for row in before], [7])
        with closing(database.get_conn()) as conn:
            self.assertFalse(pm._history_contains_item(conn, self.item, 8, self.item['content_key']))

    def test_repeated_observation_and_changed_group_use_attempt_channel(self):
        self.complete([{'step': 'thumbnail', 'code': 'HTTP_403', 'message': 'Fixture warning'}])
        before = self.history()
        for current_target in (None, 8, 7, None):
            self.manager = pm.PipelineManager()
            self.assertTrue(self.observe(current_target))
        self.assertEqual(self.history(), before)

    def test_incomplete_or_unproven_completion_never_repairs_history(self):
        for status in ('Error', 'Stopped', 'NeedsReview', 'Completed'):
            with self.subTest(status=status), closing(database.get_conn()) as conn, conn:
                conn.execute('UPDATE video_queue SET status=?,published_url=?,claim_token=? WHERE id=901',
                             (status, self.url, ''))
            self.assertFalse(self.observe())
            self.assertEqual(self.history(), [])

    def test_missing_or_mismatched_history_is_not_backfilled(self):
        self.complete()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE published_history SET content_key='unrelated-content'")
        before = self.history()
        self.assertFalse(self.observe())
        self.assertEqual(self.history(), before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DELETE FROM published_history')
        self.assertFalse(self.observe())
        self.assertEqual(self.history(), [])

    def test_mismatched_queue_attempt_cannot_be_reported_complete(self):
        self.complete()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE upload_attempts SET published_url='https://youtu.be/9bZkp7q19f0'")
        before = self.history()
        self.assertFalse(self.observe())
        self.assertEqual(self.history(), before)

    def test_newer_attempt_prevents_observing_an_old_completion(self):
        self.complete()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO upload_attempts(queue_id,attempt_no,phase,outcome) "
                         "VALUES(901,2,'Queued','Authorized')")
        before = self.history()
        self.assertFalse(self.observe())
        self.assertEqual(self.history(), before)

    def test_observation_succeeds_using_a_readonly_database_connection(self):
        self.complete()

        def readonly():
            conn = sqlite3.connect(database.APP_DB_PATH.as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            return conn

        with mock.patch.object(pm, 'get_conn', side_effect=readonly):
            self.assertTrue(self.manager._monitor_upload_completed(901))

    def test_previous_history_url_is_preserved_for_authorized_reupload(self):
        old_url = 'https://youtu.be/9bZkp7q19f0'
        with closing(database.get_conn()) as conn, conn:
            conn.execute('INSERT INTO published_history(source_post_id,source_url,channel_id,content_key,published_url) '
                         'VALUES(?,?,7,?,?)', (*self.item.values(), old_url))
        self.complete()
        self.assertTrue(self.observe())
        self.assertEqual([row['published_url'] for row in self.history()], [old_url])

    def test_legacy_null_history_is_preserved_without_new_rows(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO published_history(source_post_id,channel_id) VALUES('LEGACY',NULL)")
        self.complete()
        before = self.history()
        self.assertTrue(self.observe())
        self.assertEqual(self.history(), before)

    def test_lost_claim_or_failed_history_commit_cannot_report_success(self):
        with self.assertRaises(RuntimeError):
            pm._complete_upload_attempt(901, 'other-worker', self.attempt_id, {'id': 7}, self.url, [],
                                        backend='playwright', expected_metadata={})
        with closing(database.get_conn()) as conn, conn:
            conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON published_history "
                         "BEGIN SELECT RAISE(ABORT, 'fixture history failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete()
        self.assertFalse(self.observe())
        self.assertEqual(self.history(), [])
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT outcome FROM upload_attempts').fetchone()[0], 'InProgress')
            self.assertEqual(conn.execute('SELECT state FROM content_reservations').fetchone()[0], 'Active')


if __name__ == '__main__':
    unittest.main()
