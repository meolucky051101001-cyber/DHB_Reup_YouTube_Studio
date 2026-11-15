import csv
import json
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit
from content_identity import normalize_source_url
from phase4_rules import GROUP_SCHEMA_VERSION, normalize_group_names, serialize_group_names
from upload_approval import init_approval_schema
from metadata_intent_schema import preflight_metadata_intent_schema, migrate_metadata_intent_schema
from upload_evidence_schema import preflight_upload_evidence_schema, migrate_upload_evidence_schema

from config import (
    APP_DATA_DIR,
    APP_DB_PATH,
    APP_TEMP_DIR,  # noqa: F401 - compatibility surface used by tests and local integrations
    DHB_PROFILE_CHROME_DIR,
    APP_PROFILES_DIR,  # noqa: F401 - compatibility surface used by tests and local integrations
    load_dhb_settings,
)


class SecretCollectionError(RuntimeError):
    """Không đọc được nguồn bí mật — thà che toàn bộ còn hơn lộ dữ liệu thật."""


def queue_requires_review(conn, queue_id: int) -> bool:
    """The reservation is durable evidence even if a legacy worker changed status."""
    return bool(conn.execute(
        "SELECT 1 FROM video_queue q WHERE q.id = ? AND (q.status = 'NeedsReview' OR EXISTS "
        "(SELECT 1 FROM content_reservations r WHERE r.queue_id = q.id AND r.state = 'NeedsReview'))",
        (queue_id,),
    ).fetchone())


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(APP_DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


def normalize_legacy_group_defaults(conn, cur=None) -> int:
    """Đưa mọi nhóm kênh mặc định cũ ('Default') về tên chuẩn 'Mặc định'."""
    cur = cur or conn.cursor()
    changed = 0
    if cur.execute("SELECT 1 FROM channel_groups WHERE name = 'Default'").fetchone():
        if cur.execute("SELECT 1 FROM channel_groups WHERE name = 'Mặc định'").fetchone():
            cur.execute("UPDATE youtube_channels SET group_name = 'Mặc định' WHERE TRIM(group_name) = 'Default'")
            changed += cur.rowcount
            cur.execute("DELETE FROM channel_groups WHERE name = 'Default'")
        else:
            cur.execute("UPDATE channel_groups SET name = 'Mặc định' WHERE name = 'Default'")
            changed += cur.rowcount
    for table, column in (
        ("youtube_channels", "group_name"),
        ("video_queue", "target_group_name"),
        ("monitors", "target_group_name"),
    ):
        cur.execute(f"UPDATE {table} SET {column} = 'Mặc định' WHERE TRIM({column}) = 'Default'")
        changed += cur.rowcount
    return changed


def _fix_video_queue_emulate_default(conn, cur) -> bool:
    """Rebuild video_queue if its legacy schema defaults emulate_mobile to 1.

    ALTER TABLE cannot change a column default, and every INSERT that omits the
    column (e.g. Auto Monitor) would silently get Mobile emulation, which violates
    the Native Desktop anti-checkpoint rule.
    """
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='video_queue'"
    ).fetchone()
    if not row or not row[0]:
        return False
    create_sql = row[0]
    if not re.search(r"emulate_mobile\s+INTEGER\s+DEFAULT\s+1\b", create_sql, re.IGNORECASE):
        return False

    new_table = "video_queue_emulate_fix"
    new_sql = re.sub(
        r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)([\"'`]?)video_queue\2",
        lambda m: f"{m.group(1)}{m.group(2)}{new_table}{m.group(2)}",
        create_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    new_sql = re.sub(
        r"(emulate_mobile\s+INTEGER\s+DEFAULT\s+)1\b",
        r"\g<1>0",
        new_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    cols = [c[1] for c in cur.execute("PRAGMA table_info(video_queue)").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)
    dependent_sql = [
        r[0]
        for r in cur.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='video_queue' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        ).fetchall()
    ]

    conn.commit()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(f"DROP TABLE IF EXISTS {new_table}")
        cur.execute(new_sql)
        cur.execute(f"INSERT INTO {new_table} ({col_list}) SELECT {col_list} FROM video_queue")
        cur.execute("DROP TABLE video_queue")
        cur.execute(f"ALTER TABLE {new_table} RENAME TO video_queue")
        for index_sql in dependent_sql:
            cur.execute(index_sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def init_db():
    conn = get_conn()
    try:
        preflight_metadata_intent_schema(conn)
        preflight_upload_evidence_schema(conn)
    except Exception:
        conn.close()
        raise
    cur = conn.cursor()

    # 1. Bảng Nhóm Kênh (Channel Groups)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_groups (
            name TEXT PRIMARY KEY,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_group_memberships (
            group_name TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_name, channel_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_group_memberships_channel ON channel_group_memberships(channel_id, sort_order)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_schema_versions (
            schema_key TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 1.1 Bảng Quản Lý Chrome Profiles (Mỗi Profile có thể chứa nhiều Kênh YouTube con)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chrome_profiles (
            name TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            proxy TEXT DEFAULT '',
            source TEXT DEFAULT 'DHB', -- 'DHB' | 'Local'
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Bảng Kênh YouTube (YouTube Channels - Mỗi kênh gắn với 1 Profile cha)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS youtube_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            channel_id TEXT DEFAULT '',
            handle TEXT DEFAULT '',
            email TEXT DEFAULT '',
            group_name TEXT DEFAULT 'Mặc định',
            upload_mode TEXT DEFAULT 'playwright', -- 'playwright' | 'youtube_api'
            profile_name TEXT DEFAULT '',
            profile_path TEXT DEFAULT '',
            emulate_mobile INTEGER DEFAULT 0, -- 1: Giả lập Điện thoại (NGUY HIỂM với Profile Windows), 0: Desktop nguyên bản (mặc định an toàn chống Checkpoint)
            use_hidden_desktop INTEGER DEFAULT 0,
            proxy TEXT DEFAULT '',
            cookie TEXT DEFAULT '',
            oauth_token_json TEXT DEFAULT '',
            default_category_id TEXT DEFAULT '22', -- 22: People & Blogs, 24: Entertainment, 26: Howto & Style
            default_privacy TEXT DEFAULT 'public', -- 'public' | 'unlisted' | 'private'
            made_for_kids INTEGER DEFAULT 0,
            altered_content INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Ready',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. Bảng Nhóm Tùy Chọn Đăng (Upload Option Groups - Link Shopee Affiliate, Comment, SEO)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS upload_option_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            affiliate_enabled INTEGER DEFAULT 0,
            affiliate_url TEXT DEFAULT '',
            affiliate_CTA TEXT DEFAULT '👉 Mua ngay sản phẩm chính hãng tại đây:',
            pin_comment_enabled INTEGER DEFAULT 0,
            pin_comment_text TEXT DEFAULT '',
            append_hashtags TEXT DEFAULT '#shorts #trending #viral',
            default_tags TEXT DEFAULT '',
            category_id TEXT DEFAULT '22',
            language TEXT DEFAULT 'vi',
            made_for_kids INTEGER DEFAULT 0,
            ai_label INTEGER DEFAULT 0,
            disable_shorts_remix INTEGER DEFAULT 0,
            source_dhb_group_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 4. Bảng Dự Án (Projects)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_dhb_file TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 5. Bảng Danh Sách Video trong Dự Án (Video Queue - tương đương PhKLOMA của DHB)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS video_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL DEFAULT 'Default',
            source_type TEXT DEFAULT 'URL', -- 'URL' | 'File' | 'DHB_Project'
            platform TEXT DEFAULT 'Unknown',
            post_id TEXT DEFAULT '',
            source_url TEXT DEFAULT '',
            input_video_path TEXT DEFAULT '',
            output_video_path TEXT DEFAULT '',
            thumbnail_path TEXT DEFAULT '',
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            duration TEXT DEFAULT '00:00:00',
            view_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            upload_to TEXT DEFAULT 'Shorts', -- 'Shorts' | 'Video'
            visibility TEXT DEFAULT 'public', -- 'public' | 'unlisted' | 'private' | 'schedule'
            schedule_time TEXT DEFAULT '',
            target_type TEXT DEFAULT 'CHANNEL', -- 'CHANNEL' | 'GROUP'
            target_channel_id INTEGER DEFAULT NULL,
            target_group_name TEXT DEFAULT '',
            option_group_id INTEGER DEFAULT NULL,
            render_mode TEXT DEFAULT 'shorts_9_16', -- 'none' | 'md5_cut' | 'shorts_9_16' | 'pip_thu_nho' | 'full_16_9' | 'square_1_1' | 'audio_effect' | 'custom_ffmpeg'
            render_config_json TEXT DEFAULT '{}',
            cut_start TEXT DEFAULT '00:00:00',
            cut_end TEXT DEFAULT '00:00:00',
            download_progress INTEGER DEFAULT 0,
            render_progress INTEGER DEFAULT 0,
            upload_progress INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            content_key TEXT DEFAULT '',
            manual_reupload_authorized INTEGER DEFAULT 0,
            manual_reupload_reason TEXT DEFAULT '',
            last_attempt_id INTEGER DEFAULT NULL,
            published_url TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 6. Bảng Theo Dõi Kênh Tự Động 24/7 (Auto Monitor - tương đương bảng Monitor / ZhScDd)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_urls TEXT NOT NULL, -- Danh sách link kênh TikTok/Douyin/YT/FB cách nhau bởi dòng mới
            target_channel_id INTEGER DEFAULT NULL,
            target_group_name TEXT DEFAULT '',
            upload_to TEXT DEFAULT 'Shorts',
            render_mode TEXT DEFAULT 'shorts_9_16',
            render_config_json TEXT DEFAULT '{}',
            option_group_id INTEGER DEFAULT NULL,
            ai_Action TEXT DEFAULT 'rewrite_seo', -- 'none' | 'translate' | 'rewrite_seo'
            ai_provider TEXT DEFAULT 'gemini',
            ai_target_lang TEXT DEFAULT 'Tiếng Việt',
            ai_prompt TEXT DEFAULT '',
            min_views INTEGER DEFAULT 0,
            check_interval_minutes INTEGER DEFAULT 30,
            max_videos_per_scan INTEGER DEFAULT 3,
            is_running INTEGER DEFAULT 0,
            last_checked_at TEXT DEFAULT '',
            last_heartbeat_at TEXT DEFAULT '',
            status TEXT DEFAULT 'Stopped',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 7. Bảng Lịch sử ID Video đã Reup (Chống đăng trùng lặp cho Auto Monitor)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS published_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_post_id TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            channel_id INTEGER DEFAULT NULL,
            content_key TEXT DEFAULT '',
            published_url TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_post_id, channel_id)
        )
    """)

    # Reservation nguyên tử theo nội dung và kênh; hàng NeedsReview giữ chặn qua restart.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_reservations (
            content_key TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            queue_id INTEGER NOT NULL,
            claim_token TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'Active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (content_key, channel_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS upload_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL,
            attempt_type TEXT NOT NULL DEFAULT 'upload',
            source_post_id TEXT DEFAULT '',
            content_key TEXT DEFAULT '',
            channel_id INTEGER DEFAULT NULL,
            phase TEXT DEFAULT 'Queued',
            outcome TEXT DEFAULT 'InProgress',
            reason TEXT DEFAULT '',
            published_url TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(queue_id, attempt_no)
        )
    """)

    # 8. Bảng Nhật Ký Hệ Thống (Logs)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT DEFAULT 'System',
            channel_name TEXT DEFAULT '',
            video_title TEXT DEFAULT '',
            status TEXT DEFAULT 'INFO',
            message TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT DEFAULT '',
            queue_id INTEGER DEFAULT NULL,
            attempt_id INTEGER DEFAULT NULL,
            phase TEXT DEFAULT '',
            error_code TEXT DEFAULT ''
        )
    """)

    # 9. Bảng Cài Đặt Toàn Cục (Settings)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # 10. Bảng Preset Render có tên (học quy trình preset JSON của DHB Reup Facebook)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS render_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            render_mode TEXT DEFAULT 'shorts_9_16',
            config_json TEXT DEFAULT '{}',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS managed_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            owner_type TEXT DEFAULT '',
            owner_id INTEGER DEFAULT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_managed_artifacts_registered ON managed_artifacts(registered_at, kind)")

    # Tự động cập nhật cột mới (Migration an toàn không mất dữ liệu)
    def _add_col_if_missing(tbl, col, col_def):
        try:
            cols = [c[1] for c in cur.execute(f"PRAGMA table_info({tbl})").fetchall()]
            if col not in cols:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
        except Exception:
            pass

    _add_col_if_missing("youtube_channels", "emulate_mobile", "INTEGER DEFAULT 0")
    _add_col_if_missing("youtube_channels", "is_subchannel", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "resolution", "TEXT DEFAULT '1080p'")
    _add_col_if_missing("video_queue", "emulate_mobile", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "target_profile_name", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "made_for_kids", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "age_restriction", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "altered_content", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "is_premiere", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "visibility", "TEXT DEFAULT 'public'")
    _add_col_if_missing("video_queue", "schedule_time", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "target_group_names_json", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "group_schema_version", "INTEGER DEFAULT 1")
    _add_col_if_missing("video_queue", "assigned_channel_id", "INTEGER DEFAULT NULL")
    _add_col_if_missing("video_queue", "assigned_group_names_json", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "claim_token", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "content_key", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "manual_reupload_authorized", "INTEGER DEFAULT 0")
    _add_col_if_missing("video_queue", "manual_reupload_reason", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "last_attempt_id", "INTEGER DEFAULT NULL")
    _add_col_if_missing("published_history", "content_key", "TEXT DEFAULT ''")
    _add_col_if_missing("chrome_profiles", "login_status", "TEXT DEFAULT ''")
    _add_col_if_missing("chrome_profiles", "last_login_check", "TEXT DEFAULT ''")
    _add_col_if_missing("chrome_profiles", "antidetect_profile_id", "TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_slot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            state TEXT NOT NULL,
            owner TEXT,
            process_id INTEGER,
            queue_id INTEGER,
            attempt_id INTEGER,
            started_at REAL,
            finished_at REAL,
            cooldown_until REAL
        )
    """)
    conn.execute("INSERT OR IGNORE INTO upload_slot (id, state) VALUES (1, 'idle')")
    _add_col_if_missing("monitors", "last_heartbeat_at", "TEXT DEFAULT ''")
    _add_col_if_missing("monitors", "metadata_policy_json", "TEXT DEFAULT ''")
    _add_col_if_missing("monitors", "metadata_revision", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "monitor_metadata_policy_json", "TEXT DEFAULT ''")
    _add_col_if_missing("video_queue", "post_upload_warnings_json", "TEXT DEFAULT '[]'")
    _add_col_if_missing("upload_attempts", "post_upload_warnings_json", "TEXT DEFAULT '[]'")
    required_review_columns = {
        'monitors': {'metadata_policy_json', 'metadata_revision'},
        'video_queue': {'monitor_metadata_policy_json', 'post_upload_warnings_json'},
        'upload_attempts': {'post_upload_warnings_json'},
    }
    for table, required in required_review_columns.items():
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if not required <= actual:
            conn.rollback()
            conn.close()
            raise RuntimeError(f'Migration review không đầy đủ ở bảng {table}; dừng khởi động để giữ dữ liệu.')
    schema = conn.execute("SELECT version FROM app_schema_versions WHERE schema_key='review_metadata'").fetchone()
    if schema and schema[0] > 1:
        conn.rollback()
        conn.close()
        raise RuntimeError('Schema review_metadata mới hơn ứng dụng; không tự hạ phiên bản.')
    conn.execute("INSERT OR IGNORE INTO app_schema_versions(schema_key,version) VALUES('review_metadata',1)")
    conn.execute("UPDATE monitors SET is_running=0, status='Cần xác nhận metadata' "
                 "WHERE COALESCE(metadata_policy_json,'')='' OR COALESCE(metadata_revision,'')=''")
    _add_col_if_missing("system_logs", "run_id", "TEXT DEFAULT ''")
    _add_col_if_missing("system_logs", "queue_id", "INTEGER DEFAULT NULL")
    _add_col_if_missing("system_logs", "attempt_id", "INTEGER DEFAULT NULL")
    _add_col_if_missing("system_logs", "phase", "TEXT DEFAULT ''")
    _add_col_if_missing("system_logs", "error_code", "TEXT DEFAULT ''")

    try:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chrome_profiles_antidetect "
            "ON chrome_profiles(antidetect_profile_id) "
            "WHERE antidetect_profile_id IS NOT NULL AND antidetect_profile_id != ''"
        )
    except Exception:
        pass

    # Sửa default legacy 1 -> 0 (Monitor và mọi INSERT thiếu cột phải chạy Desktop)
    emulate_default_fixed = _fix_video_queue_emulate_default(conn, cur)
    try:
        migrate_metadata_intent_schema(conn)
    except Exception:
        conn.rollback()
        conn.close()
        raise
    try:
        migrate_upload_evidence_schema(conn)
    except Exception:
        conn.rollback()
        conn.close()
        raise
    init_approval_schema(conn)
    conn.execute("INSERT OR IGNORE INTO app_schema_versions (schema_key, version) VALUES ('upload_hardening', 1)")

    # Nhóm kênh mặc định chỉ có MỘT tên chuẩn 'Mặc định'; dữ liệu cũ ghi 'Default'
    # từng được pipeline tự thay thế ngầm, dễ đăng nhầm nhóm — chuẩn hóa một lần.
    try:
        legacy_group_rows = normalize_legacy_group_defaults(conn, cur)
    except Exception as group_err:
        legacy_group_rows = -1
        group_normalize_error = str(group_err)
    else:
        group_normalize_error = ""

    try:
        migrate_phase4_group_schema(conn, cur)
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Tăng tốc kiểm tra trùng post_id khi import lại cùng thư mục/nguồn
    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_queue_project_post "
            "ON video_queue(project_name, post_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_queue_project_content "
            "ON video_queue(project_name, content_key) WHERE content_key != ''"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_reservations_queue "
            "ON content_reservations(queue_id, state)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_attempts_queue "
            "ON upload_attempts(queue_id, attempt_no DESC)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_published_history_content_channel "
            "ON published_history(content_key, channel_id) "
            "WHERE content_key IS NOT NULL AND content_key != '' AND channel_id IS NOT NULL"
        )
    except Exception:
        pass

    # NGUYÊN TẮC AN TOÀN TÀI KHOẢN (Native Desktop): ép Desktop cho các dòng được tạo TỰ ĐỘNG
    # (kênh giữ chỗ theo Profile / kênh quét tự động / video nhập CSV) — người dùng vẫn bật Mobile
    # thủ công được cho từng video mới qua giao diện nếu thực sự cần.
    try:
        cur.execute("""
            UPDATE youtube_channels SET emulate_mobile = 0
            WHERE emulate_mobile = 1 AND (
                note LIKE '%theo Profile%' OR note LIKE '%quét tự động%'
            )
        """)
        cur.execute("""
            UPDATE video_queue SET emulate_mobile = 0
            WHERE emulate_mobile = 1 AND source_type = 'CSV' AND status = 'Pending'
        """)
    except Exception:
        pass

    # Dọn nhật ký cũ: chỉ giữ 5000 dòng gần nhất để DB không phình to vô hạn
    try:
        cur.execute("DELETE FROM system_logs WHERE id NOT IN (SELECT id FROM system_logs ORDER BY id DESC LIMIT 5000)")
    except Exception:
        pass

    conn.commit()
    conn.close()

    if emulate_default_fixed:
        add_log(
            "System",
            "Đã sửa mặc định emulate_mobile của video_queue từ 1 về 0 (Native Desktop, chống Checkpoint).",
            "SUCCESS",
        )
    if legacy_group_rows > 0:
        add_log(
            "System",
            f"Đã chuẩn hóa {legacy_group_rows} bản ghi nhóm kênh 'Default' cũ về 'Mặc định'.",
            "SUCCESS",
        )
    elif legacy_group_rows < 0:
        add_log(
            "System",
            f"Không chuẩn hóa được nhóm kênh 'Default' cũ: {group_normalize_error}",
            "ERROR",
        )

    backup_database_on_startup()

    # Đồng bộ dữ liệu khởi tạo từ C:\DHB Reup Facebook nếu chạy lần đầu
    sync_from_dhb_facebook(initial_only=True)


