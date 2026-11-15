"""Read-only G17 audits use disposable SQLite; no app, providers or runtime DB."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import audit_upload_capabilities as audit_tool


SCHEMA = """
CREATE TABLE upload_option_groups (
 id INTEGER PRIMARY KEY, name TEXT, language TEXT, disable_shorts_remix INTEGER,
 category_id TEXT, pin_comment_enabled INTEGER, affiliate_url TEXT);
CREATE TABLE youtube_channels (
 id INTEGER PRIMARY KEY, name TEXT, upload_mode TEXT, default_category_id TEXT,
 group_name TEXT, profile_name TEXT, oauth_token_json TEXT, cookie TEXT, proxy TEXT);
CREATE TABLE video_queue (
 id INTEGER PRIMARY KEY, status TEXT, target_type TEXT, target_channel_id INTEGER,
 target_group_name TEXT, target_group_names_json TEXT, group_schema_version INTEGER,
 target_profile_name TEXT, assigned_channel_id INTEGER, option_group_id INTEGER,
 age_restriction INTEGER DEFAULT 0, is_premiere INTEGER DEFAULT 0,
 source_url TEXT, input_video_path TEXT);
CREATE TABLE monitors (
 id INTEGER PRIMARY KEY, name TEXT, target_channel_id INTEGER, target_group_name TEXT,
 option_group_id INTEGER, is_running INTEGER DEFAULT 0, source_urls TEXT);
