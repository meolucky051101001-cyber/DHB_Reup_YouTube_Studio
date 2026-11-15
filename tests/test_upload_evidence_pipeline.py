"""Evidence through real worker claims, reservations, restart and read-only API."""
from contextlib import closing
import hashlib
import json
import threading
import unittest
from unittest import mock

import database
import pipeline_manager as pm
import test_api_metadata_evidence as api_fixtures
import test_review_regressions as fixtures
import youtube_uploader as yu


VIDEO_ID = 'dQw4w9WgXcQ'
VIDEO_URL = f'https://youtu.be/{VIDEO_ID}'


class UploadEvidencePipelineTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def event(self, state='received', **extra):
        return {'backend': 'playwright', 'video_id': VIDEO_ID,
                'verification_state': state, 'expected': {'category_id': '22'}, **extra}

    def attempt(self):
        with closing(database.get_conn()) as conn:
            row = conn.execute('SELECT * FROM upload_attempts WHERE queue_id=901 ORDER BY attempt_no DESC LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            return dict(row)

    def process(self, uploader):
        operations = [False, False, True]
        preview = self.manager.preview_distribution([901], operations)
        self.assertFalse(preview['items'][0]['blocked'], preview)
        self.manager._freeze_batch_assignments([901], preview['preview_token'], operations, 'evidence-worker')
        with mock.patch.object(pm, 'upload_video_to_youtube', side_effect=uploader) as upload:
            self.manager._process_single_item_safe(901, 0, *operations, threading.Semaphore(0), 'evidence-worker')
        self.assertEqual(upload.call_count, 1, self.row()['error_message'])
        return upload

    def configure_api_channel(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE youtube_channels SET upload_mode='youtube_api',channel_id=?,"
                         "oauth_token_json=? WHERE id=901",
                         (api_fixtures.CHANNEL_ID, json.dumps({'access_token': 'offline-fixture-token'})))
            conn.execute("UPDATE video_queue SET visibility='private',made_for_kids=0 WHERE id=901")

    def process_api_session(self, session):
        self.configure_api_channel()
        persisted = []
        def upload(**kwargs):
            metadata = yu.validate_upload_preflight(kwargs['video_item'], kwargs['channel'], kwargs['option_group'])
            def record(event):
                kwargs['evidence_sink'](event)
                persisted.append(json.loads(self.attempt()['evidence_json']))
            return yu._upload_via_youtube_api_with_session(
                str(self.source), metadata, kwargs['channel'], session=session,
                progress_callback=kwargs['progress_callback'], evidence_sink=record)
        with mock.patch.object(yu, '_refresh_google_access_token', return_value='offline-fixture-token'), \
                mock.patch.object(yu, '_fetch_oauth_channel_identity',
                                  return_value={'channel_id': api_fixtures.CHANNEL_ID, 'name': 'Fixture'}), \
                mock.patch.object(yu, 'get_settings', return_value={'youtube_processing_timeout_seconds': 30}):
            self.process(upload)
        self.assertEqual(sum('uploadType=resumable' in url for url, _ in session.post_calls), 1)
        self.assertEqual(sum(bool(kwargs.get('data')) for _, kwargs in session.put_calls), 1)
        return persisted

    def assert_held(self, state):
        row = self.row()
        self.assertEqual(row['status'], 'NeedsReview', row['error_message'])
        self.assertEqual(row['published_url'], '')
        attempt = self.attempt()
        self.assertEqual(attempt['outcome'], 'NeedsReview')
        evidence = json.loads(attempt['evidence_json'])
        if state is None:
            self.assertEqual(evidence, {})
        else:
            self.assertEqual(evidence['verification_state'], state)
            self.assertEqual(evidence['video_id'], VIDEO_ID)
            self.assertEqual(evidence['attempt_id'], attempt['id'])
            self.assertEqual(evidence['queue_id'], 901)
        with closing(database.get_conn()) as conn:
            reservation = conn.execute('SELECT state FROM content_reservations WHERE queue_id=901').fetchone()
            self.assertIsNotNone(reservation)
            self.assertEqual(reservation['state'], 'NeedsReview')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM published_history WHERE channel_id=901').fetchone()[0], 0)
        self.assertEqual(row['input_video_path'], str(self.source))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def restart_without_retry(self):
        evidence_before = self.attempt()['evidence_json']
        restarted = pm.PipelineManager()
        restarted.clear_stale_claims()
        with mock.patch.object(pm, 'upload_video_to_youtube') as upload:
            restarted.process_single_item(901, do_download=False, do_render=False, do_upload=True)
        upload.assert_not_called()
        self.assertEqual(self.attempt()['evidence_json'], evidence_before)
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_attempts WHERE queue_id=901').fetchone()[0], 1)

    def test_mismatch_after_received_keeps_evidence_and_reservation_without_history(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            self.assertEqual(json.loads(self.attempt()['evidence_json'])['verification_state'], 'received')
            kwargs['evidence_sink'](self.event('mismatch', observed={'category_id': '27'}, code='CATEGORY_MISMATCH'))
            raise RuntimeError('Category verification mismatch')
        self.process(upload)
        self.assert_held('mismatch')
        self.restart_without_retry()
        self.assert_held('mismatch')

    def test_api_http_category_mismatch_persists_received_then_mismatch_without_retry(self):
        session = api_fixtures.MetadataSession(readbacks=[
            api_fixtures.JsonResponse(api_fixtures.video_payload(category='24'))])
        persisted = self.process_api_session(session)
        self.assertEqual([item['verification_state'] for item in persisted], ['received', 'mismatch'])
        self.assertTrue(all(item['backend'] == 'youtube_api' for item in persisted))
        self.assertTrue(all(item['expected'] == {'category_id': '22'} for item in persisted))
        self.assertEqual(persisted[0]['observed'], {})
        self.assertEqual(persisted[-1]['observed'], {'category_id': '24'})
        self.assertEqual(persisted[-1]['code'], 'API_VIDEO_CATEGORY_MISMATCH')
        self.assert_held('mismatch')
        self.restart_without_retry()
        self.assert_held('mismatch')

    def test_api_http_verified_readback_completes_with_bound_evidence(self):
        session = api_fixtures.MetadataSession()
        persisted = self.process_api_session(session)
        self.assertEqual([item['verification_state'] for item in persisted], ['received', 'verified'])
        row, attempt = self.row(), self.attempt()
        self.assertEqual(row['status'], 'Completed', row['error_message'])
        self.assertEqual(attempt['outcome'], 'Completed')
        self.assertEqual(row['published_url'], VIDEO_URL)
        evidence = json.loads(attempt['evidence_json'])
        self.assertEqual(evidence, persisted[-1])
        self.assertEqual(evidence['backend'], 'youtube_api')
        self.assertEqual(evidence['expected'], {'category_id': '22'})
        self.assertEqual(evidence['observed'], {'category_id': '22'})
        self.assertEqual((evidence['queue_id'], evidence['attempt_id'], evidence['channel_id']),
                         (901, attempt['id'], 901))
        with closing(database.get_conn()) as conn:
            history = conn.execute('SELECT published_url FROM published_history WHERE channel_id=901').fetchall()
            self.assertEqual([item['published_url'] for item in history], [VIDEO_URL])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM content_reservations WHERE queue_id=901').fetchone()[0], 0)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        self.restart_without_retry()
        self.assertEqual(self.row()['status'], 'Completed')

    def test_api_url_without_evidence_cannot_complete_or_release_reservation(self):
        self.configure_api_channel()
        self.process(lambda **kwargs: {'url': VIDEO_URL})
        self.assert_held(None)
        self.restart_without_retry()
        self.assert_held(None)

    def test_interrupt_after_received_survives_restart_without_retry(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            raise InterruptedError('Worker interrupted after receipt')
        self.process(upload)
        self.assert_held('received')
        self.restart_without_retry()
        self.assert_held('received')

    def test_late_not_sent_exception_cannot_override_received_id(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            raise pm.UploadNotSent('Injected late guard after video ID receipt')
        self.process(upload)
        self.assert_held('received')
        self.restart_without_retry()
        self.assert_held('received')

    def test_wrong_callback_expectation_or_backend_cannot_complete(self):
        for change in ({'expected': {'category_id': '24'}}, {'backend': 'youtube_api'}):
            with self.subTest(change=change):
                fixture = UploadEvidencePipelineTests()
                fixture.setUp()
                try:
                    def upload(**kwargs):
                        kwargs['evidence_sink'](fixture.event(**change))
                        self.fail('Callback must match the approved backend and metadata')
                    fixture.process(upload)
                    fixture.assert_held(None)
                    fixture.restart_without_retry()
                finally:
                    fixture.doCleanups()

    def test_verified_metadata_and_thumbnail_warning_complete_once(self):
        warnings = [{'step': 'thumbnail', 'code': 'HTTP_403', 'message': 'Thumbnail unavailable'}]
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            kwargs['evidence_sink'](self.event('verified', observed={'category_id': '22'}))
            return {'url': VIDEO_URL, 'warnings': warnings}
        self.process(upload)
        row, attempt = self.row(), self.attempt()
        self.assertEqual(row['status'], 'Completed', row['error_message'])
        self.assertEqual(row['published_url'], VIDEO_URL)
        self.assertEqual(attempt['outcome'], 'Completed')
        self.assertEqual(json.loads(row['post_upload_warnings_json']), warnings)
        self.assertEqual(json.loads(attempt['post_upload_warnings_json']), warnings)
        self.assertEqual(json.loads(attempt['evidence_json'])['verification_state'], 'verified')
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM published_history WHERE channel_id=901').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM content_reservations WHERE queue_id=901').fetchone()[0], 0)
        self.restart_without_retry()
        self.assertEqual(self.row()['status'], 'Completed')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_evidence_write_failure_holds_attempt_without_retry(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_received_evidence BEFORE UPDATE OF evidence_json ON upload_attempts
                WHEN NEW.evidence_json != OLD.evidence_json BEGIN SELECT RAISE(ABORT, 'SIMULATED_PRIVATE_DISK_ERROR'); END""")
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            self.fail('Readback must not continue when receipt evidence cannot be persisted')
        self.process(upload)
        self.assert_held(None)
        self.assertNotIn('SIMULATED_PRIVATE_DISK_ERROR', self.row()['error_message'])
        self.restart_without_retry()
        self.assert_held(None)

    def test_evidence_endpoint_exposes_only_validated_bound_evidence_and_is_readonly(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            raise InterruptedError('Stop after receipt')
        self.process(upload)
        before = self.row()
        attempt = self.attempt()
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE upload_attempts SET error_message='PRIVATE_ERROR_SENTINEL',reason='PRIVATE_REASON_SENTINEL' WHERE id=?", (attempt['id'],))
        valid = self.client.get('/api/queue/901/upload-evidence', headers=self.headers)
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(set(valid.json()), {'queue_id', 'attempts'})
        public = valid.json()['attempts'][0]
        self.assertEqual(set(public), {'id', 'attempt_no', 'outcome', 'evidence', 'evidence_error'})
        self.assertEqual(public['evidence']['video_url'], VIDEO_URL)
        self.assertFalse(public['evidence_error'])
        self.assertNotIn('PRIVATE_', valid.text)
        stored = json.loads(attempt['evidence_json'])
        invalid = ['{"token":"PRIVATE_JSON_SENTINEL"',
                   json.dumps({**stored, 'oauth_token': 'PRIVATE_EXTRA_SENTINEL'}),
                   json.dumps({**stored, 'queue_id': 902}),
                   json.dumps({**stored, 'video_url': 'https://example.test/PRIVATE_URL_SENTINEL'})]
        for raw in invalid:
            with self.subTest(raw=raw):
                with closing(database.get_conn()) as conn, conn:
                    conn.execute('UPDATE upload_attempts SET evidence_json=? WHERE id=?', (raw, attempt['id']))
                result = self.client.get('/api/queue/901/upload-evidence', headers=self.headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()['attempts'][0]['evidence_error'])
                self.assertIsNone(result.json()['attempts'][0]['evidence'])
                self.assertNotIn('PRIVATE_', result.text)
                with closing(database.get_conn()) as conn:
                    self.assertEqual(conn.execute('SELECT evidence_json FROM upload_attempts WHERE id=?', (attempt['id'],)).fetchone()[0], raw)
        self.assertEqual(self.row(), before)
        missing = self.client.get('/api/queue/99999/upload-evidence', headers=self.headers)
        self.assertEqual(missing.status_code, 404)

    def test_empty_legacy_attempt_with_null_channel_is_no_evidence_not_corruption(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO upload_attempts(queue_id,attempt_no,channel_id,outcome,evidence_json) VALUES(901,1,NULL,'NotSent','{}')")
        before = self.row()
        result = self.client.get('/api/queue/901/upload-evidence', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        attempt = result.json()['attempts'][0]
        self.assertIsNone(attempt['evidence'])
        self.assertFalse(attempt['evidence_error'])
        self.assertEqual(self.row(), before)

    def test_completion_transaction_rolls_back_queue_history_attempt_and_reservation_on_history_failure(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            kwargs['evidence_sink'](self.event('verified', observed={'category_id': '22'}))
            return {'url': VIDEO_URL}
        with closing(database.get_conn()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_published_history BEFORE INSERT ON published_history
                BEGIN SELECT RAISE(ABORT, 'SIMULATED_HISTORY_WRITE_ERROR'); END""")
        self.process(upload)
        self.assert_held('verified')
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM published_history WHERE channel_id=901').fetchone()[0], 0)
        self.restart_without_retry()

    def test_attempt_completion_failure_rolls_back_prior_queue_and_history_writes(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            kwargs['evidence_sink'](self.event('verified', observed={'category_id': '22'}))
            return {'url': VIDEO_URL}
        with closing(database.get_conn()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_attempt_completion BEFORE UPDATE OF outcome ON upload_attempts
                WHEN NEW.outcome='Completed' BEGIN SELECT RAISE(ABORT, 'SIMULATED_ATTEMPT_WRITE_ERROR'); END""")
        self.process(upload)
        self.assert_held('verified')
        self.restart_without_retry()

    def test_reservation_release_failure_rolls_back_all_completion_writes(self):
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            kwargs['evidence_sink'](self.event('verified', observed={'category_id': '22'}))
            return {'url': VIDEO_URL}
        with closing(database.get_conn()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_reservation_release BEFORE DELETE ON content_reservations
                BEGIN SELECT RAISE(ABORT, 'SIMULATED_RESERVATION_RELEASE_ERROR'); END""")
        self.process(upload)
        self.assert_held('verified')
        self.restart_without_retry()

    def test_post_completion_log_failure_does_not_downgrade_completed_upload(self):
        warnings = [{'step': 'thumbnail', 'code': 'HTTP_403', 'message': 'Thumbnail unavailable'}]
        def upload(**kwargs):
            kwargs['evidence_sink'](self.event())
            kwargs['evidence_sink'](self.event('verified', observed={'category_id': '22'}))
            return {'url': VIDEO_URL, 'warnings': warnings}
        original_log = pm.add_log
        def log(*args, **kwargs):
            if self.row()['status'] == 'Completed':
                raise RuntimeError('late log failure')
            return original_log(*args, **kwargs)
        with mock.patch.object(pm, 'add_log', side_effect=log):
            self.process(upload)
        row, attempt = self.row(), self.attempt()
        self.assertEqual(row['status'], 'Completed')
        self.assertEqual(row['published_url'], VIDEO_URL)
        self.assertEqual(attempt['outcome'], 'Completed')
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM published_history WHERE channel_id=901').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM content_reservations WHERE queue_id=901').fetchone()[0], 0)
        self.restart_without_retry()


if __name__ == '__main__':
    unittest.main()
