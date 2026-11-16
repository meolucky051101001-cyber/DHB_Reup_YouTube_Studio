"""Rehearse or deploy a CI-verified release to the local Windows app checkout."""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import psutil
import requests

from app_instance_lock import acquire_app_instance_lock
from runtime_identity import APPLICATION_ID, source_fingerprint
from validate_review_release import backup, rehearse, sha256_file, snapshot, verify_transition


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def git(root, *args):
    return subprocess.check_output(['git', '-c', f'safe.directory={root.as_posix()}', *args],
                                   cwd=root, text=True, encoding='utf-8', timeout=45).strip()


def http_state(session, base, root, revision):
    def get(path, **kwargs):
        response = session.get(base + path, timeout=15, allow_redirects=False, **kwargs)
        response.raise_for_status()
        return response.json()

    health = get('/health/dhb')
    require(health['application'] == APPLICATION_ID and Path(health['source_root']) == root,
            'Listener is not the requested runtime.')
    headers = {'X-DHB-Token': get('/api/auth/token')['token'], 'Origin': base}
    overview = get('/api/overview', headers=headers)
    require(not overview['pipeline_running'] and overview['monitors_running'] == 0, 'Runtime is busy.')
    diag = get('/api/diagnostics', headers=headers)
    identity = diag['runtime']
    require(identity['revision'] == revision and not identity['restart_required']
            and identity['startup_fingerprint'] == source_fingerprint(root),
            'Running revision or fingerprint differs from the checkout.')
    return diag


def listeners(port):
    return [entry for entry in psutil.net_connections(kind='tcp')
            if entry.status == psutil.CONN_LISTEN and entry.laddr.port == port]


def verified_server(root, port):
    bound = listeners(port)
    if not bound:
        return None
    require(len(bound) == 1 and bound[0].laddr.ip == '127.0.0.1' and bound[0].pid,
            'Release requires a single loopback listener.')
    process = psutil.Process(bound[0].pid)
    command = process.cmdline()
    require(Path(process.cwd()) == root and command[1:] in (
                ['app.py'], ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)]),
            'Cannot verify the listening process as the app.')
    console = Path(os.environ['SystemRoot']) / 'System32' / 'conhost.exe'
    require(all(Path(child.exe()) == console for child in process.children(recursive=True)),
            'An app-owned worker process is still running.')
    return process


