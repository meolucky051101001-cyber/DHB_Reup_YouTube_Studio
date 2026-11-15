"""Read-only, aggregate-only G17 metadata impact and environment audit.

Never import app/database, migrate a schema, launch a browser or contact a provider.
The source is opened mode=ro and query_only, in one SQLite read transaction.
"""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib import metadata as packages
import json
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys

from group_distribution import choose_target_channel, resolve_target_candidates
from upload_capabilities import (UploadCapabilityError, capability_issues,
                                 legacy_option_requests, normalize_mode)


class AuditError(ValueError):
    """A stable error code; deliberately excludes raw SQLite or configuration values."""


REQUIRED_COLUMNS = {
    'upload_option_groups': {'id', 'language', 'disable_shorts_remix', 'category_id', 'pin_comment_enabled'},
    'youtube_channels': {'id', 'upload_mode', 'default_category_id'},
    'video_queue': {'id', 'status', 'target_channel_id', 'target_group_name', 'option_group_id'},
    'monitors': {'id', 'target_channel_id', 'target_group_name', 'option_group_id', 'is_running'},
}
SCHEMA_KEYS = ('review_metadata', 'upload_hardening', 'channel_group_memberships', 'metadata_intent')
STATUSES = {'Pending', 'Downloading', 'Downloaded', 'Rendering', 'Rendered', 'Uploading',
            'Completed', 'Failed', 'Error', 'Cancelled', 'Stopped', 'NeedsReview'}
TERMINAL = {'Completed', 'NeedsReview'}
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+() -]{0,127}\Z')


def _flag(value):
    return value in (1, '1', True)


def _category_bucket(value):
    text = str(value or '').strip()
    if not text:
        return 'empty'
    if not re.fullmatch(r'[1-9][0-9]{0,2}', text):
        return 'invalid'
    return 'default_22' if text == '22' else 'other_numeric'


def _language_bucket(value):
    text = str(value or '').strip()
    if not text:
        return 'empty'
    if len(text) > 64 or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', text):
        return 'invalid'
    return 'legacy_vi' if text == 'vi' else 'other_valid_tag'


def _unconfirmed(option):
    raw = option.get('metadata_intent_json')
    if raw is None or raw == '':
        return True
    try:
        return json.loads(raw) == {} if isinstance(raw, str) else raw == {}
    except (ValueError, TypeError):
        return False


