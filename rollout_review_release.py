"""Rehearse or deploy a CI-verified release to the local Windows app checkout."""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import requests

from app_instance_lock import acquire_app_instance_lock
from audit_upload_capabilities import environment_manifest
from runtime_identity import APPLICATION_ID, git_environment, git_output, source_fingerprint
from scheduled_upload_runner import release_pin as build_schedule_release_pin
from scheduled_uploads import (
    load_json as load_schedule_json, save_json as save_schedule_json,
    runtime_operation_lock, single_instance as schedule_single_instance,
    validate_manifest as validate_schedule_manifest, validate_state as validate_schedule_state,
)
from validate_review_release import backup, rehearse, sha256_file, snapshot, verify_transition


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def git(root, *args):
    return git_output(root, *args, timeout=45)


def runtime_checkout_status(root):
    """Keep old checkouts usable after the shared operation guard creates its file."""
    status = git(root, 'status', '--porcelain', '--untracked-files=all')
    return '\n'.join(line for line in status.splitlines()
                     if line != '?? data/schedule_operation.lock')


def server_arguments(port, *, access_log=False):
    args = ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)]
    return args if access_log else args + ['--no-access-log']


def http_state(session, base, root, revision):
    def get(path, *, timeout=15, **kwargs):
        response = session.get(base + path, timeout=timeout, allow_redirects=False, **kwargs)
        response.raise_for_status()
        return response.json()

    health = get('/health/dhb')
    require(health['application'] == APPLICATION_ID and Path(health['source_root']) == root,
            'Listener is not the requested runtime.')
    headers = {'X-DHB-Token': get('/api/auth/token')['token'], 'Origin': base}
    overview = get('/api/overview', headers=headers)
    require(not overview['pipeline_running'] and overview['monitors_running'] == 0, 'Runtime is busy.')
    diag = get('/api/diagnostics', headers=headers, timeout=45)
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
                ['app.py'], server_arguments(port, access_log=True), server_arguments(port)),
            'Cannot verify the listening process as the app.')
    console = Path(os.environ['SystemRoot']) / 'System32' / 'conhost.exe'
    require(all(Path(child.exe()) == console for child in process.children(recursive=True)),
            'An app-owned worker process is still running.')
    return process


def server_interpreter(process):
    """Preserve a Windows virtualenv launcher for a verified listening process."""
    executable = Path(process.exe())
    parent = process.parent()
    if parent is None:
        return executable
    launcher = Path(parent.exe())
    if (launcher.name.lower() in ('python.exe', 'pythonw.exe')
            and launcher.parent.name.lower() == 'scripts'
            and (launcher.parent.parent / 'pyvenv.cfg').is_file()
            and Path(parent.cwd()) == Path(process.cwd())
            and parent.cmdline()[1:] == process.cmdline()[1:]):
        require(launcher.is_file(), 'The previous virtualenv launcher is missing.')
        return launcher
    return executable


def verify_idle_schedule(schedule_dir):
    if schedule_dir is None or not (schedule_dir / 'manifest.json').is_file():
        return None
    manifest = load_schedule_json(schedule_dir / 'manifest.json')
    validate_schedule_manifest(manifest)
    state = load_schedule_json(schedule_dir / 'state.json')
    validate_schedule_state(state, manifest)
    require(state.get('inflight_queue_id') is None and state.get('submission_state') not in ('submitting', 'started'),
            'Scheduled upload has an active or uncertain inflight job.')
    release_path = schedule_dir / 'release.json'
    require(release_path.is_file(), 'Configured schedule release pin is missing.')
    return load_schedule_json(release_path)


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
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, query in (
            ("publishing_campaigns", "SELECT COUNT(*) FROM publishing_campaigns WHERE state='enabled'"),
            ("publishing_campaign_runs", "SELECT COUNT(*) FROM publishing_campaign_runs WHERE state='submitting'"),
            ("campaign_playlist_jobs", "SELECT COUNT(*) FROM campaign_playlist_jobs WHERE state IN ('queued','submitting')"),
        ):
            if table in tables:
                require(conn.execute(query).fetchone()[0] == 0, 'Pause campaigns and wait for active uploads before rollout.')


