"""Inspect legacy review holds. Defaults to read-only; apply requires the app lock."""
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app_instance_lock import acquire_app_instance_lock


def repair(db: Path, apply: bool = False):
    db = db.resolve(strict=True)
    lock = acquire_app_instance_lock(db.parent / 'app_instance.lock') if apply else None
    try:
        conn = sqlite3.connect(db.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            backup = None
            if apply:
                backup = db.with_name(f'{db.stem}.before-review-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.sqlite')
                with sqlite3.connect(backup) as destination:
                    conn.backup(destination)
            conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            rows = conn.execute("""
                SELECT q.id,q.status,q.claim_token,MAX(r.updated_at) AS held_at
                FROM video_queue q JOIN content_reservations r ON q.id=r.queue_id
                WHERE r.state='NeedsReview' AND q.status <> 'NeedsReview'
                GROUP BY q.id
            """).fetchall()
            changes = []
            for row in rows:
                reason = ''
                if row['claim_token'] or str(row['status']).startswith(('Uploading','Rendering','Downloading','Đang đăng')):
                    reason = 'Worker ownership must be resolved first.'
                decision = conn.execute(
                    "SELECT 1 FROM upload_attempts WHERE queue_id=? AND outcome IN "
                    "('Completed','ConfirmedPublished','Authorized') AND updated_at>=? LIMIT 1",
                    (row['id'],row['held_at']),
                ).fetchone()
                if decision:
                    reason = 'A later or simultaneous decision needs manual reconciliation.'
                changes.append({'id':row['id'],'before':row['status'],'after':'NeedsReview','blocked_reason':reason,'applied':apply and not reason})
                if apply and not reason:
                    conn.execute("UPDATE video_queue SET status='NeedsReview' WHERE id=?", (row['id'],))
                    conn.execute("INSERT INTO system_logs(module,status,message,queue_id) VALUES('ReviewRepair','WARNING',?,?)",
                                 (f"Restored review hold from {row['status']}; reservation retained.",row['id']))
            if apply:
                conn.commit()
            else:
                conn.rollback()
            return {'apply':apply,'changes':changes,'quick_check':conn.execute('PRAGMA quick_check').fetchone()[0],
                    'backup':str(backup) if backup else None,
                    'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest() if backup else None}
        finally:
            conn.close()
    finally:
        if lock:
            lock.release()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(repair(args.db,args.apply),ensure_ascii=True,indent=2))
