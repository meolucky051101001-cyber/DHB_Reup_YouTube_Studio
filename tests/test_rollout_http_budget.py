"""Diagnostics alone needs room for its three serial bounded tool probes."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

import requests
import rollout_review_release as release


class RolloutHTTPBudgetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent
        self.revision = 'a' * 40
        self.base = 'http://127.0.0.1:18686'
        self.payloads = [
            {'application': release.APPLICATION_ID, 'source_root': str(self.root)},
            {'token': 'offline-fixture-token'},
            {'pipeline_running': False, 'monitors_running': 0},
            {'runtime': {'revision': self.revision, 'restart_required': False,
                         'startup_fingerprint': 'fixture-fingerprint'}},
        ]
        self.fingerprint = mock.patch.object(release, 'source_fingerprint', return_value='fixture-fingerprint')
        self.fingerprint.start()
        self.addCleanup(self.fingerprint.stop)

    def session(self, payloads):
        session = mock.Mock(spec=requests.Session)
        responses = []
        for payload in payloads:
            if isinstance(payload, Exception):
                responses.append(payload)
            else:
                response = mock.Mock()
                response.json.return_value = payload
                responses.append(response)
        session.get.side_effect = responses
        return session

    def test_only_diagnostics_receives_the_larger_read_budget(self):
        session = self.session(self.payloads)
        result = release.http_state(session, self.base, self.root, self.revision)
        self.assertEqual(result, self.payloads[-1])
        calls = session.get.call_args_list
        self.assertEqual([15, 15, 15, 45], [call.kwargs['timeout'] for call in calls])
        self.assertEqual([self.base + path for path in (
            '/health/dhb', '/api/auth/token', '/api/overview', '/api/diagnostics')],
            [call.args[0] for call in calls])
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in calls))
        self.assertTrue(all(call[0] == 'get' for call in session.method_calls))

    def test_diagnostics_read_timeout_propagates_without_retry_or_extra_actions(self):
        timeout = requests.ReadTimeout('offline diagnostics timeout')
        session = self.session(self.payloads[:3] + [timeout])
        with self.assertRaises(requests.ReadTimeout) as raised:
            release.http_state(session, self.base, self.root, self.revision)
        self.assertIs(raised.exception, timeout)
        self.assertEqual(session.get.call_count, 4)
        self.assertTrue(all(call[0] == 'get' for call in session.method_calls))

    def test_identity_and_busy_checks_remain_fail_closed(self):
        cases = (
            (0, 'application', 'other-app', 1),
            (0, 'source_root', str(self.root / 'other'), 1),
            (2, 'pipeline_running', True, 3),
            (2, 'monitors_running', 1, 3),
            (3, 'revision', 'b' * 40, 4),
            (3, 'restart_required', True, 4),
            (3, 'startup_fingerprint', 'other-fingerprint', 4),
        )
        for index, field, value, count in cases:
            with self.subTest(field=field):
                payloads = deepcopy(self.payloads)
                target = payloads[index]['runtime'] if index == 3 else payloads[index]
                target[field] = value
                session = self.session(payloads)
                with self.assertRaises(RuntimeError):
                    release.http_state(session, self.base, self.root, self.revision)
                self.assertEqual(session.get.call_count, count)
                self.assertTrue(all(call[0] == 'get' for call in session.method_calls))


if __name__ == '__main__':
    unittest.main()