def launch(root, python, port, log_path, *, schedule_dir=None):
    env = git_environment(root, trust_root=True)
    env.pop('DHB_DISABLE_BACKGROUND_TASKS', None)
    if schedule_dir is not None:
        env['DHB_SCHEDULE_STATE_DIR'] = str(Path(schedule_dir).resolve())
    with log_path.open('xb') as log:
        return subprocess.Popen([str(python), *server_arguments(port)], cwd=root, env=env, stdin=subprocess.DEVNULL,
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


def public_ci_run(repo, run):
    """Read one public GitHub run without credentials, redirects or response URLs."""
    require(isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            and all(part not in ('.', '..') for part in repo.split('/'))
            and isinstance(run, str) and re.fullmatch(r'[0-9]+', run), 'Invalid public CI run reference.')
    base = f'https://api.github.com/repos/{repo}/actions/runs/{run}'
    with requests.Session() as session:
        session.trust_env = False  # Do not load netrc credentials or proxy configuration.

        def get(url):
            try:
                response = session.get(url, timeout=(5, 15), allow_redirects=False,
                                       headers={'Accept': 'application/vnd.github+json'})
                require(response.status_code == 200, 'Public GitHub CI request failed.')
                data = response.json()
            except (requests.RequestException, ValueError):
                raise RuntimeError('Public GitHub CI response is unavailable or malformed.') from None
            require(isinstance(data, dict), 'Invalid public GitHub CI response schema.')
            return data

        data = get(base)
        require(type(data.get('id')) is int and str(data['id']) == run
                and isinstance(data.get('repository'), dict)
                and data['repository'].get('full_name') == repo,
                'Public GitHub CI run identity differs from the requested run.')
        require(isinstance(data.get('head_sha'), str)
                and re.fullmatch(r'[0-9a-fA-F]{40}', data['head_sha'])
                and isinstance(data.get('conclusion'), str)
                and data.get('status') == 'completed', 'Invalid public GitHub CI run schema.')
        jobs, seen, total = [], set(), None
        for page in range(1, 11):
            batch = get(f'{base}/jobs?filter=latest&per_page=100&page={page}')
            count, rows = batch.get('total_count'), batch.get('jobs')
            require(type(count) is int and 0 <= count <= 1000
                    and isinstance(rows, list) and len(rows) <= 100
                    and (total is None or count == total), 'Invalid public GitHub CI jobs schema.')
            total = count
            for job in rows:
                require(isinstance(job, dict) and type(job.get('id')) is int
                        and job['id'] not in seen and type(job.get('run_id')) is int
                        and str(job['run_id']) == run
                        and isinstance(job.get('name'), str) and bool(job['name'])
                        and isinstance(job.get('conclusion'), str)
                        and ('head_sha' not in job or job['head_sha'] == data['head_sha']),
                        'Invalid or mismatched public GitHub CI job.')
                seen.add(job['id'])
                jobs.append({'name': job['name'], 'conclusion': job['conclusion']})
            require(len(jobs) <= total, 'Public GitHub CI jobs exceed their declared count.')
            if len(jobs) == total:
                return {'headSha': data['head_sha'], 'conclusion': data['conclusion'], 'jobs': jobs,
                        'url': f'https://github.com/{repo}/actions/runs/{run}'}
            require(bool(rows), 'Public GitHub CI jobs pagination is incomplete.')
        raise RuntimeError('Public GitHub CI jobs exceed the pagination limit.')


def ci_run(repo, run):
    try:
        output = subprocess.check_output(
            ['gh', 'run', 'view', run, '--repo', repo, '--json', 'headSha,conclusion,jobs,url'],
            text=True, encoding='utf-8', timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return public_ci_run(repo, run)
    return json.loads(output)


def verify_acceptance(path, revision):
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    require(result.get('revision') == revision and all(result.get(check) is True for check in
            ('full_suite', 'chromium', 'clean_start', 'deterministic', 'wall_clock_soak')),
            'Offline acceptance is incomplete for this revision.')
    soak_report = path.parent / result['soak_report']
    events = [json.loads(line) for line in soak_report.read_text(encoding='utf-8-sig').splitlines()]
    require(events and events[-1].get('event') == 'complete', 'Soak report did not reach completion.')
    soak = events[-1]
    rounds = [event for event in events if event.get('event') == 'round']
    require(soak.get('pilot') is False and soak.get('revision') == revision
            and soak.get('seconds', 0) >= 3600 and soak.get('jobs') == 1000
            and soak['uploads']['gap_seconds'] >= 1800 and len(rounds) == 10
            and {item['workers'] for item in rounds} == {1, 4, 8}
            and all(item['jobs'] == 100 for item in rounds),
            'Soak did not cover the release duration, load matrix and real cooldown.')
    repo, run = result.get('repo', ''), str(result.get('ci_run_id', ''))
    require(re.fullmatch(r'[\w.-]+/[\w.-]+', repo) and run.isdecimal(), 'Invalid CI run reference.')
    ci = ci_run(repo, run)
    require(ci['headSha'] == revision and ci['conclusion'] == 'success'
            and {'unittest', 'media'} <= {job['name'] for job in ci['jobs'] if job['conclusion'] == 'success'},
            'Release CI is incomplete for this revision.')
    return {'offline_report_sha256': sha256_file(path), 'soak_report_sha256': sha256_file(soak_report), 'ci': ci['url']}


def verify_readonly_ui(base):
    from playwright.sync_api import sync_playwright

    def route_request(route):
        request = route.request
        target = urlsplit(request.url)
        if f'{target.scheme}://{target.netloc}' != base:
            external.append(target.netloc)
            route.abort()
        elif request.method not in ('GET', 'HEAD'):
            writes.append(target.path)
            route.abort()
        elif target.path == '/api/antidetect/status':
            route.fulfill(json={'connected': False, 'error': 'Offline acceptance'})
        else:
            route.continue_()

    external, writes, errors = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers='block')
            page = context.new_page()
            context.route('**/*', route_request)
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            page.on('response', lambda response: errors.append(response.status) if response.status >= 400 else None)
            page.goto(base + '/', wait_until='networkidle')
            tabs = page.locator('.nav-tab').evaluate_all('(elements) => elements.map(el => el.dataset.tab)')
            expected = {'tab-workspace', 'tab-ai', 'tab-render', 'tab-channels', 'tab-monitor', 'tab-campaigns', 'tab-settings'}
            require(len(tabs) == len(expected) and set(tabs) == expected, 'Unexpected dashboard tabs.')
            for tab in tabs:
                page.locator(f'.nav-tab[data-tab="{tab}"]').click()
                require(page.locator('#' + tab).is_visible(), 'Dashboard tab did not open.')
            response = page.evaluate("async () => await apiFetch('/api/upload-capabilities')")
            for backend in ('playwright', 'youtube_api'):
                features = response[backend]['features']
                require(all(not features[name]['enabled'] for name in ('video_language', 'remix')),
                        'Capability gate differs from the verified release.')
        finally:
            browser.close()
    require(not writes and not errors, 'Read-only Chromium reported write attempts or errors.')
    return {'tabs': tabs, 'external_requests_blocked': len(external), 'writes': 0, 'errors': 0}


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


_RECOVERY_PROBE = r'''
import json, os, subprocess, sys, threading
from pathlib import Path
root = Path.cwd().resolve()
allowed = {
    subprocess.list2cmdline(['git', 'rev-parse', 'HEAD']),
    subprocess.list2cmdline(['git', '-c', 'safe.directory=' + root.as_posix(), '-C', str(root), 'rev-parse', 'HEAD']),
}
denied = []
def audit(event, args):
    if event == 'subprocess.Popen':
        command = args[1]
        command = command if isinstance(command, str) else subprocess.list2cmdline(command)
        if command in allowed:
            return
    elif event not in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                       'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.sendto', 'socket.sendmsg'):
        return
    denied.append(event)
    raise PermissionError('External work is forbidden during recovery rehearsal.')
sys.addaudithook(audit)
try:
    import app
    if denied or threading.active_count() != 1:
        raise RuntimeError('Recovery startup attempted background or external work.')
except BaseException as exc:
    Path(sys.argv[1]).write_text(json.dumps({'failure': type(exc).__name__,
                                           'boundary_denied': bool(denied)}), encoding='utf-8')
    raise SystemExit(1)
Path(sys.argv[1]).write_text(json.dumps({'runtime': app.RUNTIME_IDENTITY,
                                       'build': app.APP_BUILD_VERSION}), encoding='utf-8')
'''


def rehearse_recovery(root, revision, python, db):
    """Run the previous code on an isolated DB before allowing automatic restart.

    Return the in-memory source snapshot separately; never serialize its row data.
    The cloned checkout contains tracked code only and cannot import live profiles.
    """
    with tempfile.TemporaryDirectory(prefix='dhb_recovery_clone_') as folder:
        clone = Path(folder) / 'code'
        git(root, 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(root), str(clone))
        git(clone, 'checkout', '--quiet', '--detach', revision)
        require(git(clone, 'rev-parse', 'HEAD') == revision, 'Recovery checkout revision differs.')
        clone_db = clone / 'data' / 'dhb_youtube.sqlite'
        clone_db.parent.mkdir(parents=True, exist_ok=True)
        backup(db, clone_db)
        before = snapshot(clone_db)
        fingerprint = source_fingerprint(clone)
        output = Path(folder) / 'startup.json'
        env = git_environment(clone, trust_root=True)
        env['DHB_DISABLE_BACKGROUND_TASKS'] = '1'
        env['DHB_ROOT'] = str(Path(folder) / 'absent-legacy-tool')
        process = subprocess.Popen([str(python), '-c', _RECOVERY_PROBE, str(output)], cwd=clone,
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            try:
                result = process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Recovery startup rehearsal timed out.') from None
            if result != 0 or not output.is_file():
                reason = json.loads(output.read_text(encoding='utf-8')) if output.is_file() else {}
                failure = reason.get('failure', '')
                failure = failure if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}', failure) else 'StartupError'
                raise RuntimeError(f'Recovery startup rehearsal failed ({failure}); runtime was not changed.')
        finally:
            stop_owned_launch(process)
        identity = json.loads(output.read_text(encoding='utf-8'))['runtime']
        require(identity['revision'] == revision and identity['startup_fingerprint'] == fingerprint,
                'Recovery startup identity differs from its pinned checkout.')
        changes = verify_transition(before, snapshot(clone_db), startup=True)
        return {'revision': revision, 'startup_fingerprint': fingerprint,
                'existing_evidence_preserved': True, 'changes': changes}, before


def restore_previous(root, branch, previous, python, port, evidence, session, base, db, recovery_before, *, schedule_dir=None):
    """Restart only the reviewed old code against exactly the rehearsed DB state."""
    require(recovery_before is not None and snapshot(db) == recovery_before,
            'Recovery DB differs from its verified snapshot. Runtime remains stopped; DB retained.')
    git(root, 'switch', branch)
    require(git(root, 'rev-parse', 'HEAD') == previous and not runtime_checkout_status(root),
            'Recovery checkout changed. Runtime remains stopped; DB retained.')
    require(not listeners(port), 'Recovery port is occupied; no process was started.')
    restored = launch(root, python, port, evidence / f'restored-{previous[:12]}.log',
                      schedule_dir=schedule_dir)
    try:
        wait_for_start(session, base, root, previous, restored)
        verify_transition(recovery_before, snapshot(db), startup=True)
    except BaseException:
        stop_owned_launch(restored)
        raise


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
    require(branch and not runtime_checkout_status(root), 'Runtime checkout must be clean and on a branch.')
    git(root, 'check-ref-format', '--branch', args.branch)
    db = root / 'data' / 'dhb_youtube.sqlite'
    schedule_override = getattr(args, 'schedule_state_dir', None)
    if schedule_override is None:
        environment_schedule = os.environ.get('DHB_SCHEDULE_STATE_DIR', '').strip()
        # The runtime resolves a relative environment override from its own cwd.
        schedule_override = (root / environment_schedule if environment_schedule
                             else root / 'data' / 'scheduled_uploads' / 'default')
    schedule_dir = Path(schedule_override).resolve()
    base = f'http://127.0.0.1:{args.port}'
    with ExitStack() as locks:
        # Match the runner: runtime operation, configured schedule, then app singleton.
        # The runtime guard also excludes work if a schedule appears during rehearsal.
        if args.apply:
            locks.enter_context(runtime_operation_lock(root))
        schedule_configured = (schedule_dir / 'manifest.json').is_file()
        if args.apply and schedule_configured:
            locks.enter_context(schedule_single_instance(schedule_dir))
        session = locks.enter_context(requests.Session())
        session.trust_env = False
        process = verified_server(root, args.port)
        if process is not None:
            http_state(session, base, root, previous)
        verify_idle_db(db)
        previous_schedule_release = verify_idle_schedule(schedule_dir)
        rehearsal = rehearse(db)
        previous_python = server_interpreter(process) if process is not None else python
        recovery, recovery_before = rehearse_recovery(root, previous, previous_python, db)
        if not args.apply:
            return {'mode': 'dry-run', 'revision': revision, 'previous_revision': previous,
                    'running_pid': process.pid if process is not None else None,
                    'schedule_configured': previous_schedule_release is not None,
                    'rehearsal': rehearsal, 'recovery': recovery}

        require(args.acceptance is not None, 'A completed acceptance report is required.')
        acceptance = verify_acceptance(args.acceptance, revision)
        expected_build = environment_manifest(source)['build']
        require(expected_build, 'Release build is missing from the source.')
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
        verify_idle_db(db)
        require((schedule_dir / 'manifest.json').is_file() == schedule_configured,
                'Schedule configuration changed during preflight.')
        previous_schedule_release = verify_idle_schedule(schedule_dir)
        schedule_backup = evidence / f'before-schedule-release-{revision[:12]}.json'
        stopped, switched, schedule_updated, launched = False, False, False, None
        try:
            if process is not None:
                process.terminate()
                stopped = True
                process.wait(timeout=15)
            require(not listeners(args.port), 'Runtime port is still in use.')
            lock = acquire_app_instance_lock(root / 'data' / 'app_instance.lock')
            try:
                verify_idle_db(db)
                require((schedule_dir / 'manifest.json').is_file() == schedule_configured,
                        'Schedule configuration changed during preflight.')
                previous_schedule_release = verify_idle_schedule(schedule_dir)
                require(git(root, 'rev-parse', 'HEAD') == previous and not runtime_checkout_status(root),
                        'Runtime checkout changed during preflight.')
                backup(db, backup_path)
                if previous_schedule_release is not None:
                    save_schedule_json(schedule_backup, previous_schedule_release)
                before = snapshot(backup_path)
                rehearsal = rehearse(backup_path)
                # Mark the prior result unusable before testing the locked snapshot.
                recovery_before = None
                recovery, recovery_before = rehearse_recovery(root, previous, previous_python, backup_path)
                require(snapshot(db) == before, 'Runtime DB changed following backup.')
                require((schedule_dir / 'manifest.json').is_file() == schedule_configured,
                        'Schedule configuration changed during rehearsal.')
                require(verify_idle_schedule(schedule_dir) == previous_schedule_release,
                        'Schedule release pin changed during rehearsal.')
                git(root, 'switch', '-c', args.branch, revision)
                switched = True
                if schedule_configured:
                    save_schedule_json(schedule_dir / 'release.json',
                                       build_schedule_release_pin(root, revision, expected_build))
                    schedule_updated = True
            finally:
                lock.release()
            launched = launch(root, python, args.port, runtime_log, schedule_dir=schedule_dir)
            diag = wait_for_start(session, base, root, revision, launched)
            require(diag['version'] == expected_build, 'Live build differs from the pinned release build.')
            verify_idle_db(db)
            before_browser = snapshot(db)
            browser = verify_readonly_ui(base)
            require(snapshot(db) == before_browser, 'Runtime DB changed during read-only Chromium.')
            changes = verify_transition(before, snapshot(db), startup=True)
            report = {'mode': 'applied', 'previous_revision': previous, 'previous_branch': branch,
                      'runtime_branch': args.branch, 'runtime': diag['runtime'], 'build': diag['version'],
                      'tools': [{'name': tool.get('name'), 'version': tool.get('version')} for tool in diag['tools']],
                      'schemas': diag['database']['schemas'], 'backup_sha256': sha256_file(backup_path),
                      'rehearsal': rehearsal, 'recovery': recovery, 'changes': changes,
                      'chromium_smoke': browser, **versions, **acceptance}
            with manifest.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, indent=2)
            return report
        except BaseException:
            if schedule_updated and previous_schedule_release is not None:
                try:
                    save_schedule_json(schedule_dir / 'release.json', previous_schedule_release)
                except Exception:
                    pass
            if launched is not None:
                stop_owned_launch(launched)
                raise RuntimeError('Post-start verification failed. Runtime stopped and current DB retained.') from None
            if stopped and not listeners(args.port):
                restore_previous(root, branch, previous, previous_python, args.port,
                                 evidence, session, base, db, recovery_before, schedule_dir=schedule_dir)
            elif switched:
                git(root, 'switch', branch)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8686)
    parser.add_argument('--schedule-state-dir', type=Path)
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(rollout(parser.parse_args()), indent=2))
