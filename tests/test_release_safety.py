"""Release regressions use disposable data, fake processes and local-only clones."""
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import database
import requests
import rollout_review_release as release
import runtime_identity
import test_review_regressions as fixtures
from validate_review_release import rehearse, snapshot, verify_transition


class LaunchSafetyTests(unittest.TestCase):
    def test_real_stream_query_token_is_absent_from_launch_log(self):
        sentinel = 'OFFLINE_STREAM_TOKEN_SENTINEL'
        with tempfile.TemporaryDirectory(prefix='stream log fixture ') as folder:
            root = Path(folder).resolve()
            (root / 'app.py').write_text(
                "from fastapi import FastAPI, Response\napp=FastAPI()\n"
                "@app.get('/api/queue/1/stream')\ndef stream(): return Response('synthetic media')\n",
                encoding='utf-8')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            log_path = root / 'server.log'
            process = release.launch(root, Path(sys.executable), port, log_path)
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), 'Fixture server exited before request.')
                        try:
                            response = session.get(f'http://127.0.0.1:{port}/api/queue/1/stream',
                                                   params={'token': sentinel}, timeout=1)
                            break
                        except requests.ConnectionError:
                            time.sleep(.05)
                    else:
                        self.fail('Fixture server startup timed out.')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, 'synthetic media')
            finally:
                release.stop_owned_launch(process)
                process.wait(timeout=5)
            log = log_path.read_text(encoding='utf-8')
            self.assertNotIn(sentinel, log)
            self.assertNotIn('/api/queue/1/stream', log)

    def test_launch_disables_access_log_and_scopes_git_without_mutating_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            inherited = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.sslVerify',
                         'GIT_CONFIG_VALUE_0': 'true', 'DHB_DISABLE_BACKGROUND_TASKS': '1'}
            with mock.patch.dict(os.environ, inherited, clear=True), mock.patch.object(release.subprocess, 'Popen') as popen:
                release.launch(root, Path(sys.executable), 8686, root / 'server.log')
                command = popen.call_args.args[0]
                env = popen.call_args.kwargs['env']
                self.assertIn('--no-access-log', command)
                self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.sslVerify')
                self.assertEqual(env['GIT_CONFIG_KEY_1'], 'safe.directory')
                self.assertEqual(env['GIT_CONFIG_VALUE_1'], root.as_posix())
                self.assertNotIn('DHB_DISABLE_BACKGROUND_TASKS', env)
                self.assertEqual(os.environ['GIT_CONFIG_COUNT'], '1')

    def test_process_matcher_accepts_old_and_new_exact_commands_only(self):
        root = Path.cwd()
        old = ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8686']
        listener = SimpleNamespace(laddr=SimpleNamespace(ip='127.0.0.1'), pid=123)
        process = mock.Mock()
        process.cwd.return_value = str(root)
        process.children.return_value = []
        with mock.patch.object(release, 'listeners', return_value=[listener]), mock.patch.object(release.psutil, 'Process', return_value=process):
            for args in (['app.py'], old, old + ['--no-access-log']):
                with self.subTest(args=args):
                    process.cmdline.return_value = [sys.executable, *args]
                    self.assertIs(release.verified_server(root, 8686), process)
            for args in (old + ['--reload'], old + ['--workers', '2'],
                         [part.replace('127.0.0.1', '0.0.0.0') for part in old],
                         old[:-1] + ['9999'], old + ['--no-access-log', '--reload']):
                with self.subTest(rejected=args):
                    process.cmdline.return_value = [sys.executable, *args]
                    with self.assertRaises(RuntimeError):
                        release.verified_server(root, 8686)

    def test_identity_pins_git_to_resolved_source_root(self):
        with tempfile.TemporaryDirectory(prefix='source with spaces ') as folder:
            root = Path(folder).resolve()
            with mock.patch.object(runtime_identity.subprocess, 'check_output', return_value='a' * 40) as probe:
                self.assertEqual(runtime_identity.capture_runtime(root)['revision'], 'a' * 40)
            command = probe.call_args.args[0]
            self.assertIn('safe.directory=' + root.as_posix(), command)
            self.assertEqual(command[command.index('-C') + 1], str(root))


class LogRetentionTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def seed_logs(self, count):
        database.init_db()  # normalize fixture rows before comparing migration
        with closing(database.get_conn()) as conn, conn:
            conn.execute('DELETE FROM system_logs')
            conn.executemany("INSERT INTO system_logs(module,message,status) VALUES('fixture',?,'INFO')",
                             [(f'log {index}',) for index in range(count)])

    def test_rehearsal_preserves_more_than_five_thousand_logs(self):
        self.seed_logs(5002)
        before = snapshot(database.APP_DB_PATH)
        result = rehearse(database.APP_DB_PATH)
        self.assertTrue(result['migration_idempotent'])
        self.assertTrue(result['restore_verified'])
        self.assertEqual(result['counts']['system_logs'], 5002)
        self.assertEqual(snapshot(database.APP_DB_PATH), before)

    def test_startup_preserves_logs_and_housekeeping_prunes_only_old_rows(self):
        self.seed_logs(5002)
        before = snapshot(database.APP_DB_PATH)
        database.init_db()
        verify_transition(before, snapshot(database.APP_DB_PATH), startup=True)
        with closing(database.get_conn()) as conn:
            retained = list(conn.execute('SELECT * FROM system_logs ORDER BY id DESC LIMIT 5000'))
        with mock.patch.object(database, 'backup_database_on_startup'):
            database.run_housekeeping()
        with closing(database.get_conn()) as conn:
            rows = {row['id']: tuple(row) for row in conn.execute('SELECT * FROM system_logs')}
        self.assertEqual(len(rows), 5001)
        self.assertTrue(all(rows[row['id']] == tuple(row) for row in retained))
        after = snapshot(database.APP_DB_PATH)
        for table in before[1]:
            if table != 'system_logs':
                self.assertEqual(before[1][table], after[1][table], table)

    def test_housekeeping_retention_boundaries_preserve_newest_ids_and_audit(self):
        for count in (0, 4999, 5000, 5001):
            with self.subTest(count=count):
                self.seed_logs(count)
                with closing(database.get_conn()) as conn:
                    expected = {row['id']: tuple(row) for row in conn.execute(
                        'SELECT * FROM system_logs ORDER BY id DESC LIMIT 5000')}
                with mock.patch.object(database, 'backup_database_on_startup'):
                    database.run_housekeeping()
                with closing(database.get_conn()) as conn:
                    rows = {row['id']: tuple(row) for row in conn.execute('SELECT * FROM system_logs')}
                self.assertEqual(len(rows), min(count, 5000) + int(count > 5000))
                self.assertTrue(all(rows[item_id] == row for item_id, row in expected.items()))