def backup_database_on_startup():
    """Sao lưu DB bằng SQLite Backup API (an toàn với WAL), giữ tối đa 5 bản gần nhất."""
    try:
        backup_dir = APP_DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = backup_dir / f"dhb_youtube_{stamp}.sqlite"
        src = sqlite3.connect(str(APP_DB_PATH))
        dst = sqlite3.connect(str(dest))
        src.backup(dst)
        dst.close()
        src.close()
        backups = sorted(backup_dir.glob("dhb_youtube_*.sqlite"))
        for old in backups[:-5]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as e:
        add_log("Housekeeping", f"Lỗi sao lưu database: {e}", "WARNING")


def export_all_tables_csv(target_dir: str, keep: int = 10) -> Dict[str, Any]:
    """Export toàn bộ bảng ra CSV (UTF-8 BOM) trong một thư mục có timestamp.

    Dùng cho backup ngoài ổ tool; giữ tối đa `keep` bản export gần nhất.
    """
    from csv_safety import ENCODING_VERSION, encode_csv_cell

    raw_target = str(target_dir or "").strip()
    if not raw_target:
        raise ValueError("Chưa cấu hình thư mục export CSV.")
    base = Path(raw_target).expanduser()
    base.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = base / f"DHB_Export_{stamp}"
    suffix = 1
    while dest.exists():
        dest = base / f"DHB_Export_{stamp}_{suffix}"
        suffix += 1
    dest.mkdir(parents=True, exist_ok=False)

    conn = get_conn()
    secret_values = collect_secret_values(connection=conn)
    table_rows: Dict[str, int] = {}
    total_rows = 0
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for table in tables:
            cursor = conn.execute(f'SELECT * FROM "{table}"')
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            with open(dest / f"{table}.csv", "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in rows:
                    out_row = []
                    for column in columns:
                        value = row[column]
                        if _is_secret_column(column):
                            value = ""
                        elif table == "app_settings" and column == "value" and is_secret_setting_key(row["key"]):
                            value = ""
                        elif isinstance(value, str):
                            value = redact_secrets(value, secret_values)
                        out_row.append(encode_csv_cell(value))
                    writer.writerow(out_row)
            table_rows[table] = len(rows)
            total_rows += len(rows)
    finally:
        conn.close()

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "database": str(APP_DB_PATH),
        "tables": table_rows,
        "cell_encoding": ENCODING_VERSION,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    keep = max(1, min(100, int(keep or 10)))
    folders = sorted(
        [p for p in base.glob("DHB_Export_*") if p.is_dir()], key=lambda p: p.name
    )
    removed = []
    for old in folders[:-keep]:
        try:
            shutil.rmtree(old)
            removed.append(old.name)
        except Exception:
            pass

    return {
        "path": str(dest),
        "tables": len(table_rows),
        "rows": total_rows,
        "removed": removed,
    }


def run_housekeeping():
    """Bảo trì định kỳ: backup DB và cắt log; giữ lịch sử đăng để chống trùng bền vững."""
    backup_database_on_startup()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM system_logs WHERE id NOT IN (SELECT id FROM system_logs ORDER BY id DESC LIMIT 5000)")
        log_deleted = cur.rowcount
        conn.commit()
        if log_deleted:
            add_log("Housekeeping", f"Đã dọn {log_deleted} log cũ; lịch sử đăng được giữ để chống trùng lâu dài.", "INFO")
    except Exception as e:
        add_log("Housekeeping", f"Lỗi dọn dẹp dữ liệu định kỳ: {e}", "WARNING")
    finally:
        conn.close()

    # Dọn riêng render_text đã đăng ký trong manifest, theo cùng bộ bảo vệ đường dẫn và queue refs.
    try:
        conn = get_conn()
        try:
            old_text_artifacts = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM managed_artifacts WHERE kind = 'render_text' "
                    "AND registered_at < datetime('now', '-1 day')"
                ).fetchall()
            ]
        finally:
            conn.close()
        if old_text_artifacts:
            from storage_manager import cleanup_managed_artifacts
            cleanup_managed_artifacts(old_text_artifacts, retention_days=1)
    except Exception as e:
        add_log("Storage", f"Không dọn được artifact text render cũ: {type(e).__name__}", "WARNING")

    # Export CSV toàn bộ DB ra thư mục ngoài nếu chủ tool đã cấu hình
    try:
        settings = get_settings()
        target = str(settings.get("external_backup_dir") or "").strip()
        if target:
            keep = int(settings.get("external_backup_keep", 10) or 10)
            result = export_all_tables_csv(target, keep=keep)
            add_log(
                "Housekeeping",
                f"Đã export CSV định kỳ {result['tables']} bảng ({result['rows']} dòng) → {result['path']}",
                "SUCCESS",
            )
    except Exception as e:
        add_log("Housekeeping", f"Lỗi export CSV định kỳ: {e}", "WARNING")


