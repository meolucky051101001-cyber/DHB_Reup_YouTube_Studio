"""CI transports share the same immutable release acceptance requirements."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import requests
import rollout_review_release as release


REPO = 'owner/project'
RUN = '42'
SHA = 'a' * 40
BASE = f'https://api.github.com/repos/{REPO}/actions/runs/{RUN}'


def response(data, status=200):
    result = mock.Mock(status_code=status)
    result.json.return_value = deepcopy(data)
    return result


def job(identity, name, conclusion='success'):
    return {'id': identity, 'run_id': 42, 'head_sha': SHA,
            'name': name, 'conclusion': conclusion}


class CIRunTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.acceptance = root / 'acceptance.json'
        record = dict.fromkeys(('full_suite', 'chromium', 'clean_start', 'deterministic', 'wall_clock_soak'), True)
        record.update(revision=SHA, repo=REPO, ci_run_id=RUN, soak_report='soak.jsonl')
        self.acceptance.write_text(json.dumps(record), encoding='utf-8')
        events = [{'event': 'round', 'jobs': 100, 'workers': workers}
                  for workers in (1, 4, 8, 1, 4, 8, 1, 4, 8, 8)]
        events.append({'event': 'complete', 'pilot': False, 'revision': SHA,
                       'seconds': 3600, 'jobs': 1000, 'uploads': {'gap_seconds': 1800}})
        (root / 'soak.jsonl').write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
        self.run = {'id': 42, 'repository': {'full_name': REPO}, 'head_sha': SHA,
                    'conclusion': 'success', 'status': 'completed', 'html_url': 'https://untrusted.invalid/'}
        self.jobs = {'total_count': 2, 'jobs': [job(1, 'unittest'), job(2, 'media')]}
        self.cli = self.stack.enter_context(mock.patch.object(
            release.subprocess, 'check_output', side_effect=FileNotFoundError('gh unavailable')))
        self.factory = self.stack.enter_context(mock.patch.object(release.requests, 'Session'))
        self.http = self.factory.return_value.__enter__.return_value
        self.http.get.side_effect = [response(self.run), response(self.jobs)]

    def verify(self):
        return release.verify_acceptance(self.acceptance, SHA)

    def test_cli_success_preserves_command_and_does_not_request_http(self):
        self.cli.side_effect = None
        self.cli.return_value = json.dumps({'headSha': SHA, 'conclusion': 'success',
                                           'jobs': self.jobs['jobs'], 'url': 'cli-result'})
        self.assertEqual(self.verify()['ci'], 'cli-result')
        self.cli.assert_called_once_with(
            ['gh', 'run', 'view', RUN, '--repo', REPO, '--json', 'headSha,conclusion,jobs,url'],
            text=True, encoding='utf-8', timeout=30)
        self.factory.assert_not_called()

    def test_cli_missing_nonzero_and_timeout_use_public_transport(self):
        for error in (FileNotFoundError(), subprocess.CalledProcessError(4, ['gh']),
                      subprocess.TimeoutExpired(['gh'], 30)):
            with self.subTest(error=type(error).__name__):
                self.cli.side_effect = error
                self.http.get.reset_mock()
                self.http.get.side_effect = [response(self.run), response(self.jobs)]
                self.assertEqual(self.verify()['ci'], f'https://github.com/{REPO}/actions/runs/{RUN}')
                self.assertFalse(self.http.trust_env)
                self.assertEqual([call.args[0] for call in self.http.get.call_args_list],
                                 [BASE, BASE + '/jobs?filter=latest&per_page=100&page=1'])
                for call in self.http.get.call_args_list:
                    self.assertEqual(call.kwargs, {'timeout': (5, 15), 'allow_redirects': False,
                                                  'headers': {'Accept': 'application/vnd.github+json'}})

    def test_cli_wrong_sha_or_malformed_json_is_not_hidden_by_fallback(self):
        self.cli.side_effect = None
        for payload in ('not-json', json.dumps({'headSha': 'b' * 40, 'conclusion': 'success',
                                               'jobs': self.jobs['jobs'], 'url': 'cli-result'})):
            with self.subTest(payload=payload):
                self.cli.return_value = payload
                with self.assertRaises((RuntimeError, ValueError)):
                    self.verify()
        self.factory.assert_not_called()

    def test_public_wrong_sha_and_failed_run_fail_original_gate(self):
        for change in ({'head_sha': 'b' * 40}, {'conclusion': 'failure'}):
            with self.subTest(change=change):
                jobs = deepcopy(self.jobs)
                for row in jobs['jobs']:
                    row['head_sha'] = change.get('head_sha', SHA)
                self.http.get.side_effect = [response({**self.run, **change}), response(jobs)]
                with self.assertRaisesRegex(RuntimeError, 'Release CI is incomplete'):
                    self.verify()

    def test_public_wrong_repository_or_run_id_and_missing_identity_fail(self):
        for change in ({'id': 43}, {'id': True}, {'repository': {'full_name': 'other/project'}},
                       {'repository': {}}, {'repository': None}):
            with self.subTest(change=change):
                self.http.get.side_effect = [response({**self.run, **change})]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_failed_or_missing_required_job_fails_original_gate(self):
        for jobs in ([job(1, 'unittest'), job(2, 'media', 'failure')],
                     [job(1, 'unittest')], [job(2, 'media')], []):
            with self.subTest(jobs=jobs):
                self.http.get.side_effect = [response(self.run), response({'total_count': len(jobs), 'jobs': jobs})]
                with self.assertRaisesRegex(RuntimeError, 'Release CI is incomplete'):
                    self.verify()

    def test_http_errors_redirect_and_rate_limit_fail_closed(self):
        for status in (301, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.http.get.side_effect = [response({}, status)]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_network_timeout_and_malformed_json_fail_closed(self):
        malformed = response({})
        malformed.json.side_effect = ValueError('invalid JSON')
        for result in (requests.Timeout('offline'), malformed):
            with self.subTest(result=type(result).__name__):
                self.http.get.side_effect = [result]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_run_and_jobs_schema_fail_closed(self):
        for data in ([], {}, {**self.run, 'head_sha': 'short'}, {**self.run, 'status': 'in_progress'}):
            with self.subTest(run=data):
                self.http.get.side_effect = [response(data)]
                with self.assertRaises(RuntimeError):
                    self.verify()
        for data in ({}, {'total_count': True, 'jobs': []}, {'total_count': 2, 'jobs': {}},
                     {'total_count': 1001, 'jobs': []}, {'total_count': 0, 'jobs': self.jobs['jobs']}):
            with self.subTest(jobs=data):
                self.http.get.side_effect = [response(self.run), response(data)]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_job_schema_identity_and_sha_fail_closed(self):
        for change in ({'id': True}, {'run_id': 43}, {'head_sha': 'b' * 40},
                       {'name': None}, {'conclusion': None}):
            with self.subTest(change=change):
                rows = [{**job(1, 'unittest'), **change}, job(2, 'media')]
                self.http.get.side_effect = [response(self.run), response({'total_count': 2, 'jobs': rows})]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_all_job_pages_are_fetched_using_only_fixed_urls(self):
        first = [job(1, 'unittest')] + [job(index, f'other-{index}') for index in range(2, 101)]
        self.http.get.side_effect = [response(self.run), response({'total_count': 101, 'jobs': first}),
                                    response({'total_count': 101, 'jobs': [job(101, 'media')],
                                              'next': 'https://untrusted.invalid/jobs'})]
        self.verify()
        self.assertEqual(self.http.get.call_args_list[-1].args[0],
                         BASE + '/jobs?filter=latest&per_page=100&page=2')
        self.assertEqual(self.http.get.call_count, 3)

    def test_incomplete_duplicate_or_changing_pages_fail_closed(self):
        batches = ([{'total_count': 2, 'jobs': []}],
                   [{'total_count': 2, 'jobs': [job(1, 'unittest')]},
                    {'total_count': 2, 'jobs': [job(1, 'media')]}],
                   [{'total_count': 2, 'jobs': [job(1, 'unittest')]},
                    {'total_count': 3, 'jobs': [job(2, 'media')]}])
        for pages in batches:
            with self.subTest(pages=pages):
                self.http.get.side_effect = [response(self.run)] + [response(page) for page in pages]
                with self.assertRaises(RuntimeError):
                    self.verify()

    def test_pagination_limit_is_bounded(self):
        pages = [{'total_count': 1000, 'jobs': [job(index, f'job-{index}')]} for index in range(1, 11)]
        self.http.get.side_effect = [response(self.run)] + [response(page) for page in pages]
        with self.assertRaisesRegex(RuntimeError, 'pagination limit'):
            self.verify()
        self.assertEqual(self.http.get.call_count, 11)

    def test_invalid_repository_or_run_cannot_construct_request_urls(self):
        for repo, run in (('owner/project/../../evil', RUN), ('https://evil.invalid', RUN),
                          ('../project', RUN), ('owner/..', RUN),
                          (REPO, '42?url=evil'), (REPO, '٤٢')):
            with self.subTest(repo=repo, run=run):
                with self.assertRaises(RuntimeError):
                    release.public_ci_run(repo, run)
        self.factory.assert_not_called()

    def test_offline_flags_and_soak_still_fail_before_any_ci_transport(self):
        record = json.loads(self.acceptance.read_text(encoding='utf-8'))
        record['deterministic'] = False
        self.acceptance.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Offline acceptance is incomplete'):
            self.verify()
        self.cli.assert_not_called()
        self.factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
