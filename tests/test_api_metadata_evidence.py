"""Offline API payload/readback/failure fixtures from the documented G19 contract."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import youtube_uploader as yu
import upload_capabilities
from test_p3_uploader import CHANNEL_ID, VIDEO_ID, _Response, _ResumableSession


def video_payload(category='22', language=None, channel_id=CHANNEL_ID, synthetic=False):
    snippet = {'categoryId': category}
    if channel_id is not None:
        snippet['channelId'] = channel_id
    if language is not None:
        snippet['defaultAudioLanguage'] = language
    status = {'uploadStatus': 'processed', 'privacyStatus': 'private',
              'selfDeclaredMadeForKids': False}
    if synthetic != 'OMIT':
        status['containsSyntheticMedia'] = synthetic
    return {'items': [{'id': VIDEO_ID, 'snippet': snippet,
                       'status': status,
                       'processingDetails': {'processingStatus': 'succeeded'}}]}


class JsonResponse(_Response):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(status, headers=headers, text='provider-secret-must-not-escape')
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)


class MetadataSession(_ResumableSession):
    def __init__(self, readbacks=None, catalog=None, put_steps=None):
        super().__init__(put_steps if put_steps is not None else [_Response(200, body={'id': VIDEO_ID})])
        self.readbacks = list(readbacks or [JsonResponse(video_payload())])
        self.catalog = catalog if catalog is not None else JsonResponse({'items': [
            {'id': '22', 'snippet': {'assignable': True}}]})
        self.events = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        catalog = url.endswith('/videoCategories')
        self.events.append('catalog' if catalog else 'readback')
        value = self.catalog if catalog else self.readbacks.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, url, **kwargs):
        self.events.append('post')
        return super().post(url, **kwargs)

    def put(self, url, **kwargs):
        self.events.append('put-status' if not kwargs.get('data') else 'put-file')
        return super().put(url, **kwargs)


class ApiMetadataEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'clip.mp4'
        self.path.write_bytes(b'offline-video-fixture')
        self.metadata = {'title': 'Fixture', 'description': '', 'tags': [], 'category_id': '22',
                         'made_for_kids': False, 'altered_content': False, 'privacy_status': 'private'}
        self.channel = {'channel_id': CHANNEL_ID, 'oauth_token_json': '{"refresh_token":"offline"}'}
        for target, name, value in (
            (yu, '_refresh_google_access_token', 'offline-token'),
            (yu, '_fetch_oauth_channel_identity', {'channel_id': CHANNEL_ID, 'name': 'Fixture'}),
            (yu, 'get_settings', {'youtube_processing_timeout_seconds': 30}),
        ):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def upload(self, session, sink=None, metadata=None, progress=None):
        return yu._upload_via_youtube_api_with_session(
            str(self.path), metadata or self.metadata, self.channel,
            session=session, evidence_sink=sink, progress_callback=progress)

    def assert_one_upload(self, session):
        self.assertEqual(sum('uploadType=resumable' in url for url, _ in session.post_calls), 1)
        self.assertEqual(sum(bool(kwargs.get('data')) for _, kwargs in session.put_calls), 1)

    def test_catalog_precedes_upload_and_received_evidence_precedes_callbacks_and_readback(self):
        session, evidence = MetadataSession(), []
        def sink(event):
            session.events.append(event['verification_state'])
            evidence.append(event)
        def progress(percent, _message):
            if percent == 96:
                self.assertEqual(evidence[-1]['verification_state'], 'received')
                session.events.append('progress-after-id')
        result = self.upload(session, sink, progress=progress)
        self.assertEqual(result['url'], f'https://youtu.be/{VIDEO_ID}')
        self.assertEqual(session.events, ['catalog', 'post', 'put-file', 'received',
                                          'progress-after-id', 'readback', 'verified'])
        self.assertEqual(session.get_calls[0][1]['params'], {'part': 'snippet', 'id': '22'})
        self.assertEqual(session.get_calls[1][1]['params'], {'part': 'snippet,status,processingDetails', 'id': VIDEO_ID})
        expected_v2 = {
            'category_id': '22',
            'privacy_status': 'private',
            'made_for_kids': False,
            'altered_content': False,
            'publish_at': None,
            'youtube_channel_id': CHANNEL_ID,
        }
        self.assertEqual(evidence[0], {'version': 2, 'backend': 'youtube_api', 'video_id': VIDEO_ID,
            'verification_state': 'received', 'expected': expected_v2,
            'observed': {}, 'code': 'API_VIDEO_ID_RECEIVED', 'field_states': {}})
        self.assertEqual(evidence[-1]['observed']['category_id'], '22')
        self.assertEqual(evidence[-1]['observed']['youtube_channel_id'], CHANNEL_ID)
        self.assertEqual(evidence[-1]['observed']['privacy_status'], 'private')
        self.assertEqual(evidence[-1]['observed']['made_for_kids'], False)
        self.assertEqual(evidence[-1]['observed']['altered_content'], False)
        self.assert_one_upload(session)

    def test_resumable_status_id_also_persists_before_return_or_readback(self):
        session = MetadataSession(put_steps=[requests.Timeout('lost response'), _Response(201, body={'id': VIDEO_ID})])
        evidence = []
        def sink(event):
            session.events.append(event['verification_state'])
            evidence.append(event)
        self.upload(session, sink)
        self.assertEqual(session.events, ['catalog', 'post', 'put-file', 'put-status', 'received', 'readback', 'verified'])
        self.assertEqual([event['video_id'] for event in evidence], [VIDEO_ID, VIDEO_ID])
        self.assert_one_upload(session)

    def test_sink_failure_after_received_id_stops_without_readback_or_retry(self):
        for status_query in (False, True):
            with self.subTest(status_query=status_query):
                steps = [requests.Timeout('lost'), _Response(200, body={'id': VIDEO_ID})] if status_query else None
                session = MetadataSession(put_steps=steps)
                received = []
                def sink(event):
                    received.append(event)
                    raise OSError('evidence-store-unavailable')
                with self.assertRaisesRegex(OSError, 'evidence-store-unavailable'):
                    self.upload(session, sink)
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0]['video_id'], VIDEO_ID)
                self.assertNotIn('readback', session.events)
                self.assert_one_upload(session)

    def test_catalog_failures_are_not_sent_and_never_create_upload_session(self):
        cases = []
        cases.extend(JsonResponse({'items': [{'id': '22', 'snippet': {'assignable': value}}]})
                     for value in (False, None, 'true', 1))
        cases.extend([
            JsonResponse({'items': []}), JsonResponse({'items': [{'id': '24', 'snippet': {'assignable': True}}]}),
            JsonResponse({'items': [{'id': '22', 'snippet': {'assignable': True}}] * 2}),
            JsonResponse({'items': [None]}), JsonResponse([]), JsonResponse({'items': {}}),
            JsonResponse(ValueError('provider-secret-must-not-escape')), requests.Timeout('provider-secret-must-not-escape'),
        ])
        cases.extend(JsonResponse({}, status=code) for code in (403, 404, 429, 500))
        for response in cases:
            with self.subTest(response=response):
                session, evidence = MetadataSession(catalog=response), []
                with self.assertRaises(yu.UploadNotSent) as raised:
                    self.upload(session, evidence.append)
                self.assertNotIn('provider-secret', str(raised.exception))
                self.assertFalse(session.post_calls)
                self.assertFalse(session.put_calls)
                self.assertFalse(evidence)

    def test_wrong_oauth_channel_stops_before_catalog_and_upload(self):
        session = MetadataSession()
        with mock.patch.object(yu, '_fetch_oauth_channel_identity', return_value={'channel_id': 'UC'+'b'*22, 'name': 'Other'}):
            with self.assertRaises(RuntimeError):
                self.upload(session)
        self.assertFalse(session.get_calls)
        self.assertFalse(session.post_calls)
        self.assertFalse(session.put_calls)

    def test_readback_mismatch_and_missing_fields_preserve_received_id_without_reupload(self):
        wrong_id = video_payload()
        wrong_id['items'][0]['id'] = 'abcdefghijk'
        missing = video_payload()
        missing['items'][0].pop('snippet')
        cases = [(JsonResponse(video_payload('24')), 'API_VIDEO_CATEGORY_MISMATCH', 'mismatch'),
                 (JsonResponse(missing), 'API_VIDEO_CATEGORY_MISSING', 'unknown'),
                 (JsonResponse(wrong_id), 'API_VIDEO_READBACK_ID_MISSING', 'unknown'),
                 (JsonResponse({'items': [None]}), 'API_VIDEO_READBACK_MALFORMED', 'unknown'),
                 (JsonResponse([]), 'API_VIDEO_READBACK_MALFORMED', 'unknown'),
                 (JsonResponse(ValueError('provider-secret-must-not-escape')), 'API_VIDEO_READBACK_MALFORMED', 'unknown'),
                 (JsonResponse({}, status=403), 'API_VIDEO_READBACK_HTTP', 'unknown')]
        for response, code, state in cases:
            with self.subTest(code=code):
                session, evidence = MetadataSession(readbacks=[response]), []
                with self.assertRaises(yu.UploadVerificationError) as raised:
                    self.upload(session, evidence.append)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('provider-secret', str(raised.exception))
                self.assertEqual([event['verification_state'] for event in evidence], ['received', state])
                self.assertEqual(evidence[-1]['video_id'], VIDEO_ID)
                self.assertEqual(evidence[-1]['code'], code)
                self.assert_one_upload(session)

    def test_read_retries_are_bounded_by_deadline_and_never_repeat_upload(self):
        now = [100.0]
        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.assertLessEqual(seconds, 30)
            now[0] += seconds
        transient = [JsonResponse({}, status=429, headers={'Retry-After': '999999'}),
                     JsonResponse({}, status=503), requests.Timeout('provider-secret-must-not-escape')]
        for response in transient:
            with self.subTest(response=response):
                now[0] = 100.0
                session, evidence = MetadataSession(readbacks=[response] * 10), []
                with mock.patch.object(yu.time, 'monotonic', side_effect=lambda: now[0]), \
                        mock.patch.object(yu.time, 'sleep', side_effect=sleep):
                    with self.assertRaises(yu.UploadVerificationError) as raised:
                        self.upload(session, evidence.append)
                self.assertEqual(raised.exception.code, 'API_VIDEO_READBACK_TIMEOUT')
                self.assertLessEqual(now[0], 130.0)
                self.assertEqual(evidence[-1]['verification_state'], 'unknown')
                self.assert_one_upload(session)

    def test_transient_read_failure_can_recover_without_reupload(self):
        session = MetadataSession(readbacks=[JsonResponse({}, status=503), JsonResponse(video_payload())])
        evidence = []
        with mock.patch.object(yu.time, 'sleep'):
            self.upload(session, evidence.append)
        self.assertEqual([event['verification_state'] for event in evidence], ['received', 'verified'])
        self.assert_one_upload(session)

    def test_malformed_final_response_never_invents_id_or_reads_video(self):
        for response in (JsonResponse([]), JsonResponse({'id': 'provider-secret'}), JsonResponse(ValueError('provider-secret')),
                         *(JsonResponse({'id': value}) for value in (12345678901, None, [VIDEO_ID], ' '+VIDEO_ID))):
            session, evidence = MetadataSession(put_steps=[response]), []
            with self.assertRaises(yu.UploadVerificationError) as raised:
                self.upload(session, evidence.append)
            self.assertNotIn('provider-secret', str(raised.exception))
            self.assertEqual(evidence, [])
            self.assertNotIn('readback', session.events)
            self.assert_one_upload(session)

    def test_language_readback_compares_audio_field_without_mapping_title_language(self):
        metadata = {**self.metadata, 'video_language': 'en-US'}
        for language, expected_code in [('en-US', None), ('en', 'API_VIDEO_LANGUAGE_MISMATCH'),
                                        (None, 'API_VIDEO_LANGUAGE_MISSING')]:
            with self.subTest(language=language):
                payload = video_payload(language=language)
                payload['items'][0]['snippet']['defaultLanguage'] = 'en-US'
                session, evidence = MetadataSession(readbacks=[JsonResponse(payload)]), []
                if expected_code:
                    with self.assertRaises(yu.UploadVerificationError) as raised:
                        yu._verify_api_video(session, VIDEO_ID, 'fixture', metadata, evidence_sink=evidence.append)
                    self.assertEqual(raised.exception.code, expected_code)
                else:
                    yu._verify_api_video(session, VIDEO_ID, 'fixture', metadata, evidence_sink=evidence.append)
                    self.assertEqual(evidence[-1]['verification_state'], 'verified')
                self.assertFalse(session.post_calls)
                self.assertFalse(session.put_calls)

    def test_audio_language_upload_stays_gated_without_documented_insert_support(self):
        session = MetadataSession()
        with self.assertRaises(upload_capabilities.UploadCapabilityError):
            self.upload(session, metadata={**self.metadata, 'video_language': 'en-US'})
        self.assertEqual(session.events, [])
        self.assertFalse(upload_capabilities.capability_matrix()['youtube_api']['features']['video_language']['enabled'])

    def test_audio_language_keep_never_sends_either_language_field(self):
        session = MetadataSession()
        self.upload(session, metadata={**self.metadata, 'video_language': None, 'legacy_language': 'vi'})
        snippet = session.post_calls[0][1]['json']['snippet']
        self.assertNotIn('defaultAudioLanguage', snippet)
        self.assertNotIn('defaultLanguage', snippet)


if __name__ == '__main__':
    unittest.main()