def content_identity_migration_report() -> Dict[str, int]:
    """Read-only counts for legacy rows; never infers or fills uncertain identities."""
    conn = get_conn()
    try:
        queue_rows = conn.execute(
            "SELECT id, project_name, source_type, platform, post_id, source_url, "
            "input_video_path, content_key FROM video_queue"
        ).fetchall()
        history_rows = conn.execute(
            "SELECT source_post_id, source_url, channel_id, content_key FROM published_history"
        ).fetchall()

        source_keys = set()
        duplicate_candidates = 0
        file_hash_pending = missing_local_files = source_id_available = 0
        invalid_source_url = missing_source_identity = 0
        identified = 0
        for row in queue_rows:
            has_content_key = bool(str(row["content_key"] or "").strip())
            if has_content_key:
                identified += 1
            source_type = str(row["source_type"] or "").lower()
            platform = str(row["platform"] or "").lower()
            is_local = source_type in ("file", "localfile") or platform in ("localfile", "localupload")
            if is_local:
                if has_content_key:
                    continue
                path = str(row["input_video_path"] or "").strip()
                if path and Path(path).is_file():
                    file_hash_pending += 1
                else:
                    missing_local_files += 1
                continue

            post_id = str(row["post_id"] or "").strip()
            url_key = normalize_source_url(row["source_url"])
            if post_id:
                source_id_available += 1
                dedupe_key = (row["project_name"], platform, post_id)
            elif url_key:
                dedupe_key = (row["project_name"], "url", url_key)
            else:
                if str(row["source_url"] or "").strip():
                    invalid_source_url += 1
                else:
                    missing_source_identity += 1
                continue
            if dedupe_key in source_keys:
                duplicate_candidates += 1
            else:
                source_keys.add(dedupe_key)

        return {
            "queue_total": len(queue_rows),
            "queue_with_content_key": identified,
            "local_file_hash_pending": file_hash_pending,
            "missing_local_file": missing_local_files,
            "source_id_available": source_id_available,
            "invalid_source_url": invalid_source_url,
            "missing_source_identity": missing_source_identity,
            "queue_duplicate_candidates": duplicate_candidates,
            "history_total": len(history_rows),
            "history_missing_content_key": sum(not str(r["content_key"] or "").strip() for r in history_rows),
            "history_missing_channel": sum(r["channel_id"] is None for r in history_rows),
            "history_missing_source_id": sum(not str(r["source_post_id"] or "").strip() for r in history_rows),
        }
    finally:
        conn.close()


