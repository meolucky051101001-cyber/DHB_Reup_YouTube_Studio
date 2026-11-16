"""AI editing must not own SQLite while a provider runs or overwrite newer work."""
import json
import shutil
import sqlite3
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

import database
import test_review_regressions as fixtures
from fastapi import HTTPException
from upload_approval import PreviewStale


class AiEditConcurrencyTests(unittest.TestCase):
    row = fixtures.ReviewRegressions.row
    reserve_review = fixtures.ReviewRegressions.reserve_review

    def setUp(self):
        fixtures.ReviewRegressions.setUp(self)
        network = mock.patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def optimize(self, **kwargs):
        return {'title': 'Generated ' + kwargs.get('title', ''), 'description': 'Generated description',
                'tags': 'generated', 'provider_used': 'gemini', 'fallback': False, 'links_preserved': 0}

    def edit(self, ids=None, **kwargs):
        return self.app.api_ai_process_queue({'ids': ids if ids is not None else [901],
                                             'mode': 'rewrite_seo', **kwargs})

    def update(self, sql, values=()):
        with closing(database.get_conn()) as conn, conn:
            return conn.execute(sql, values).lastrowid

    def test_provider_runs_without_write_lock_after_each_previous_item(self):
        writers = []

        def provider(**kwargs):
            with closing(sqlite3.connect(database.APP_DB_PATH, timeout=.1)) as conn:
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute("INSERT INTO system_logs(module,message) VALUES('AI fixture','independent writer')")
                    conn.commit()
                    writers.append('available')
                except sqlite3.OperationalError:
                    writers.append('locked')
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit([901, 902])
        self.assertEqual(writers, ['available', 'available'])
        self.assertEqual(result['updated'], 2)

    def test_worker_finishes_with_review_reservation_while_provider_waits(self):
        entered, finished = threading.Event(), threading.Event()

        def provider(**kwargs):
            entered.set()
            self.assertTrue(finished.wait(5), 'concurrent writer did not finish')
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.edit)
            self.assertTrue(entered.wait(5))
            try:
                self.reserve_review()
                before = self.row()
            finally:
                finished.set()
            result = future.result(timeout=5)
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['failed'][0]['code'], 'item_guarded')
        self.assertEqual(self.row(), before)
        with closing(database.get_conn()) as conn:
            self.assertEqual(tuple(conn.execute('SELECT state,claim_token FROM content_reservations').fetchone()),
                             ('NeedsReview', 'previous'))

    def test_concurrent_editor_does_not_get_overwritten_and_other_item_commits(self):
        calls = 0

        def provider(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.update("UPDATE video_queue SET title='Other editor' WHERE id=901")
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit([901, 902])
        self.assertFalse(result['ok'])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['failed'][0]['code'], 'stale_snapshot')
        self.assertEqual(self.row()['title'], 'Other editor')
        self.assertEqual(self.row(902)['title'], 'Generated Title')
        self.assertEqual(result['provider_counts'], {'gemini': 1})

    def test_reused_attempt_changes_are_detected_without_a_new_attempt_id(self):
        attempt = self.update("INSERT INTO upload_attempts(queue_id,attempt_no,phase,outcome) VALUES(901,1,'Queued','Authorized')")
        self.update('UPDATE video_queue SET last_attempt_id=? WHERE id=901', (attempt,))

        def provider(**kwargs):
            self.update("UPDATE upload_attempts SET phase='Upload',outcome='Failed',reason='new work' WHERE id=?", (attempt,))
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit()
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['failed'][0]['code'], 'stale_snapshot')
        self.assertEqual(self.row()['title'], 'Title')

    def test_destination_change_is_detected(self):
        def provider(**kwargs):
            self.update('UPDATE video_queue SET target_channel_id=902 WHERE id=901')
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit()
        self.assertEqual(result['failed'][0]['code'], 'stale_snapshot')
        self.assertEqual(self.row()['title'], 'Title')

    def test_current_reservation_blocks_even_when_queue_is_rendered_and_claim_empty(self):
        def provider(**kwargs):
            self.reserve_review('Rendered')
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit()
        self.assertEqual(result['failed'][0]['code'], 'item_guarded')
        self.assertEqual(self.row()['title'], 'Title')

    def test_missing_requested_or_deleted_item_is_reported_once(self):
        def provider(**kwargs):
            self.update('DELETE FROM video_queue WHERE id=901')
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider):
            result = self.edit([901, 901, 99999])
        self.assertFalse(result['ok'])
        self.assertEqual(result['updated'], 0)
        self.assertEqual({(entry['id'], entry['code']) for entry in result['failed']},
                         {(901, 'item_missing'), (99999, 'item_missing')})
        self.assertEqual(len(result['failed']), 2)

    def test_initial_busy_selection_fails_before_provider_or_any_write(self):
        self.reserve_review()
        before = [self.row(i) for i in (901, 902)]
        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata') as provider:
            with self.assertRaises(HTTPException) as caught:
                self.edit([901, 902])
            self.assertEqual(caught.exception.status_code, 409)
            provider.assert_not_called()
        self.assertEqual([self.row(i) for i in (901, 902)], before)

    def test_batch_limits_allow_partial_success_without_truncation(self):
        for field, length in (('title', 95), ('description', 4995)):
            with self.subTest(field=field):
                self.update('UPDATE video_queue SET title=?,description=? WHERE id=901',
                            ('t' * (length if field == 'title' else 5), 'd' * (length if field == 'description' else 5)))
                self.update("UPDATE video_queue SET title='Short',description='Short' WHERE id=902")
                before = self.row()
                result = self.edit([901, 902], mode='batch_edit', edit_field=field,
                                   edit_op='append_end', replace_str='1234567890')
                self.assertFalse(result['ok'])
                self.assertEqual(result['updated'], 1)
                self.assertEqual(result['failed'][0]['code'], 'invalid_metadata')
                self.assertEqual(self.row(), before)
                self.assertEqual(self.row(902)[field], 'Short 1234567890')

    def test_batch_edit_preserves_unselected_fields_and_checks_race(self):
        self.update("UPDATE video_queue SET description='  unselected whitespace  ' WHERE id=901")
        result = self.edit(mode='batch_edit', edit_field='title', edit_op='uppercase')
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.row()['description'], '  unselected whitespace  ')
        original = self.app.apply_batch_text_edit

        def edited(*args):
            self.update("UPDATE video_queue SET title='New editor' WHERE id=901")
            return original(*args)

        with mock.patch.object(self.app, 'apply_batch_text_edit', side_effect=edited):
            result = self.edit(mode='batch_edit', edit_field='title', edit_op='uppercase')
        self.assertEqual(result['failed'][0]['code'], 'stale_snapshot')
        self.assertEqual(self.row()['title'], 'New editor')

    def test_invalid_ai_output_is_rejected_atomically(self):
        outputs = [None, {'title': 'Missing'}, {'title': [], 'description': '', 'tags': ''},
                   {'title': 'x' * 101, 'description': '', 'tags': ''},
                   {'title': 'Valid', 'description': 'x' * 5001, 'tags': ''}]
        before = self.row()
        for output in outputs:
            with self.subTest(output_type=type(output).__name__), mock.patch.object(
                    self.app, 'ai_optimize_youtube_metadata', return_value=output):
                result = self.edit()
            self.assertEqual(result['updated'], 0)
            self.assertEqual(result['failed'][0]['code'], 'invalid_metadata')
            self.assertEqual(self.row(), before)

    def test_provider_failure_keeps_prior_commits_and_does_not_echo_secret(self):
        sentinel = 'PRIVATE_PROVIDER_TOKEN'
        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata',
                               side_effect=[self.optimize(title='first'), RuntimeError(sentinel)]):
            result = self.edit([901, 902])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['failed'][0]['code'], 'provider_failed')
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(self.row()['title'], 'Generated first')
        self.assertEqual(self.row(902)['title'], 'Title')

    def test_two_ai_requests_with_same_snapshot_only_one_wins(self):
        ready = threading.Barrier(2)

        def provider(**kwargs):
            ready.wait(timeout=5)
            return self.optimize(**kwargs)

        with mock.patch.object(self.app, 'ai_optimize_youtube_metadata', side_effect=provider), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.edit) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(result['updated'] for result in results), [0, 1])
        self.assertEqual(next(result for result in results if not result['updated'])['failed'][0]['code'],
                         'stale_snapshot')

    def test_successful_edit_invalidates_existing_upload_preview(self):
        options = [False, False, True]
        preview = self.manager.preview_distribution([901], options)
        self.assertTrue(preview['preview_token'])
        result = self.edit(mode='batch_edit', edit_op='append_end', replace_str='updated')
        self.assertEqual(result['updated'], 1)
        with self.assertRaises(PreviewStale):
            self.manager._freeze_batch_assignments([901], preview['preview_token'], options, 'stale-ai-edit')
        self.assertFalse(self.row()['claim_token'])

    def test_empty_ids_select_only_project_and_request_validation_is_400(self):
        result = self.edit([], mode='batch_edit', project_name='review', edit_op='uppercase')
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.row()['title'], 'TITLE')
        self.assertEqual(self.row(902)['title'], 'Title')
        for request in ({'ids': 'bad'}, {'mode': 'unknown'}, {'edit_field': 'status'}):
            with self.subTest(request=request), self.assertRaises(HTTPException) as caught:
                self.app.api_ai_process_queue(request)
            self.assertEqual(caught.exception.status_code, 400)

    def test_empty_selection_can_edit_default_and_monitor_projects(self):
        for project in ('Default', 'Monitor - Offline fixture'):
            with self.subTest(project=project):
                self.update("UPDATE video_queue SET project_name=?,title='Target' WHERE id=901", (project,))
                result = self.edit([], mode='batch_edit', project_name=project, edit_op='uppercase')
                self.assertEqual(result['updated'], 1)
                self.assertEqual(self.row()['title'], 'TARGET')
                self.assertEqual(self.row(902)['title'], 'Title')
        self.update("UPDATE video_queue SET project_name='Default',title='Default selection' WHERE id=901")
        result = self.edit([], mode='batch_edit', edit_op='uppercase')
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.row()['title'], 'DEFAULT SELECTION')

    def test_falsey_nontext_project_cannot_select_default(self):
        self.update("UPDATE video_queue SET project_name='Default' WHERE id=901")
        before = self.row()
        for project in (False, 0, [], {}):
            with self.subTest(project=project), self.assertRaises(HTTPException) as caught:
                self.edit([], mode='batch_edit', project_name=project, edit_op='uppercase')
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(self.row(), before)


