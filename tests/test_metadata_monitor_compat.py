"""G18 additive migration preserves old Monitor approvals when semantics stay valid."""
import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from group_distribution import resolve_target_candidates
import metadata_intent_schema
import metadata_resolution
import monitor_policy
import upload_capabilities
from upload_capabilities import UploadCapabilityError, capability_matrix


LEGACY_SCHEMA = """
CREATE TABLE app_schema_versions(schema_key TEXT PRIMARY KEY,version INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE youtube_channels(id INTEGER PRIMARY KEY,name TEXT,channel_id TEXT,upload_mode TEXT,
 default_category_id TEXT DEFAULT '22',default_privacy TEXT DEFAULT 'private',
 made_for_kids INTEGER DEFAULT 1,altered_content INTEGER DEFAULT 1,profile_name TEXT DEFAULT '',
 group_name TEXT DEFAULT 'group',note TEXT DEFAULT '',status TEXT DEFAULT 'Ready');
CREATE TABLE upload_option_groups(id INTEGER PRIMARY KEY,name TEXT,category_id TEXT DEFAULT '22',
 language TEXT DEFAULT 'vi',disable_shorts_remix INTEGER DEFAULT 0,pin_comment_enabled INTEGER DEFAULT 0,
 made_for_kids INTEGER DEFAULT 0,ai_label INTEGER DEFAULT 0);
CREATE TABLE chrome_profiles(name TEXT PRIMARY KEY,path TEXT,source TEXT,proxy TEXT);
CREATE TABLE video_queue(id INTEGER PRIMARY KEY,assigned_channel_id INTEGER,claim_token TEXT DEFAULT '',
 monitor_metadata_policy_json TEXT,visibility TEXT,made_for_kids INTEGER,altered_content INTEGER);
CREATE TABLE channel_groups(name TEXT PRIMARY KEY);
CREATE TABLE channel_group_memberships(group_name TEXT,channel_id INTEGER,sort_order INTEGER DEFAULT 0);
"""


def legacy_revision(conn, target):
    """Freeze the actual pre-G18 hash contract, before columns are migrated."""
    channels, groups = resolve_target_candidates(conn, target)
    row = conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (target['option_group_id'],)).fetchone()
    bindings = []
    for channel in channels:
        binding = {key: value for key, value in channel.items()
                   if key not in ('status', 'note', 'created_at', 'name', 'email', 'handle')}
        profile = conn.execute('SELECT * FROM chrome_profiles WHERE name=?', (channel.get('profile_name') or '',)).fetchone()
        bindings.append({'channel': binding, 'profile': dict(profile) if profile else None})
    payload = {'target': target, 'groups': groups, 'bindings': bindings, 'options': dict(row) if row else None}
    return hashlib.sha256(monitor_policy.encode(payload).encode()).hexdigest()


class MetadataMonitorCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(LEGACY_SCHEMA)
        self.conn.execute("INSERT INTO youtube_channels(id,name,channel_id,upload_mode) VALUES(1,'API fixture',?,'youtube_api')",
                          ('UC' + 'a' * 22,))
        self.conn.execute("INSERT INTO youtube_channels(id,name,channel_id,upload_mode) VALUES(2,'Studio fixture',?,'playwright')",
                          ('UC' + 'b' * 22,))
        self.conn.execute("INSERT INTO upload_option_groups(id,name) VALUES(1,'Legacy options')")
        self.conn.execute("INSERT INTO channel_groups(name) VALUES('group')")
        self.conn.executemany("INSERT INTO channel_group_memberships(group_name,channel_id) VALUES('group',?)", [(1,), (2,)])
        self.conn.commit()

    def old_monitor(self, channel_id=1, *, option=1):
        mon = {'target_channel_id': channel_id, 'target_group_name': 'group', 'option_group_id': option,
               'metadata_policy_json': monitor_policy.encode(monitor_policy.DEFAULT_POLICY)}
        target = monitor_policy.context(mon)
        mon['metadata_revision'] = legacy_revision(self.conn, target)
        envelope = {'version': 1, 'choices': monitor_policy.DEFAULT_POLICY, 'context': target,
                    'revision': mon['metadata_revision']}
        return mon, envelope

    def test_api_old_envelope_hash_and_values_survive_only_default_columns(self):
        for option in (1, None):
            with self.subTest(option=option):
                mon, envelope = self.old_monitor(option=option)
                # Roll back only schema additions after each case, keeping the old
                # calculation independent of the post-migration compatibility code.
                self.conn.execute('BEGIN')
                try:
                    metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
                    result = monitor_policy.validate_session(self.conn, mon)
                    self.assertEqual(result['revision'], mon['metadata_revision'])
                    self.assertFalse(result['blocked'])
                    channel = dict(self.conn.execute('SELECT * FROM youtube_channels WHERE id=1').fetchone())
                    item = {'monitor_metadata_policy_json': monitor_policy.encode(envelope)}
                    monitor_policy.resolve_item(self.conn, item, channel)
                    self.assertEqual(item['visibility'], 'private')
                    self.assertEqual(item['made_for_kids'], 0 if option else 1)
                    self.assertEqual(item['altered_content'], 0 if option else 1)
                    self.assertEqual(json.loads(item['monitor_metadata_policy_json']), envelope)
                finally:
                    self.conn.rollback()

    def test_studio_old_envelope_keeps_evidence_but_requires_category_confirmation(self):
        mon, envelope = self.old_monitor(channel_id=2)
        before = monitor_policy.encode(envelope)
        metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
        result = monitor_policy.preview(self.conn, mon)
        self.assertEqual(result['revision'], mon['metadata_revision'])
        self.assertTrue(result['blocked'])
        self.assertEqual(result['channels'][0]['reason_codes'], ['UPLOAD_CATEGORY_CONFIRMATION_REQUIRED'])
        with self.assertRaises(UploadCapabilityError):
            monitor_policy.validate_session(self.conn, mon)
        self.assertEqual(monitor_policy.encode(envelope), before)
        self.conn.execute("UPDATE youtube_channels SET studio_category_mode='keep' WHERE id=2")
        renewed = monitor_policy.preview(self.conn, mon)
        self.assertFalse(renewed['blocked'])
        self.assertNotEqual(renewed['revision'], mon['metadata_revision'])
        with self.assertRaisesRegex(ValueError, 'đã đổi'):
            monitor_policy.validate_session(self.conn, mon)

    def test_mixed_group_evaluates_legacy_studio_and_api_without_rewriting_envelope(self):
        mon, envelope = self.old_monitor(channel_id=None)
        metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
        result = monitor_policy.preview(self.conn, mon)
        self.assertEqual(result['revision'], mon['metadata_revision'])
        self.assertTrue(result['blocked'])
        by_id = {row['channel_id']: row for row in result['channels']}
        self.assertFalse(by_id[1]['blocked'])
        self.assertTrue(by_id[2]['blocked'])
        self.assertEqual(envelope['revision'], mon['metadata_revision'])

    def test_explicit_false_audience_ai_stay_explicit_after_migration(self):
        mon, envelope = self.old_monitor(option=None)
        choices = {**monitor_policy.DEFAULT_POLICY, 'made_for_kids': False, 'altered_content': False,
                   'visibility': 'unlisted'}
        mon['metadata_policy_json'] = monitor_policy.encode(choices)
        envelope['choices'] = choices
        metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
        result = monitor_policy.validate_session(self.conn, mon)
        values = result['channels'][0]
        self.assertEqual([values[key] for key in monitor_policy.FIELDS], ['unlisted', 0, 0])
        self.assertEqual(set(values['sources'].values()), {'monitor'})

    def test_canonical_confirmation_and_capability_revision_invalidate_old_session(self):
        mon, _ = self.old_monitor()
        metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
        canonical = {'version': 1, 'studio_category_mode': 'keep', 'video_language': {'mode': 'keep'}, 'remix': 'keep'}
        self.conn.execute('UPDATE upload_option_groups SET metadata_intent_json=? WHERE id=1',
                          (monitor_policy.encode(canonical),))
        with self.assertRaisesRegex(ValueError, 'đã đổi'):
            monitor_policy.validate_session(self.conn, mon)
        confirmed = monitor_policy.preview(self.conn, mon)
        mon['metadata_revision'] = confirmed['revision']
        matrix = capability_matrix()
        matrix['youtube_api']['revision'] = 'fixture-next-capability-revision'
        with mock.patch.object(metadata_resolution, 'capability_matrix', return_value=matrix):
            with self.assertRaisesRegex(ValueError, 'đã đổi'):
                monitor_policy.validate_session(self.conn, mon)

    def test_disabled_api_category_gate_blocks_old_envelope_and_direct_guard(self):
        mon, _ = self.old_monitor()
        metadata_intent_schema.migrate_metadata_intent_schema(self.conn)
        before_revision = monitor_policy.preview(self.conn, mon)['revision']
        matrix = capability_matrix()
        matrix['youtube_api']['features']['category']['enabled'] = False
        with mock.patch.object(upload_capabilities, 'capability_matrix', return_value=matrix), \
                mock.patch.object(metadata_resolution, 'capability_matrix', return_value=matrix):
            result = monitor_policy.preview(self.conn, mon)
            self.assertEqual(result['revision'], before_revision)
            self.assertTrue(result['blocked'])
            self.assertIn('UPLOAD_CATEGORY_UNSUPPORTED', result['channels'][0]['reason_codes'])
            with self.assertRaises(UploadCapabilityError) as raised:
                monitor_policy.validate_session(self.conn, mon)
            self.assertIn('UPLOAD_CATEGORY_UNSUPPORTED', [issue['code'] for issue in raised.exception.issues])
            with self.assertRaises(UploadCapabilityError) as direct:
                upload_capabilities.require_capabilities('youtube_api', {'category_id': '22'})
            self.assertEqual(direct.exception.code, 'UPLOAD_CATEGORY_UNSUPPORTED')


if __name__ == '__main__':
    unittest.main()
