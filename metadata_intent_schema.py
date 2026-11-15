"""Additive, transactional metadata-intent schema; caller owns connection lifetime."""


SCHEMA_KEY = 'metadata_intent'
SCHEMA_VERSION = 1
COLUMNS = (
    ('upload_option_groups', 'metadata_intent_json', "TEXT NOT NULL DEFAULT '{}'"),
    ('youtube_channels', 'studio_category_mode', "TEXT NOT NULL DEFAULT 'legacy_unconfirmed'"),
)


def preflight_metadata_intent_schema(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_schema_versions'").fetchone():
        return 0
    row = conn.execute('SELECT version FROM app_schema_versions WHERE schema_key=?', (SCHEMA_KEY,)).fetchone()
    if row is None:
        return 0
    version = row[0]
    if type(version) is not int or version not in (0, SCHEMA_VERSION):
        raise RuntimeError('Schema metadata_intent mới hơn hoặc không hợp lệ; không tự hạ phiên bản.')
    return version


def _column_info(conn, table, column):
    return next((row for row in conn.execute(f'PRAGMA table_info({table})') if row[1] == column), None)


def _add_column(conn, table, column, declaration):
    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


def migrate_metadata_intent_schema(conn):
    version = preflight_metadata_intent_schema(conn)
    conn.execute('SAVEPOINT metadata_intent_migration')
    try:
        for table, column, declaration in COLUMNS:
            info = _column_info(conn, table, column)
            if info is None:
                if version == SCHEMA_VERSION:
                    raise RuntimeError('Schema metadata_intent đã có stamp nhưng thiếu cột; cần phục hồi trên bản sao trước.')
                _add_column(conn, table, column, declaration)
                info = _column_info(conn, table, column)
            default = '{}' if column == 'metadata_intent_json' else 'legacy_unconfirmed'
            if (info is None or str(info[2]).upper() != 'TEXT' or info[3] != 1
                    or str(info[4]).strip("'\"") != default):
                raise RuntimeError('Schema metadata_intent không đúng hợp đồng; không tiếp tục migration.')
        # A separate trigger also protects databases whose older general binding
        # trigger was installed with CREATE IF NOT EXISTS before this column existed.
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS upload_approval_guard_channel_metadata_intent
            BEFORE UPDATE OF studio_category_mode ON youtube_channels
            WHEN OLD.studio_category_mode IS NOT NEW.studio_category_mode AND EXISTS (
                SELECT 1 FROM video_queue q
                WHERE q.assigned_channel_id=OLD.id AND COALESCE(q.claim_token,'')<>''
            )
            BEGIN SELECT RAISE(ABORT, 'UPLOAD_BINDING_BUSY'); END
        """)
        if version != SCHEMA_VERSION:
            conn.execute('''INSERT INTO app_schema_versions(schema_key,version) VALUES(?,?)
                ON CONFLICT(schema_key) DO UPDATE SET version=excluded.version,updated_at=CURRENT_TIMESTAMP''',
                (SCHEMA_KEY, SCHEMA_VERSION))
        conn.execute('RELEASE metadata_intent_migration')
    except Exception:
        conn.execute('ROLLBACK TO metadata_intent_migration')
        conn.execute('RELEASE metadata_intent_migration')
        raise
