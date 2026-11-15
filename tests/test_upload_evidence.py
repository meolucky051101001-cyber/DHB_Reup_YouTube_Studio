"""G19 durable evidence tests on isolated SQLite state."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import upload_evidence as evidence
import upload_evidence_schema as schema


DDL = """
CREATE TABLE app_schema_versions(schema_key TEXT PRIMARY KEY, version INTEGER,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE upload_attempts(id INTEGER PRIMARY KEY, queue_id INTEGER, attempt_no INTEGER,
 channel_id INTEGER, content_key TEXT, outcome TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(queue_id,attempt_no));
CREATE TABLE video_queue(id INTEGER PRIMARY KEY, claim_token TEXT, last_attempt_id INTEGER,
 content_key TEXT, status TEXT);
CREATE TABLE content_reservations(content_key TEXT, channel_id INTEGER, queue_id INTEGER,
 claim_token TEXT, state TEXT, PRIMARY KEY(content_key,channel_id));
"""


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'evidence.sqlite'
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(DDL)
        self.conn.execute("INSERT INTO video_queue VALUES(1,'claim-a',10,'sha256:content','Uploading')")
        self.conn.execute("INSERT INTO upload_attempts(id,queue_id,attempt_no,channel_id,content_key,outcome) VALUES(10,1,1,7,'sha256:content','InProgress')")
        self.conn.execute("INSERT INTO content_reservations VALUES('sha256:content',7,1,'claim-a','Active')")
        self.conn.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=0.05)

    def sink(self):
        return evidence.make_evidence_sink(self.connect, 1, 'claim-a', 10, 7,
                                           backend='youtube_api', expected={'category_id': '22'})

    def event(self, state='received', **extra):
        value = {'backend': 'youtube_api', 'video_id': 'abc123XYZ_0',
                 'verification_state': state, 'expected': {'category_id': '22'}}
        value.update(extra)
        return value

    def stored(self):
        return json.loads(self.conn.execute('SELECT evidence_json FROM upload_attempts WHERE id=10').fetchone()[0])

    def test_received_persists_before_readback_and_is_idempotent(self):
        first = self.sink()(self.event())
        self.assertEqual(first['video_url'], 'https://youtu.be/abc123XYZ_0')
        self.assertEqual(self.stored()['video_id'], 'abc123XYZ_0')
        again = self.sink()(self.event())
        self.assertEqual(again['video_id'], first['video_id'])
        self.assertEqual(self.stored(), first)
        self.assertEqual((first['channel_id'], first['queue_id'], first['attempt_id']), (7, 1, 10))
        # Simulate losing all adapter memory after receipt, then a fresh process/read connection.
        with closing(self.connect()) as reopened:
            persisted = json.loads(reopened.execute('SELECT evidence_json FROM upload_attempts WHERE id=10').fetchone()[0])
        self.assertEqual(persisted, first)

    def test_public_read_validates_expected_row_and_empty_sentinel(self):
        self.assertIsNone(evidence.read_evidence('{}', 1, 10, 7))
        first = self.sink()(self.event())
        raw = json.dumps(first)
        self.assertEqual(evidence.read_evidence(raw, 1, 10, 7), first)
        for ids in ((2, 10, 7), (1, 11, 7), (1, 10, 8)):
            with self.subTest(ids=ids):
                with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_corrupt$'):
                    evidence.read_evidence(raw, *ids)

    def test_public_read_rejects_noninteger_context_and_stored_ids(self):
        first = self.sink()(self.event())
        for value in (True, False, '1', 1.0, None, 0, -1, [], {}):
            for index, key in enumerate(('queue_id', 'attempt_id', 'channel_id')):
                with self.subTest(key=key, value=value):
                    ids = [1, 10, 7]
                    ids[index] = value
                    with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_context_invalid$'):
                        evidence.read_evidence('{}', *ids)
                    raw = json.dumps({**first, key: value})
                    with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_corrupt$'):
                        evidence.read_evidence(raw, 1, 10, 7)

    def test_public_read_rejects_malformed_duplicate_and_foreign_fields_sanitized(self):
        first = self.sink()(self.event())
        secret = 'PRIVATE_READER_SECRET'
        raw = json.dumps(first)
        cases = [None, {}, '', 'null', '[]', '{broken', secret,
                 json.dumps({**first, 'token': secret}),
                 json.dumps({**first, 'video_url': 'https://example.test/' + secret}),
                 json.dumps({**first, 'expected': {'category_id': '22', 'token': secret}}),
                 json.dumps({**first, 'received_at': secret}),
                 raw.replace('"version": 1', '"version": 1,"version": 1'),
                 raw.replace('"category_id": "22"', '"category_id": "22","category_id": "22"'),
                 '\ud800', '[' * 1200 + ']' * 1200, 'x' * (evidence.MAX_JSON + 1)]
        for invalid in cases:
            with self.subTest(value=repr(invalid)[:50]):
                with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_corrupt$') as raised:
                    evidence.read_evidence(invalid, 1, 10, 7)
                self.assertNotIn(secret, str(raised.exception))

    def test_verified_mismatch_unknown_transitions_are_monotonic(self):
        self.sink()(self.event())
        verified = self.sink()(self.event('verified', observed={'category_id': '22'}))
        self.assertEqual(verified['verification_state'], 'verified')
        for state in ('received', 'mismatch', 'unknown'):
            with self.subTest(state=state):
                with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_terminal_transition'):
                    self.sink()(self.event(state, observed={'category_id': '24'} if state != 'received' else None))
        self.assertEqual(self.stored()['verification_state'], 'verified')

    def test_mismatch_expected_observed_and_explicit_code_are_retained(self):
        self.sink()(self.event())
        result = self.sink()(self.event('mismatch', observed={'category_id': '24'}, code='CATEGORY_READBACK_MISMATCH'))
        self.assertEqual(result['code'], 'CATEGORY_READBACK_MISMATCH')
        self.assertEqual(result['observed'], {'category_id': '24'})
        self.assertEqual(self.sink()(self.event('mismatch', observed={'category_id': '24'}, code='CATEGORY_READBACK_MISMATCH')), result)
        with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_terminal_conflict'):
            self.sink()(self.event('mismatch', observed={'category_id': '26'}, code='CATEGORY_READBACK_MISMATCH'))

    def test_terminal_state_cannot_be_first_or_claim_verification_without_matching_values(self):
        for state in ('verified', 'mismatch', 'unknown'):
            with self.subTest(state=state):
                with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_received_required'):
                    self.sink()(self.event(state, observed={'category_id': '22'}))
        self.sink()(self.event())
        with self.assertRaisesRegex(evidence.EvidenceError, 'verified_metadata_mismatch'):
            self.sink()(self.event('verified', observed={'category_id': '24'}))
        unknown = self.sink()(self.event('unknown', code='READBACK_TIMEOUT'))
        self.assertEqual(unknown['verification_state'], 'unknown')
        self.assertEqual(unknown['observed'], {})

    def test_different_id_backend_or_expected_cannot_overwrite(self):
        self.sink()(self.event())
        cases = [
            {'video_id': 'differentID'},
            {'backend': 'playwright'},
            {'expected': {'category_id': '24'}},
        ]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_identity_conflict'):
                    self.sink()(self.event(**change))
        self.assertEqual(self.stored()['video_id'], 'abc123XYZ_0')

    def test_first_event_cannot_replace_approved_backend_or_expected_metadata(self):
        cases = [{'backend': 'playwright'}, {'expected': {'category_id': '24'}},
                 {'expected': {'category_id': '22', 'video_language': 'en-US'}}]
        connect = mock.Mock(wraps=self.connect)
        sink = evidence.make_evidence_sink(connect, 1, 'claim-a', 10, 7,
                                           backend='youtube_api', expected={'category_id': '22'})
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_identity_conflict$'):
                    sink(self.event(**change))
                self.assertEqual(self.stored(), {})
        connect.assert_not_called()

    def test_approved_metadata_is_copied_and_language_cannot_be_dropped(self):
        approved = {'category_id': '22', 'video_language': 'en-US'}
        sink = evidence.make_evidence_sink(self.connect, 1, 'claim-a', 10, 7,
                                           backend='youtube_api', expected=approved)
        approved['category_id'] = '24'
        approved.pop('video_language')
        for expectation in (approved, {'category_id': '22'}):
            with self.subTest(expected=expectation):
                with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_identity_conflict$'):
                    sink(self.event(expected=expectation))
        result = sink(self.event(expected={'category_id': '22', 'video_language': 'en-US'}))
        self.assertEqual(result['expected'], {'category_id': '22', 'video_language': 'en-US'})

    def test_invalid_approved_backend_and_metadata_are_rejected_before_connect(self):
        connect = mock.Mock(wraps=self.connect)
        for backend in (None, [], {}, True, 'other', 'YouTube_API'):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(evidence.EvidenceError, '^sink_context_invalid$'):
                    evidence.make_evidence_sink(connect, 1, 'claim-a', 10, 7,
                                                backend=backend, expected={'category_id': '22'})
        for expected in (None, [], {}, {'category_id': 22}, {'category_id': '22', 'token': 'PRIVATE'}):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(evidence.EvidenceError, '^sink_context_invalid$'):
                    evidence.make_evidence_sink(connect, 1, 'claim-a', 10, 7,
                                                backend='youtube_api', expected=expected)
        connect.assert_not_called()
        self.assertEqual(self.stored(), {})

    def test_stale_claim_attempt_and_reservation_are_rejected_without_write(self):
        self.sink()(self.event())
        before = self.stored()
        for field, value, code in [('claim_token', 'claim-b', 'evidence_ownership_lost'),
                                   ('last_attempt_id', 99, 'evidence_ownership_lost'),
                                   ('outcome', 'Finished', 'evidence_ownership_lost')]:
            with self.subTest(field=field):
                self.conn.execute(f'UPDATE video_queue SET {field}=? WHERE id=1' if field != 'outcome'
                                  else 'UPDATE upload_attempts SET outcome=? WHERE id=10', (value,))
                self.conn.commit()
                with self.assertRaisesRegex(evidence.EvidenceError, code):
                    self.sink()(self.event('verified', observed={'category_id': '22'}))
                self.conn.execute("UPDATE video_queue SET claim_token='claim-a',last_attempt_id=10 WHERE id=1")
                self.conn.execute("UPDATE upload_attempts SET outcome='InProgress' WHERE id=10")
                self.conn.commit()
        self.conn.execute("UPDATE content_reservations SET state='NeedsReview' WHERE queue_id=1")
        self.conn.commit()
        with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_ownership_lost'):
            self.sink()(self.event('verified', observed={'category_id': '22'}))
        self.assertEqual(self.stored(), before)

    def test_invalid_and_secret_fields_are_rejected_without_leaking(self):
        secret = 'OAUTH_COOKIE_PROXY_SECRET'
        cases = [
            self.event(video_id=secret),
            self.event(expected={'category_id': '22', 'secret': secret}),
            self.event(code='Bearer ' + secret),
            {**self.event(), 'secret': secret},
            {**self.event(), 'channel_id': 999},
            {**self.event(), 'video_url': 'https://example.test/' + secret},
            self.event(observed={'video_language': secret}),
            self.event(backend=[]),
            self.event(verification_state={}),
            self.event('received', observed={'category_id': '22'}),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(evidence.EvidenceError) as raised:
                    self.sink()(value)
                self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(self.stored(), {})

    def test_wrong_content_channel_attempt_and_reservation_owner_cannot_write(self):
        changes = [('upload_attempts', 'queue_id', 2), ('upload_attempts', 'channel_id', 8),
                   ('upload_attempts', 'content_key', 'different'), ('video_queue', 'content_key', 'different'),
                   ('content_reservations', 'claim_token', 'claim-b'), ('content_reservations', 'queue_id', 2),
                   ('content_reservations', 'channel_id', 8), ('content_reservations', 'content_key', 'different')]
        for table, column, value in changes:
            with self.subTest(table=table, column=column):
                before = self.conn.execute(f'SELECT {column} FROM {table}').fetchone()[0]
                self.conn.execute(f'UPDATE {table} SET {column}=?', (value,))
                self.conn.commit()
                with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_ownership_lost'):
                    self.sink()(self.event())
                self.assertEqual(self.stored(), {})
                self.conn.execute(f'UPDATE {table} SET {column}=?', (before,))
                self.conn.commit()

    def test_corrupt_existing_evidence_is_not_replaced(self):
        for raw in ('null', '[]', '{broken', '{"version":99}', 'x' * (evidence.MAX_JSON + 1)):
            with self.subTest(raw=raw[:30]):
                self.conn.execute('UPDATE upload_attempts SET evidence_json=? WHERE id=10', (raw,))
                self.conn.commit()
                with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_corrupt'):
                    self.sink()(self.event())
                self.assertEqual(self.conn.execute('SELECT evidence_json FROM upload_attempts WHERE id=10').fetchone()[0], raw)

    def test_write_failure_and_database_lock_preserve_existing_evidence(self):
        first = self.sink()(self.event())
        self.conn.execute("""CREATE TRIGGER reject_evidence BEFORE UPDATE ON upload_attempts
            BEGIN SELECT RAISE(ABORT,'PRIVATE_SQL_ERROR'); END""")
        self.conn.commit()
        with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_write_failed$'):
            self.sink()(self.event('verified', observed={'category_id': '22'}))
        self.assertEqual(self.stored(), first)
        self.conn.execute('DROP TRIGGER reject_evidence')
        self.conn.commit()
        self.conn.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_write_failed$'):
                self.sink()(self.event('verified', observed={'category_id': '22'}))
        finally:
            self.conn.rollback()
        self.assertEqual(self.stored(), first)

    def test_connection_failure_is_sanitized_and_full_durability_precedes_transaction(self):
        with self.assertRaisesRegex(evidence.EvidenceError, '^evidence_write_failed$'):
            evidence.make_evidence_sink(mock.Mock(side_effect=OSError('PRIVATE_CONNECT_ERROR')), 1, 'claim-a', 10, 7,
                                        backend='youtube_api', expected={'category_id': '22'})(self.event())
        statements = []
        def traced_connect():
            connection = self.connect()
            connection.set_trace_callback(statements.append)
            return connection
        evidence.make_evidence_sink(traced_connect, 1, 'claim-a', 10, 7,
                                    backend='youtube_api', expected={'category_id': '22'})(self.event())
        self.assertLess(statements.index('PRAGMA synchronous=FULL'), statements.index('BEGIN IMMEDIATE'))
        self.assertIn('COMMIT', statements)

    def test_bounds_and_context_are_fail_closed(self):
        with self.assertRaisesRegex(evidence.EvidenceError, 'sink_context_invalid'):
            evidence.make_evidence_sink(self.connect, 1, '', 10, 7,
                                        backend='youtube_api', expected={'category_id': '22'})
        huge = self.event(code='A' * 64)
        with mock.patch.object(evidence, 'MAX_JSON', 20):
            with self.assertRaisesRegex(evidence.EvidenceError, 'evidence_too_large'):
                self.sink()(huge)
        self.assertEqual(self.stored(), {})


class EvidenceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE app_schema_versions(schema_key TEXT PRIMARY KEY, version INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE upload_attempts(id INTEGER PRIMARY KEY, outcome TEXT);
            INSERT INTO upload_attempts VALUES(1,'InProgress');
        ''')

    def test_migration_preserves_attempts_and_is_idempotent(self):
        before = self.conn.execute('SELECT * FROM upload_attempts').fetchall()
        schema.migrate_upload_evidence_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT id,outcome FROM upload_attempts').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT evidence_json FROM upload_attempts').fetchone()[0], '{}')
        first = list(self.conn.iterdump())
        schema.migrate_upload_evidence_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), first)

    def test_failure_after_alter_rolls_back_column_and_stamp(self):
        before = list(self.conn.iterdump())
        with mock.patch.object(schema, '_stamp', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                schema.migrate_upload_evidence_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_migration_respects_outer_rollback_and_rejects_incomplete_or_wrong_column(self):
        before = list(self.conn.iterdump())
        self.conn.execute('BEGIN')
        schema.migrate_upload_evidence_schema(self.conn)
        self.conn.rollback()
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("INSERT INTO app_schema_versions(schema_key,version) VALUES('upload_evidence',1)")
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, 'thiếu cột'):
            schema.migrate_upload_evidence_schema(self.conn)
        self.conn.execute("DELETE FROM app_schema_versions WHERE schema_key='upload_evidence'")
        self.conn.execute("ALTER TABLE upload_attempts ADD COLUMN evidence_json INTEGER DEFAULT 0")
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, 'không đúng hợp đồng'):
            schema.migrate_upload_evidence_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM app_schema_versions WHERE schema_key='upload_evidence'").fetchone()[0], 0)

    def test_future_schema_rejected_without_mutation(self):
        self.conn.execute("INSERT INTO app_schema_versions VALUES('upload_evidence',99,CURRENT_TIMESTAMP)")
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(RuntimeError, 'upload_evidence'):
            schema.migrate_upload_evidence_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
