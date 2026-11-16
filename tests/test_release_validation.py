import shutil
import sqlite3
import unittest
import threading
import time
from contextlib import closing
from unittest import mock

import database
import pipeline_manager as pm
import test_review_regressions as fixtures
from audit_review_data import audit
from validate_review_release import backup, rehearse, snapshot, verify_transition


class ReleaseValidationTests(unittest.TestCase):
    setUp=fixtures.ReviewRegressions.setUp
    row=fixtures.ReviewRegressions.row
    reserve_review=fixtures.ReviewRegressions.reserve_review

    def test_migration_and_restore_preserve_review_evidence(self):
        self.reserve_review('Rendered')
        # Seed rows through the existing group normalization before this release's
        # migration rehearsal (the production baseline already has that migration).
        database.init_db()
        before=self.row()
        report=rehearse(database.APP_DB_PATH)
        self.assertTrue(report['restore_verified'])
        self.assertEqual(self.row(),before)
        self.assertEqual(audit(database.APP_DB_PATH)['review_holds'],[{'id':901,'status':'Rendered'}])

    def test_rehearsal_compares_all_old_fields_and_the_three_additive_columns(self):
        self.reserve_review('Rendered')
        database.init_db()
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DROP TRIGGER upload_approval_guard_channel_metadata_intent')
            conn.execute('DROP TRIGGER upload_approval_guard_youtube_channels_update')
            conn.execute('ALTER TABLE youtube_channels DROP COLUMN studio_category_mode')
            conn.execute('ALTER TABLE upload_option_groups DROP COLUMN metadata_intent_json')
            conn.execute('ALTER TABLE upload_attempts DROP COLUMN evidence_json')
            conn.execute("DELETE FROM app_schema_versions WHERE schema_key IN ('metadata_intent','upload_evidence')")
            conn.execute("UPDATE app_schema_versions SET updated_at='2026-01-01 00:00:00'")
        before = snapshot(database.APP_DB_PATH)
        report = rehearse(database.APP_DB_PATH)
        self.assertEqual(len(report['counts']), 19)
        self.assertEqual(report['changes'], {'youtube_channels': ['studio_category_mode'],
                         'upload_option_groups': ['metadata_intent_json'],
                         'upload_attempts': ['evidence_json'], 'logs_appended': 0})
        self.assertEqual(snapshot(database.APP_DB_PATH), before)

    def test_comparison_rejects_removal_or_mutation_of_guarded_data(self):
        database.add_log('System', 'Retain this startup history', 'INFO')
        before = snapshot(database.APP_DB_PATH)
        for table, statement in (
            ('app_settings', "UPDATE app_settings SET value='changed' WHERE key='openai_api_key'"),
            ('upload_slot', "UPDATE upload_slot SET cooldown_until=10"),
            ('system_logs', "DELETE FROM system_logs"),
            ('app_schema_versions', "UPDATE app_schema_versions SET updated_at='2026-01-01 00:00:00' "
                                    "WHERE schema_key='channel_group_memberships'"),
        ):
            with self.subTest(table=table):
                target = database.APP_DB_PATH.with_name(table + '.sqlite')
                backup(database.APP_DB_PATH, target)
                with closing(sqlite3.connect(target)) as conn, conn:
                    conn.execute(statement)
                with self.assertRaises(RuntimeError):
                    verify_transition(before, snapshot(target), startup=True)

    def test_backup_fails_cleanly_for_lock_or_insufficient_space(self):
        source = database.APP_DB_PATH.with_name('locked.sqlite')
        target = source.with_name('backup.sqlite')
        with closing(sqlite3.connect(source, isolation_level=None)) as conn:
            conn.execute('CREATE TABLE data (id INTEGER PRIMARY KEY)')
            conn.execute('BEGIN EXCLUSIVE')
            with self.assertRaises((sqlite3.OperationalError, TimeoutError)):
                backup(source, target, timeout=.2)
            self.assertFalse(target.exists())
            conn.rollback()
        space = shutil.disk_usage(source.parent)._replace(free=0)
        with mock.patch('validate_review_release.shutil.disk_usage', return_value=space):
            with self.assertRaisesRegex(OSError, 'Insufficient space'):
                backup(source, target)
        self.assertFalse(target.exists())

    def test_partial_migration_aborts_without_stamp(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute('ALTER TABLE video_queue DROP COLUMN post_upload_warnings_json')
            conn.execute("DELETE FROM app_schema_versions WHERE schema_key='review_metadata'")
        original=database.get_conn
        def denied_connection():
            conn=original()
            conn.set_authorizer(lambda action,*_: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
            return conn
        with mock.patch.object(database,'get_conn',side_effect=denied_connection):
            with self.assertRaisesRegex(RuntimeError,'Migration review'):
                database.init_db()
        with closing(database.get_conn()) as conn:
            self.assertIsNone(conn.execute("SELECT version FROM app_schema_versions WHERE schema_key='review_metadata'").fetchone())
        database.init_db()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT version FROM app_schema_versions WHERE schema_key='review_metadata'").fetchone()[0],1)

    def test_newer_schema_is_not_downgraded(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE app_schema_versions SET version=99 WHERE schema_key='review_metadata'")
        with self.assertRaisesRegex(RuntimeError,'mới hơn'):
            database.init_db()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT version FROM app_schema_versions WHERE schema_key='review_metadata'").fetchone()[0],99)

    def test_shared_source_finishes_on_two_channels_across_restart(self):
        options=[False,False,True]
        before=self.source.read_bytes()
        for index,item_id in enumerate((901,902)):
            # Simulate startup's full monotonic cooldown, then advance both clocks.
            with closing(database.get_conn()) as conn:
                until=conn.execute('SELECT cooldown_until FROM upload_slot WHERE id=1').fetchone()[0]
            anchor=(until,time.monotonic()+pm.UPLOAD_MIN_GAP_SECONDS) if until else None
            with mock.patch.object(pm.time,'time',return_value=time.time()+index*3600), \
                 mock.patch.object(pm.time,'monotonic',return_value=time.monotonic()+index*3600), \
                 mock.patch.object(pm,'_COOLDOWN_ANCHOR',anchor), \
                 mock.patch.object(pm,'upload_video_to_youtube',return_value={'url':'https://youtu.be/dQw4w9WgXcQ'}):
                manager=pm.PipelineManager()
                preview=manager.preview_distribution([item_id],options)
                self.assertFalse(preview['items'][0]['blocked'],preview)
                token=f'shared-{item_id}'
                manager._freeze_batch_assignments([item_id],preview['preview_token'],options,token)
                stop=threading.Event()
                deadline=threading.Timer(5,stop.set)
                deadline.start()
                try:
                    manager._process_single_item_safe(item_id,0,*options,threading.Semaphore(0),token,stop_event=stop)
                finally:
                    deadline.cancel()
            self.assertEqual(self.row(item_id)['status'],'Completed')
            self.assertEqual(self.source.read_bytes(),before)
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT channel_id) FROM published_history').fetchone()[0],2)
