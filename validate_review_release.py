"""Rehearse additive migrations and restore on temporary clones, never the source DB."""
import argparse
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import mock

import database

TABLES=('video_queue','published_history','upload_attempts','content_reservations','youtube_channels','monitors')


def backup(source, target):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(target)) as dest:
            src.backup(dest)


def snapshot(path, columns=None):
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise RuntimeError('SQLite quick_check failed.')
        columns=columns or {table:[row[1] for row in conn.execute(f'PRAGMA table_info({table})')] for table in TABLES}
        result={table:sorted(conn.execute(f'SELECT {",".join(fields)} FROM {table}').fetchall(),key=repr)
                for table,fields in columns.items()}
        return columns,result


def rehearse(source):
    with tempfile.TemporaryDirectory(prefix='dhb_release_clone_') as folder:
        root=Path(folder)
        clone=root/'clone.sqlite'
        original=root/'before.sqlite'
        restored=root/'restored.sqlite'
        backup(source,original)
        backup(original,clone)
        columns,before=snapshot(original)
        with mock.patch.object(database,'APP_DB_PATH',clone), mock.patch.object(database,'APP_DATA_DIR',root), \
             mock.patch.object(database,'APP_TEMP_DIR',root/'temp'), mock.patch.object(database,'load_dhb_settings',return_value={}):
            database.init_db()
            _,after=snapshot(clone,columns)
            # Legacy monitors are intentionally stopped, but all publication evidence
            # and existing queue/channel metadata must survive the migration.
            for table in TABLES:
                if table!='monitors' and before[table]!=after[table]:
                    raise RuntimeError(f'Migration changed existing evidence in {table}.')
            _,first=snapshot(clone)
            database.init_db()
            _,second=snapshot(clone)
            if first!=second:
                raise RuntimeError('Migration is not idempotent.')
        backup(original,restored)
        _,restored_rows=snapshot(restored,columns)
        if before!=restored_rows:
            raise RuntimeError('Restored rows differ from backup.')
        return {'quick_check':'ok','migration_idempotent':True,'existing_evidence_preserved':True,'restore_verified':True,
                'counts':{table:len(rows) for table,rows in before.items()},
                'backup_sha256':hashlib.sha256(original.read_bytes()).hexdigest()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=json.dumps(rehearse(args.db),indent=2)
    if args.output:
        args.output.write_text(report,encoding='utf-8')
    else:
        print(report)
