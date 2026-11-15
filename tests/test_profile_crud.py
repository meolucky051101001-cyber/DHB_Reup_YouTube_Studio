"""Local profile CRUD on a disposable DB and Chrome directory."""
import unittest
from contextlib import closing
from unittest import mock
from urllib.parse import quote

import config
import database
import test_review_regressions as fixtures


class ProfileCrudTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        self.profiles = self.root / 'profiles'
        self.profiles.mkdir()
        patch = mock.patch.object(config, 'APP_PROFILES_DIR', self.profiles)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, method, name='', payload=None):
        return self.client.request(method, '/api/profiles' + ('/' + quote(name, safe='') if name else ''),
                                   headers=self.headers, json=payload)

    def snapshot(self):
        with closing(database.get_conn()) as conn:
            return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                    for t in ('chrome_profiles','youtube_channels','channel_group_memberships',
                              'video_queue','monitors','published_history','upload_attempts','content_reservations')}

    def create(self, name='Profile Việt', **values):
        response = self.request('POST', payload={'name':name, **values})
        self.assertEqual(response.status_code, 200, response.text)
        with closing(database.get_conn()) as conn:
            return conn.execute('SELECT id FROM youtube_channels WHERE profile_name=?', (name,)).fetchone()[0]

    def test_invalid_names_and_payloads_do_not_create_files_or_records(self):
        # Even an unfixed implementation must never write an absolute test path.
        patch = mock.patch.object(self.app.Path, 'mkdir', side_effect=AssertionError('invalid payload reached filesystem'))
        patch.start()
        self.addCleanup(patch.stop)
        for value in ([], {}, True, 1, None, '', '..', '../outside', 'dir/file', r'dir\file',
                      'C:\\escape', 'CON', 'nul.txt', 'LPT1', 'COM¹.log', 'bad.', 'bad ', 'x'*201,
                      'bad\0name', 'bad\nname', 'a:b', 'a?b'):
            with self.subTest(value=value):
                before = self.snapshot()
                response = self.request('POST', payload={'name':value})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(list(self.profiles.iterdir()), [])
        for payload in ({'proxy':{}}, {'note':[]}, {'path':'elsewhere'}, {'source':'Antidetect'}):
            self.assertEqual(self.request('POST', payload={'name':'good',**payload}).status_code, 400)
        self.assertEqual(list(self.profiles.iterdir()), [])

    def test_duplicate_names_disk_aliases_and_synced_bindings_are_preserved(self):
        self.create(proxy='host:123:user:secret')
        before = self.snapshot()
        for name in ('Profile Việt', 'PROFILE VIỆT'):
            self.assertEqual(self.request('POST', payload={'name':name,'proxy':'different'}).status_code, 409)
        self.assertEqual(self.snapshot(), before)
        (self.profiles/'Existing').mkdir()
        (self.profiles/'Existing'/'Cookies').write_text('keep', encoding='utf-8')
        self.assertEqual(self.request('POST', payload={'name':'Existing'}).status_code, 409)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO chrome_profiles(name,path,source,antidetect_profile_id) VALUES('Synced','external','Antidetect','external-id')")
        before = self.snapshot()
        self.assertEqual(self.request('POST', payload={'name':'Synced'}).status_code, 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.profiles/'Existing'/'Cookies').read_text(encoding='utf-8'), 'keep')

    def test_create_and_delete_unused_placeholder_preserves_chrome_directory(self):
        channel = self.create()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT group_name FROM youtube_channels WHERE id=?',(channel,)).fetchone()[0], 'Mặc định')
        cookie = self.profiles/'Profile Việt'/'Cookies'
        cookie.write_bytes(b'keep')
        self.assertEqual(self.request('DELETE', 'Profile Việt').status_code, 200)
        self.assertTrue(cookie.is_file())
        with closing(database.get_conn()) as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM channel_group_memberships WHERE channel_id=?',(channel,)).fetchone())
        self.assertEqual(self.request('DELETE', 'Profile Việt').status_code, 404)

    def test_direct_group_monitor_and_history_links_block_delete(self):
        channel = self.create()
        cases = [
            ('UPDATE video_queue SET target_channel_id=? WHERE id=901', (channel,)),
            ("UPDATE video_queue SET target_profile_name='Profile Việt' WHERE id=901", ()),
            ('UPDATE video_queue SET assigned_channel_id=? WHERE id=901', (channel,)),
            ("UPDATE video_queue SET target_channel_id=NULL,target_type='GROUP',target_group_names_json='[\"Mặc định\"]' WHERE id=901", ()),
            ("INSERT INTO monitors(name,source_urls,target_channel_id) VALUES('direct','https://example.test',?)",(channel,)),
            ("INSERT INTO monitors(name,source_urls,target_group_name) VALUES('group','https://example.test','Mặc định')",()),
            ("INSERT INTO published_history(source_post_id,channel_id) VALUES('fixture',?)",(channel,)),
            ("INSERT INTO upload_attempts(queue_id,attempt_no,channel_id) VALUES(901,1,?)",(channel,)),
            ("INSERT INTO content_reservations(content_key,channel_id,queue_id,claim_token,state) VALUES('fixture',?,901,'x','NeedsReview')",(channel,)),
        ]
        for sql, params in cases:
            with self.subTest(sql=sql):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute(sql, params)
                before = self.snapshot()
                self.assertEqual(self.request('DELETE', 'Profile Việt').status_code, 409)
                self.assertEqual(self.snapshot(), before)
                with closing(database.get_conn()) as conn, conn:
                    conn.execute("UPDATE video_queue SET target_channel_id=901,target_profile_name='',assigned_channel_id=NULL,target_type='CHANNEL',target_group_names_json='[]' WHERE id=901")
                    for table in ('monitors','published_history','upload_attempts','content_reservations'):
                        conn.execute(f'DELETE FROM {table}')

    def test_proxy_mask_partial_note_and_antidetect_ownership(self):
        channel = self.create(proxy='host:123:user:secret',note='old')
        response = self.request('PUT','Profile Việt',{'proxy':'host:123:***:***','note':''})
        self.assertEqual(response.status_code,200,response.text)
        with closing(database.get_conn()) as conn:
            self.assertEqual(tuple(conn.execute("SELECT proxy,note FROM chrome_profiles WHERE name='Profile Việt'").fetchone()),('host:123:user:secret',''))
        self.assertEqual(self.request('PUT','Profile Việt',{'proxy':'new:123'}).status_code,200)
        with closing(database.get_conn()) as conn, conn:
            self.assertEqual(conn.execute('SELECT proxy FROM youtube_channels WHERE id=?',(channel,)).fetchone()[0],'new:123')
            conn.execute("UPDATE chrome_profiles SET source='Antidetect',antidetect_profile_id='id' WHERE name='Profile Việt'")
        before = self.snapshot()
        self.assertEqual(self.request('PUT','Profile Việt',{'proxy':'','note':'must rollback'}).status_code,400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.request('PUT','Profile Việt',{'note':'allowed'}).status_code,200)
        for payload in ({'proxy':{}}, {'note':[]}, {'name':'new'}, {'path':'elsewhere'}):
            self.assertEqual(self.request('PUT','Profile Việt',payload).status_code,400)

    def test_busy_proxy_update_and_late_failure_roll_back_all_tables(self):
        channel = self.create(proxy='old:123')
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET target_channel_id=?,status='NeedsReview' WHERE id=901", (channel,))
        before = self.snapshot()
        self.assertEqual(self.request('PUT','Profile Việt',{'proxy':'new:123'}).status_code,409)
        self.assertEqual(self.snapshot(),before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET status='Pending' WHERE id=901")
            conn.execute("CREATE TRIGGER reject_proxy BEFORE UPDATE OF proxy ON youtube_channels BEGIN SELECT RAISE(ABORT,'fixture'); END")
        before = self.snapshot()
        self.assertEqual(self.request('PUT','Profile Việt',{'proxy':'new:123','note':'changed'}).status_code,500)
        self.assertEqual(self.snapshot(),before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DROP TRIGGER reject_proxy')
        self.assertEqual(self.request('PUT','Profile Việt',{'proxy':'new:123'}).status_code,200)

    def test_failed_create_and_delete_roll_back_memberships_and_profile(self):
        with mock.patch.object(self.app, 'set_channel_group_memberships', side_effect=RuntimeError('fixture')):
            before = self.snapshot()
            self.assertEqual(self.request('POST',payload={'name':'Failed'}).status_code,500)
            self.assertEqual(self.snapshot(),before)
        self.assertFalse((self.profiles/'Failed').exists())
        self.create()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("CREATE TRIGGER reject_profile_delete BEFORE DELETE ON chrome_profiles BEGIN SELECT RAISE(ABORT,'fixture'); END")
        before = self.snapshot()
        self.assertEqual(self.request('DELETE','Profile Việt').status_code,500)
        self.assertEqual(self.snapshot(),before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DROP TRIGGER reject_profile_delete')
        self.assertEqual(self.request('DELETE','Profile Việt').status_code,200)

    def test_channel_delete_cannot_bypass_profile_reference_guards(self):
        channel = self.create()
        with closing(database.get_conn()) as conn, conn:
            conn.execute('UPDATE video_queue SET target_channel_id=? WHERE id=901', (channel,))
        before = self.snapshot()
        response = self.client.delete(f'/api/channels/{channel}', headers=self.headers)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.snapshot(),before)

    def test_delayed_probe_cannot_recreate_deleted_profile_or_apply_stale_binding(self):
        for suffix, delete in [('refresh-channels',True), ('check-login',False)]:
            with self.subTest(suffix=suffix):
                name = 'Probe-' + suffix
                self.create(name)
                def probe(*args, **kwargs):
                    response = self.request('DELETE' if delete else 'PUT', name, {'proxy':'changed:123'})
                    self.assertEqual(response.status_code,200,response.text)
                    return {'logged_in':True,'channels':[{'name':'Stale','channel_id':'UC'+'c'*22}]}
                with mock.patch.object(self.app,'probe_profile_login',side_effect=probe):
                    response = self.client.post('/api/profiles/'+name+'/'+suffix, headers=self.headers)
                self.assertEqual(response.status_code,409,response.text)
                with closing(database.get_conn()) as conn:
                    self.assertIsNone(conn.execute("SELECT 1 FROM youtube_channels WHERE name='Stale'").fetchone())
                    if not delete:
                        self.assertEqual(tuple(conn.execute('SELECT proxy,login_status FROM chrome_profiles WHERE name=?',(name,)).fetchone()), ('changed:123',''))

    def test_synced_display_name_with_slash_can_be_edited_without_changing_binding(self):
        name = 'Synced / Việt'
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO chrome_profiles(name,path,source,antidetect_profile_id) VALUES(?,'antidetect://id','Antidetect','id')",(name,))
        response = self.request('PUT',name,{'note':'ghi chú'})
        self.assertEqual(response.status_code,200,response.text)
        with closing(database.get_conn()) as conn:
            self.assertEqual(tuple(conn.execute('SELECT path,source,antidetect_profile_id,note FROM chrome_profiles WHERE name=?',(name,)).fetchone()),('antidetect://id','Antidetect','id','ghi chú'))
        self.assertEqual(self.request('DELETE',name).status_code,200)