SECRET_CHANNEL_COLUMNS = {"oauth_token_json", "cookie"}
SECRET_SETTING_KEYS = {
    "gemini_api_key", "openai_api_key", "deepseek_api_key", "youtube_api_key",
    "facebook_cookie", "douyin_cookie", "tiktok_cookie", "instagram_cookie",
}
PUBLIC_SETTING_KEYS = {
    "gemini_model", "openai_model", "deepseek_model", "download_resolution",
    "download_resolution_tiktok", "download_cookies_file", "hide_browser",
    "delete_temp_after_upload", "max_concurrent_threads", "max_concurrent_renders",
    "upload_delay_seconds", "checks_timeout_seconds", "monitor_upload_gap_minutes",
    "max_uploads_per_run", "thumbnail_source", "external_backup_dir", "external_backup_keep",
}
SETTING_INT_RANGES = {
    "max_concurrent_threads": (1, 10),
    "max_concurrent_renders": (1, 8),
    "upload_delay_seconds": (0, 3600),
    "checks_timeout_seconds": (30, 600),
    "monitor_upload_gap_minutes": (0, 1440),
    "max_uploads_per_run": (0, 100),
    "external_backup_keep": (1, 100),
}
SETTING_BOOL_KEYS = {"hide_browser", "delete_temp_after_upload"}
SETTING_STRING_LIMITS = {
    "gemini_model": 160, "openai_model": 160, "deepseek_model": 160,
    "download_resolution": 80, "download_resolution_tiktok": 16,
    "download_cookies_file": 2048, "external_backup_dir": 2048,
    "thumbnail_source": 16,
}
_LOG_CONTEXT = threading.local()
_SECRET_CACHE_LOCK = threading.Lock()
_SECRET_CACHE_AT = 0.0
_SECRET_CACHE_VALUES: List[str] = []


