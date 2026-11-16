import tempfile
import unittest
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import runtime_identity
from runtime_identity import APPLICATION_ID, capture_runtime, git_output, runtime_status


class RuntimeIdentityTests(unittest.TestCase):
    def test_normal_script_start_reads_its_checkout_without_git_config_workaround(self):
        with tempfile.TemporaryDirectory(prefix='identity with spaces ') as directory:
            root = Path(directory).resolve() / 'source'
            root.mkdir()
            shutil.copy2(Path(runtime_identity.__file__), root / 'runtime_identity.py')
            entry = root / 'app.py'
            entry.write_text('import json\nfrom pathlib import Path\nfrom runtime_identity import capture_runtime\n'
                             'print(json.dumps(capture_runtime(Path(__file__).resolve().parent)))\n', encoding='utf-8')
            git_output(root, 'init', '--quiet')
            git_output(root, 'add', 'app.py', 'runtime_identity.py')
            git_output(root, '-c', 'user.name=Offline test', '-c', 'user.email=fixture@invalid',
                       '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'identity fixture')
            revision = git_output(root, 'rev-parse', 'HEAD')
            env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_CONFIG_')}
            env['GIT_TEST_ASSUME_DIFFERENT_OWNER'] = '1'
            env['GIT_DIR'] = str(Path(directory) / 'unrelated.git')
            result = json.loads(subprocess.check_output([sys.executable, str(entry)], cwd=directory, env=env,
                                                        text=True, timeout=10))
            self.assertEqual(result['revision'], revision)
            self.assertEqual(result['source_root'], str(root))
            self.assertEqual(result['startup_fingerprint'], runtime_identity.source_fingerprint(root))

    def test_missing_git_reports_unknown_revision_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(runtime_identity.subprocess, 'check_output', side_effect=FileNotFoundError):
                self.assertIsNone(capture_runtime(Path(directory))['revision'])

    def test_disk_change_preserves_fingerprint_and_revision_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text("VERSION = 1", encoding="utf-8")
            startup = capture_runtime(root)
            self.assertEqual(startup["application"], APPLICATION_ID)
            self.assertFalse(runtime_status(root, startup)["restart_required"])

            source.write_text("VERSION = 2", encoding="utf-8")
            current = runtime_status(root, startup)

            self.assertTrue(current["restart_required"])
            self.assertEqual(current["startup_fingerprint"], startup["startup_fingerprint"])
            self.assertEqual(current["revision"], startup["revision"])
            self.assertNotEqual(current["disk_fingerprint"], startup["startup_fingerprint"])
