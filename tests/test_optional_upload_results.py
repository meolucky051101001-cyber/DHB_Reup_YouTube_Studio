import json
import threading
import unittest
from contextlib import closing
from unittest import mock

import requests
import database
import pipeline_manager as pm
import youtube_uploader as yu
from test_p3_uploader import _Response, _ResumableSession, ResumableUploadTests, VIDEO_ID, CHANNEL_ID
import test_review_regressions as fixtures


class OptionalResultTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp
    row = fixtures.ReviewRegressions.row

    def test_thumbnail_http_and_timeout_results_do_not_repeat_video_upload(self):
        for response, expected in [(_Response(403), 'HTTP_403'),(_Response(500),'HTTP_500'),
                                   (requests.Timeout('sensitive response'), 'RESULT_UNKNOWN'),
                                   (_Response(200,body={}), 'RESULT_UNKNOWN'),
                                   (_Response(200,body={'items':[{'default':{'url':'https://example.test/thumb.jpg'}}]}),None)]:
            with self.subTest(expected=expected):
                session = _ResumableSession([_Response(200,body={'id':VIDEO_ID})])
                original_post = session.post
                def post(url, **kwargs):
                    if 'thumbnails/set' in url:
                        session.post_calls.append((url,kwargs))
                        if isinstance(response, Exception):
                            raise response
                        return response
                    return original_post(url,**kwargs)
                session.post=post
                fixture=ResumableUploadTests()
                with mock.patch.object(yu,'_refresh_google_access_token',return_value='fixture-token'), \
                     mock.patch.object(yu,'_fetch_oauth_channel_identity',return_value={'channel_id':CHANNEL_ID}), \
                     mock.patch.object(yu,'get_settings',return_value={}), mock.patch.object(yu,'add_log'):
                    result=yu._upload_via_youtube_api_with_session(str(self.source),fixture._metadata(),fixture._channel(),thumbnail_path=str(self.source),session=session)
                self.assertEqual(result['url'],f'https://youtu.be/{VIDEO_ID}')
                self.assertEqual(len(session.put_calls),1)
                self.assertEqual(len(session.post_calls),2)
                self.assertEqual([w['code'] for w in result['warnings']], [expected] if expected else [])
                self.assertNotIn('sensitive response',json.dumps(result))

    def test_completed_warnings_survive_restart_without_retry(self):
        warnings=[{'step':'thumbnail','code':'HTTP_403','message':'Thumbnail unavailable'}]
        options=[False,False,True]
        token=self.manager.preview_distribution([901],options)['preview_token']
        self.manager._freeze_batch_assignments([901],token,options,'optional-test')
        with mock.patch.object(pm,'upload_video_to_youtube',return_value={'url':f'https://youtu.be/{VIDEO_ID}','warnings':warnings}) as upload:
            self.manager._process_single_item_safe(901,0,*options,threading.Semaphore(0),'optional-test')
            self.assertEqual(self.row()['status'],'Completed')
            self.assertEqual(json.loads(self.row()['post_upload_warnings_json']),warnings)
            with closing(database.get_conn()) as conn:
                attempt=dict(conn.execute('SELECT * FROM upload_attempts WHERE queue_id=901').fetchone())
                self.assertEqual(attempt['outcome'],'Completed')
                self.assertEqual(json.loads(attempt['post_upload_warnings_json']),warnings)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM published_history WHERE channel_id=901').fetchone()[0],1)
            restarted=pm.PipelineManager()
            restarted.process_single_item(901,do_download=False,do_render=False,do_upload=True)
            self.assertEqual(upload.call_count,1)

    def test_pin_is_rejected_on_all_dispatch_paths_before_network(self):
        for mode in ('youtube_api','playwright'):
            with self.subTest(mode=mode), mock.patch.object(yu,'upload_via_youtube_api') as api, mock.patch.object(yu,'upload_via_playwright_studio') as studio:
                with self.assertRaisesRegex(RuntimeError,'Ghim bình luận'):
                    yu.upload_video_to_youtube({'title':'Test','input_video_path':str(self.source)},
                        {'channel_id':CHANNEL_ID,'upload_mode':mode}, {'pin_comment_enabled':1,'pin_comment_text':'Keep this intent'})
                api.assert_not_called()
                studio.assert_not_called()
        with mock.patch.object(yu,'_refresh_google_access_token') as refresh:
            with self.assertRaisesRegex(RuntimeError,'Ghim bình luận'):
                yu._upload_via_youtube_api_with_session('missing',{'pin_comment':'Keep'}, {})
            refresh.assert_not_called()