class RecoveryPreservationTests(unittest.TestCase):
    setUp = fixtures.ReviewRegressions.setUp

    def previous_checkout(self, *, prune=False, network=False):
        root = self.root / ('old-pruning-app' if prune else 'old-app')
        root.mkdir()
        shutil.copy2(Path(runtime_identity.__file__), root / 'runtime_identity.py')
        code = """import sqlite3
from pathlib import Path
from runtime_identity import capture_runtime
root = Path(__file__).resolve().parent
APP_BUILD_VERSION = 'recovery-fixture'
RUNTIME_IDENTITY = capture_runtime(root)
"""
        if prune:
            code += """with sqlite3.connect(root / 'data' / 'dhb_youtube.sqlite') as conn:
    conn.execute('DELETE FROM system_logs WHERE id NOT IN (SELECT id FROM system_logs ORDER BY id DESC LIMIT 5000)')
"""
        if network:
            code += """import socket
try:
    socket.getaddrinfo('forbidden.invalid', 443)
except PermissionError:
    pass
"""
        (root / 'app.py').write_text(code, encoding='utf-8')
        release.git(root, 'init', '--quiet')
        release.git(root, 'add', 'app.py', 'runtime_identity.py')
        release.git(root, '-c', 'user.name=Offline test', '-c', 'user.email=fixture@invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'recovery fixture')
        return root, release.git(root, 'rev-parse', 'HEAD')

    def test_previous_code_runs_with_scoped_identity_and_preserves_source(self):
        database.init_db()
        root, revision = self.previous_checkout()
        before = snapshot(database.APP_DB_PATH)
        report, approved = release.rehearse_recovery(root, revision, Path(sys.executable), database.APP_DB_PATH)
        self.assertEqual(report['revision'], revision)
        self.assertTrue(report['existing_evidence_preserved'])
        self.assertEqual(approved, before)
        self.assertEqual(snapshot(database.APP_DB_PATH), before)

    def test_old_pruning_code_is_rejected_without_changing_source(self):
        database.init_db()
        with closing(database.get_conn()) as conn, conn:
            conn.executemany("INSERT INTO system_logs(module,message) VALUES('fixture',?)",
                             [(f'log {index}',) for index in range(5002)])
        root, revision = self.previous_checkout(prune=True)
        before = snapshot(database.APP_DB_PATH)
        with self.assertRaisesRegex(RuntimeError, 'Startup removed'):
            release.rehearse_recovery(root, revision, Path(sys.executable), database.APP_DB_PATH)
        self.assertEqual(snapshot(database.APP_DB_PATH), before)

    def test_swallowed_network_attempt_still_rejects_recovery(self):
        root, revision = self.previous_checkout(network=True)
        before = snapshot(database.APP_DB_PATH)
        with self.assertRaisesRegex(RuntimeError, 'Recovery startup rehearsal failed'):
            release.rehearse_recovery(root, revision, Path(sys.executable), database.APP_DB_PATH)
        self.assertEqual(snapshot(database.APP_DB_PATH), before)

    def test_changed_or_unverified_db_cannot_launch_old_code(self):
        before = snapshot(database.APP_DB_PATH)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO system_logs(message) VALUES('after rehearsal')")
        for approved in (None, before):
            with self.subTest(verified=approved is not None), mock.patch.object(release, 'launch') as launch:
                with self.assertRaisesRegex(RuntimeError, 'verified snapshot'):
                    release.restore_previous(self.root, 'old', 'a' * 40, Path(sys.executable), 8686,
                                             self.root, mock.Mock(), 'http://127.0.0.1:8686',
                                             database.APP_DB_PATH, approved)
                launch.assert_not_called()

    def test_recovery_start_failure_stops_owned_child(self):
        before = snapshot(database.APP_DB_PATH)
        proc = mock.Mock()
        with mock.patch.object(release, 'git', side_effect=['', 'a' * 40, '']), \
             mock.patch.object(release, 'listeners', return_value=[]), \
             mock.patch.object(release, 'launch', return_value=proc), \
             mock.patch.object(release, 'wait_for_start', side_effect=RuntimeError('probe failed')), \
             mock.patch.object(release, 'stop_owned_launch') as stop:
            with self.assertRaisesRegex(RuntimeError, 'probe failed'):
                release.restore_previous(self.root, 'old', 'a' * 40, Path(sys.executable), 8686,
                                         self.root, mock.Mock(), 'http://127.0.0.1:8686',
                                         database.APP_DB_PATH, before)
            stop.assert_called_once_with(proc)

    def test_failure_checkpoints_only_restore_verified_prestart_state(self):
        runtime = self.root / 'runtime'
        evidence = self.root / 'evidence'
        runtime.mkdir()
        evidence.mkdir()
        args = SimpleNamespace(runtime=runtime, evidence=evidence, python=Path(sys.executable),
                               port=8686, revision='b' * 40, branch='candidate', apply=True,
                               acceptance=evidence / 'accepted.json')
        source = Path(release.__file__).resolve().parent
        before = snapshot(database.APP_DB_PATH)
        recovery_report = {'existing_evidence_preserved': True}
        for failure_at in ('preflight', 'before-switch', 'launch', 'after-start'):
            old_process, new_process = mock.Mock(), mock.Mock()
            def git(root, *command):
                if command == ('rev-parse', 'HEAD'):
                    return args.revision if root == source else 'a' * 40
                if command == ('branch', '--show-current'):
                    return 'previous'
                if command[:2] == ('switch', '-c') and failure_at == 'before-switch':
                    raise RuntimeError('fault before switch')
                return ''

            with self.subTest(failure_at=failure_at), ExitStack() as patches:
                replacements = {
                    'git': {'side_effect': git}, 'verified_server': {'return_value': old_process},
                    'http_state': {}, 'verify_idle_db': {}, 'rehearse': {'return_value': {}},
                    'rehearse_recovery': {'return_value': (recovery_report, before),
                                          'side_effect': RuntimeError('unsafe previous startup') if failure_at == 'preflight' else None},
                    'server_interpreter': {'return_value': Path(sys.executable)},
                    'verify_acceptance': {'return_value': {}},
                    'environment_manifest': {'return_value': {'build': 'fixture'}},
                    'interpreter_versions': {'return_value': {}}, 'listeners': {'return_value': []},
                    'backup': {}, 'snapshot': {'return_value': before}, 'acquire_app_instance_lock': {},
                    'launch': {'return_value': new_process, 'side_effect': RuntimeError('fault at launch') if failure_at == 'launch' else None},
                    'wait_for_start': {'side_effect': RuntimeError('fault after start')},
                    'restore_previous': {}, 'stop_owned_launch': {},
                }
                mocks = {name: patches.enter_context(mock.patch.object(release, name, **options))
                         for name, options in replacements.items()}
                restore, stop = mocks['restore_previous'], mocks['stop_owned_launch']
                with self.assertRaises(RuntimeError):
                    release.rollout(args)
                if failure_at == 'preflight':
                    old_process.terminate.assert_not_called()
                    mocks['launch'].assert_not_called()
                    restore.assert_not_called()
                elif failure_at == 'after-start':
                    restore.assert_not_called()
                    stop.assert_called_once_with(new_process)
                else:
                    self.assertEqual(restore.call_count, 1)
                    self.assertEqual(restore.call_args.args[-1], before)
                    stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