class AiEditFrontendTests(unittest.TestCase):
    def test_partial_results_refresh_both_modes_and_duplicate_click_is_ignored(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the frontend regression gate')
        source = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const fn = source.slice(source.indexOf('async function runAIProcess(type)'), source.indexOf('async function loadRenderResources()'));
async function check(mode) {
  const alerts = []; let requests = 0, refreshes = 0, release;
  const context = {getTargetIdsOrAlert: () => [901,902], gAIProcessRunning: false,
    document: {getElementById: () => ({value: 'value'}), querySelectorAll: () => []},
    apiFetch: () => {requests++; return new Promise(resolve => release = resolve);},
    alert: message => alerts.push(message), loadQueue: async () => {refreshes++;}};
  vm.createContext(context); vm.runInContext(fn, context);
  const first = context.runAIProcess(mode), duplicate = context.runAIProcess(mode);
  assert.equal(requests, 1, 'duplicate click sent another request');
  release({updated:1,ok:false,failed:[{id:901,code:'stale_snapshot',error:'Current data changed'}]});
  await Promise.all([first, duplicate]);
  assert.equal(refreshes, 1); assert.equal(alerts.length, 1);
  assert.match(alerts[0], /901/); assert.match(alerts[0], /stale_snapshot/);
  assert.match(alerts[0], /Current data changed/);
  assert.doesNotMatch(alerts[0], /metadata gốc được giữ/);
  const next = context.runAIProcess(mode); assert.equal(requests, 2);
  release({updated:1,ok:true,failed:[]}); await next;
}
(async () => {await check('batch_edit'); await check('ai');})().catch(error => {console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
