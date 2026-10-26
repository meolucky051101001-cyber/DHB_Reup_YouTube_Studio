import sys
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# Trỏ đúng database của dự án đang chạy (thư mục chứa file script này)
DB_PATH = Path(__file__).parent / "data" / "dhb_youtube.sqlite"
if not DB_PATH.exists():
    print(f"KHONG TIM THAY DATABASE: {DB_PATH}")
    print("Hay chay START_DHB_YOUTUBE.bat truoc de khoi tao he thong.")
    sys.exit(1)

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

rows = cur.execute('SELECT id, title, render_mode, download_progress, render_progress, upload_progress, status, input_video_path, output_video_path, error_message FROM video_queue ORDER BY id DESC LIMIT 6').fetchall()
print('--- VIDEO QUEUE ---')
for r in rows:
    print(f"#{r['id']}: [{r['status']}] | Render: {r['render_progress']}% | Upload: {r['upload_progress']}%")
    print(f"  Title: {r['title'][:70]}")
    print(f"  Input: {r['input_video_path']}")
    print(f"  Output: {r['output_video_path']}")
    if r['error_message']:
        print(f"  Error: {r['error_message']}")

logs = cur.execute('SELECT created_at, module, status, message FROM system_logs ORDER BY id DESC LIMIT 8').fetchall()
print('\n--- RECENT LOGS ---')
for l in logs:
    print(f"[{l['created_at']}] [{l['module']}] {l['status']}: {l['message']}")
conn.close()