CREATE TABLE channel_groups (name TEXT PRIMARY KEY);
CREATE TABLE channel_group_memberships (group_name TEXT, channel_id INTEGER, sort_order INTEGER);
CREATE TABLE app_schema_versions (schema_key TEXT PRIMARY KEY, version INTEGER);
"""


class UploadCapabilityAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'private-database.sqlite'
        self.secret = 'PRIVATE_SENTINEL_DO_NOT_OUTPUT'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript(SCHEMA)
            conn.executemany('INSERT INTO upload_option_groups VALUES(?,?,?,?,?,?,?)', [
                (1, self.secret, 'vi', 0, '22', 0, self.secret),
                (2, self.secret, 'vi', 1, '24', 0, self.secret),
                (3, self.secret, 'en-US', 0, '26', 1, self.secret),
            ])
            conn.executemany('INSERT INTO youtube_channels VALUES(?,?,?,?,?,?,?,?,?)', [
                (1, self.secret, 'playwright', '22', 'group', self.secret, self.secret, self.secret, self.secret),
                (2, self.secret, 'youtube_api', '24', 'group', self.secret, self.secret, self.secret, self.secret),
            ])
            conn.execute("INSERT INTO channel_groups VALUES('group')")
            conn.executemany("INSERT INTO channel_group_memberships VALUES('group',?,?)", [(1, 0), (2, 1)])
            conn.executemany('INSERT INTO app_schema_versions VALUES(?,?)', [('review_metadata', 1), (self.secret, 7)])
        self.queue(1, channel=1, options=2)
        self.queue(2, channel=2, options=2, status='Completed')
        self.queue(3, channel=1, options=1, age=1)
        self.queue(4, channel=2, options=1)
        self.queue(5, channel=1, options=None)
        self.queue(6, channel=999, options=999)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO monitors VALUES(1,?,NULL,?,2,1,?)', (self.secret, 'group', self.secret))
            conn.execute('INSERT INTO monitors VALUES(2,?,2,NULL,1,0,?)', (self.secret, self.secret))

    def queue(self, identifier, *, channel=None, options=None, status='Pending', age=0, group=None, assigned=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('''INSERT INTO video_queue(id,status,target_type,target_channel_id,target_group_name,
                target_group_names_json,group_schema_version,assigned_channel_id,option_group_id,
                age_restriction,source_url,input_video_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier, status, 'GROUP' if group else 'CHANNEL', channel, group,
                 json.dumps([group]) if group else '', 1, assigned, options, age, self.secret, self.secret))

    def test_aggregate_counts_and_read_only_bytes_and_schema(self):
        before = self.db.read_bytes()
        with closing(sqlite3.connect(self.db)) as conn:
            schema = conn.execute('SELECT * FROM sqlite_master').fetchall()
        report = audit_tool.audit(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM sqlite_master').fetchall(), schema)
        self.assertEqual(report['options']['language'], {'legacy_vi': 2, 'other_valid_tag': 1})
        self.assertEqual(report['options']['restrictive_remix'], 1)
        self.assertEqual(report['options']['remix_zero_unconfirmed'], 2)
        self.assertEqual(report['channels']['backend'], {'playwright': 1, 'youtube_api': 1})
        self.assertEqual(report['queue']['total'], 6)
        self.assertEqual(report['queue']['with_legacy_vi'], 4)
        self.assertEqual(report['queue']['new_g17_guard_affected'], 3)
        self.assertEqual(report['queue']['new_g17_guard_affected_nonterminal'], 2)
        self.assertEqual(report['queue']['missing_options'], 1)
        self.assertEqual(report['queue']['unresolved_destination'], 1)
        self.assertEqual(report['schema']['unrecognized_version_entries'], 1)
        self.assertNotIn(self.secret, json.dumps(report))
        self.assertNotIn(str(self.db), json.dumps(report))

    def test_monitor_counts_entity_once_and_checks_all_candidates(self):
        report = audit_tool.audit(self.db)['monitors']
        self.assertEqual(report['total'], 2)
        self.assertEqual(report['candidate_evaluations'], 3)
        self.assertEqual(report['mixed_backends'], 1)
        self.assertEqual(report['backend'], {'playwright': 1, 'youtube_api': 2})
        self.assertEqual(report['new_g17_guard_affected'], 1)
        self.assertEqual(report['guard_affected_by_code']['UPLOAD_REMIX_UNSUPPORTED'], 1)

    def test_queue_uses_frozen_destination_and_rejects_stale_assignment(self):
        self.queue(7, group='group', assigned=1, age=1)
        self.queue(8, group='group', assigned=2, age=1)
        self.queue(9, group='group', assigned=999, age=1)
        report = audit_tool.audit(self.db)['queue']
        # Both backends block age; only Studio rejection has moved earlier in G17.
        self.assertEqual(report['new_g17_guard_affected'], 4)
        self.assertEqual(report['guard_affected'], 5)
        self.assertEqual(report['unresolved_destination'], 2)

    def test_studio_premiere_is_new_guard_but_api_premiere_is_existing(self):
        self.queue(7, channel=1, options=1)
        self.queue(8, channel=2, options=1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE video_queue SET is_premiere=1 WHERE id IN (7,8)')
        report = audit_tool.audit(self.db)['queue']
        self.assertEqual(report['guard_affected_by_code']['UPLOAD_PREMIERE_UNSUPPORTED'], 2)
        self.assertEqual(report['new_g17_guard_affected'], 4)
        self.assertEqual(report['new_g17_guard_affected_nonterminal'], 3)

    def test_unknown_values_and_invalid_flags_do_not_leak_or_abort(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE youtube_channels SET upload_mode=? WHERE id=2', (self.secret,))
            conn.execute('UPDATE upload_option_groups SET language=?,category_id=?,disable_shorts_remix=? WHERE id=2',
                         (self.secret, self.secret, self.secret))
            conn.execute('UPDATE video_queue SET status=? WHERE id=3', (self.secret,))
        report = audit_tool.audit(self.db)
        self.assertEqual(report['channels']['backend']['invalid'], 1)
        self.assertEqual(report['options']['language']['invalid'], 1)
        self.assertEqual(report['options']['invalid_flags'], {'disable_shorts_remix': 1})
        self.assertIn('UPLOAD_METADATA_FLAG_INVALID', report['queue']['guard_affected_by_code'])
        self.assertIn('UPLOAD_BACKEND_UNSUPPORTED', report['queue']['guard_affected_by_code'])
        self.assertEqual(report['queue']['by_status']['Other'], 1)
        self.assertNotIn(self.secret, json.dumps(report))

    def test_missing_database_is_not_created(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaisesRegex(audit_tool.AuditError, '^database_unavailable$'):
            audit_tool.audit(missing)
        self.assertFalse(missing.exists())

    def test_invalid_database_and_schema_have_sanitized_failures(self):
        invalid = self.root / 'invalid.sqlite'
        invalid.write_text(self.secret)
        with self.assertRaisesRegex(audit_tool.AuditError, '^database_read_failed$'):
            audit_tool.audit(invalid)
        unsupported = self.root / 'unsupported.sqlite'
        with closing(sqlite3.connect(unsupported)) as conn:
            conn.execute('CREATE TABLE secret(value TEXT)')
        before = unsupported.read_bytes()
        with self.assertRaisesRegex(audit_tool.AuditError, '^unsupported_schema$'):
            audit_tool.audit(unsupported)
        self.assertEqual(unsupported.read_bytes(), before)

    def test_database_lock_fails_closed_without_repair(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('BEGIN EXCLUSIVE')
            with self.assertRaisesRegex(audit_tool.AuditError, '^database_read_failed$'):
                audit_tool.audit(self.db)
            conn.rollback()
        self.assertEqual(audit_tool.audit(self.db)['queue']['total'], 6)

    def test_audit_sees_wal_commits_without_checkpointing(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('UPDATE upload_option_groups SET disable_shorts_remix=1 WHERE id=1')
            writer.commit()
            db_before = self.db.read_bytes()
            wal = Path(str(self.db) + '-wal')
            wal_before = wal.read_bytes()
            self.assertEqual(audit_tool.audit(self.db)['options']['restrictive_remix'], 2)
            self.assertEqual(self.db.read_bytes(), db_before)
            self.assertEqual(wal.read_bytes(), wal_before)

    def test_cli_refuses_database_and_sidecar_output(self):
        before = self.db.read_bytes()
        for suffix in ('', '-wal', '-shm', '-journal'):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = audit_tool.main(['--db', str(self.db), '--output', str(self.db) + suffix])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(stderr.getvalue())['error'], 'output_must_not_replace_database')
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_report_and_failure_are_machine_readable_without_paths(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(audit_tool.main(['--db', str(self.db)]), 0)
        self.assertTrue(json.loads(stdout.getvalue())['read_only'])
        with redirect_stderr(stderr):
            self.assertEqual(audit_tool.main(['--db', str(self.root / self.secret)]), 2)
        self.assertEqual(json.loads(stderr.getvalue()), {'error': 'database_unavailable'})
        self.assertNotIn(self.secret, stdout.getvalue() + stderr.getvalue())

    def test_cli_refuses_hardlinked_database_output(self):
        alias = self.root / 'report.json'
        try:
            os.link(self.db, alias)
        except OSError as exc:
            self.skipTest(f'Hardlinks unavailable: {type(exc).__name__}')
        before = self.db.read_bytes()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(audit_tool.main(['--db', str(self.db), '--output', str(alias)]), 2)
        self.assertEqual(self.db.read_bytes(), before)

    def test_manifest_does_not_execute_app_or_guess_tool_versions(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'app.py').write_text('APP_BUILD_VERSION = "2026.11.15-crud"\nraise RuntimeError("must never import")\n')
        (source / 'requirements.txt').write_text('example-lib==1.2.3\n')
        with mock.patch.object(audit_tool, '_git', side_effect=['a' * 40, '']), \
             mock.patch.object(audit_tool.packages, 'version', return_value='1.2.3'):
            manifest = audit_tool.environment_manifest(source, observed_tools={'browser': '123.0.1'})
        self.assertEqual(manifest['build'], '2026.11.15-crud')
        self.assertEqual(manifest['revision'], 'a' * 40)
        self.assertFalse(manifest['checkout_dirty'])
        self.assertTrue(manifest['dependencies']['example-lib']['matches_pin'])
        self.assertEqual(manifest['tools']['browser']['evidence'], 'operator_supplied')
        self.assertEqual(manifest['tools']['ffmpeg'], {'version': None, 'evidence': 'not_observed'})
        self.assertNotIn(str(source), json.dumps(manifest))


if __name__ == '__main__':
    unittest.main()