def verify_idle_db(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        for query in (
            "SELECT COUNT(*) FROM video_queue WHERE COALESCE(claim_token,'')<>''",
            "SELECT COUNT(*) FROM monitors WHERE is_running=1",
            "SELECT COUNT(*) FROM upload_slot WHERE state NOT IN ('idle','cooldown')",
            "SELECT COUNT(*) FROM content_reservations WHERE state='Active'",
            "SELECT COUNT(*) FROM upload_approvals WHERE state='accepted'",
        ):
            require(conn.execute(query).fetchone()[0] == 0, 'Database has active work or upload recovery.')


def launch(root, python, port, log_path):
    env = os.environ.copy()
    env.pop('DHB_DISABLE_BACKGROUND_TASKS', None)
    with log_path.open('xb') as log:
        return subprocess.Popen([str(python), '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1',
                                 '--port', str(port)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)


def wait_for_start(session, base, root, revision, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        require(process.poll() is None, 'App exited during startup; see the private log.')
        try:
            return http_state(session, base, root, revision)
        except requests.ConnectionError:
            time.sleep(.2)
    raise RuntimeError('App startup timed out.')


def stop_owned_launch(launch_process):
    if launch_process.poll() is not None:
        return
    process = psutil.Process(launch_process.pid)
    processes = process.children(recursive=True) + [process]
    for item in processes:
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(processes, timeout=15)
    require(not remaining, 'The launched app did not terminate.')


def verify_acceptance(path, revision):
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    require(result.get('revision') == revision and all(result.get(check) is True for check in
            ('full_suite', 'chromium', 'clean_start', 'deterministic', 'wall_clock_soak')),
            'Offline acceptance is incomplete for this revision.')
    repo, run = result.get('repo', ''), str(result.get('ci_run_id', ''))
    require(re.fullmatch(r'[\w.-]+/[\w.-]+', repo) and run.isdecimal(), 'Invalid CI run reference.')
    ci = json.loads(subprocess.check_output(
        ['gh', 'run', 'view', run, '--repo', repo, '--json', 'headSha,conclusion,jobs,url'],
        text=True, encoding='utf-8', timeout=30))
    require(ci['headSha'] == revision and ci['conclusion'] == 'success'
            and {'unittest', 'media'} <= {job['name'] for job in ci['jobs'] if job['conclusion'] == 'success'},
            'Release CI is incomplete for this revision.')
    return {'offline_report_sha256': sha256_file(path), 'ci': ci['url']}


def interpreter_versions(python, root):
    probe = """
import importlib.metadata, json, platform
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    try:
        chromium = browser.version
    finally:
        browser.close()
print(json.dumps({'python': platform.python_version(), 'chromium': chromium,
                  'dependencies': sorted(f'{d.metadata["Name"]}=={d.version}'
                                         for d in importlib.metadata.distributions())}))
"""
    return json.loads(subprocess.check_output([str(python), '-c', probe], cwd=root, text=True, timeout=30))


def rollout(args):
    source = Path(__file__).resolve().parent
    root, evidence, python = (value.resolve() for value in (args.runtime, args.evidence, args.python))
    require(root != source, 'Use a separate runtime checkout.')
    require(not evidence.is_relative_to(root) and not evidence.is_relative_to(source)
            and evidence.is_dir(), 'Use an existing private evidence directory outside both checkouts.')
    require(python.is_file(), 'Choose an existing runtime Python executable.')
    revision = git(source, 'rev-parse', 'HEAD')
    require(revision == args.revision and not git(source, 'status', '--porcelain'), 'Release source must be clean and pinned.')
    previous = git(root, 'rev-parse', 'HEAD')
    branch = git(root, 'branch', '--show-current')
    require(branch and not git(root, 'status', '--porcelain'), 'Runtime checkout must be clean and on a branch.')
    git(root, 'check-ref-format', '--branch', args.branch)
    db = root / 'data' / 'dhb_youtube.sqlite'
    base = f'http://127.0.0.1:{args.port}'
    with requests.Session() as session:
        session.trust_env = False
        process = verified_server(root, args.port)
        if process is not None:
            http_state(session, base, root, previous)
        verify_idle_db(db)
        rehearsal = rehearse(db)
        if not args.apply:
            return {'mode': 'dry-run', 'revision': revision, 'previous_revision': previous,
                    'running_pid': process.pid if process is not None else None, 'rehearsal': rehearsal}

        require(args.acceptance is not None, 'A completed acceptance report is required.')
        acceptance = verify_acceptance(args.acceptance, revision)
        versions = interpreter_versions(python, source)
        git(root, 'fetch', str(source), revision)
        git(root, 'merge-base', '--is-ancestor', previous, revision)
        require(args.branch not in git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').splitlines(),
                'Use a new local release branch.')
        backup_path = evidence / f'before-{revision[:12]}.sqlite'
        runtime_log = evidence / f'rollout-{revision[:12]}.log'
        manifest = evidence / f'manifest-{revision[:12]}.json'
        require(not any(path.exists() for path in (backup_path, runtime_log, manifest)),
                'Release evidence already exists. Use an empty evidence directory.')
        current = verified_server(root, args.port)
        require(current == process, 'Runtime listener changed during preflight.')
        if process is not None:
            http_state(session, base, root, previous)
        previous_python = Path(process.exe()) if process is not None else python
        verify_idle_db(db)
        stopped, switched, launched = False, False, None
        try:
            if process is not None:
                process.terminate()
                stopped = True
                process.wait(timeout=15)
            require(not listeners(args.port), 'Runtime port is still in use.')
            lock = acquire_app_instance_lock(root / 'data' / 'app_instance.lock')
            try:
                verify_idle_db(db)
                require(git(root, 'rev-parse', 'HEAD') == previous and not git(root, 'status', '--porcelain'),
                        'Runtime checkout changed during preflight.')
                backup(db, backup_path)
                before = snapshot(backup_path)
                rehearsal = rehearse(backup_path)
                require(snapshot(db) == before, 'Runtime DB changed following backup.')
                git(root, 'switch', '-c', args.branch, revision)
                switched = True
            finally:
                lock.release()
            launched = launch(root, python, args.port, runtime_log)
            diag = wait_for_start(session, base, root, revision, launched)
            verify_idle_db(db)
            changes = verify_transition(before, snapshot(db), startup=True)
            report = {'mode': 'applied', 'previous_revision': previous, 'previous_branch': branch,
                      'runtime_branch': args.branch, 'runtime': diag['runtime'], 'build': diag['version'],
                      'tools': [{'name': tool.get('name'), 'version': tool.get('version')} for tool in diag['tools']],
                      'schemas': diag['database']['schemas'], 'backup_sha256': sha256_file(backup_path),
                      'rehearsal': rehearsal, 'changes': changes, **versions, **acceptance}
            with manifest.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, indent=2)
            return report
        except BaseException:
            if launched is not None:
                stop_owned_launch(launched)
                raise RuntimeError('Post-start verification failed. Runtime stopped and current DB retained.') from None
            if switched:
                git(root, 'switch', branch)
            if stopped and not listeners(args.port):
                restored = launch(root, previous_python, args.port,
                                  evidence / f'restored-{previous[:12]}.log')
                wait_for_start(session, base, root, previous, restored)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8686)
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(rollout(parser.parse_args()), indent=2))
