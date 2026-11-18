"""Configuration HTTP regressions using a disposable production SQLite schema."""
import json
import unittest
from contextlib import closing

import database
import test_review_regressions as fixtures


class ConfigValidationTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def request(self, method, path, payload):
        return self.client.request(method, path, headers=self.headers, json=payload)

    def row(self, table, item_id):
        with closing(database.get_conn()) as conn:
            row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
            return dict(row) if row else None

    def snapshot(self):
        with closing(database.get_conn()) as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('youtube_channels', 'upload_option_groups', 'channel_group_memberships')}

    def create_option(self, **values):
        response = self.request('POST', '/api/options', {'name':'Tùy chọn Việt', **values})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    def test_false_strings_and_legacy_boolean_values_are_consistent(self):
        channel_fields = ('emulate_mobile','use_hidden_desktop','made_for_kids','altered_content')
        option_fields = ('affiliate_enabled','pin_comment_enabled','made_for_kids','ai_label','disable_shorts_remix')
        option_id = self.create_option()
        for value, expected in [('false',0),('0',0),('off',0),('Không',0),(' true ',1),('1',1),(False,0),(True,1),(None,0)]:
            with self.subTest(value=value):
                for path, fields, table, item_id in [('/api/channels/901',channel_fields,'youtube_channels',901),
                                                       (f'/api/options/{option_id}',option_fields,'upload_option_groups',option_id)]:
                    response = self.request('PUT', path, {**dict.fromkeys(fields,value),
                        **({'affiliate_url':'https://example.test/product'} if table=='upload_option_groups' else {})})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual([self.row(table,item_id)[field] for field in fields], [expected]*len(fields))
        option_id = self.create_option(made_for_kids='false', ai_label='0')
        self.assertEqual((self.row('upload_option_groups',option_id)['made_for_kids'],self.row('upload_option_groups',option_id)['ai_label']), (0,0))
        response = self.request('POST','/api/channels',{'name':'New channel','made_for_kids':'false','altered_content':True})
        self.assertEqual(response.status_code,200,response.text)
        with closing(database.get_conn()) as conn:
            row = conn.execute("SELECT made_for_kids,altered_content FROM youtube_channels WHERE name='New channel'").fetchone()
        self.assertEqual(tuple(row),(0,1))

    def test_invalid_payloads_reject_without_any_configuration_write(self):
        option_id = self.create_option()
        common = [{'name':[]},{'name':{'secret':'do-not-echo-8675309'}},{'name':True},{'name':123},
                  {'name':' '},{'name':None},{'name':'x'*201},{'name':'bad\x00name'}, {'made_for_kids':'maybe'},{'made_for_kids':2},
                  {'made_for_kids':1.5},{'made_for_kids':[]},{'made_for_kids':{}}, {'made_for_kids':1.0}]
        cases = [('/api/channels','/api/channels/901',common + [
                    {'default_privacy':'schedule'},{'default_category_id':False},{'channel_id':'not-a-channel'},
                    {'profile_name':['bad']},{'proxy':{'password':'do-not-echo-8675309'}},{'group_names':[1]},
                    {'group_names':'[invalid'},{'oauth_token_json':['do-not-echo-8675309']}]),
                 ('/api/options',f'/api/options/{option_id}',common + [
                    {'category_id':False},{'category_id':'bad'},{'language':{}},{'language':'not a language'},
                    {'append_hashtags':[]},{'affiliate_url':'javascript:alert(1)'},{'affiliate_url':{'url':'bad'}}])]
        for create_path, update_path, payloads in cases:
            for payload in payloads:
                for method, path in [('POST',create_path),('PUT',update_path)]:
                    with self.subTest(method=method,path=path,payload=payload):
                        before = self.snapshot()
                        response = self.request(method,path,{'name':'Changed but rejected',**payload})
                        self.assertEqual(response.status_code,400,response.text)
                        self.assertNotIn('do-not-echo-8675309',response.text)
                        self.assertEqual(self.snapshot(),before)

    def test_group_only_update_applies_and_conflicting_aliases_are_atomic(self):
        with closing(database.get_conn()) as conn, conn:
            conn.executemany('INSERT INTO channel_groups(name) VALUES(?)',[('Nhóm A',),('Nhóm B',)])
        response = self.request('PUT','/api/channels/901',{'group_names':['Nhóm A','Nhóm B']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['ok'],response.text)
        self.assertEqual(self.row('youtube_channels',901)['group_name'],'Nhóm A')
        listed = self.client.get('/api/channels',headers=self.headers).json()
        self.assertEqual(next(row for row in listed if row['id']==901)['group_names'],['Nhóm A','Nhóm B'])
        for payload in [{'name':'Changed','group_names':['Nhóm A'],'group_name':'Nhóm B'},
                        {'name':'Changed','group_names':['Missing']}, {'name':'Changed','group_names':[]}]:
            before = self.snapshot()
            response = self.request('PUT','/api/channels/901',payload)
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual(self.snapshot(),before)

    def test_option_empty_text_and_partial_updates_preserve_user_intent(self):
        option_id = self.create_option(append_hashtags='', affiliate_CTA='', pin_comment_text='Dòng 1\nDòng 2', language='en-US')
        before = self.row('upload_option_groups',option_id)
        self.assertEqual((before['append_hashtags'],before['affiliate_CTA']),('',''))
        response = self.request('PUT',f'/api/options/{option_id}',{'ai_label':'0'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.row('upload_option_groups',option_id),before)

    def test_oauth_mode_creation_and_change_use_the_same_local_validation(self):
        before = self.snapshot()
        for token in ('','{not-json',json.dumps({'refresh_token':'do-not-echo-8675309'})):
            response = self.request('POST','/api/channels',{'name':'OAuth','upload_mode':'youtube_api','oauth_token_json':token})
            self.assertEqual(response.status_code,400,response.text)
            self.assertNotIn('do-not-echo-8675309',response.text)
            self.assertEqual(self.snapshot(),before)
        token = json.dumps({'client_id':'fixture-client','client_secret':'fixture-secret','refresh_token':'fixture-refresh'})
        response = self.request('POST','/api/channels',{'name':'Valid OAuth','upload_mode':'youtube_api','oauth_token_json':token})
        self.assertEqual(response.status_code,200,response.text)
        response = self.request('PUT','/api/channels/901',{'upload_mode':'youtube_api','oauth_token_json':token})
        self.assertEqual(response.status_code,200,response.text)
        before = self.row('youtube_channels',901)
        response = self.request('PUT','/api/channels/901',{'oauth_token_json':json.dumps({'refresh_token':'broken'})})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.row('youtube_channels',901),before)
        response = self.request('PUT','/api/channels/901',{'oauth_token_json':'','note':'Only note changed'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.row('youtube_channels',901)['oauth_token_json'],token)

    def test_masked_proxy_and_antidetect_ownership_are_preserved(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET proxy='host:8080:user:private-secret' WHERE id=901")
            conn.execute("INSERT INTO chrome_profiles(name,path,source,antidetect_profile_id) VALUES('AD fixture','antidetect://fixture','Antidetect','fixture')")
        response = self.request('PUT','/api/channels/901',{'proxy':'host:8080:user:***','note':'Tiếng Việt'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.row('youtube_channels',901)['proxy'],'host:8080:user:private-secret')
        response = self.request('PUT','/api/channels/901',{'profile_name':'AD fixture','emulate_mobile':True,'proxy':'host:9090'})
        self.assertEqual(response.status_code,200,response.text)
        row = self.row('youtube_channels',901)
        self.assertEqual((row['profile_path'],row['proxy'],row['emulate_mobile']),('antidetect://fixture','',0))

    def test_missing_rows_and_failed_membership_write_do_not_report_success(self):
        from unittest import mock
        for path in ('/api/channels/999999','/api/options/999999'):
            response = self.request('PUT',path,{'name':'Missing'})
            self.assertEqual(response.status_code,404,response.text)
        before = self.snapshot()
        with mock.patch.object(self.app,'set_channel_group_memberships',side_effect=RuntimeError('fixture transaction failure')):
            response = self.request('POST','/api/channels',{'name':'Rollback fixture'})
            self.assertEqual(response.status_code,500,response.text)
        self.assertEqual(self.snapshot(),before)
        with mock.patch.object(self.app,'set_channel_group_memberships',side_effect=RuntimeError('fixture transaction failure')):
            response = self.request('PUT','/api/channels/901',{'name':'Partial update','group_names':['Mặc định']})
            self.assertEqual(response.status_code,500,response.text)
        self.assertEqual(self.snapshot(),before)
        # A second writer must be able to proceed after the failed transaction.
        response = self.request('POST','/api/channels',{'name':'After rollback'})
        self.assertEqual(response.status_code,200,response.text)

    def test_affiliate_enable_and_clear_validate_the_final_state(self):
        option_id = self.create_option()
        before = self.snapshot()
        response = self.request('PUT',f'/api/options/{option_id}',{'affiliate_enabled':True})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.snapshot(),before)
        response = self.request('PUT',f'/api/options/{option_id}',{'affiliate_enabled':True,'affiliate_url':'https://example.test/product?ref=1'})
        self.assertEqual(response.status_code,200,response.text)
        before = self.snapshot()
        response = self.request('PUT',f'/api/options/{option_id}',{'affiliate_url':''})
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.snapshot(),before)
        response = self.request('PUT',f'/api/options/{option_id}',{'affiliate_enabled':'false','affiliate_url':''})
        self.assertEqual(response.status_code,200,response.text)

    def test_active_channel_guard_and_legacy_fields_keep_their_contract(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET assigned_channel_id=901,claim_token='fixture-active' WHERE id=901")
        before = self.snapshot()
        response = self.request('PUT','/api/channels/901',{'default_privacy':'private'})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.snapshot(),before)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET claim_token='' WHERE id=901")
            conn.execute("INSERT INTO channel_groups(name) VALUES('Legacy group')")
        response = self.request('PUT','/api/channels/901',{'group_names':'["Legacy group"]','ignored_display_field':{},'default_category_id':22})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.row('youtube_channels',901)['group_name'],'Legacy group')
        response = self.request('PUT','/api/channels/901',{'group_names':'','group_name':'Mặc định'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.row('youtube_channels',901)['group_name'],'Mặc định')
