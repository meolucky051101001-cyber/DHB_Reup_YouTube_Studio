"""G18 canonical intent, legacy compatibility and additive migration regressions."""
from contextlib import closing
import copy
import json
import sqlite3
import unittest
from unittest import mock

import database
import metadata_intent as intent
import metadata_intent_schema as schema
import test_config_validation as config_fixtures


def confirmed(*, category='inherit', language=None, remix='keep'):
    return {'version': 1, 'studio_category_mode': category,
            'video_language': {'mode': 'keep'} if language is None else {'mode': 'apply', 'value': language},
            'remix': remix}


class MetadataIntentParserTests(unittest.TestCase):
    def test_complete_object_round_trip_and_no_input_mutation(self):
        for value in ({}, confirmed(), confirmed(category='apply', language='en-US', remix='disable')):
            before = copy.deepcopy(value)
            self.assertEqual(intent.parse_intent(value), value)
            self.assertEqual(intent.parse_intent(intent.encode_intent(value)), value)
            self.assertEqual(value, before)

    def test_invalid_intent_is_strict_and_does_not_echo_values(self):
        secret = 'PRIVATE_PAYLOAD_SENTINEL'
        valid = confirmed()
        invalid = [None, '', 'null', '[]', [], False, 0, {secret: secret},
                   {'version': 1}, {**valid, 'extra': secret}, {**valid, 'version': True},
                   {**valid, 'version': 2}, {**valid, 'version': '1'},
                   {**valid, 'studio_category_mode': secret}, {**valid, 'studio_category_mode': []},
                   {**valid, 'remix': False}, {**valid, 'remix': secret},
                   {**valid, 'video_language': None},
                   {**valid, 'video_language': {'mode': 'keep', 'value': secret}},
                   {**valid, 'video_language': {'mode': 'apply'}},
                   {**valid, 'video_language': {'mode': 'apply', 'value': 'en-US', 'extra': secret}},
                   {**valid, 'video_language': {'mode': 'apply', 'value': ''}},
                   {**valid, 'video_language': {'mode': 'apply', 'value': secret}},
                   '{"version":1,"version":2}', 'x' * 4097]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    intent.parse_intent(value)
                self.assertNotIn(secret, str(raised.exception))