def _invalidate_secret_cache() -> None:
    global _SECRET_CACHE_AT, _SECRET_CACHE_VALUES
    with _SECRET_CACHE_LOCK:
        _SECRET_CACHE_AT = 0.0
        _SECRET_CACHE_VALUES = []


def set_log_context(**values: Any) -> None:
    """Set structured context for logs emitted by the current worker thread."""
    current = dict(getattr(_LOG_CONTEXT, "values", {}) or {})
    current.update({key: value for key, value in values.items() if value is not None})
    _LOG_CONTEXT.values = current


def clear_log_context() -> None:
    _LOG_CONTEXT.values = {}


def is_secret_setting_key(key: str) -> bool:
    k = str(key or "").strip().lower()
    return (
        k in SECRET_SETTING_KEYS
        or k in {"api_key", "token", "password", "secret", "cookie", "oauth_token_json"}
        or k.endswith(("_api_key", "_cookie", "_token", "_password", "_secret"))
    )


def _is_secret_column(column: str) -> bool:
    c = str(column or "").strip().lower()
    return c in SECRET_CHANNEL_COLUMNS or is_secret_setting_key(c)


def public_settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = settings if settings is not None else get_settings()
    result: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if is_secret_setting_key(key):
            result[f"{key}_configured"] = bool(str(value or "").strip())
        elif key in PUBLIC_SETTING_KEYS:
            result[key] = value
    return result


def validate_public_settings(settings: Any) -> Dict[str, Any]:
    """Validate the small, supported set of settings writable from the local API."""
    if not isinstance(settings, dict):
        raise ValueError("Cấu hình phải là một object JSON.")  # noqa: TRY004 - API maps validation errors to 400
    unknown = sorted(str(key) for key in settings if key not in PUBLIC_SETTING_KEYS)
    if unknown:
        secret_names = [key for key in unknown if is_secret_setting_key(key)]
        if secret_names:
            raise ValueError("Giá trị bí mật phải được ghi qua thao tác lưu khóa riêng.")
        raise ValueError("Có trường cài đặt không được hỗ trợ: " + ", ".join(unknown[:8]))

    result: Dict[str, Any] = {}
    for key, value in settings.items():
        if key in SETTING_INT_RANGES:
            if type(value) is not int:
                raise ValueError(f"Cài đặt {key} phải là số nguyên.")
            low, high = SETTING_INT_RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"Cài đặt {key} phải nằm trong khoảng {low}–{high}.")
            result[key] = value
        elif key in SETTING_BOOL_KEYS:
            if type(value) is not bool:
                raise ValueError(f"Cài đặt {key} phải là true/false.")
            result[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"Cài đặt {key} phải là chuỗi.")
            normalized = value.strip()
            if len(normalized) > SETTING_STRING_LIMITS[key] or "\x00" in normalized:
                raise ValueError(f"Cài đặt {key} vượt giới hạn độ dài hoặc có ký tự không hợp lệ.")
            if key == "thumbnail_source" and normalized not in {"reup", "generate"}:
                raise ValueError("Nguồn thumbnail chỉ nhận reup hoặc generate.")
            if key == "download_resolution_tiktok" and normalized not in {"540p", "720p", "1080p"}:
                raise ValueError("Độ phân giải tải TikTok chỉ nhận 540p, 720p hoặc 1080p.")
            result[key] = normalized
    return result


def validate_secret_settings(settings: Any) -> Dict[str, str]:
    if not isinstance(settings, dict) or not settings:
        raise ValueError("Cần gửi ít nhất một khóa bí mật để lưu.")  # noqa: TRY004 - API maps validation errors to 400
    result: Dict[str, str] = {}
    for key, value in settings.items():
        if key not in SECRET_SETTING_KEYS:
            raise ValueError(f"Không hỗ trợ lưu khóa bí mật {key}.")
        if not isinstance(value, str):
            raise ValueError(f"Giá trị {key} phải là chuỗi.")  # noqa: TRY004 - API maps validation errors to 400
        normalized = value.strip()
        if not normalized:
            continue
        if len(normalized) > 20000 or "\x00" in normalized:
            raise ValueError(f"Giá trị {key} vượt giới hạn hoặc có ký tự không hợp lệ.")
        result[key] = normalized
    return result


