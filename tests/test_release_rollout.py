"""Rollback must preserve the interpreter that owns a Windows runtime."""
import os
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock


import psutil
from rollout_review_release import server_interpreter


class RollbackInterpreterTests(unittest.TestCase):
    def test_verified_virtualenv_parent_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / 'Scripts'
            scripts.mkdir()
            launcher = scripts / 'python.exe'
            launcher.touch()
            (root / 'pyvenv.cfg').write_text('home = base-python\n', encoding='utf-8')
            command = ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8686', '--no-access-log']
            parent = mock.Mock()
            parent.exe.return_value = str(launcher)
            parent.cwd.return_value = str(root)
            parent.cmdline.return_value = [str(launcher), *command]
            server = mock.Mock()
            server.exe.return_value = str(root / 'base-python.exe')
            server.cwd.return_value = str(root)
            server.cmdline.return_value = [server.exe.return_value, *command]
            server.parent.return_value = parent

            self.assertEqual(server_interpreter(server), launcher)
            parent.cmdline.return_value = [str(launcher), 'unrelated.py']
            self.assertEqual(server_interpreter(server), Path(server.exe.return_value))

    @unittest.skipUnless(os.name == 'nt', 'Windows virtualenv launcher regression')
    def test_actual_virtualenv_child_recovers_its_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            venv.EnvBuilder(with_pip=False).create(root / 'venv')
            launcher = root / 'venv' / 'Scripts' / 'python.exe'
            command = 'import os,time; print(os.getpid(), flush=True); time.sleep(15)'
            child = subprocess.Popen(
                [str(launcher), '-c', command], cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            server = None
            try:
                server = psutil.Process(int(child.stdout.readline().strip()))
                self.assertNotEqual(Path(server.exe()), launcher)
                restored = server_interpreter(server)
                self.assertEqual(restored, launcher)
                prefix = subprocess.check_output(
                    [str(restored), '-c', 'import sys; print(sys.prefix)'],
                    cwd=root, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).strip()
                self.assertEqual(Path(prefix), root / 'venv')
            finally:
                if server is not None and server.is_running():
                    server.terminate()
                    server.wait(timeout=5)
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()
