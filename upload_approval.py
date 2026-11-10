"""Durable approvals for destinations and upload settings."""
import hashlib
import json
import os
import re
import time


class PreviewStale(ValueError):
    pass


_QUEUE_FIELDS = (
    "project_name", "source_type", "platform", "source_url", "post_id",
    "target_type", "target_channel_id", "target_profile_name",
    "title", "description", "tags", "upload_to", "visibility", "schedule_time", "option_group_id",
    "made_for_kids", "altered_content", "age_restriction", "is_premiere",
    "render_mode", "render_config_json", "cut_start", "cut_end", "resolution",
    "emulate_mobile", "manual_reupload_authorized", "manual_reupload_reason",
)
_CHANNEL_FIELDS = (
    "id", "channel_id", "profile_name", "profile_path", "upload_mode", "proxy",
    "default_category_id", "default_privacy", "made_for_kids", "altered_content",
    "emulate_mobile", "use_hidden_desktop", "is_subchannel",
)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def asset_stamps(paths):
    result = []
    for path in paths:
        try:
            stat = os.stat(path) if path else None
        except OSError:
            stat = None
        result.append([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] if stat else None)
    return result


def upload_source_path(item):
    return item.get("output_video_path") or item.get("input_video_path") or ""


def approval_item(conn, item, channel, groups):
    channel_id = str(channel.get("channel_id") or "").strip()
    if not channel_id:
        raise PreviewStale("Kênh đích chưa có YouTube Channel ID.")
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise PreviewStale("YouTube Channel ID không đúng định dạng. Hãy xác minh lại kênh.")
    duplicate = conn.execute(
        "SELECT 1 FROM youtube_channels WHERE TRIM(channel_id) = ? AND id <> ? LIMIT 1",
        (channel_id, channel["id"]),
    ).fetchone()
    if duplicate:
        raise PreviewStale("YouTube Channel ID xuất hiện ở nhiều dòng kênh. Hãy đối soát liên kết.")
    name = channel.get("profile_name") or ""
    binding = conn.execute(
        "SELECT path, source, antidetect_profile_id, proxy FROM chrome_profiles WHERE name = ?",
        (name,),
    ).fetchone()
    if name and not binding:
        raise PreviewStale("Profile của kênh đích không còn trong danh sách đã lưu.")
    profile = dict(binding) if binding else {}
    profile_id = str(profile.get("antidetect_profile_id") or "").strip()
    path = str(channel.get("profile_path") or "").strip()
    if str(profile.get("source") or "").lower() == "antidetect" or path.lower().startswith("antidetect://"):
        if not profile_id or (path.lower().startswith("antidetect://") and path[13:].split("/")[0] != profile_id):
            raise PreviewStale("Liên kết UUID của profile Antidetect thiếu hoặc không khớp.")
    option_id = item.get("option_group_id")
    options = conn.execute("SELECT * FROM upload_option_groups WHERE id = ?", (option_id,)).fetchone() if option_id else None
    paths = [item.get(key) or "" for key in ("input_video_path", "output_video_path", "thumbnail_path")]
    return {
        "id": item["id"],
        "channel_id": channel["id"],
        "youtube_channel_id": channel_id,
        "profile_name": name,
        "antidetect_profile_id": profile_id,
        "groups": groups,
        "paths": paths,
        "asset_stamps": asset_stamps(paths),
        "timezone": list(time.tzname),
        "video": {key: digest(item.get(key)) if key == "source_url" else item.get(key) for key in _QUEUE_FIELDS},
        "binding_revision": digest({
            "channel": {key: channel.get(key) for key in _CHANNEL_FIELDS},
            "profile": profile,
            "cookie": digest(channel.get("cookie")),
            "oauth": digest(channel.get("oauth_token_json")),
        }),
        "option_revision": digest(dict(options) if options else None),
    }


def init_approval_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_approvals (
            token TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            state TEXT NOT NULL,
            options_json TEXT NOT NULL,
            run_token TEXT UNIQUE,
            origin TEXT NOT NULL DEFAULT 'manual'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_approval_items (
            token TEXT NOT NULL,
            queue_id INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            PRIMARY KEY (token, queue_id)
        )
    """)
    # Protect all writers, including profile sync and alternate API routes.
    active = "COALESCE(q.claim_token, '') <> ''"
    for table, key, fields in (
        ("youtube_channels", "c.id = OLD.id", _CHANNEL_FIELDS + ("cookie", "oauth_token_json")),
        ("chrome_profiles", "c.profile_name = OLD.name", ("name", "path", "source", "proxy", "antidetect_profile_id")),
    ):
        occupied = (
            "EXISTS (SELECT 1 FROM youtube_channels c JOIN video_queue q ON q.assigned_channel_id = c.id "
            f"WHERE {key} AND {active})"
        )
        changed = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in fields)
        for event, condition in (("DELETE", occupied), ("UPDATE", f"({changed}) AND {occupied}")):
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS upload_approval_guard_{table}_{event.lower()}
                BEFORE {event} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'UPLOAD_BINDING_BUSY'); END
            """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS upload_approval_guard_reserved_channel
        BEFORE UPDATE OF channel_id ON youtube_channels
        WHEN OLD.channel_id IS NOT NEW.channel_id AND EXISTS (
            SELECT 1 FROM content_reservations r WHERE r.channel_id = OLD.id
        )
        BEGIN SELECT RAISE(ABORT, 'UPLOAD_BINDING_BUSY'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS upload_approval_guard_reserved_channel_delete
        BEFORE DELETE ON youtube_channels
        WHEN EXISTS (SELECT 1 FROM content_reservations r WHERE r.channel_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'UPLOAD_BINDING_BUSY'); END
    """)
