"""Release/scheduler exclusion uses disposable files and fake runtime operations."""
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import rollout_review_release as release
from scheduled_uploads import (
    CONTRACT_VERSION, MANIFEST_VERSION, ScheduleError, ScheduleRunner, new_state,
    runtime_operation_lock, single_instance,
)


class RolloutScheduleLockTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='rollout schedule fixture ')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.source = self.root / 'release-source'
        self.source.mkdir()
        self.runtime = self.root / 'runtime'
        self.evidence = self.root / 'evidence'
        self.runtime.mkdir()
        self.evidence.mkdir()
        self.schedule = self.runtime / 'data' / 'scheduled_uploads' / 'default'
        self.args = SimpleNamespace(
            runtime=self.runtime, evidence=self.evidence, python=Path(sys.executable),
            revision='b' * 40, branch='candidate', port=8686, apply=True,
            acceptance=self.evidence / 'accepted.json',
        )
        self.old_process = mock.Mock(pid=123)
        self.new_process = mock.Mock(pid=456)
        self.old_pin = {'revision': 'a' * 40, 'build': 'previous'}
        self.new_pin = {'revision': 'b' * 40, 'build': 'candidate'}
        self.before = ({}, {})
        self.phases = []
        environment = mock.patch.dict(os.environ, {'DHB_SCHEDULE_STATE_DIR': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def configure(self):
        self.schedule.mkdir(parents=True)
        manifest = {
            'version': MANIFEST_VERSION, 'runner_contract': CONTRACT_VERSION,
            'project': 'fixture', 'cadence_hours': 72,
            'start_at': '2026-11-20T23:15:00+07:00',
            'channel': {'id': 1, 'channel_id': 'UC' + 'a' * 22, 'name': 'fixture'},
            'source_directory': str(self.root),
            'jobs': [{'queue_id': 1, 'path': str(self.root / 'fixture.mp4'),
                      'sha256': '0' * 64}],
        }
        for name, value in (('manifest.json', manifest), ('state.json', new_state(manifest)),
                            ('release.json', self.old_pin)):
            release.save_schedule_json(self.schedule / name, value)
        return self.schedule_bytes()

    def schedule_bytes(self):
        return {name: (self.schedule / name).read_bytes()
                for name in ('manifest.json', 'state.json', 'release.json')}

    def assert_competitor_busy(self, operation, expected, phase):
        outcomes = []

        def compete():
            try:
                operation()
            except ScheduleError as exc:
                outcomes.append(exc.code)
            except BaseException as exc:
                outcomes.append(type(exc).__name__)
            else:
                outcomes.append('competitor acquired the release lock')

        runner = threading.Thread(target=compete, daemon=True)
        runner.start()
        runner.join(timeout=2)
        self.assertFalse(runner.is_alive(), 'Busy scheduler lock must fail without waiting.')
        self.assertEqual(outcomes, [expected], phase)

    def assert_runner_excluded(self, phase):
        self.phases.append(phase)

        def enter_runtime():
            with runtime_operation_lock(self.runtime):
                pass

        self.assert_competitor_busy(enter_runtime, 'RUNTIME_OPERATION_ACTIVE', phase)
        if not (self.schedule / 'manifest.json').is_file():
            return
        api = mock.Mock(side_effect=AssertionError('Fixture must not contact a runtime.'))
        self.assert_competitor_busy(
            lambda: ScheduleRunner(self.schedule, self.runtime, api_factory=api).run(),
            'RUNTIME_OPERATION_ACTIVE', phase)
        api.assert_not_called()
        if self.expect_schedule_lock:
            def enter_schedule():
                with single_instance(self.schedule):
                    pass

            self.assert_competitor_busy(enter_schedule, 'RUNNER_ALREADY_ACTIVE', phase)

    def assert_lock_released(self):
        with runtime_operation_lock(self.runtime), single_instance(self.schedule):
            pass

    def fake_operations(self):
        self.expect_schedule_lock = (self.schedule / 'manifest.json').is_file()
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(
            release, '__file__', str(self.source / 'rollout_review_release.py')))

        def git(root, *command):
            if command == ('rev-parse', 'HEAD'):
                return self.args.revision if root == self.source else 'a' * 40
            if command == ('branch', '--show-current'):
                return 'previous'
            if command[:2] == ('switch', '-c'):
                self.assert_runner_excluded('switch')
            return ''

        def acquire(_path):
            self.assert_runner_excluded('app-lock')
            return SimpleNamespace(release=lambda: self.assert_runner_excluded('app-unlock'))

        def launch(*_args, **_kwargs):
            self.assert_runner_excluded('launch')
            return self.new_process

        def wait(*_args):
            self.assert_runner_excluded('startup-check')
            return {'version': 'candidate', 'runtime': {}, 'tools': [],
                    'database': {'schemas': {}}}

        def browser(*_args):
            self.assert_runner_excluded('browser-check')
            return {}

        operations = {
            'git': {'side_effect': git},
            'verified_server': {'return_value': self.old_process},
            'http_state': {}, 'verify_idle_db': {},
            'rehearse': {'return_value': {}},
            'rehearse_recovery': {'return_value': ({}, self.before)},
            'server_interpreter': {'return_value': Path(sys.executable)},
            'verify_acceptance': {'return_value': {}},
            'environment_manifest': {'return_value': {'build': 'candidate'}},
            'interpreter_versions': {'return_value': {}},
            'listeners': {'return_value': []}, 'backup': {},
            'snapshot': {'return_value': self.before},
            'acquire_app_instance_lock': {'side_effect': acquire},
            'launch': {'side_effect': launch}, 'wait_for_start': {'side_effect': wait},
            'build_schedule_release_pin': {'return_value': self.new_pin},
            'verify_readonly_ui': {'side_effect': browser},
            'verify_transition': {'return_value': {}},
            'sha256_file': {'return_value': '0' * 64},
            'restore_previous': {'side_effect': lambda *_args, **_kwargs: self.assert_runner_excluded('rollback')},
            'stop_owned_launch': {'side_effect': lambda *_args: self.assert_runner_excluded('stop-new')},
        }
        return {name: patches.enter_context(mock.patch.object(release, name, **options))
                for name, options in operations.items()}

    def test_busy_runner_rejects_apply_before_touching_runtime(self):
        original = self.configure()
        operations = self.fake_operations()
        with single_instance(self.schedule):
            with self.assertRaises(ScheduleError) as caught:
                release.rollout(self.args)
        self.assertEqual(caught.exception.code, 'RUNNER_ALREADY_ACTIVE')
        operations['verified_server'].assert_not_called()
        operations['acquire_app_instance_lock'].assert_not_called()
        operations['launch'].assert_not_called()
        self.old_process.terminate.assert_not_called()
        self.assertEqual(self.schedule_bytes(), original)
        self.assert_lock_released()

    def test_success_excludes_runner_before_app_lock_through_verification(self):
        original = self.configure()
        self.fake_operations()
        result = release.rollout(self.args)
        self.assertEqual(result['mode'], 'applied')
        self.assertEqual(self.phases, ['app-lock', 'switch', 'app-unlock', 'launch',
                                      'startup-check', 'browser-check'])
        current = self.schedule_bytes()
        for name in ('manifest.json', 'state.json'):
            self.assertEqual(current[name], original[name])
        self.assertEqual(json.loads(current['release.json']), self.new_pin)
        self.assert_lock_released()

    def test_preflight_failure_releases_lock_without_stopping_runtime(self):
        original = self.configure()
        operations = self.fake_operations()

        def fail(*_args, **_kwargs):
            self.assert_runner_excluded('preflight')
            raise RuntimeError('unsafe previous startup')

        operations['rehearse_recovery'].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'unsafe previous startup'):
            release.rollout(self.args)
        self.old_process.terminate.assert_not_called()
        operations['restore_previous'].assert_not_called()
        self.assertEqual(self.schedule_bytes(), original)
        self.assert_lock_released()

    def test_launch_failure_restores_pin_and_old_runtime_under_lock(self):
        original = self.configure()
        operations = self.fake_operations()

        def fail(*_args, **_kwargs):
            self.assert_runner_excluded('failed-launch')
            raise RuntimeError('launch rejected')

        operations['launch'].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'launch rejected'):
            release.rollout(self.args)
        self.assertIn('rollback', self.phases)
        operations['restore_previous'].assert_called_once()
        self.assertEqual(self.schedule_bytes(), original)
        self.assert_lock_released()

    def test_post_start_failure_stops_owned_child_while_runner_remains_excluded(self):
        original = self.configure()
        operations = self.fake_operations()

        def fail(*_args, **_kwargs):
            self.assert_runner_excluded('failed-startup-check')
            raise RuntimeError('startup identity mismatch')

        operations['wait_for_start'].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'Runtime stopped and current DB retained'):
            release.rollout(self.args)
        operations['restore_previous'].assert_not_called()
        operations['stop_owned_launch'].assert_called_once_with(self.new_process)
        self.assertIn('stop-new', self.phases)
        self.assertEqual(self.schedule_bytes(), original)
        self.assert_lock_released()

    def test_rollback_failure_still_releases_schedule_lock(self):
        self.configure()
        operations = self.fake_operations()
        operations['launch'].side_effect = RuntimeError('launch rejected')

        def fail(*_args, **_kwargs):
            self.assert_runner_excluded('failed-rollback')
            raise RuntimeError('rollback rejected')

        operations['restore_previous'].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'rollback rejected'):
            release.rollout(self.args)
        self.assertIn('failed-rollback', self.phases)
        self.assert_lock_released()

    def test_dry_run_does_not_create_schedule_lock_or_change_inputs(self):
        original = self.configure()
        self.args.apply = False
        operations = self.fake_operations()
        self.assertEqual(release.rollout(self.args)['mode'], 'dry-run')
        self.assertFalse((self.schedule / 'schedule.lock').exists())
        self.assertFalse((self.runtime / 'data' / 'schedule_operation.lock').exists())
        self.assertEqual(self.schedule_bytes(), original)
        operations['acquire_app_instance_lock'].assert_not_called()
        operations['launch'].assert_not_called()

    def test_unconfigured_apply_does_not_create_schedule_directory(self):
        operations = self.fake_operations()
        self.assertEqual(release.rollout(self.args)['mode'], 'applied')
        self.assertFalse(self.schedule.exists())
        self.assertTrue((self.runtime / 'data' / 'schedule_operation.lock').is_file())
        operations['build_schedule_release_pin'].assert_not_called()

    def test_unconfigured_existing_directory_is_unchanged(self):
        self.schedule.mkdir(parents=True)
        marker = self.schedule / 'unconfigured.txt'
        marker.write_text('keep fixture', encoding='utf-8')
        operations = self.fake_operations()
        self.assertEqual(release.rollout(self.args)['mode'], 'applied')
        self.assertEqual(list(self.schedule.iterdir()), [marker])
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep fixture')
        operations['build_schedule_release_pin'].assert_not_called()

    def test_app_lock_failure_keeps_scheduler_excluded_during_recovery(self):
        original = self.configure()
        operations = self.fake_operations()

        def fail(*_args, **_kwargs):
            self.assert_runner_excluded('failed-app-lock')
            raise RuntimeError('app singleton busy')

        operations['acquire_app_instance_lock'].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'app singleton busy'):
            release.rollout(self.args)
        operations['restore_previous'].assert_called_once()
        self.assertIn('rollback', self.phases)
        self.assertEqual(self.schedule_bytes(), original)
        self.assert_lock_released()

    def test_unconfigured_dry_run_does_not_create_schedule_directory(self):
        self.args.apply = False
        operations = self.fake_operations()
        self.assertEqual(release.rollout(self.args)['mode'], 'dry-run')
        self.assertFalse(self.schedule.exists())
        self.assertFalse((self.runtime / 'data' / 'schedule_operation.lock').exists())
        operations['acquire_app_instance_lock'].assert_not_called()
        operations['build_schedule_release_pin'].assert_not_called()

    def test_new_schedule_during_preflight_is_rejected_without_repinning(self):
        operations = self.fake_operations()
        created = {}

        def configure_after_preflight(*_args):
            created.update(self.configure())
            return SimpleNamespace(release=mock.Mock())

        operations['acquire_app_instance_lock'].side_effect = configure_after_preflight
        with self.assertRaisesRegex(RuntimeError, 'Schedule configuration changed'):
            release.rollout(self.args)
        self.assertEqual(self.schedule_bytes(), created)
        self.assertFalse((self.schedule / 'schedule.lock').exists())
        operations['build_schedule_release_pin'].assert_not_called()
        operations['launch'].assert_not_called()

    def test_environment_schedule_lock_is_honored_before_runtime_mutation(self):
        default_schedule = self.schedule
        self.schedule = self.root / 'environment schedule'
        original = self.configure()
        operations = self.fake_operations()
        with mock.patch.dict(os.environ, {'DHB_SCHEDULE_STATE_DIR': str(self.schedule)}):
            with single_instance(self.schedule):
                with self.assertRaises(ScheduleError) as caught:
                    release.rollout(self.args)
            self.assertEqual(os.environ['DHB_SCHEDULE_STATE_DIR'], str(self.schedule))
        self.assertEqual(caught.exception.code, 'RUNNER_ALREADY_ACTIVE')
        self.assertFalse(default_schedule.exists())
        self.assertEqual(self.schedule_bytes(), original)
        self.old_process.terminate.assert_not_called()
        operations['launch'].assert_not_called()
        self.assert_lock_released()

    def test_relative_environment_schedule_uses_runtime_working_directory(self):
        self.schedule = self.runtime / 'relative schedule'
        original = self.configure()
        operations = self.fake_operations()
        with mock.patch.dict(os.environ, {'DHB_SCHEDULE_STATE_DIR': 'relative schedule'}):
            with single_instance(self.schedule):
                with self.assertRaises(ScheduleError) as caught:
                    release.rollout(self.args)
            self.assertEqual(os.environ['DHB_SCHEDULE_STATE_DIR'], 'relative schedule')
        self.assertEqual(caught.exception.code, 'RUNNER_ALREADY_ACTIVE')
        self.assertEqual(self.schedule_bytes(), original)
        self.old_process.terminate.assert_not_called()
        operations['launch'].assert_not_called()
        self.assert_lock_released()

    def test_cli_schedule_overrides_environment_and_is_forwarded_to_launch(self):
        ignored = self.root / 'ignored environment schedule'
        ignored.mkdir()
        self.schedule = self.root / 'explicit CLI schedule'
        self.args.schedule_state_dir = self.schedule
        self.configure()
        operations = self.fake_operations()
        with mock.patch.dict(os.environ, {'DHB_SCHEDULE_STATE_DIR': str(ignored)}):
            with single_instance(ignored):
                self.assertEqual(release.rollout(self.args)['mode'], 'applied')
            self.assertEqual(os.environ['DHB_SCHEDULE_STATE_DIR'], str(ignored))
        self.assertEqual(operations['launch'].call_args.kwargs['schedule_dir'], self.schedule)
        self.assertFalse((ignored / 'release.json').exists())
        self.assert_lock_released()

    def test_launch_pins_child_schedule_without_changing_parent_environment(self):
        requested = self.root / 'child schedule'
        inherited = {'DHB_SCHEDULE_STATE_DIR': 'inherited-schedule',
                     'DHB_DISABLE_BACKGROUND_TASKS': '1'}
        with mock.patch.dict(os.environ, inherited, clear=True), mock.patch.object(
            release.subprocess, 'Popen',
        ) as popen:
            release.launch(self.runtime, Path(sys.executable), 8686,
                           self.evidence / 'child.log', schedule_dir=requested)
            child = popen.call_args.kwargs['env']
            self.assertEqual(child['DHB_SCHEDULE_STATE_DIR'], str(requested.resolve()))
            self.assertNotIn('DHB_DISABLE_BACKGROUND_TASKS', child)
            self.assertEqual(dict(os.environ), inherited)

    def test_rollback_forwards_the_resolved_schedule_to_old_child(self):
        requested = self.root / 'rollback schedule'
        child = mock.Mock()
        with mock.patch.object(release, 'snapshot', return_value=self.before), \
             mock.patch.object(release, 'git', side_effect=['', 'a' * 40, '']), \
             mock.patch.object(release, 'listeners', return_value=[]), \
             mock.patch.object(release, 'launch', return_value=child) as launch, \
             mock.patch.object(release, 'wait_for_start'), \
             mock.patch.object(release, 'verify_transition'):
            release.restore_previous(
                self.runtime, 'previous', 'a' * 40, Path(sys.executable), 8686,
                self.evidence, mock.Mock(), 'http://127.0.0.1:8686',
                self.runtime / 'data' / 'dhb_youtube.sqlite', self.before,
                schedule_dir=requested,
            )
        self.assertEqual(launch.call_args.kwargs['schedule_dir'], requested)

    def test_missing_configured_pin_fails_before_stopping_or_repinning(self):
        original = self.configure()
        (self.schedule / 'release.json').unlink()
        operations = self.fake_operations()
        with self.assertRaisesRegex(RuntimeError, 'Configured schedule release pin is missing'):
            release.rollout(self.args)
        for name in ('manifest.json', 'state.json'):
            self.assertEqual((self.schedule / name).read_bytes(), original[name])
        self.assertFalse((self.schedule / 'release.json').exists())
        self.old_process.terminate.assert_not_called()
        operations['acquire_app_instance_lock'].assert_not_called()
        operations['build_schedule_release_pin'].assert_not_called()
        operations['launch'].assert_not_called()
        operations['backup'].assert_not_called()
        self.assert_lock_released()

    def test_runtime_guard_blocks_unconfigured_apply_before_runtime_access(self):
        operations = self.fake_operations()
        with runtime_operation_lock(self.runtime):
            with self.assertRaises(ScheduleError) as caught:
                release.rollout(self.args)
        self.assertEqual(caught.exception.code, 'RUNTIME_OPERATION_ACTIVE')
        self.assertFalse(self.schedule.exists())
        operations['verified_server'].assert_not_called()
        self.old_process.terminate.assert_not_called()
        operations['launch'].assert_not_called()

    def test_schedule_created_during_first_rehearsal_stops_before_runtime_mutation(self):
        operations = self.fake_operations()
        created = {}

        def rehearse(*_args):
            created.update(self.configure())
            self.assert_runner_excluded('first-rehearsal')
            return {}, self.before

        operations['rehearse_recovery'].side_effect = rehearse
        with self.assertRaisesRegex(RuntimeError, 'Schedule configuration changed'):
            release.rollout(self.args)
        self.assertEqual(self.schedule_bytes(), created)
        self.assertFalse((self.schedule / 'schedule.lock').exists())
        self.old_process.terminate.assert_not_called()
        operations['acquire_app_instance_lock'].assert_not_called()
        operations['build_schedule_release_pin'].assert_not_called()
        operations['launch'].assert_not_called()
        self.assert_lock_released()

    def test_schedule_created_during_locked_rehearsal_cannot_be_released_or_started(self):
        operations = self.fake_operations()
        created = {}
        calls = 0

        def rehearse(*_args):
            nonlocal calls
            calls += 1
            if calls == 2:
                created.update(self.configure())
                self.assert_runner_excluded('locked-rehearsal')
            return {}, self.before

        operations['rehearse_recovery'].side_effect = rehearse
        with self.assertRaisesRegex(RuntimeError, 'Schedule configuration changed during rehearsal'):
            release.rollout(self.args)
        self.assertEqual(calls, 2)
        self.assertEqual(self.schedule_bytes(), created)
        self.assertFalse((self.schedule / 'schedule.lock').exists())
        operations['build_schedule_release_pin'].assert_not_called()
        operations['launch'].assert_not_called()
        self.assertFalse(any(call.args[1:3] == ('switch', '-c')
                             for call in operations['git'].call_args_list))
        operations['restore_previous'].assert_called_once()
        self.assertIn('rollback', self.phases)
        self.assert_lock_released()

    def test_runtime_status_excludes_only_the_exact_untracked_guard_record(self):
        guard = '?? data/schedule_operation.lock'
        with mock.patch.object(release, 'git', return_value=guard) as git:
            self.assertEqual(release.runtime_checkout_status(self.runtime), '')
        git.assert_called_once_with(self.runtime, 'status', '--porcelain', '--untracked-files=all')
        for dirty in (' M data/schedule_operation.lock', '?? data/unrelated.txt',
                      ' M app.py', '?? data/schedule_operation.lock.extra'):
            with self.subTest(dirty=dirty), mock.patch.object(
                release, 'git', return_value=guard + '\n' + dirty,
            ):
                self.assertEqual(release.runtime_checkout_status(self.runtime), dirty)

    def test_clean_runtime_gates_accept_the_owned_untracked_guard(self):
        self.configure()
        operations = self.fake_operations()
        base_git = operations['git'].side_effect

        def git(root, *command):
            if command == ('status', '--porcelain', '--untracked-files=all'):
                return '?? data/schedule_operation.lock'
            return base_git(root, *command)

        operations['git'].side_effect = git
        self.assertEqual(release.rollout(self.args)['mode'], 'applied')
        self.assert_lock_released()

    def test_tracked_guard_change_is_rejected_before_runtime_mutation(self):
        operations = self.fake_operations()
        base_git = operations['git'].side_effect

        def git(root, *command):
            if command == ('status', '--porcelain', '--untracked-files=all'):
                return ' M data/schedule_operation.lock'
            return base_git(root, *command)

        operations['git'].side_effect = git
        with self.assertRaisesRegex(RuntimeError, 'Runtime checkout must be clean'):
            release.rollout(self.args)
        self.old_process.terminate.assert_not_called()
        operations['launch'].assert_not_called()
        operations['acquire_app_instance_lock'].assert_not_called()

    def test_custom_schedule_directory_uses_the_same_runner_lock(self):
        default_schedule = self.schedule
        self.schedule = self.root / 'custom schedule'
        self.args.schedule_state_dir = self.schedule
        self.configure()
        self.fake_operations()
        self.assertEqual(release.rollout(self.args)['mode'], 'applied')
        self.assertFalse(default_schedule.exists())
        self.assert_lock_released()


if __name__ == '__main__':
    unittest.main()