class MetadataIntentConfigTests(unittest.TestCase):
    setUp = config_fixtures.ConfigValidationTests.setUp
    request = config_fixtures.ConfigValidationTests.request
    row = config_fixtures.ConfigValidationTests.row
    snapshot = config_fixtures.ConfigValidationTests.snapshot
    create_option = config_fixtures.ConfigValidationTests.create_option

    def test_legacy_create_and_unrelated_edits_do_not_confirm_defaults(self):
        option_id = self.create_option()
        before = self.row('upload_option_groups', option_id)
        self.assertEqual(before['metadata_intent_json'], '{}')
        self.assertEqual((before['language'], before['disable_shorts_remix']), ('vi', 0))
        response = self.request('PUT', f'/api/options/{option_id}', {'name': 'Renamed'})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.row('upload_option_groups', option_id)
        self.assertEqual({**before, 'name': 'Renamed'}, after)
        response = self.request('PUT', f'/api/options/{option_id}', {'language': 'en-US', 'disable_shorts_remix': 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row('upload_option_groups', option_id)['metadata_intent_json'], '{}')
        response = self.request('POST', '/api/channels', {'name': 'Legacy default channel'})
        self.assertEqual(response.status_code, 200, response.text)
        with closing(database.get_conn()) as conn:
            row = conn.execute("SELECT studio_category_mode FROM youtube_channels WHERE name='Legacy default channel'").fetchone()
        self.assertEqual(row[0], 'legacy_unconfirmed')

    def test_canonical_create_uses_supplied_fields_not_generated_legacy_defaults(self):
        canonical = confirmed(language='en-US', remix='disable')
        option_id = self.create_option(metadata_intent_json=canonical)
        row = self.row('upload_option_groups', option_id)
        self.assertEqual(intent.parse_intent(row['metadata_intent_json']), canonical)
        self.assertEqual((row['language'], row['disable_shorts_remix']), ('en-US', 1))
        response = self.client.get('/api/options', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        listed = next(row for row in response.json() if row['id'] == option_id)
        self.assertEqual(listed['metadata_intent_json'], intent.encode_intent(canonical))

    def test_conflicting_dual_create_is_atomic(self):
        for extra in ({'language': 'vi'}, {'disable_shorts_remix': 0}):
            with self.subTest(extra=extra):
                before = self.snapshot()
                response = self.request('POST', '/api/options',
                    {'name': 'Must not exist', 'metadata_intent_json': confirmed(language='en', remix='disable'), **extra})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.snapshot(), before)

    def test_old_clients_cannot_change_confirmed_fields_but_can_rename(self):
        option_id = self.create_option(metadata_intent_json=confirmed(language='en', remix='disable'))
        for change in ({'category_id': '24'}, {'language': 'vi'}, {'disable_shorts_remix': False}):
            before = self.snapshot()
            response = self.request('PUT', f'/api/options/{option_id}', {'name': 'Rejected rename', **change})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.snapshot(), before)
        row = self.row('upload_option_groups', option_id)
        response = self.request('PUT', f'/api/options/{option_id}',
            {'name': 'Allowed rename', 'language': 'en', 'disable_shorts_remix': 'true', 'category_id': 22})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row('upload_option_groups', option_id), {**row, 'name': 'Allowed rename'})

    def test_keep_language_preserves_value_and_remix_release_requires_explicit_intent(self):
        option_id = self.create_option(metadata_intent_json=confirmed(language='en-US', remix='disable'))
        response = self.request('PUT', f'/api/options/{option_id}', {'metadata_intent_json': confirmed()})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row('upload_option_groups', option_id)
        self.assertEqual((row['language'], row['disable_shorts_remix']), ('en-US', 0))
        self.assertEqual(intent.parse_intent(row['metadata_intent_json']), confirmed())
        response = self.request('PUT', f'/api/options/{option_id}', {'name': 'Name only'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row('upload_option_groups', option_id)['metadata_intent_json'], row['metadata_intent_json'])

    def test_canonical_change_and_mirror_commit_together_or_not_at_all(self):
        option_id = self.create_option(metadata_intent_json=confirmed(language='vi', remix='keep'))
        for values in ({'language': 'vi'}, {'disable_shorts_remix': 0}):
            before = self.snapshot()
            response = self.request('PUT', f'/api/options/{option_id}',
                {'name': 'Rejected', 'metadata_intent_json': confirmed(language='en', remix='disable'), **values})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.snapshot(), before)
        canonical = confirmed(category='apply', language='en', remix='disable')
        response = self.request('PUT', f'/api/options/{option_id}',
            {'category_id': '24', 'metadata_intent_json': canonical, 'language': 'en', 'disable_shorts_remix': 1})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row('upload_option_groups', option_id)
        self.assertEqual((row['category_id'], row['language'], row['disable_shorts_remix']), ('24', 'en', 1))
        self.assertEqual(intent.parse_intent(row['metadata_intent_json']), canonical)

    def test_canonical_cannot_be_reset_to_legacy_and_invalid_requests_are_sanitized(self):
        option_id = self.create_option(metadata_intent_json=confirmed(language='en', remix='disable'))
        for raw, expected in [({}, 409), ('{}', 409), (None, 400), ('', 400),
                              ({**confirmed(), 'private-secret': 'private-secret'}, 400)]:
            before = self.snapshot()
            response = self.request('PUT', f'/api/options/{option_id}', {'metadata_intent_json': raw, 'name': 'Rejected'})
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn('private-secret', response.text)
            self.assertEqual(self.snapshot(), before)

    def test_channel_mode_confirmation_legacy_client_and_note_preservation(self):
        response = self.request('PUT', '/api/channels/901', {'studio_category_mode': 'keep'})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.snapshot()
        response = self.request('PUT', '/api/channels/901', {'default_category_id': '24', 'note': 'Must not save'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        response = self.request('PUT', '/api/channels/901', {'default_category_id': '22', 'note': 'Allowed note'})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row('youtube_channels', 901)
        self.assertEqual((row['studio_category_mode'], row['note']), ('keep', 'Allowed note'))
        response = self.request('PUT', '/api/channels/901', {'default_category_id': '24', 'studio_category_mode': 'apply'})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.snapshot()
        response = self.request('PUT', '/api/channels/901', {'studio_category_mode': 'legacy_unconfirmed'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)

    def test_channel_create_and_get_expose_explicit_choice(self):
        response = self.request('POST', '/api/channels', {'name': 'Chosen metadata', 'studio_category_mode': 'keep'})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.get('/api/channels', headers=self.headers)
        row = next(row for row in response.json() if row['name'] == 'Chosen metadata')
        self.assertEqual(row['studio_category_mode'], 'keep')
        for value in (None, '', False, [], 'secret-value'):
            before = self.snapshot()
            response = self.request('PUT', '/api/channels/901', {'studio_category_mode': value, 'note': 'Must not save'})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn('secret-value', response.text)
            self.assertEqual(self.snapshot(), before)

    def test_busy_channel_mode_is_protected_in_http_and_direct_sql(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET studio_category_mode='keep' WHERE id=901")
            conn.execute("UPDATE video_queue SET assigned_channel_id=901,claim_token='active-fixture' WHERE id=901")
        before = self.snapshot()
        response = self.request('PUT', '/api/channels/901', {'studio_category_mode': 'apply'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        with closing(database.get_conn()) as conn, conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'UPLOAD_BINDING_BUSY'):
                conn.execute("UPDATE youtube_channels SET studio_category_mode='apply' WHERE id=901")

    def test_sql_failure_does_not_leave_mirrors_or_intent_partially_written(self):
        option_id = self.create_option()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_intent BEFORE UPDATE OF metadata_intent_json ON upload_option_groups
                BEGIN SELECT RAISE(ABORT,'INTENT_TEST_FAILURE'); END""")
        before = self.snapshot()
        response = self.request('PUT', f'/api/options/{option_id}',
            {'name': 'Rejected', 'metadata_intent_json': confirmed(language='en', remix='disable')})
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.snapshot(), before)

    def test_referenced_options_cannot_be_deleted_from_queue_or_monitor(self):
        for reference in ('queue', 'monitor'):
            with self.subTest(reference=reference):
                option_id = self.create_option(metadata_intent_json=confirmed(category='keep'))
                with closing(database.get_conn()) as conn, conn:
                    if reference == 'queue':
                        conn.execute('UPDATE video_queue SET option_group_id=? WHERE id=901', (option_id,))
                    else:
                        conn.execute('INSERT INTO monitors(name,source_urls,target_channel_id,option_group_id) VALUES(?,?,901,?)',
                                     ('Retain metadata option', 'https://example.test/fixture', option_id))
                    before = list(conn.iterdump())
                response = self.client.delete(f'/api/options/{option_id}', headers=self.headers)
                self.assertEqual(response.status_code, 409, response.text)
                with closing(database.get_conn()) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def test_unreferenced_option_can_be_deleted_and_missing_option_is_404(self):
        option_id = self.create_option(metadata_intent_json=confirmed(category='keep'))
        response = self.client.delete(f'/api/options/{option_id}', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.row('upload_option_groups', option_id))
        before = self.snapshot()
        response = self.client.delete(f'/api/options/{option_id}', headers=self.headers)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.snapshot(), before)

    def test_missing_option_blocks_preview_without_falling_back_or_changing_queue(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE video_queue SET option_group_id=999999 WHERE id=901')
            before = dict(conn.execute('SELECT * FROM video_queue WHERE id=901').fetchone())
            before_approvals = conn.execute('SELECT COUNT(*) FROM upload_approvals').fetchone()[0]
        response = self.request('POST', '/api/queue/preview-distribution',
                                {'ids': [901], 'do_download': False, 'do_render': False, 'do_upload': True})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview['items'][0]['blocked'])
        self.assertFalse(preview['preview_id'])
        self.assertTrue(any('tùy chọn' in reason.lower() for reason in preview['items'][0]['reasons']))
        with closing(database.get_conn()) as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM video_queue WHERE id=901').fetchone()), before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_approvals').fetchone()[0], before_approvals)

    def test_option_metadata_revision_rejects_stale_editor_atomically(self):
        option_id = self.create_option(metadata_intent_json=confirmed(category='keep'))
        def listed():
            return next(row for row in self.client.get('/api/options', headers=self.headers).json() if row['id'] == option_id)
        old = listed()
        response = self.request('PUT', f'/api/options/{option_id}',
            {'metadata_intent_json': confirmed(category='apply'), 'expected_metadata_revision': old['metadata_revision']})
        self.assertEqual(response.status_code, 200, response.text)
        current = listed()
        self.assertNotEqual(current['metadata_revision'], old['metadata_revision'])
        before = self.snapshot()
        response = self.request('PUT', f'/api/options/{option_id}',
            {'metadata_intent_json': confirmed(category='keep'), 'name': 'Stale overwrite',
             'expected_metadata_revision': old['metadata_revision']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        response = self.request('PUT', f'/api/options/{option_id}',
            {'name': 'Current editor rename', 'expected_metadata_revision': current['metadata_revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed()['metadata_revision'], current['metadata_revision'])
        self.assertEqual(listed()['name'], 'Current editor rename')
        self.assertNotIn('metadata_revision', self.row('upload_option_groups', option_id))

    def test_channel_metadata_revision_ignores_note_but_rejects_stale_choices(self):
        def listed():
            return next(row for row in self.client.get('/api/channels', headers=self.headers).json() if row['id'] == 901)
        old = listed()
        response = self.request('PUT', '/api/channels/901',
            {'studio_category_mode': 'apply', 'default_category_id': '24',
             'expected_metadata_revision': old['metadata_revision']})
        self.assertEqual(response.status_code, 200, response.text)
        current = listed()
        self.assertNotEqual(current['metadata_revision'], old['metadata_revision'])
        before = self.snapshot()
        response = self.request('PUT', '/api/channels/901',
            {'studio_category_mode': 'keep', 'default_category_id': '22', 'note': 'Must roll back',
             'expected_metadata_revision': old['metadata_revision']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        response = self.request('PUT', '/api/channels/901',
            {'note': 'Current note', 'expected_metadata_revision': current['metadata_revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed()['metadata_revision'], current['metadata_revision'])
        self.assertEqual(listed()['note'], 'Current note')
        self.assertNotIn('metadata_revision', self.row('youtube_channels', 901))

    def test_future_schema_preflight_prevents_any_initialization_mutation(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE app_schema_versions SET version=2 WHERE schema_key='metadata_intent'")
            before = list(conn.iterdump())
        with self.assertRaisesRegex(RuntimeError, 'metadata_intent'):
            database.init_db()
        with closing(database.get_conn()) as conn:
            self.assertEqual(list(conn.iterdump()), before)


class MetadataIntentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE app_schema_versions(schema_key TEXT PRIMARY KEY, version INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE upload_option_groups(id INTEGER PRIMARY KEY, language TEXT, disable_shorts_remix INTEGER);
            INSERT INTO upload_option_groups VALUES(1,'vi',0),(2,'en-US',1);
            CREATE TABLE youtube_channels(id INTEGER PRIMARY KEY, default_category_id TEXT);
            INSERT INTO youtube_channels VALUES(1,'22'),(2,'24');
            CREATE TABLE video_queue(id INTEGER PRIMARY KEY, assigned_channel_id INTEGER, claim_token TEXT,
                status TEXT, published_url TEXT);
            INSERT INTO video_queue VALUES(1,1,'','NeedsReview',''),(2,2,'','Completed','https://youtu.be/fixture');
            CREATE TABLE upload_attempts(id INTEGER PRIMARY KEY,evidence TEXT);
            INSERT INTO upload_attempts VALUES(1,'retained');
        ''')

    def test_additive_schema_preserves_legacy_values_and_is_idempotent(self):
        tables = ('upload_option_groups', 'youtube_channels', 'video_queue', 'upload_attempts')
        columns = {table: [row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')] for table in tables}
        before = {table: self.conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in tables}
        schema.migrate_metadata_intent_schema(self.conn)
        for table in tables:
            self.assertEqual(self.conn.execute(f'SELECT {",".join(columns[table])} FROM {table} ORDER BY id').fetchall(), before[table])
        self.assertEqual(self.conn.execute('SELECT metadata_intent_json FROM upload_option_groups').fetchall(), [('{}',), ('{}',)])
        self.assertEqual(self.conn.execute('SELECT studio_category_mode FROM youtube_channels').fetchall(), [('legacy_unconfirmed',), ('legacy_unconfirmed',)])
        first = list(self.conn.iterdump())
        schema.migrate_metadata_intent_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), first)

    def test_second_alter_failure_rolls_back_both_columns_and_stamp(self):
        before = list(self.conn.iterdump())
        original = schema._add_column
        def fail_channel(conn, table, column, declaration):
            if table == 'youtube_channels':
                raise sqlite3.OperationalError('injected ALTER failure')
            return original(conn, table, column, declaration)
        with mock.patch.object(schema, '_add_column', side_effect=fail_channel):
            with self.assertRaises(sqlite3.OperationalError):
                schema.migrate_metadata_intent_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)
        schema.migrate_metadata_intent_schema(self.conn)
        self.assertEqual(schema.preflight_metadata_intent_schema(self.conn), 1)

    def test_outer_transaction_controls_migration_commit(self):
        before = list(self.conn.iterdump())
        self.conn.execute('BEGIN')
        schema.migrate_metadata_intent_schema(self.conn)
        self.conn.rollback()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_future_stamp_or_incomplete_stamped_schema_is_not_repaired(self):
        self.conn.execute("INSERT INTO app_schema_versions(schema_key,version) VALUES('metadata_intent',99)")
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(RuntimeError, 'metadata_intent'):
            schema.migrate_metadata_intent_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("UPDATE app_schema_versions SET version=1 WHERE schema_key='metadata_intent'")
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(RuntimeError, 'thiếu cột'):
            schema.migrate_metadata_intent_schema(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