def public_channel(row: Any) -> Dict[str, Any]:
    item = dict(row)
    has_oauth = bool(str(item.pop("oauth_token_json", "") or "").strip())
    has_cookie = bool(str(item.pop("cookie", "") or "").strip())
    item["has_oauth_token"] = has_oauth
    item["has_cookie"] = has_cookie
    if "proxy" in item:
        item["proxy"] = mask_proxy_credentials(item.get("proxy"))
    return item


def mask_proxy_credentials(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc and parsed.username is not None and parsed.password is not None:
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            return redact_secrets(parsed._replace(netloc=f"{parsed.username}:***@{host}").geturl(), [])
    except (ValueError, UnicodeError):
        pass
    parts = raw.split(":")
    if len(parts) == 4 and parts[1].isdigit() and parts[2]:
        return ":".join(parts[:3]) + ":***"
    return redact_secrets(raw, [])


def collect_secret_values(settings: Optional[Dict[str, Any]] = None, connection: Optional[sqlite3.Connection] = None) -> List[str]:
    global _SECRET_CACHE_AT, _SECRET_CACHE_VALUES
    if settings is None and connection is None:
        with _SECRET_CACHE_LOCK:
            if _SECRET_CACHE_AT and datetime.now().timestamp() - _SECRET_CACHE_AT < 5:
                return list(_SECRET_CACHE_VALUES)
    try:
        if settings is not None:
            data = settings
        elif connection is not None:
            data = {}
            for row in connection.execute("SELECT key, value FROM app_settings"):
                try:
                    data[row["key"]] = json.loads(row["value"])
                except Exception:
                    data[row["key"]] = row["value"]
        else:
            data = get_settings()
    except Exception as exc:
        raise SecretCollectionError("Không đọc được cấu hình bí mật để che dữ liệu.") from exc
    values = []
    for key, value in (data or {}).items():
        if not is_secret_setting_key(key):
            continue
        raw = str(value or "").strip()
        if len(raw) >= 6:
            values.append(raw)
    # Include channel credentials and only the password component of stored proxies.
    owned_connection = connection is None
    conn = connection
    try:
        if conn is None:
            conn = get_conn()
        try:
            for row in conn.execute("SELECT oauth_token_json, cookie FROM youtube_channels"):
                for raw_value in row:
                    raw = str(raw_value or "").strip()
                    if raw:
                        values.append(raw)
            for row in conn.execute("SELECT proxy FROM youtube_channels UNION ALL SELECT proxy FROM chrome_profiles"):
                raw = str(row[0] or "").strip()
                parts = raw.split(":")
                if len(parts) == 4 and parts[1].isdigit() and parts[3]:
                    values.append(parts[3])
                else:
                    try:
                        parsed = urlsplit(raw)
                        if parsed.password:
                            values.append(parsed.password)
                    except ValueError:
                        pass
        finally:
            if owned_connection:
                conn.close()
    except Exception as exc:
        raise SecretCollectionError("Không đọc được thông tin đăng nhập để che dữ liệu.") from exc
    result = sorted(set(values), key=len, reverse=True)
    if settings is None and connection is None:
        with _SECRET_CACHE_LOCK:
            _SECRET_CACHE_VALUES = result
            _SECRET_CACHE_AT = datetime.now().timestamp()
    return result


def redact_secrets(text: Any, secrets: Optional[List[str]] = None) -> str:
    out = str(text or "")
    if secrets is None:
        try:
            values = collect_secret_values()
        except SecretCollectionError:
            return "***"
    else:
        values = secrets
    for secret in values:
        if secret:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(authorization:\s*(?:bearer|basic)\s+|\b(?:bearer|basic)\s+)[^\s,;]+", r"\1***", out)
    out = re.sub(
        r"(?i)([?&](?:key|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|oauth[_-]?token|token|password|proxy[_-]?password|passwd|client[_-]?secret|secret|cookie)=)[^&\s#]+",
        r"\1***", out,
    )
    out = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|oauth[_-]?token|token|password|proxy[_-]?password|passwd|client[_-]?secret|secret|cookie)\b\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        r"\1***", out,
    )
    out = re.sub(
        r"(?i)(\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|oauth[_-]?token|token|password|proxy[_-]?password|passwd|client[_-]?secret|secret|cookie|oauth_token_json)\"\s*:\s*\")[^\"]*(\")",
        r"\1***\2", out,
    )
    out = re.sub(r"(?i)(https?://[^/@\s:]+:)[^/@\s]+@", r"\1***@", out)
    out = re.sub(r"(?i)(cookie(?:\s+header)?\s*:\s*)[^\r\n]+", r"\1***", out)
    out = re.sub(r"(?i)((?:\d{1,3}\.){3}\d{1,3}:\d{2,5}:[^:\s]+:)[^\s]+", r"\1***", out)
    return out


def add_log(
    module: str,
    message: str,
    status: str = "INFO",
    channel_name: str = "",
    video_title: str = "",
    *,
    run_id: Optional[str] = None,
    queue_id: Optional[int] = None,
    attempt_id: Optional[int] = None,
    phase: Optional[str] = None,
    error_code: Optional[str] = None,
):
    conn = get_conn()
    context = dict(getattr(_LOG_CONTEXT, "values", {}) or {})
    safe_status = redact_secrets(status)
    log_error_code = str(error_code or context.get("error_code") or ("ERR_UNCLASSIFIED" if safe_status.upper() == "ERROR" else ""))
    context_run_id = run_id if run_id is not None else context.get("run_id", "")
    context_queue_id = queue_id if queue_id is not None else context.get("queue_id")
    context_attempt_id = attempt_id if attempt_id is not None else context.get("attempt_id")
    context_phase = phase if phase is not None else context.get("phase", "")
    conn.execute(
        "INSERT INTO system_logs (module, channel_name, video_title, status, message, created_at, run_id, queue_id, attempt_id, phase, error_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            redact_secrets(module), redact_secrets(channel_name), redact_secrets(video_title), safe_status,
            redact_secrets(message), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            redact_secrets(context_run_id), context_queue_id,
            context_attempt_id, redact_secrets(context_phase), redact_secrets(log_error_code),
        ),
    )
    conn.commit()
    conn.close()