def _schema(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_COLUMNS.keys() <= tables:
        raise AuditError('unsupported_schema')
    for table, required in REQUIRED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if not required <= present:
            raise AuditError('unsupported_schema')
    # Hash schema SQL, but never emit it: defaults or identifiers may contain private data.
    schema_rows = [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    result = {'user_version': conn.execute('PRAGMA user_version').fetchone()[0],
              'fingerprint_sha256': hashlib.sha256(json.dumps(schema_rows).encode()).hexdigest(),
              'known_versions': {}, 'unrecognized_version_entries': 0}
    if 'app_schema_versions' in tables:
        for key, version in conn.execute('SELECT schema_key,version FROM app_schema_versions'):
            if key in SCHEMA_KEYS and type(version) is int:
                result['known_versions'][key] = version
            else:
                result['unrecognized_version_entries'] += 1
    return result


def _options(conn):
    rows = {row['id']: dict(row) for row in conn.execute('SELECT * FROM upload_option_groups')}
    stats = {'total': len(rows), 'unconfirmed_intent': 0, 'restrictive_remix': 0,
             'remix_zero_unconfirmed': 0, 'pin_requested': 0,
             'invalid_flags': Counter(), 'language': Counter(), 'category': Counter()}
    for option in rows.values():
        legacy = _unconfirmed(option)
        stats['unconfirmed_intent'] += legacy
        stats['restrictive_remix'] += _flag(option.get('disable_shorts_remix'))
        stats['remix_zero_unconfirmed'] += legacy and option.get('disable_shorts_remix') in (0, '0', False)
        stats['pin_requested'] += _flag(option.get('pin_comment_enabled'))
        stats['language'][_language_bucket(option.get('language'))] += 1
        stats['category'][_category_bucket(option.get('category_id'))] += 1
        for field in ('disable_shorts_remix', 'pin_comment_enabled'):
            try:
                legacy_option_requests({field: option.get(field)})
            except UploadCapabilityError:
                stats['invalid_flags'][field] += 1
    return rows, stats


def _mode(value):
    try:
        return normalize_mode(value)
    except UploadCapabilityError:
        return 'invalid'


def _new_g17_issue(mode, issue):
    # Existing API age/premiere and both-backend pin guards were already enforced.
    # G17 adds restrictive remix/Studio premiere and moves the Studio age guard earlier.
    return (issue['code'] == 'UPLOAD_REMIX_UNSUPPORTED'
            or (mode == 'playwright' and issue['code'] in
                ('UPLOAD_AGE_RESTRICTION_UNSUPPORTED', 'UPLOAD_PREMIERE_UNSUPPORTED')))


def _impact(conn, table, options):
    monitor = table == 'monitors'
    counts = {'total': 0, 'without_options': 0, 'missing_options': 0,
              'with_legacy_vi': 0, 'with_restrictive_remix': 0,
              'unresolved_destination': 0, 'guard_affected': 0,
              'new_g17_guard_affected': 0, 'new_g17_guard_affected_nonterminal': 0,
              'guard_affected_by_code': Counter(), 'backend': Counter(),
              'effective_category': Counter()}
    if monitor:
        counts.update({'running': 0, 'mixed_backends': 0, 'candidate_evaluations': 0})
    else:
        counts['by_status'] = Counter()
    for raw in conn.execute(f'SELECT * FROM {table}'):
        item = dict(raw)
        counts['total'] += 1
        if monitor:
            counts['running'] += _flag(item.get('is_running'))
        else:
            counts['by_status'][item['status'] if item['status'] in STATUSES else 'Other'] += 1
        option_id = item.get('option_group_id')
        option = options.get(option_id) if option_id else None
        counts['without_options'] += not bool(option_id)
        counts['missing_options'] += bool(option_id) and option is None
        if option:
            counts['with_legacy_vi'] += _unconfirmed(option) and option.get('language') == 'vi'
            counts['with_restrictive_remix'] += _flag(option.get('disable_shorts_remix'))
        try:
            if monitor:
                target = {'target_type': 'CHANNEL' if item.get('target_channel_id') else 'GROUP',
                          'target_channel_id': item.get('target_channel_id'),
                          'target_group_name': item.get('target_group_name') or 'Mặc định'}
                candidates, _ = resolve_target_candidates(conn, target)
            else:
                channel, _ = choose_target_channel(conn, item)
                candidates = [channel]
        except (ValueError, TypeError, KeyError):
            counts['unresolved_destination'] += 1
            continue
        modes, issues_seen, new_issue = set(), set(), False
        for channel in candidates:
            mode = _mode(channel.get('upload_mode'))
            modes.add(mode)
            value = (option.get('category_id') if option else None) or channel.get('default_category_id') or '22'
            counts['effective_category'][_category_bucket(value)] += 1
            if monitor:
                counts['candidate_evaluations'] += 1
            if mode == 'invalid':
                issues_seen.add('UPLOAD_BACKEND_UNSUPPORTED')
                continue
            try:
                metadata = {**item, **legacy_option_requests(option)}
                issues = capability_issues(mode, metadata)
            except UploadCapabilityError as exc:
                issues = exc.issues
            for issue in issues:
                issues_seen.add(issue['code'])
                new_issue |= _new_g17_issue(mode, issue)
        # Count entities once per backend/code, not repeatedly per matching candidate.
        for mode in modes:
            counts['backend'][mode] += 1
        if monitor:
            counts['mixed_backends'] += len(modes) > 1
        for code in issues_seen:
            counts['guard_affected_by_code'][code] += 1
        counts['guard_affected'] += bool(issues_seen)
        counts['new_g17_guard_affected'] += new_issue
        counts['new_g17_guard_affected_nonterminal'] += new_issue and (monitor or item['status'] not in TERMINAL)
    return counts


def audit(db):
    """Return aggregates from a consistent read snapshot; never migrate or repair."""
    try:
        path = Path(db).resolve(strict=True)
        if not path.is_file():
            raise AuditError('database_unavailable')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    except (OSError, sqlite3.Error):
        raise AuditError('database_unavailable') from None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise AuditError('database_integrity_failed')
        schema = _schema(conn)
        options, option_stats = _options(conn)
        channels = {'total': 0, 'backend': Counter(), 'category': Counter()}
        for row in conn.execute('SELECT upload_mode,default_category_id FROM youtube_channels'):
            channels['total'] += 1
            channels['backend'][_mode(row['upload_mode'])] += 1
            channels['category'][_category_bucket(row['default_category_id'])] += 1
        return {'report_version': 1, 'read_only': True, 'quick_check': 'ok', 'schema': schema,
                'options': option_stats, 'channels': channels,
                'queue': _impact(conn, 'video_queue', options),
                'monitors': _impact(conn, 'monitors', options),
                'limitations': [
                    'Aggregate impact only; no approval, repair, eligibility or upload verification.',
                    'Queue uses the current deterministic/assigned destination; Monitor checks every candidate.',
                    'Counts include historical rows; nonterminal excludes Completed and NeedsReview.',
                    'G17 new guard impact covers restrictive remix, Studio premiere and earlier Studio age rejection.',
                    'Legacy language values are unconfirmed, never treated as opt-in.',
                ]}
    except sqlite3.Error:
        raise AuditError('database_read_failed') from None
    finally:
        conn.close()


def _git(root, *args):
    try:
        result = subprocess.run(['git', '-c', f'safe.directory={root.as_posix()}', '-C', str(root), *args],
                                capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _safe_version(value):
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def environment_manifest(source_root, *, observed_tools=None):
    """Inspect code/installed metadata only; tool versions are explicitly supplied evidence."""
    root = Path(source_root).resolve(strict=True)
    head = _git(root, 'rev-parse', 'HEAD')
    status = _git(root, 'status', '--porcelain')
    build = None
    try:
        for node in ast.parse((root / 'app.py').read_text(encoding='utf-8-sig')).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'APP_BUILD_VERSION' for t in node.targets):
                build = _safe_version(ast.literal_eval(node.value))
    except (OSError, SyntaxError, ValueError):
        pass
    dependencies = {}
    try:
        lines = (root / 'requirements.txt').read_text(encoding='utf-8-sig').splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9.+!-]+)', line.strip())
        if not match:
            continue
        name, pinned = match.groups()
        try:
            installed = _safe_version(packages.version(name))
        except packages.PackageNotFoundError:
            installed = None
        dependencies[name] = {'pinned': pinned, 'installed': installed, 'matches_pin': installed == pinned}
    tools = {}
    for name in ('browser', 'ffmpeg', 'ffprobe'):
        value = _safe_version((observed_tools or {}).get(name))
        tools[name] = {'version': value, 'evidence': 'operator_supplied' if value else 'not_observed'}
    return {'collected_at_utc': datetime.now(timezone.utc).isoformat(),
            'revision': head if head and re.fullmatch(r'[a-f0-9]{40,64}', head) else None,
            'checkout_dirty': status != '' if status is not None else None,
            'build': build, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'dependencies': dependencies, 'tools': tools,
            'scope': 'Current audit process; not proof of the running app environment.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--browser-version')
    parser.add_argument('--ffmpeg-version')
    parser.add_argument('--ffprobe-version')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output:
            output = args.output.resolve()
            source = args.db.resolve()
            protected = [source, *[Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal')]]
            if output in protected or any(p.exists() and output.exists() and output.samefile(p) for p in protected):
                raise AuditError('output_must_not_replace_database')
        result = audit(args.db)
        if args.source_root:
            result['environment'] = environment_manifest(args.source_root, observed_tools={
                'browser': args.browser_version, 'ffmpeg': args.ffmpeg_version, 'ffprobe': args.ffprobe_version})
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + '\n'
        if args.output:
            args.output.write_text(encoded, encoding='utf-8')
        else:
            print(encoded, end='')
    except AuditError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print(json.dumps({'error': 'audit_io_failed'}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
