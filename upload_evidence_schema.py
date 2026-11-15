"""Additive upload evidence schema migration; caller owns the connection."""

SCHEMA_KEY = 'upload_evidence'
SCHEMA_VERSION = 1


def preflight_upload_evidence_schema(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_schema_versions'").fetchone():
        return 0
    row = conn.execute("SELECT version FROM app_schema_versions WHERE schema_key=?", (SCHEMA_KEY,)).fetchone()
    if row is None:
        return 0
    version = row[0]
    if type(version) is not int or version not in (0, SCHEMA_VERSION):
        raise RuntimeError('Schema upload_evidence mới hơn hoặc không hợp lệ; không tự hạ phiên bản.')
    return version


def _column_info(conn):
    return next((row for row in conn.execute('PRAGMA table_info(upload_attempts)') if row[1] == 'evidence_json'), None)


def _stamp(conn):
    conn.execute('''INSERT INTO app_schema_versions(schema_key,version) VALUES(?,?)
        ON CONFLICT(schema_key) DO UPDATE SET version=excluded.version,updated_at=CURRENT_TIMESTAMP''',
                 (SCHEMA_KEY, SCHEMA_VERSION))


def migrate_upload_evidence_schema(conn):
    version = preflight_upload_evidence_schema(conn)
    conn.execute('SAVEPOINT upload_evidence_migration')
    try:
        info = _column_info(conn)
        if info is None:
            if version == SCHEMA_VERSION:
                raise RuntimeError('Schema upload_evidence đã có stamp nhưng thiếu cột evidence_json.')
            conn.execute("ALTER TABLE upload_attempts ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'")
            info = _column_info(conn)
        if (info is None or str(info[2]).upper() != 'TEXT' or info[3] != 1
                or str(info[4]).strip("'\"") != '{}'):
            raise RuntimeError('Schema upload_evidence không đúng hợp đồng; không tiếp tục migration.')
        if version != SCHEMA_VERSION:
            _stamp(conn)
        conn.execute('RELEASE upload_evidence_migration')
    except Exception:
        conn.execute('ROLLBACK TO upload_evidence_migration')
        conn.execute('RELEASE upload_evidence_migration')
        raise
