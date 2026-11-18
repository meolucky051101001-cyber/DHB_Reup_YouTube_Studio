"""Validate monitor configuration before persistence or worker startup."""
import json
import threading
import unittest
from contextlib import closing
from unittest import mock

import database
import monitor_policy
import test_review_regressions as fixtures


class MonitorValidationTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def create(self, **values):
        return self.client.post('/api/monitors', headers=self.headers, json={
            'name':'Monitor Việt', 'source_urls':'https://www.youtube.com/@fixture',
            'target_channel_id':901, 'metadata_policy':monitor_policy.DEFAULT_POLICY, **values})

    def rows(self):
        with closing(database.get_conn()) as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM monitors ORDER BY id')]

    def test_invalid_types_bounds_and_choices_leave_no_monitor(self):
        cases = [{'name':[]},{'name':123},{'source_urls':{}},{'source_urls':'javascript:alert(1)'},
                 {'source_urls':'https://www.youtube.com/@fixture\nmissing-local-path'},
                 {'target_channel_id':True},{'target_channel_id':1.5},{'target_channel_id':-1},
                 {'option_group_id':[]},{'option_group_id':'1.5'},
                 {'min_views':-1},{'min_views':1.5},{'min_views':True},{'min_views':2**63},
                 {'check_interval_minutes':0},{'check_interval_minutes':-1},{'check_interval_minutes':4},
                 {'check_interval_minutes':'bad'},{'check_interval_minutes':None},
                 {'max_videos_per_scan':0},{'max_videos_per_scan':1001},{'max_videos_per_scan':True},
                 {'upload_to':'bad'},{'render_mode':'unsupported'},{'ai_Action':'unsupported'},
                 {'ai_provider':{}},{'metadata_policy':{'version':True}},
                 {'metadata_policy':{'version':1,'made_for_kids':'false'}}]
        for values in cases:
            with self.subTest(values=values):
                before = self.rows()
                response = self.create(**values)
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(self.rows(),before)

    def test_unknown_references_and_duplicate_names_return_clear_errors(self):
        for values in ({'target_channel_id':999999}, {'target_channel_id':None,'target_group_name':'Missing'},
                       {'option_group_id':999999}):
            response = self.create(**values)
            self.assertEqual(response.status_code,400,response.text)
            self.assertEqual(self.rows(),[])
        response = self.create()
        self.assertEqual(response.status_code,200,response.text)
        before = self.rows()
        response = self.create()
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.rows(),before)
        response = self.create(name='After duplicate')
        self.assertEqual(response.status_code,200,response.text)

    def test_legacy_integer_strings_local_sources_and_policy_require_confirmation(self):
        url = 'https://www.youtube.com/@fixture?z=last&a=first'
        with mock.patch.object(self.manager,'start_monitor') as start:
            response = self.create(source_urls=f' {url}\n\n{self.source} ',target_channel_id='901',
                                   option_group_id='',min_views='0',check_interval_minutes='5',max_videos_per_scan='1000',
                                   metadata_policy=json.dumps({**monitor_policy.DEFAULT_POLICY,'made_for_kids':False}))
            self.assertEqual(response.status_code,200,response.text)
            start.assert_not_called()
        row = self.rows()[0]
        self.assertEqual(row['source_urls'],url+'\n'+str(self.source))
        self.assertEqual((row['target_channel_id'],row['option_group_id'],row['min_views'],row['check_interval_minutes'],row['max_videos_per_scan']),
                         (901,None,0,5,1000))
        self.assertEqual((row['is_running'],row['metadata_revision']),(0,''))
        response = self.client.get(f"/api/monitors/{row['id']}/metadata-preview",headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        preview = response.json()
        self.assertEqual(preview['channels'][0]['made_for_kids'],0)
        response = self.client.post(f"/api/monitors/{row['id']}/metadata-confirm",headers=self.headers,
                                    json={'policy':preview['policy'],'revision':preview['revision']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.rows()[0]['metadata_revision'],preview['revision'])

    def test_empty_group_can_be_configured_without_silently_selecting_a_channel(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO channel_groups(name) VALUES('Empty group')")
        response = self.create(target_channel_id=None,target_group_name='Empty group')
        self.assertEqual(response.status_code,200,response.text)
        row = self.rows()[0]
        self.assertIsNone(row['target_channel_id'])
        response = self.client.get(f"/api/monitors/{row['id']}/metadata-preview",headers=self.headers)
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.rows()[0]['is_running'],0)

    def test_invalid_source_error_does_not_echo_signed_url(self):
        response = self.create(source_urls='https://user:private-token-8675309@example.test/path')
        self.assertEqual(response.status_code,400,response.text)
        self.assertNotIn('private-token-8675309',response.text)

    def test_monitor_oversampling_respects_the_extractor_limit(self):
        import pipeline_manager as pm
        response = self.create(max_videos_per_scan=1000)
        self.assertEqual(response.status_code,200,response.text)
        row = self.rows()[0]
        stop = threading.Event()
        with closing(database.get_conn()) as conn, conn:
            revision = monitor_policy.preview(conn,row)['revision']
            conn.execute('UPDATE monitors SET is_running=1,metadata_revision=? WHERE id=?',(revision,row['id']))
        self.manager._monitor_threads[row['id']] = stop
        def cancelled_scan(*args, **kwargs):
            self.assertEqual(kwargs['max_items_per_url'],1000)
            self.assertIs(kwargs['stop_event'],stop)
            stop.set()
            raise InterruptedError('fixture cancellation')
        with mock.patch.object(pm,'scan_social_urls',side_effect=cancelled_scan) as scan:
            self.manager._monitor_loop(row['id'],stop)
        scan.assert_called_once()