def register_managed_artifact(path: str, kind: str, owner_type: str = "", owner_id: Optional[int] = None) -> Optional[int]:
    """Đăng ký một file do tool tạo; file nguồn người dùng không được gọi hàm này."""
    raw = Path(str(path or "")).expanduser()
    try:
        if raw.is_symlink() or not raw.is_file():
            return None
        resolved = raw.resolve(strict=True)
        canonical = os.path.normcase(str(resolved))
        size_bytes = int(resolved.stat().st_size)
    except (OSError, RuntimeError):
        return None
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO managed_artifacts (path, kind, owner_type, owner_id, size_bytes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, owner_type=excluded.owner_type,
                   owner_id=excluded.owner_id, size_bytes=excluded.size_bytes,
                   registered_at=CURRENT_TIMESTAMP""",
            (canonical, str(kind or "file"), str(owner_type or ""), owner_id, size_bytes),
        )
        row = conn.execute("SELECT id FROM managed_artifacts WHERE path = ?", (canonical,)).fetchone()
        conn.commit()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def get_managed_artifacts() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM managed_artifacts ORDER BY registered_at DESC, id DESC")]
    finally:
        conn.close()


def remove_managed_artifact(artifact_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM managed_artifacts WHERE id = ?", (int(artifact_id),))
        conn.commit()
    finally:
        conn.close()


def get_settings() -> Dict[str, Any]:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    conn.close()
    res = {}
    for r in rows:
        try:
            res[r["key"]] = json.loads(r["value"])
        except Exception:
            res[r["key"]] = r["value"]
    return res


def save_settings(new_settings: Dict[str, Any]):
    conn = get_conn()
    for k, v in new_settings.items():
        val_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, val_str),
        )
    conn.commit()
    conn.close()
    if any(is_secret_setting_key(key) for key in new_settings):
        _invalidate_secret_cache()


def sync_from_dhb_facebook(initial_only: bool = False) -> Dict[str, int]:
    """
    Đồng bộ CHỈ các cài đặt hệ thống (API Keys) và Chrome Profiles từ máy tính.
    TUYỆT ĐỐI KHÔNG đồng bộ các dự án video Facebook (.dhb) hay các Page Facebook từ DHB.
    """
    stats = {"groups": 0, "profiles": 0, "option_groups": 0, "projects": 0}
    conn = get_conn()
    cur = conn.cursor()

    # Đảm bảo có project YouTube sạch
    cur.execute("INSERT OR IGNORE INTO projects (name) VALUES ('Dự án YouTube #1')")
    cur.execute("INSERT OR IGNORE INTO channel_groups (name, description) VALUES ('Mặc định', 'Nhóm mặc định')")

    if initial_only:
        existing_settings = cur.execute("SELECT COUNT(*) as c FROM app_settings").fetchone()["c"]
        if existing_settings > 0:
            conn.commit()
            conn.close()
            return stats

    # 1. Đồng bộ setting.json (API keys, download config)
    dhb_cfg = load_dhb_settings()
    default_app_settings = {
        "gemini_api_key": dhb_cfg.get("Gemini_API", ""),
        "gemini_model": "gemini-2.5-flash",
        "openai_api_key": dhb_cfg.get("OpenAI_API", "") if dhb_cfg.get("OpenAI_API") != "Token" else "",
        "openai_model": dhb_cfg.get("OpenAI_Model", "gpt-4.1"),
        "deepseek_api_key": dhb_cfg.get("DeepSeek_API", "") if dhb_cfg.get("DeepSeek_API") != "Token" else "",
        "deepseek_model": "deepseek-chat",
        "youtube_api_key": dhb_cfg.get("Youtube_API", ""),
        "tiktok_cookie": dhb_cfg.get("TikTok_Cookie", ""),
        "douyin_cookie": dhb_cfg.get("Ixigua_Cookie", ""),
        "instagram_cookie": dhb_cfg.get("Instagram_Cookie", ""),
        "facebook_cookie": dhb_cfg.get("Facebook_Cookie", ""),
        "download_resolution": dhb_cfg.get("Download_Resolution", "1080p FullHD"),
        "download_cookies_file": "",
        "hide_browser": bool(dhb_cfg.get("HideBrowser", False)),
        "delete_temp_after_upload": bool(dhb_cfg.get("DeleteTemp", True)),
        "max_concurrent_threads": 2,
        "upload_delay_seconds": 10,
    }
    for k, v in default_app_settings.items():
        val_str = json.dumps(v, ensure_ascii=False)
        cur.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (k, val_str),
        )

    # 2. Đồng bộ các thư mục Profile Chrome để đăng YouTube
    if DHB_PROFILE_CHROME_DIR.exists():
        for pdir in DHB_PROFILE_CHROME_DIR.iterdir():
            if pdir.is_dir() and not pdir.name.startswith("."):
                pname = pdir.name
                cur.execute(
                    "INSERT OR IGNORE INTO chrome_profiles (name, path, source, note) VALUES (?, ?, 'DHB', 'Chrome Profile')",
                    (pname, str(pdir)),
                )
                chk = cur.execute("SELECT id FROM youtube_channels WHERE profile_name = ?", (pname,)).fetchone()
                if not chk:
                    cur.execute(
                        """
                        INSERT INTO youtube_channels
                        (name, handle, group_name, upload_mode, profile_name, profile_path, status, note)
                        VALUES (?, ?, 'Mặc định', 'playwright', ?, ?, 'Ready', 'Kênh YouTube chính theo Profile')
                        """,
                        (f"Kênh YouTube - {pname}", f"@{pname.lower().replace(' ', '')}", pname, str(pdir)),
                    )
                    set_channel_group_memberships(conn, int(cur.lastrowid), ["Mặc định"])
                    stats["profiles"] += 1

    # Đảm bảo có cấu hình đăng Shorts mẫu
    cur.execute("""
        INSERT OR IGNORE INTO upload_option_groups
        (name, affiliate_enabled, affiliate_url, pin_comment_enabled, pin_comment_text, append_hashtags)
        VALUES ('Cấu hình Đăng YouTube Shorts Chuẩn', 0, '', 0, '', '#shorts #trending #viral')
    """)

    conn.commit()
    conn.close()
    add_log(
        "Sync",
        f"Đã cập nhật cài đặt API và {stats['profiles']} Profile Chrome cho YouTube (Loại trừ toàn bộ dữ liệu Facebook).",
        "SUCCESS",
    )
    return stats


def migrate_phase4_group_schema(conn, cur=None) -> Dict[str, int]:
    """Backfill legacy single-group rows into the versioned membership schema."""
    cur = cur or conn.cursor()
    stats = {"groups_added": 0, "memberships_added": 0, "queue_rows_backfilled": 0}

    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_group_memberships (
            group_name TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_name, channel_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_channel_group_memberships_channel ON channel_group_memberships(channel_id, sort_order)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_schema_versions (
            schema_key TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for name, definition in (
        ("target_group_names_json", "TEXT DEFAULT ''"),
        ("group_schema_version", "INTEGER DEFAULT 1"),
        ("assigned_channel_id", "INTEGER DEFAULT NULL"),
        ("assigned_group_names_json", "TEXT DEFAULT ''"),
    ):
        columns = {row[1] for row in cur.execute("PRAGMA table_info(video_queue)").fetchall()}
        if name not in columns:
            cur.execute(f"ALTER TABLE video_queue ADD COLUMN {name} {definition}")

    cur.execute("""
        INSERT OR IGNORE INTO channel_groups (name, description)
        SELECT DISTINCT TRIM(group_name), '' FROM youtube_channels
        WHERE COALESCE(TRIM(group_name), '') != ''
    """)
    stats["groups_added"] = max(0, cur.rowcount)
    cur.execute("""
        INSERT OR IGNORE INTO channel_group_memberships (group_name, channel_id, sort_order, schema_version)
        SELECT TRIM(group_name), id, id, ? FROM youtube_channels
        WHERE COALESCE(TRIM(group_name), '') != ''
    """, (GROUP_SCHEMA_VERSION,))
    stats["memberships_added"] = max(0, cur.rowcount)

    rows = cur.execute(
        "SELECT id, target_type, target_group_name, target_group_names_json, group_schema_version "
        "FROM video_queue"
    ).fetchall()
    for row in rows:
        if int(row[4] or 0) >= GROUP_SCHEMA_VERSION and str(row[3] or "").strip():
            continue
        legacy = row[2] or ""
        is_group = str(row[1] or "GROUP").upper() == "GROUP"
        names = normalize_group_names(
            row[3],
            legacy_name=legacy,
            default="Mặc định" if is_group else None,
        )
        cur.execute(
            "UPDATE video_queue SET target_group_names_json = ?, group_schema_version = ? WHERE id = ?",
            (serialize_group_names(names), GROUP_SCHEMA_VERSION, row[0]),
        )
        stats["queue_rows_backfilled"] += 1

    cur.execute(
        "INSERT INTO app_schema_versions (schema_key, version, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(schema_key) DO UPDATE SET version = excluded.version, updated_at = CURRENT_TIMESTAMP",
        ("channel_group_memberships", GROUP_SCHEMA_VERSION),
    )
    return stats


def set_channel_group_memberships(conn, channel_id: int, group_names: Any) -> List[str]:
    """Replace memberships transactionally while retaining the legacy primary group."""
    names = normalize_group_names(group_names, default="Mặc định")
    placeholders = ",".join("?" for _ in names)
    existing = {
        str(row[0]) for row in conn.execute(
            f"SELECT name FROM channel_groups WHERE name IN ({placeholders})", names
        ).fetchall()
    }
    missing = [name for name in names if name not in existing]
    if missing:
        raise ValueError("Không tìm thấy nhóm kênh: " + ", ".join(missing))
    conn.execute("DELETE FROM channel_group_memberships WHERE channel_id = ?", (int(channel_id),))
    conn.executemany(
        "INSERT INTO channel_group_memberships (group_name, channel_id, sort_order, schema_version) VALUES (?, ?, ?, ?)",
        [(name, int(channel_id), idx, GROUP_SCHEMA_VERSION) for idx, name in enumerate(names)],
    )
    conn.execute("UPDATE youtube_channels SET group_name = ? WHERE id = ?", (names[0], int(channel_id)))
    return names


def get_all_profiles() -> List[Dict[str, Any]]:
    conn = get_conn()
    profiles = conn.execute("SELECT * FROM chrome_profiles ORDER BY name ASC").fetchall()
    res = []
    for p in profiles:
        pdict = dict(p)
        cnt = conn.execute("SELECT COUNT(*) as c FROM youtube_channels WHERE profile_name = ?", (pdict["name"],)).fetchone()["c"]
        pdict["channel_count"] = cnt
        pdict["proxy"] = mask_proxy_credentials(pdict.get("proxy"))
        res.append(pdict)
    conn.close()
    return res


def get_profile_binding(profile_name: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_antidetect_profile_id(profile_name: str) -> str:
    if not profile_name:
        return ""
    conn = get_conn()
    row = conn.execute(
        "SELECT antidetect_profile_id FROM chrome_profiles WHERE name = ?",
        (profile_name,),
    ).fetchone()
    conn.close()
    return str(row["antidetect_profile_id"] or "").strip() if row else ""


def _antidetect_proxy_display(proxy: Any) -> str:
    if not isinstance(proxy, dict) or not proxy.get("enabled"):
        return ""
    host = str(proxy.get("host") or "").strip()
    port = proxy.get("port")
    if not host or not port:
        return ""
    return f"{host}:{port}"


def sync_antidetect_profiles(remote_profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Đồng bộ profile từ Antidetect vào chrome_profiles, giữ nguyên kênh đang gắn.

    Không ghi proxy credentials (API Antidetect đã che mật khẩu) và không đụng
    tới thư mục Chrome cục bộ. Profile trùng tên với profile local được đặt
    tên riêng để không ghi đè dữ liệu cũ.
    """
    conn = get_conn()
    added = 0
    updated = 0
    skipped = 0
    synced: List[Dict[str, Any]] = []
    remote_ids = set()
    try:
        rows = conn.execute("SELECT name, antidetect_profile_id FROM chrome_profiles").fetchall()
        existing_by_ad = {
            str(r["antidetect_profile_id"]).strip(): r["name"]
            for r in rows
            if str(r["antidetect_profile_id"] or "").strip()
        }
        used_names = {r["name"] for r in rows}

        for item in remote_profiles or []:
            if not isinstance(item, dict):
                skipped += 1
                continue
            ad_id = str(item.get("id") or "").strip()
            if not ad_id:
                skipped += 1
                continue
            remote_ids.add(ad_id)
            remote_name = str(item.get("name") or "").strip() or f"Antidetect {ad_id[:8]}"
            proxy_display = _antidetect_proxy_display(item.get("proxy"))
            path = f"antidetect://{ad_id}"

            if ad_id in existing_by_ad:
                local_name = existing_by_ad[ad_id]
                conn.execute(
                    "UPDATE chrome_profiles SET source = 'Antidetect', path = ?, proxy = ? "
                    "WHERE antidetect_profile_id = ?",
                    (path, proxy_display, ad_id),
                )
                updated += 1
            else:
                local_name = remote_name
                suffix = 2
                while local_name in used_names:
                    local_name = f"{remote_name} (Antidetect {suffix})"
                    suffix += 1
                conn.execute(
                    """
                    INSERT INTO chrome_profiles
                        (name, path, proxy, source, note, antidetect_profile_id)
                    VALUES (?, ?, ?, 'Antidetect', ?, ?)
                    """,
                    (
                        local_name,
                        path,
                        proxy_display,
                        "Đồng bộ từ Antidetect — trình duyệt, proxy và fingerprint do Antidetect quản lý.",
                        ad_id,
                    ),
                )
                used_names.add(local_name)
                existing_by_ad[ad_id] = local_name
                added += 1

            synced.append({
                "name": local_name,
                "antidetect_profile_id": ad_id,
                "status": item.get("status") or "",
                "proxy": proxy_display,
            })

        missing = sorted(
            existing_by_ad[ad_id] for ad_id in existing_by_ad if ad_id not in remote_ids
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total": len(remote_profiles or []),
        "missing": missing,
        "profiles": synced,
    }


def get_channels_by_profile(profile_name: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC", (profile_name,)).fetchall()
    conn.close()
    return [public_channel(r) for r in rows]
