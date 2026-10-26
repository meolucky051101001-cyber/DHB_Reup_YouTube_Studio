import base64
import csv
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from config import (
    APP_DATA_DIR,
    APP_DB_PATH,
    APP_TEMP_DIR,
    DHB_MAIN_SQLITE,
    DHB_OPTION_SQLITE,
    DHB_PROJECT_DIR,
    DHB_PROFILE_CHROME_DIR,
    APP_PROFILES_DIR,
    load_dhb_settings,
)


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
    cur = conn.cursor()

    # 1. Bảng Nhóm Kênh (Channel Groups)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channel_groups (
            name TEXT PRIMARY KEY,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
            published_url TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_post_id, channel_id)
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    _add_col_if_missing("video_queue", "claim_token", "TEXT DEFAULT ''")
    _add_col_if_missing("chrome_profiles", "login_status", "TEXT DEFAULT ''")
    _add_col_if_missing("chrome_profiles", "last_login_check", "TEXT DEFAULT ''")
    _add_col_if_missing("monitors", "last_heartbeat_at", "TEXT DEFAULT ''")

    # Sửa default legacy 1 -> 0 (Monitor và mọi INSERT thiếu cột phải chạy Desktop)
    emulate_default_fixed = _fix_video_queue_emulate_default(conn, cur)

    # Nhóm kênh mặc định chỉ có MỘT tên chuẩn 'Mặc định'; dữ liệu cũ ghi 'Default'
    # từng được pipeline tự thay thế ngầm, dễ đăng nhầm nhóm — chuẩn hóa một lần.
    try:
        legacy_group_rows = normalize_legacy_group_defaults(conn, cur)
    except Exception as group_err:
        legacy_group_rows = -1
        group_normalize_error = str(group_err)
    else:
        group_normalize_error = ""

    # Tăng tốc kiểm tra trùng post_id khi import lại cùng thư mục/nguồn
    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_queue_project_post "
            "ON video_queue(project_name, post_id)"
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
                        out_row.append(value)
                    writer.writerow(out_row)
            table_rows[table] = len(rows)
            total_rows += len(rows)
    finally:
        conn.close()

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "database": str(APP_DB_PATH),
        "tables": table_rows,
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
    """Bảo trì định kỳ cho các phiên chạy 24/7: backup DB + cắt log + dọn lịch sử cũ."""
    backup_database_on_startup()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM system_logs WHERE id NOT IN (SELECT id FROM system_logs ORDER BY id DESC LIMIT 5000)")
        log_deleted = cur.rowcount
        cur.execute("DELETE FROM published_history WHERE created_at < datetime('now', '-90 days')")
        hist_deleted = cur.rowcount
        conn.commit()
        if log_deleted or hist_deleted:
            add_log("Housekeeping", f"Đã dọn {log_deleted} log cũ và {hist_deleted} bản ghi lịch sử quá 90 ngày.", "INFO")
    except Exception as e:
        add_log("Housekeeping", f"Lỗi dọn dẹp dữ liệu định kỳ: {e}", "WARNING")
    finally:
        conn.close()

    # Dọn file text watermark tạm quá 1 ngày tuổi (sinh ra khi render có chữ chạy / watermark chữ)
    try:
        import time
        for f in APP_TEMP_DIR.glob("wmtext_*.txt"):
            if f.stat().st_mtime < time.time() - 86400:
                f.unlink()
    except Exception:
        pass

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


SECRET_CHANNEL_COLUMNS = {"oauth_token_json", "cookie"}


def is_secret_setting_key(key: str) -> bool:
    k = str(key or "").strip().lower()
    return k in {
        "gemini_api_key",
        "openai_api_key",
        "deepseek_api_key",
        "youtube_api_key",
        "facebook_cookie",
        "douyin_cookie",
        "tiktok_cookie",
        "instagram_cookie",
    } or k.endswith("_api_key") or k.endswith("_cookie")


def _is_secret_column(column: str) -> bool:
    c = str(column or "").strip().lower()
    return c in SECRET_CHANNEL_COLUMNS or is_secret_setting_key(c)


def public_settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = settings if settings is not None else get_settings()
    result: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if is_secret_setting_key(key):
            result[f"{key}_configured"] = bool(str(value or "").strip())
        else:
            result[key] = value
    return result


def public_channel(row: Any) -> Dict[str, Any]:
    item = dict(row)
    has_oauth = bool(str(item.pop("oauth_token_json", "") or "").strip())
    has_cookie = bool(str(item.pop("cookie", "") or "").strip())
    item["has_oauth_token"] = has_oauth
    item["has_cookie"] = has_cookie
    return item


def collect_secret_values(settings: Optional[Dict[str, Any]] = None) -> List[str]:
    try:
        data = settings if settings is not None else get_settings()
    except Exception:
        return []
    values = []
    for key, value in (data or {}).items():
        if not is_secret_setting_key(key):
            continue
        raw = str(value or "").strip()
        if len(raw) >= 6:
            values.append(raw)
    return sorted(set(values), key=len, reverse=True)


def redact_secrets(text: Any, secrets: Optional[List[str]] = None) -> str:
    out = str(text or "")
    values = secrets if secrets is not None else collect_secret_values()
    for secret in values:
        if secret:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1***", out)
    out = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***", out)
    return out


def add_log(module: str, message: str, status: str = "INFO", channel_name: str = "", video_title: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO system_logs (module, channel_name, video_title, status, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (module, channel_name, video_title, status, redact_secrets(message), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
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


def get_all_profiles() -> List[Dict[str, Any]]:
    conn = get_conn()
    profiles = conn.execute("SELECT * FROM chrome_profiles ORDER BY name ASC").fetchall()
    res = []
    for p in profiles:
        pdict = dict(p)
        cnt = conn.execute("SELECT COUNT(*) as c FROM youtube_channels WHERE profile_name = ?", (pdict["name"],)).fetchone()["c"]
        pdict["channel_count"] = cnt
        res.append(pdict)
    conn.close()
    return res


def get_channels_by_profile(profile_name: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC", (profile_name,)).fetchall()
    conn.close()
    return [public_channel(r) for r in rows]