"""Compare a release and rehearse migration/restore without modifying the source."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

import database
import metadata_intent_schema
import upload_evidence_schema

TABLES = (
    'projects', 'render_presets', 'chrome_profiles', 'youtube_channels',
    'video_queue', 'published_history', 'upload_attempts', 'content_reservations',
    'monitors', 'upload_option_groups', 'channel_groups', 'channel_group_memberships',
    'upload_approvals', 'upload_approval_items', 'managed_artifacts',
    'app_settings', 'upload_slot', 'app_schema_versions', 'system_logs',
)
NEW_DEFAULTS = {
    'youtube_channels': {'studio_category_mode': 'legacy_unconfirmed'},
    'upload_option_groups': {'metadata_intent_json': '{}'},
    'upload_attempts': {'evidence_json': '{}'},
}
NEW_STAMPS = {module.SCHEMA_KEY: module.SCHEMA_VERSION
              for module in (metadata_intent_schema, upload_evidence_schema)}


def _read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def backup(source, target, *, timeout=20):
    """Create a new, consistent SQLite backup including committed WAL data."""
    target = Path(target)
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError('SQLite backup did not complete before the deadline.')

    with closing(_read_only(source)) as src:
        # An exclusive reader on a rollback-mode DB may fail on these queries
        # before the backup callback runs. The source connection has a short busy timeout.
        size = src.execute('PRAGMA page_count').fetchone()[0] * src.execute('PRAGMA page_size').fetchone()[0]
        if shutil.disk_usage(target.resolve().parent).free < size + (1 << 20):
            raise OSError('Insufficient space for SQLite backup.')
        with target.open('xb'):
            pass
        try:
            with closing(sqlite3.connect(target, timeout=1)) as dest:
                src.backup(dest, pages=128, progress=progress, sleep=.05)
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(path, columns=None):
    """Return a consistent in-memory snapshot. Row contents stay out of reports."""
    with closing(_read_only(path)) as conn:
        conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite quick_check failed.')
        if columns is None:
            columns = {table: [row[1] for row in conn.execute(f'PRAGMA table_info({_identifier(table)})')]
                       for table in TABLES}
        result = {}
        for table, fields in columns.items():
            if not fields:
                raise RuntimeError(f'Required release table is missing: {table}.')
            query = ','.join('t.' + _identifier(field) for field in fields)
            result[table] = sorted(conn.execute(f'SELECT {query} FROM {_identifier(table)} AS t').fetchall(), key=repr)
        return columns, result


def _keyed(fields, rows, key):
    index = fields.index(key)
    return {row[index]: dict(zip(fields, row)) for row in rows}


def _schema_delta(before, after):
    if set(after) - set(before) - NEW_STAMPS.keys():
        raise RuntimeError('Unexpected schema stamp added.')
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if (key not in NEW_STAMPS or (old is not None and old['version'] != 0)
                or new is None or new['version'] != NEW_STAMPS[key] or not new['updated_at']):
            raise RuntimeError('Unexpected schema stamp change.')
    if any(after.get(key, {}).get('version') != version for key, version in NEW_STAMPS.items()):
        raise RuntimeError('Required release schema stamp missing.')


def _setting_enabled(raw):
    try:
        return bool(json.loads(raw))
    except (TypeError, ValueError):
        return bool(raw)


def verify_transition(before, after, *, startup=False):
    """Verify old rows field by field and only allow reviewed release changes."""
    old_columns, old_rows = before
    new_columns, new_rows = after
    changes = {}
    for table in TABLES:
        fields = old_columns[table]
        current = new_columns[table]
        required = NEW_DEFAULTS.get(table, {})
        added = set(current) - set(fields)
        if set(fields) - set(current) or added - required.keys() or required.keys() - set(current):
            raise RuntimeError(f'Unexpected columns in {table}.')
        for field in added:
            if any(row[current.index(field)] != required[field] for row in new_rows[table]):
                raise RuntimeError(f'Unexpected migration value in {table}.{field}.')
        if added:
            changes[table] = sorted(added)
        rows = sorted((tuple(row[current.index(field)] for field in fields) for row in new_rows[table]), key=repr)
        previous = old_rows[table]
        if table == 'app_schema_versions':
            _schema_delta(_keyed(fields, previous, 'schema_key'), _keyed(fields, rows, 'schema_key'))
        elif table == 'system_logs':
            old = _keyed(fields, previous, 'id')
            new = _keyed(fields, rows, 'id')
            last_id = max(old, default=0)
            if any(new.get(key) != value for key, value in old.items()) or any(
                    key <= last_id for key in new.keys() - old.keys()):
                raise RuntimeError('Startup removed, changed or backdated existing logs.')
            changes['logs_appended'] = len(new) - len(old)
        elif table == 'app_settings' and startup:
            old = _keyed(fields, previous, 'key')
            new = _keyed(fields, rows, 'key')
            key = 'auto_start_on_launch'
            if key in old and _setting_enabled(old[key]['value']) and new.get(key) == {'key': key, 'value': 'false'}:
                old[key] = new[key]
            if old != new:
                raise RuntimeError('Startup changed application settings outside the auto-start guard.')
        elif table == 'monitors':
            old = _keyed(fields, previous, 'id')
            new = _keyed(fields, rows, 'id')
            for row in old.values():
                if not row['is_running'] and (not row['metadata_policy_json'] or not row['metadata_revision']):
                    row.update(is_running=0, status='Cần xác nhận metadata')
            if old != new:
                raise RuntimeError('Migration changed monitors outside the existing confirmation guard.')
        elif rows != previous:
            raise RuntimeError(f'Migration changed existing data in {table}.')
    return changes


def rehearse(source):
    with tempfile.TemporaryDirectory(prefix='dhb_release_clone_') as folder:
        root = Path(folder)
        clone, original, restored = (root / name for name in ('clone.sqlite', 'before.sqlite', 'restored.sqlite'))
        backup(source, original)
        backup(original, clone)
        before = snapshot(original)
        with (mock.patch.object(database, 'APP_DB_PATH', clone),
              mock.patch.object(database, 'APP_DATA_DIR', root),
              mock.patch.object(database, 'APP_TEMP_DIR', root / 'temp'),
              mock.patch.object(database, 'DHB_PROFILE_CHROME_DIR', root / 'no-import'),
              mock.patch.object(database, 'load_dhb_settings', return_value={})):
            database.init_db()
            first = snapshot(clone)
            changes = verify_transition(before, first)
            database.init_db()
            if first != snapshot(clone):
                raise RuntimeError('Migration is not idempotent.')
        backup(original, restored)
        if before != snapshot(restored):
            raise RuntimeError('Restored rows differ from backup.')
        return {
            'quick_check': 'ok', 'migration_idempotent': True, 'existing_evidence_preserved': True,
            'restore_verified': True, 'counts': {table: len(rows) for table, rows in before[1].items()},
            'changes': changes, 'backup_sha256': sha256_file(original),
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = json.dumps(rehearse(args.db), indent=2)
    if args.output:
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(report + '\n')
    else:
        print(report)
