"""Real SQLite/API tests for monitor policy, with the upload network boundary mocked."""
import json
from contextlib import closing
from unittest import mock

import database
import monitor_policy as policy
import pipeline_manager as pm
from test_review_regressions import ReviewRegressions


class MonitorPolicyTests(ReviewRegressions):
    def seed_monitor(self, choices=None, option=None):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET default_privacy='private',made_for_kids=1,altered_content=1 WHERE id=901")
            mon = {'target_channel_id': 901, 'option_group_id': option,
                   'metadata_policy_json': policy.encode(choices or policy.DEFAULT_POLICY)}
            mon['metadata_revision'] = policy.preview(conn, mon)['revision']
            conn.execute('INSERT INTO monitors(name,source_urls,target_channel_id,option_group_id,metadata_policy_json,metadata_revision) VALUES(?,?,?,?,?,?)',
                         ('Policy', 'https://www.youtube.com/@fixture', 901, option, mon['metadata_policy_json'], mon['metadata_revision']))
            mon['id'] = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            source = {'source_type': 'File', 'platform': 'LocalFile', 'post_id': 'monitor-fixture',
                      'source_url': '', 'input_video_path': str(self.source), 'duration': '00:00:04', 'view_count': 0, 'like_count': 0}
            item_id = pm.insert_monitor_queue_item(conn, 'Monitor - Policy', mon, source, 'Policy test', '', '')
        return mon, item_id

    def test_inheritance_reaches_snapshot_and_uploader(self):
        mon, item_id = self.seed_monitor()
        with mock.patch.object(pm, 'upload_video_to_youtube', return_value={'url': 'https://youtu.be/dQw4w9WgXcQ'}) as upload:
            self.manager.process_single_item(item_id, do_download=False, do_render=False, do_upload=True,
                                             claim_token='monitor-run', owner='monitor')
        self.assertEqual(self.row(item_id)['status'], 'Completed')
        args = upload.call_args
        item = args.kwargs.get('video_item') or args.args[1]
        self.assertEqual([item[k] for k in policy.FIELDS], ['private', 1, 1])
        with closing(database.get_conn()) as conn:
            snapshot = json.loads(conn.execute('SELECT snapshot_json FROM upload_approval_items WHERE queue_id=?', (item_id,)).fetchone()[0])
        self.assertEqual([snapshot['video'][k] for k in policy.FIELDS], ['private', 1, 1])

    def test_false_and_options_precedence(self):
        with closing(database.get_conn()) as conn, conn:
            option = conn.execute("INSERT INTO upload_option_groups(name,made_for_kids,ai_label) VALUES('False option',0,0)").lastrowid
        mon, item_id = self.seed_monitor(option=option)
        self.assertEqual([self.row(item_id)[k] for k in policy.FIELDS], ['private', 0, 0])
        for value in (True, False):
            choices = {**policy.DEFAULT_POLICY, 'made_for_kids': value, 'altered_content': value}
            values, sources = policy.effective(choices, {'default_privacy':'private','made_for_kids':1,'altered_content':1}, {'made_for_kids':not value,'ai_label':not value})
            self.assertEqual(values['made_for_kids'], int(value))
            self.assertEqual(values['altered_content'], int(value))
            self.assertEqual(sources['made_for_kids'], 'monitor')

    def test_changed_configuration_blocks_preview_and_monitor(self):
        mon, item_id = self.seed_monitor()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET default_privacy='public' WHERE id=901")
        preview = self.manager.preview_distribution([item_id], [False,False,True])
        self.assertTrue(preview['items'][0]['blocked'])
        with mock.patch.object(pm.threading, 'Thread') as thread:
            self.assertFalse(self.manager.start_monitor(mon['id'])['ok'])
            thread.assert_not_called()

    def test_manual_edit_adopts_explicit_values(self):
        _, item_id = self.seed_monitor()
        response = self.client.post('/api/queue/bulk-update', headers=self.headers,
                                    json={'ids':[item_id], 'updates':{'visibility':'unlisted','made_for_kids':False}})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row(item_id)
        self.assertEqual(row['monitor_metadata_policy_json'], '')
        self.assertEqual(row['visibility'], 'unlisted')
        self.assertEqual(row['made_for_kids'], 0)
        self.assertEqual(row['altered_content'], 1)

    def test_legacy_migration_is_idempotent_and_requires_confirmation(self):
        with closing(database.get_conn()) as conn, conn:
            legacy = conn.execute("INSERT INTO monitors(name,source_urls,is_running) VALUES('Legacy','https://example.test',1)").lastrowid
        database.init_db()
        database.init_db()
        with closing(database.get_conn()) as conn:
            row = dict(conn.execute('SELECT * FROM monitors WHERE id=?', (legacy,)).fetchone())
        self.assertEqual(row['is_running'], 0)
        self.assertEqual(row['metadata_policy_json'], '')
        self.assertFalse(self.manager.start_monitor(legacy)['ok'])

    def test_stale_confirmation_rejected(self):
        mon, _ = self.seed_monitor()
        response = self.client.get(f"/api/monitors/{mon['id']}/metadata-preview", headers=self.headers)
        preview = response.json()
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE youtube_channels SET made_for_kids=0 WHERE id=901')
        response = self.client.post(f"/api/monitors/{mon['id']}/metadata-confirm", headers=self.headers, json=preview)
        self.assertEqual(response.status_code, 409, response.text)

    def test_group_uses_assigned_channel_defaults_and_membership_revision(self):
        from group_distribution import choose_target_channel
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Policy group')")
            conn.executemany('INSERT INTO channel_group_memberships(channel_id,group_name) VALUES(?,?)', [(901,'Policy group'),(902,'Policy group')])
            conn.execute("UPDATE youtube_channels SET default_privacy='private',made_for_kids=1 WHERE id=901")
            conn.execute("UPDATE youtube_channels SET default_privacy='unlisted',altered_content=1 WHERE id=902")
            mon = {'target_group_name':'Policy group','metadata_policy_json':policy.encode(policy.DEFAULT_POLICY)}
            envelope = policy.preview(conn, mon)['envelope']
            for queue_id in (901,902):
                item = {'id':queue_id,'target_type':'GROUP','target_group_name':'Policy group','monitor_metadata_policy_json':policy.encode(envelope)}
                channel, _ = choose_target_channel(conn, item)
                policy.resolve_item(conn, item, channel)
                self.assertEqual(item['visibility'], channel['default_privacy'])
                self.assertEqual(item['made_for_kids'], channel['made_for_kids'])
                self.assertEqual(item['altered_content'], channel['altered_content'])
            conn.execute("DELETE FROM channel_group_memberships WHERE channel_id=902 AND group_name='Policy group'")
            with self.assertRaises(ValueError):
                policy.resolve_item(conn, item, channel)

    def test_manual_approval_freeze_is_atomic_when_config_changes(self):
        _, item_id = self.seed_monitor()
        options = [False,False,True]
        preview = self.manager.preview_distribution([item_id], options)
        self.assertFalse(preview['items'][0]['blocked'], preview)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE youtube_channels SET altered_content=0 WHERE id=901')
        with self.assertRaises(ValueError):
            self.manager._freeze_batch_assignments([item_id],preview['preview_token'],options,'race-test')
        self.assertFalse(self.row(item_id)['claim_token'])
        self.assertIsNone(self.row(item_id)['assigned_channel_id'])
