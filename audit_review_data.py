"""Read-only release audit. Reports IDs and recovery candidates, never credentials."""
import argparse
import json
import sqlite3
from pathlib import Path

from content_identity import sha256_file


def audit(db):
    conn = sqlite3.connect(Path(db).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('BEGIN')
        counts={table:conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('video_queue','published_history','upload_attempts','content_reservations','youtube_channels','monitors')}
        missing=[]
        for row in conn.execute('SELECT * FROM video_queue'):
            row=dict(row)
            source=Path(row['input_video_path']) if row.get('input_video_path') else None
            output=Path(row['output_video_path']) if row.get('output_video_path') else None
            if source and not source.is_file() and not (output and output.is_file()):
                candidates=[]
                archive=source.parent/'uploaded'
                if archive.is_dir():
                    for candidate in archive.glob('*'+source.suffix):
                        # Bound scanning to the historical archive directory. A matching
                        # basename alone is a suggestion and never permission to rebind.
                        if candidate.is_file() and (candidate.stem==source.stem or candidate.stem.startswith(source.stem+'_')):
                            expected=str(row.get('content_key') or '')
                            verified=expected.startswith('sha256:') and sha256_file(candidate)==expected
                            candidates.append({'path':str(candidate),'hash_matches':bool(verified)})
                missing.append({'id':row['id'],'status':row['status'],'source':str(source),'candidates':candidates})
        held=[dict(row) for row in conn.execute("SELECT DISTINCT q.id,q.status FROM video_queue q JOIN content_reservations r ON r.queue_id=q.id WHERE r.state='NeedsReview'")]
        return {'quick_check':conn.execute('PRAGMA quick_check').fetchone()[0],'counts':counts,
                'review_holds':held,'missing_source_and_output':missing,
                'missing_channel_ids':[row[0] for row in conn.execute("SELECT id FROM youtube_channels WHERE TRIM(COALESCE(channel_id,''))=''")],
                'legacy_monitor_candidates':[row[0] for row in conn.execute("SELECT id FROM video_queue WHERE project_name LIKE 'Monitor -%' AND COALESCE(monitor_metadata_policy_json,'')='' AND status NOT IN ('Completed','NeedsReview')")],
                'monitors_requiring_confirmation':[row[0] for row in conn.execute("SELECT id FROM monitors WHERE COALESCE(metadata_policy_json,'')='' OR COALESCE(metadata_revision,'')=''")],
                'active_claims':[row[0] for row in conn.execute("SELECT id FROM video_queue WHERE COALESCE(claim_token,'')<>''")]}
    finally:
        conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=json.dumps(audit(args.db),ensure_ascii=True,indent=2)
    if args.output:
        args.output.write_text(result,encoding='utf-8')
    else:
        print(result)
