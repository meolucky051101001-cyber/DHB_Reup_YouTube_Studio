import sys
import os
import re
import csv
import io
import json
import hashlib
import monitor_policy
import threading
import subprocess
import sqlite3
from contextlib import closing
from functools import lru_cache
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body, File, UploadFile, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from starlette.concurrency import run_in_threadpool

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import (
    APP_ROOT,
    APP_DATA_DIR,
    APP_DB_PATH,
    APP_TEMP_DIR,
    DHB_ROOT,
    FFMPEG_EXE,
    FFPROBE_EXE,
    YTDL_EXE,
    list_dhb_render_resources,
    load_dhb_render_configs,
)
from api_security import (
    generate_api_token,
    is_allowed_host,
    is_same_local_origin,
    token_matches,
)
from content_identity import copy_stream_and_hash, normalize_source_url
from database import (
    init_db,
    get_conn,
    get_settings,
    save_settings,
    sync_from_dhb_facebook,
    add_log,
    get_all_profiles,
    get_channels_by_profile,
    get_profile_binding,
    sync_antidetect_profiles,
    run_housekeeping,
    export_all_tables_csv,
    collect_secret_values,
    validate_public_settings,
    validate_secret_settings,
    public_channel,
    public_settings,
    redact_secrets,
    content_identity_migration_report,
    set_channel_group_memberships,
    register_managed_artifact,
    queue_requires_review,
)
from antidetect_client import AntidetectClient, AntidetectError
from scanner_downloader import scan_social_urls, probe_video_info, probe_video_infos, VIDEO_EXTENSIONS, VideoProbeError, ScanSourceError
from ai_engine import ai_optimize_youtube_metadata, apply_batch_text_edit, SourceLinksTooLongError
from youtube_uploader import (
    open_profile_for_login,
    probe_profile_login,
    get_oauth_token_local_status,
    check_oauth_token_status,
    normalize_published_video_url,
    recheck_api_video_receipt,
)
from scheduled_uploads import ScheduleError, read_schedule_status, sync_schedule_folder
from pipeline_manager import pipeline_manager, upload_slot_recovery_status, acknowledge_upload_slot_recovery
from group_distribution import target_group_names
from phase4_rules import (
    GROUP_SCHEMA_VERSION, normalize_group_names, serialize_group_names,
    validate_metadata_fields, validate_queue_fields, validate_source_url,
)
from queue_import import preview_csv_import
from app_instance_lock import AppInstanceAlreadyRunning, acquire_app_instance_lock
from render_engine import normalize_render_speed
from runtime_identity import capture_runtime, runtime_status
from upload_evidence import EvidenceError, read_evidence
from config_validation import parse_config_input, validate_config, validate_option_state
from metadata_intent import MetadataIntentConflict, metadata_revision, prepare_channel_update, prepare_option_update
from profile_crud import ProfileCreate, ProfileUpdate, binding_references
from render_preset_validation import PresetCreate, PresetUpdate, validate_render_config, public_preset, preset_revision
from monitor_validation import validate_monitor_create
from storage_manager import (
    DEFAULT_RETENTION_DAYS,
    cleanup_managed_artifacts,
    ensure_disk_space,
    storage_overview,
)

BACKGROUND_TASKS_DISABLED = os.environ.get("DHB_DISABLE_BACKGROUND_TASKS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

if BACKGROUND_TASKS_DISABLED:
    # Test harness đã trỏ DB sang thư mục tạm: không giữ lock thật và không chạy worker nền.
    _APP_INSTANCE_LOCK = None
else:
    try:
        # Own the local DB before migration/recovery. A second server process must
        # never clear claims or touch jobs managed by the first one.
        _APP_INSTANCE_LOCK = acquire_app_instance_lock(APP_DATA_DIR / "app_instance.lock")
    except AppInstanceAlreadyRunning as exc:
        raise RuntimeError(str(exc)) from exc

init_db()
pipeline_manager.clear_stale_claims()

# P0-1: tool KHÔNG tự chạy hàng đợi khi khởi động. Luôn tắt cờ cũ nếu còn bật
# để mọi phiên bản UI/DB cũ không thể kích hoạt đăng tự động ngoài ý muốn.
try:
    if get_settings().get("auto_start_on_launch"):
        save_settings({"auto_start_on_launch": False})
        add_log(
            "Pipeline",
            "Đã tắt 'Tự chạy hàng đợi khi mở tool' — chỉ chạy khi người dùng bấm nút.",
            "INFO",
        )
except Exception:
    pass


def _is_duplicate_post_id(conn, project_name: str, post_id: str) -> bool:
    """Video đã có trong hàng đợi của cùng dự án (post_id ổn định theo nguồn/đường dẫn file)."""
    if not post_id:
        return False
    return conn.execute(
        "SELECT 1 FROM video_queue WHERE project_name = ? AND post_id = ? LIMIT 1",
        (project_name, post_id),
    ).fetchone() is not None


def _is_duplicate_content_key(conn, project_name: str, content_key: str) -> bool:
    if not content_key:
        return False
    return conn.execute(
        "SELECT 1 FROM video_queue WHERE project_name = ? AND content_key = ? LIMIT 1",
        (project_name, content_key),
    ).fetchone() is not None


def _validated_group_names(conn, raw: Any, legacy: Any = "", default: Optional[str] = None) -> List[str]:
    try:
        is_missing = raw is None or (isinstance(raw, str) and not raw.strip())
        names = normalize_group_names(raw, legacy_name=legacy, default=default if is_missing and not legacy else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not names:
        raise HTTPException(400, "Phải chọn ít nhất một nhóm kênh; nhóm rỗng không thể dùng làm đích.")
    placeholders = ",".join("?" for _ in names)
    found = {
        str(row[0]) for row in conn.execute(
            f"SELECT name FROM channel_groups WHERE name IN ({placeholders})", names
        ).fetchall()
    }
    missing = [name for name in names if name not in found]
    if missing:
        raise HTTPException(400, "Không tìm thấy nhóm kênh: " + ", ".join(missing))
    return names


def _queue_target_updates(conn, updates: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize mutually exclusive channel/profile/group destination edits."""
    target = dict(updates)
    destination_changed = any(k in updates for k in (
        "target_channel_id", "target_profile_name", "target_group_name", "target_group_names",
        "target_group_names_json", "target_type",
    ))
    if not destination_changed:
        return target

    try:
        aliases = [normalize_group_names(updates[key]) for key in ('target_group_names', 'target_group_names_json') if key in updates]
        if len(aliases) == 2 and aliases[0] != aliases[1]:
            raise ValueError('Các trường nhóm kênh có giá trị mâu thuẫn.')
        if aliases and 'target_group_name' in updates:
            legacy = normalize_group_names(updates['target_group_name'])
            if legacy and legacy != aliases[0][:1]:
                raise ValueError('Tên nhóm cũ không khớp danh sách nhóm.')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    channel_id = target.get("target_channel_id", current.get("target_channel_id"))
    profile_name = str(target.get("target_profile_name", current.get("target_profile_name")) or "").strip()
    raw_groups = target.get("target_group_names", target.get("target_group_names_json"))
    if raw_groups is None and "target_group_name" in target:
        raw_groups = target.get("target_group_name")
    explicit_channel = "target_channel_id" in updates and bool(updates.get("target_channel_id"))
    explicit_profile = "target_profile_name" in updates and bool(str(updates.get("target_profile_name") or "").strip())
    try:
        has_groups = raw_groups is not None and bool(normalize_group_names(raw_groups))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    requested_type = str(updates.get('target_type') or '').upper()
    if has_groups and (explicit_channel or explicit_profile or requested_type in ('CHANNEL', 'PROFILE')):
        raise HTTPException(400, 'Chỉ chọn kênh/profile hoặc nhóm; không chọn đồng thời.')
    if requested_type == 'GROUP' and (explicit_channel or explicit_profile):
        raise HTTPException(400, 'Đích GROUP không được kèm kênh/profile cụ thể.')
    if explicit_profile and not explicit_channel:
        channel_id = None
    if not explicit_channel and not explicit_profile and (has_groups or (raw_groups is not None and str(target.get("target_type") or "").upper() == "GROUP")):
        groups = _validated_group_names(conn, raw_groups, default="Mặc định")
        target["target_group_names_json"] = serialize_group_names(groups)
        target["target_group_name"] = groups[0]
        target["group_schema_version"] = GROUP_SCHEMA_VERSION
        target["target_type"] = "GROUP"
        target["target_channel_id"] = None
        target["target_profile_name"] = ""
    elif channel_id:
        channel = conn.execute("SELECT id, profile_name FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
        if not channel:
            raise HTTPException(400, f"Không tìm thấy kênh đích #{channel_id}.")
        target["target_type"] = "CHANNEL"
        target["target_group_names_json"] = "[]"
        target["target_group_name"] = ""
        target["group_schema_version"] = GROUP_SCHEMA_VERSION
        explicit_profile = str(updates.get("target_profile_name") or "").strip()
        target["target_profile_name"] = str(channel["profile_name"] or explicit_profile)
    elif profile_name:
        rows = conn.execute(
            "SELECT id FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC", (profile_name,)
        ).fetchall()
        if len(rows) != 1:
            raise HTTPException(
                400,
                f"Profile '{profile_name}' phải ánh xạ đúng một kênh YouTube; hiện có {len(rows)}.",
            )
        target["target_type"] = "PROFILE"
        target["target_channel_id"] = None
        target["target_group_names_json"] = "[]"
        target["target_group_name"] = ""
        target["group_schema_version"] = GROUP_SCHEMA_VERSION
    else:
        raise HTTPException(400, "Chọn một kênh, một Profile có đúng một kênh hoặc ít nhất một nhóm.")

    # Editing a destination is explicit confirmation; stale assignments must be cleared.
    target["assigned_channel_id"] = None
    target["assigned_group_names_json"] = ""
    target.pop('target_group_names', None)
    return target


def _ensure_destination_not_active(row: Dict[str, Any], destination_changed: bool):
    status = str(row.get("status") or "").strip()
    active = status.startswith(("Uploading", "Downloading", "Rendering", "Đang đăng"))
    if destination_changed and (active or str(row.get("claim_token") or "")):
        raise HTTPException(409, f"Video #{row.get('id')} đang được worker giữ; không thể đổi kênh đích lúc này.")


_QUEUE_BUSY_STATUS_PREFIXES = (
    "Uploading", "Đang đăng",
    "Rendering", "Đang Render",
    "Downloading", "Đang tải",
    "Chờ giãn cách",
)


def _normalize_queue_ids(ids) -> List[int]:
    normalized = []
    for value in ids or []:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            normalized.append(item_id)
    return list(dict.fromkeys(normalized))


def _blocked_queue_rows(conn, ids) -> List[str]:
    normalized = _normalize_queue_ids(ids)
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"SELECT id, status, claim_token, "
        f"(SELECT state FROM content_reservations cr WHERE cr.queue_id = video_queue.id "
        f"ORDER BY CASE state WHEN 'NeedsReview' THEN 0 ELSE 1 END LIMIT 1) AS reservation_state "
        f"FROM video_queue WHERE id IN ({placeholders})",
        normalized,
    ).fetchall()
    blocked = []
    for row in rows:
        status = str(row["status"] or "").strip()
        reservation_state = str(row["reservation_state"] or "").strip()
        if (
            str(row["claim_token"] or "").strip()
            or status.startswith(_QUEUE_BUSY_STATUS_PREFIXES)
            or status == "NeedsReview"
            or reservation_state
        ):
            reason = "cần đối soát" if status == "NeedsReview" or reservation_state == "NeedsReview" else "đang bị giữ"
            blocked.append(f"#{row['id']} ({status or reason}; {reason})")
    return blocked


def _ensure_queue_items_idle(conn, ids, action: str):
    blocked = _blocked_queue_rows(conn, ids)
    if blocked:
        raise HTTPException(
            409,
            f"Không thể {action} vì còn video đang xử lý hoặc cần đối soát: {', '.join(blocked[:5])}"
            + ("..." if len(blocked) > 5 else "")
            + ". Hãy bấm Dừng và chờ luồng hiện tại kết thúc rồi thử lại.",
        )


def _validate_project_name(value: Any) -> str:
    """Validate a project label before it becomes a cross-table reference."""
    if not isinstance(value, str):
        raise HTTPException(400, "Tên dự án phải là chuỗi văn bản.")
    name = value.strip()
    if not name:
        raise HTTPException(400, "Tên dự án không được để trống.")
    if "\x00" in name:
        raise HTTPException(400, "Tên dự án không được chứa ký tự NUL.")
    if len(name) > 200:
        raise HTTPException(400, "Tên dự án không được vượt quá 200 ký tự.")
    if name == 'Default' or name.startswith('Monitor - '):
        raise HTTPException(400, "Tên dự án này dành riêng cho hệ thống. Hãy chọn tên khác.")
    return name


def _project_monitor_name(conn, project_name: str) -> bool:
    """Return whether a project is owned by an existing Monitor."""
    prefix = "Monitor - "
    if not project_name.startswith(prefix):
        return False
    monitor_name = project_name[len(prefix):].strip()
    if not monitor_name:
        return False
    return conn.execute("SELECT 1 FROM monitors WHERE name = ? LIMIT 1", (monitor_name,)).fetchone() is not None


def _capture_import_project(conn, name):
    """Capture identity before slow probing; imports may create a new project."""
    if not isinstance(name, str):
        raise HTTPException(400, 'Tên dự án phải là chuỗi văn bản.')
    name = name.strip()
    row = conn.execute('SELECT id, name FROM projects WHERE name=?', (name,)).fetchone()
    if row is None and name == 'Default':
        row = conn.execute('SELECT id, name FROM projects ORDER BY id LIMIT 1').fetchone()
    if row is None:
        name = _validate_project_name(name)
        conn.execute('INSERT INTO projects(name) VALUES(?)', (name,))
        row = conn.execute('SELECT id, name FROM projects WHERE name=?', (name,)).fetchone()
    return int(row['id']), row['name']


def _current_import_project(conn, project_id):
    row = conn.execute('SELECT name FROM projects WHERE id=?', (project_id,)).fetchone()
    if row is None:
        raise HTTPException(409, 'Dự án đã bị xóa trong lúc nhập video. Hãy chọn lại dự án và thử lại.')
    return row['name']


# Kiểm tra trùng + ghi queue phải liền mạch, trong khi ffprobe được chạy song song bên ngoài lock
_import_insert_lock = threading.Lock()


def _housekeeping_worker():
    """Bảo trì định kỳ mỗi 6 giờ: backup DB, cắt log, dọn lịch sử — dành cho phiên Monitor chạy 24/7."""
    import time
    while True:
        time.sleep(6 * 3600)
        try:
            run_housekeeping()
        except Exception as e:
            add_log("Housekeeping", f"Lỗi vòng bảo trì định kỳ: {e}", "WARNING")


if not BACKGROUND_TASKS_DISABLED:
    threading.Thread(target=_housekeeping_worker, daemon=True, name="DHB-Housekeeping").start()
    pipeline_manager.start_monitor_supervisor()

APP_BUILD_VERSION = "2026.11.18-oauth"


def get_schedule_state_dir() -> Path:
    custom = os.environ.get("DHB_SCHEDULE_STATE_DIR", "").strip()
    return Path(custom) if custom else (APP_DATA_DIR / "scheduled_uploads" / "default")
RUNTIME_IDENTITY = capture_runtime(APP_ROOT)
app = FastAPI(title="DHB Reup YouTube Studio", version=APP_BUILD_VERSION)

API_TOKEN = generate_api_token()
TOKEN_EXEMPT_PATHS = {"/api/auth/token"}
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_api_token(request: Request) -> str:
    token = request.headers.get("x-dhb-token") or ""
    if not token and request.url.path.endswith("/stream"):
        token = request.query_params.get("token") or ""
    return token


_JSON_SECRET_KEYS = {
    "token", "accesstoken", "refreshtoken", "idtoken", "authtoken", "sessiontoken",
    "oauthtoken", "oauthtokenjson", "apikey", "clientsecret", "secret",
    "password", "passwd", "cookie", "authorization",
}
_JSON_OAUTH_PRESENCE_KEYS = {"hasoauthtoken", "hasaccesstoken", "hasrefreshtoken"}


@lru_cache(maxsize=4096)
def _normalize_json_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _redact_json_value(value: Any, secrets: List[str]) -> Any:
    """Redact JSON values structurally so text patterns cannot corrupt JSON syntax."""
    if isinstance(value, dict):
        safe = {}
        for key, child in value.items():
            normalized_key = _normalize_json_key(str(key))
            if isinstance(child, bool) and normalized_key in _JSON_OAUTH_PRESENCE_KEYS:
                safe[key] = child
                continue
            is_secret = (
                normalized_key in _JSON_SECRET_KEYS
                or normalized_key.endswith(("apikey", "clientsecret", "password", "passwd", "token"))
            )
            safe[key] = "***" if is_secret and child is not None else _redact_json_value(child, secrets)
        return safe
    if isinstance(value, list):
        return [_redact_json_value(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    return value


_FRAME_DENY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    for key, value in _FRAME_DENY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _parse_if_none_match(header: str) -> set:
    return {item.strip() for item in header.split(",") if item.strip()}


@app.middleware("http")
async def guard_local_api(request: Request, call_next):
    if not is_allowed_host(request.headers.get("host", "")):
        return _apply_security_headers(
            JSONResponse({"detail": "Host không hợp lệ — tool chỉ phục vụ localhost."}, status_code=403)
        )

    path = request.url.path
    if path.startswith("/api/") and path not in TOKEN_EXEMPT_PATHS:
        if request.method in STATE_CHANGING_METHODS and not is_same_local_origin(
            request.headers.get("origin", ""), request.headers.get("host", "")
        ):
            return _apply_security_headers(
                JSONResponse({"detail": "Origin không hợp lệ — chặn yêu cầu cross-site."}, status_code=403)
            )
        token = _extract_api_token(request)
        if not token_matches(token, API_TOKEN):
            return _apply_security_headers(
                JSONResponse({"detail": "Thiếu hoặc sai API token cục bộ."}, status_code=401)
            )

    response = _apply_security_headers(await call_next(request))
    content_type = response.headers.get("content-type", "").lower()
    if (
        path != "/api/auth/token"
        and response.status_code not in {204, 304}
        and ("application/json" in content_type or "+json" in content_type)
    ):
        try:
            raw_body = b"".join([chunk async for chunk in response.body_iterator])
            payload = json.loads(raw_body.decode("utf-8"))
            safe_payload = _redact_json_value(payload, collect_secret_values())
            safe_body = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            etag = ""
            if request.method == "GET" and response.status_code == 200:
                etag = 'W/"' + hashlib.sha1(safe_body).hexdigest() + '"'
            if etag and etag in _parse_if_none_match(request.headers.get("if-none-match", "")):
                safe_response = Response(status_code=304, background=response.background)
            else:
                safe_response = Response(
                    content=safe_body,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/json"),
                    background=response.background,
                )
            if etag:
                safe_response.headers["ETag"] = etag
        except Exception:
            # The endpoint may already have completed a mutation. Do not return
            # the consumed body (which may contain credentials) or imply success.
            safe_response = _apply_security_headers(
                JSONResponse(
                    {"detail": "Không thể tạo phản hồi JSON an toàn."},
                    status_code=500,
                    background=response.background,
                )
            )

        # Preserve repeated headers such as Set-Cookie, while dropping metadata
        # that describes the old representation or encoding.
        invalidated = {
            b"content-length", b"content-encoding", b"transfer-encoding",
            b"content-type", b"etag", b"content-md5", b"digest",
        }
        generated = [
            item for item in safe_response.raw_headers
            if item[0].lower() in {b"content-type", b"content-length", b"etag"}
        ]
        preserved = [item for item in response.raw_headers if item[0].lower() not in invalidated]
        safe_response.raw_headers = preserved + generated
        return safe_response
    return response


@app.exception_handler(HTTPException)
async def safe_http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        code = str(exc.detail.get("code") or "").strip()
        message = redact_secrets(str(exc.detail.get("message") or "Yêu cầu không hợp lệ."))
        body: Dict[str, Any] = {"detail": message}
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code):
            body["code"] = code
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
    detail = exc.detail if isinstance(exc.detail, (str, int, float, bool)) or exc.detail is None else str(exc.detail)
    return JSONResponse(
        {"detail": redact_secrets(detail)},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def safe_validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for entry in exc.errors():
        errors.append({
            "loc": entry.get("loc", []),
            "msg": redact_secrets(entry.get("msg", "Dữ liệu không hợp lệ.")),
            "type": entry.get("type", "value_error"),
        })
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(Exception)
async def safe_internal_exception_handler(request: Request, exc: Exception):
    if str(exc) == "UPLOAD_BINDING_BUSY":
        return JSONResponse({"detail": "Kênh hoặc profile đang được giữ bởi lượt xử lý hoặc reservation."}, status_code=409)
    try:
        add_log("API", f"Unhandled {type(exc).__name__}: {exc}", "ERROR", error_code="ERR_INTERNAL")
    except Exception:
        pass
    return JSONResponse({"detail": "Lỗi nội bộ máy chủ. Hãy kiểm tra Nhật Ký Hệ Thống."}, status_code=500)


@app.get("/api/auth/token")
def api_auth_token():
    return JSONResponse({"token": API_TOKEN}, headers={"Cache-Control": "no-store"})


STATIC_DIR = APP_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/health/dhb", include_in_schema=False)
def dhb_health():
    return JSONResponse(
        {key: RUNTIME_IDENTITY[key] for key in ("application", "source_root")},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/overview")
def get_overview():
    conn = get_conn()
    channels_count = conn.execute("SELECT COUNT(*) as c FROM youtube_channels").fetchone()["c"]
    groups_count = conn.execute("SELECT COUNT(*) as c FROM channel_groups").fetchone()["c"]
    projects = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall()]
    total_videos = conn.execute("SELECT COUNT(*) as c FROM video_queue").fetchone()["c"]
    completed_videos = conn.execute("SELECT COUNT(*) as c FROM video_queue WHERE status = 'Completed'").fetchone()["c"]
    monitors_running = conn.execute("SELECT COUNT(*) as c FROM monitors WHERE is_running = 1").fetchone()["c"]
    conn.close()
    return {
        "channels_count": channels_count,
        "groups_count": groups_count,
        "projects": projects,
        "total_videos": total_videos,
        "completed_videos": completed_videos,
        "monitors_running": monitors_running,
        "pipeline_running": pipeline_manager.is_running,
        "batch_state": pipeline_manager.batch_state,
        "dhb_connected": DHB_ROOT.exists(),
        "ffmpeg_path": FFMPEG_EXE,
        "ytdl_path": YTDL_EXE,
    }


def _probe_local_tool(label: str, executable: str, version_args: Optional[List[str]] = None) -> Dict[str, Any]:
    command = str(executable or "").strip()
    result: Dict[str, Any] = {
        "name": label,
        "command": Path(command).name if command else "(chưa cấu hình)",
        "available": False,
        "version": "",
        "error": "",
    }
    if not command:
        result["error"] = "Chưa cấu hình executable."
        return result
    try:
        completed = subprocess.run(
            [command, *(version_args or ["-version"])], capture_output=True, text=True, timeout=6, check=False,
            encoding="utf-8", errors="replace",
        )
        output = (completed.stdout or completed.stderr or "").splitlines()
        result["available"] = completed.returncode == 0
        result["version"] = redact_secrets(output[0][:240]) if output else ""
        if completed.returncode != 0:
            result["error"] = "Executable trả mã lỗi khi kiểm tra phiên bản."
    except (OSError, subprocess.TimeoutExpired):
        result["error"] = "Không tìm thấy executable hoặc hết thời gian kiểm tra."
    return result


@app.get("/api/diagnostics")
def api_diagnostics():
    conn = get_conn()
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema_rows = [
            {"key": str(row["schema_key"]), "version": int(row["version"])}
            for row in conn.execute("SELECT schema_key, version FROM app_schema_versions ORDER BY schema_key")
        ]
        needs_review = int(conn.execute("SELECT COUNT(*) FROM video_queue WHERE status = 'NeedsReview'").fetchone()[0])
        artifact_count = int(conn.execute("SELECT COUNT(*) FROM managed_artifacts").fetchone()[0])
        heartbeat_row = conn.execute(
            "SELECT COUNT(*) AS running, MAX(last_heartbeat_at) AS latest FROM monitors WHERE is_running = 1"
        ).fetchone()
        heartbeat = {"running_monitors": int(heartbeat_row["running"] or 0), "latest": heartbeat_row["latest"] or ""}
        db_bytes = int(APP_DB_PATH.stat().st_size) if APP_DB_PATH.exists() else 0
    finally:
        conn.close()

    try:
        with pipeline_manager._monitor_lock:
            monitor_workers = sum(1 for thread in pipeline_manager._monitor_workers.values() if thread.is_alive())
            monitor_heartbeats = sum(1 for thread in pipeline_manager._monitor_heartbeats.values() if thread.is_alive())
            supervisor_alive = bool(
                pipeline_manager._monitor_supervisor and pipeline_manager._monitor_supervisor.is_alive()
            )
        workers = {
            "pipeline_running": bool(pipeline_manager.is_running),
            "active_batch_threads": int(pipeline_manager._active_threads),
            "monitor_workers": monitor_workers,
            "monitor_heartbeats": monitor_heartbeats,
            "monitor_supervisor_alive": supervisor_alive,
        }
    except Exception:
        workers = {"pipeline_running": False, "active_batch_threads": 0, "monitor_workers": 0,
                   "monitor_heartbeats": 0, "monitor_supervisor_alive": False}

    try:
        storage = storage_overview(DEFAULT_RETENTION_DAYS)
        storage_summary = {
            "free_bytes": int(storage.get("free_bytes") or 0),
            "temp_bytes": int((storage.get("temp") or {}).get("bytes") or 0),
            "output_bytes": int((storage.get("output") or {}).get("bytes") or 0),
            "artifact_count": artifact_count,
        }
    except Exception:
        storage_summary = {"free_bytes": 0, "temp_bytes": 0, "output_bytes": 0,
                           "artifact_count": artifact_count, "available": False}

    return {
        "version": APP_BUILD_VERSION,
        "runtime": runtime_status(APP_ROOT, RUNTIME_IDENTITY),
        "batch_state": pipeline_manager.batch_state,
        "upload_slot": upload_slot_recovery_status(),
        "database": {"available": APP_DB_PATH.exists(), "bytes": db_bytes, "user_version": user_version,
                     "schemas": schema_rows},
        "workers": workers,
        "needs_review_count": needs_review,
        "storage": storage_summary,
        "heartbeat": heartbeat,
        "scheduled_uploads": read_schedule_status(get_schedule_state_dir()),
        "tools": [
            _probe_local_tool("FFmpeg", FFMPEG_EXE, ["-version"]),
            _probe_local_tool("FFprobe", FFPROBE_EXE, ["-version"]),
            _probe_local_tool("yt-dlp", YTDL_EXE, ["--version"]),
        ],
    }


@app.get("/api/scheduled-uploads/status")
def api_scheduled_uploads_status():
    return read_schedule_status(get_schedule_state_dir())


class _InProcessScheduleAPI:
    """In-process adapter for safe schedule folder synchronization from the Web UI."""

    def __init__(self, _base_url, _runtime_dir, _release):
        self.session = None

    def connect(self):
        return None

    def diagnostics(self, *, idle=False):
        diag = api_diagnostics()
        workers = diag.get("workers") or {}
        if idle and any(workers.get(key) for key in ("pipeline_running", "active_batch_threads", "monitor_workers")):
            raise ScheduleError("WORKER_BUSY")
        return diag

    def verify_channel(self, manifest):
        expected = manifest["channel"]
        channels = api_list_channels(profile=None)
        channel = next((row for row in channels if row.get("id") == expected["id"]), None)
        if (not channel or any(channel.get(key) != expected[key] for key in ("channel_id", "name"))
                or channel.get("upload_mode") != "youtube_api"):
            raise ScheduleError("CHANNEL_CHANGED")

    def project_rows(self, project):
        return api_get_queue(project=project)

    def delete_rows(self, ids):
        return api_delete_queue_items(data={"ids": list(ids)})

    def scan_folder(self, manifest):
        return api_scan_and_add(data={
            "project_name": manifest["project"],
            "urls": manifest["source_directory"],
            "max_items": 500,
            "recursive": False,
            "target_channel_id": manifest["channel"]["id"],
            "upload_to": "Shorts",
            "render_mode": "shorts_9_16",
            "resolution": "1080p",
            "visibility": "private",
            "schedule_time": "",
            "made_for_kids": 0,
            "age_restriction": 0,
            "altered_content": 0,
            "emulate_mobile": False,
        })

    def preview_batch(self, manifest, queue_ids):
        if not queue_ids:
            return
        result = api_preview_distribution(data={
            "ids": list(queue_ids),
            "do_download": False,
            "do_render": False,
            "do_upload": True,
        })
        items = result.get("items") or []
        if not result.get("ok") or len(items) != len(queue_ids):
            raise ScheduleError("PREVIEW_BLOCKED")
        expected = manifest["channel"]
        for row in items:
            if (row.get("blocked") or row.get("channel_id") != expected["id"]
                    or row.get("channel_name") != expected["name"] or row.get("upload_mode") != "youtube_api"
                    or row.get("visibility") != "private" or row.get("schedule_time")):
                raise ScheduleError("PREVIEW_CHANGED")


@app.post("/api/scheduled-uploads/sync")
def api_scheduled_uploads_sync():
    state_dir = get_schedule_state_dir()
    try:
        result = sync_schedule_folder(
            state_dir,
            APP_ROOT,
            api_factory=_InProcessScheduleAPI,
            check_release=False,
        )
        add_log(
            "Schedule",
            f"Đã đồng bộ thư mục lịch đăng (+{result['added_count']} mới, -{result['removed_pending_count']} mất file).",
            "SUCCESS",
        )
        return result
    except ScheduleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": f"Không thể đồng bộ thư mục lịch đăng ({exc.code}).", "code": exc.code},
        ) from exc


@app.post("/api/sync-dhb")
def api_sync_dhb():
    stats = sync_from_dhb_facebook(initial_only=False)
    return {"ok": True, "stats": stats}


# ==========================================================
# QUẢN LÝ DỰ ÁN & HÀNG ĐỢI VIDEO (MAIN WORKSPACE)
# ==========================================================
@app.get("/api/projects")
def api_list_projects():
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.*, (SELECT COUNT(*) FROM video_queue v WHERE v.project_name = p.name) as video_count
        FROM projects p ORDER BY p.id ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects")
def api_create_project(data: Dict[str, Any] = Body(...)):
    name = _validate_project_name(data.get('name'))
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise HTTPException(409, 'Tên dự án đã tồn tại. Hãy chọn tên khác.')
        conn.execute('INSERT INTO projects(name) VALUES(?)', (name,))
    return {"ok": True, "name": name}


@app.put('/api/projects/{name:path}')
def api_rename_project(name: str, data: Dict[str, Any] = Body(...)):
    new_name = _validate_project_name(data.get('name'))
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise HTTPException(404, 'Không tìm thấy dự án này.')
        if _project_monitor_name(conn, name):
            raise HTTPException(409, 'Dự án thuộc Monitor. Hãy quản lý liên kết trong Monitor trước.')
        if new_name == name:
            return {'ok': True, 'name': name}
        if (conn.execute('SELECT 1 FROM projects WHERE name=?', (new_name,)).fetchone()
                or conn.execute('SELECT 1 FROM video_queue WHERE project_name=?', (new_name,)).fetchone()):
            raise HTTPException(409, 'Tên mới đã có dự án hoặc hàng đợi. Không thể gộp dữ liệu khi đổi tên.')
        ids = [r[0] for r in conn.execute('SELECT id FROM video_queue WHERE project_name=?', (name,))]
        _ensure_queue_items_idle(conn, ids, 'đổi tên dự án')
        conn.execute('UPDATE projects SET name=?,updated_at=CURRENT_TIMESTAMP WHERE name=?', (new_name, name))
        conn.execute('UPDATE video_queue SET project_name=? WHERE project_name=?', (new_name, name))
    return {'ok': True, 'name': new_name}


@app.delete("/api/projects/{name:path}")
def api_delete_project(name: str):
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise HTTPException(404, 'Không tìm thấy dự án này.')
        if _project_monitor_name(conn, name):
            raise HTTPException(409, 'Dự án thuộc Monitor. Hãy quản lý liên kết trong Monitor trước.')
        total_projects = conn.execute("SELECT COUNT(*) as c FROM projects").fetchone()["c"]
        if total_projects <= 1:
            raise HTTPException(409, "Cần giữ lại ít nhất 1 dự án để làm việc.")
        if conn.execute('SELECT 1 FROM video_queue WHERE project_name=? LIMIT 1', (name,)).fetchone():
            raise HTTPException(409, 'Dự án còn video. Hãy quản lý hàng đợi trước; chỉ xóa được dự án rỗng.')
        conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    return {"ok": True}


@app.get("/api/queue")
def api_get_queue_route(project: str = ""):
    # Rows are plain SQLite values, so skip FastAPI's generic encoder on this large payload.
    return JSONResponse(api_get_queue(project))


def api_get_queue(project: str = ""):
    conn = get_conn()
    if not project or project == "Default":
        p_row = conn.execute("SELECT name FROM projects ORDER BY id ASC LIMIT 1").fetchone()
        project = p_row["name"] if p_row else "Dự án YouTube #1"
    rows = conn.execute(
        "SELECT *, COALESCE(claim_token, '') <> '' AS is_claimed, "
        "(status = 'NeedsReview' OR EXISTS (SELECT 1 FROM content_reservations r "
        "WHERE r.queue_id = video_queue.id AND r.state = 'NeedsReview')) AS requires_review "
        "FROM video_queue WHERE project_name = ? ORDER BY id DESC",
        (project,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/queue/content-identity-report")
def api_content_identity_report():
    """Read-only summary for legacy queue/history rows without fingerprints."""
    return content_identity_migration_report()


@app.get("/api/queue/{item_id}/upload-evidence")
def api_upload_evidence(item_id: int):
    """Return only validated evidence, without changing reconciliation state."""
    with closing(get_conn()) as conn:
        if not conn.execute('SELECT 1 FROM video_queue WHERE id=?', (item_id,)).fetchone():
            raise HTTPException(404, 'Không tìm thấy video này trong hàng đợi.')
        rows = conn.execute(
            'SELECT id,attempt_no,channel_id,outcome,evidence_json FROM upload_attempts '
            'WHERE queue_id=? ORDER BY attempt_no DESC,id DESC', (item_id,)
        ).fetchall()
    attempts = []
    public_outcomes = {'InProgress', 'NeedsReview', 'Completed', 'Error', 'NotSent',
                       'Cancelled', 'Warning', 'Authorized', 'ConfirmedPublished'}
    # Old/manual attempts can have no evidence. Never return arbitrary stored
    # response bodies, error text, or URLs from an invalid evidence document.
    for row in rows:
        invalid = False
        try:
            if row['channel_id'] is None and row['evidence_json'] == '{}':
                evidence = None  # Historical attempts can lack a bound channel.
            else:
                evidence = read_evidence(row['evidence_json'], item_id, row['id'], row['channel_id'])
        except EvidenceError:
            evidence, invalid = None, True
        attempts.append({'id': row['id'], 'attempt_no': row['attempt_no'],
                         'outcome': row['outcome'] if row['outcome'] in public_outcomes else 'Unknown', 'evidence': evidence,
                         'evidence_error': invalid})
    return {'queue_id': item_id, 'attempts': attempts}


@app.post("/api/queue/scan")
def api_scan_and_add(data: Dict[str, Any] = Body(...)):
    project_name = data.get("project_name") or "Default"
    raw_urls = data.get("urls") or ""
    urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not urls:
        raise HTTPException(400, "Vui lòng nhập ít nhất 1 đường dẫn URL hoặc đường dẫn thư mục.")
    normalized_sources = []
    for value in urls:
        if Path(value).exists():
            normalized_sources.append(value)
            continue
        try:
            normalized_sources.append(validate_source_url(value))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    urls = normalized_sources

    try:
        raw_max_items = data.get("max_items", 20)
        if isinstance(raw_max_items, bool) or not re.fullmatch(r"[0-9]+", str(raw_max_items).strip()):
            raise ValueError()
        max_items = int(raw_max_items)
        if not 1 <= max_items <= 1000:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, 'Số video mỗi nguồn phải là số nguyên từ 1 đến 1000.')
    min_views = int(data.get("min_views", 0) or 0)
    min_likes = int(data.get("min_likes", 0) or 0)
    min_comments = int(data.get("min_comments", 0) or 0)
    only_shorts = bool(data.get("only_shorts", False))
    recursive = bool(data.get("recursive", False))
    target_channel_id = data.get("target_channel_id") or None
    raw_group_names = data.get("target_group_names", data.get("target_group_names_json"))
    if raw_group_names is None:
        raw_group_names = data.get("target_group_name") or "Mặc định"
    validation_conn = get_conn()
    try:
        group_names = _validated_group_names(
            validation_conn, raw_group_names, legacy=data.get("target_group_name") or "", default="Mặc định"
        ) if not target_channel_id else []
        if target_channel_id and not validation_conn.execute(
            "SELECT 1 FROM youtube_channels WHERE id = ?", (target_channel_id,)
        ).fetchone():
            raise HTTPException(400, f"Không tìm thấy kênh đích #{target_channel_id}.")
    finally:
        validation_conn.close()
    option_group_id = data.get("option_group_id") or None
    render_mode = data.get("render_mode") or "shorts_9_16"
    upload_to = data.get("upload_to") or "Shorts"
    resolution = data.get("resolution") or "1080p"
    emulate_mobile = 1 if data.get("emulate_mobile", False) else 0
    base_schedule_time = (data.get("schedule_time") or "").strip()
    schedule_interval_minutes = int(data.get("schedule_interval_minutes") or 0)
    is_premiere = data.get("is_premiere", 0)
    made_for_kids = data.get("made_for_kids", 0)
    age_restriction = data.get("age_restriction", 0)
    altered_content = data.get("altered_content", 0)

    from datetime import datetime, timedelta
    start_dt = None
    try:
        normalized = validate_queue_fields({
            "visibility": data.get("visibility") or "public",
            "schedule_time": base_schedule_time,
            "is_premiere": is_premiere,
            "made_for_kids": made_for_kids,
            "age_restriction": age_restriction,
            "altered_content": altered_content,
            "upload_to": upload_to,
            "render_mode": render_mode,
            "resolution": resolution,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    visibility = normalized["visibility"]
    is_premiere = normalized["is_premiere"]
    made_for_kids = normalized["made_for_kids"]
    age_restriction = normalized["age_restriction"]
    altered_content = normalized["altered_content"]
    base_schedule_time = normalized["schedule_time"]
    if visibility == "schedule":
        start_dt = datetime.fromisoformat(base_schedule_time)
        schedule_interval_minutes = max(30, schedule_interval_minutes)

    with closing(get_conn()) as project_conn, project_conn:
        project_conn.execute('BEGIN IMMEDIATE')
        project_id, project_name = _capture_import_project(project_conn, project_name)
    try:
        scanned = scan_social_urls(
            urls=urls,
            max_items_per_url=max_items,
            min_views=min_views,
            min_likes=min_likes,
            min_comments=min_comments,
            only_shorts=only_shorts,
            recursive_local=recursive,
        )
    except VideoProbeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except ScanSourceError as exc:
        raise HTTPException(502, str(exc)) from exc

    with closing(get_conn()) as conn, conn, _import_insert_lock:
        conn.execute('BEGIN IMMEDIATE')
        project_name = _current_import_project(conn, project_id)
        added_count = 0
        skipped_count = 0
        seen_post_ids = set()
        for item in scanned:
            post_id = str(item.get("post_id") or "").strip()
            if post_id:
                if post_id in seen_post_ids or _is_duplicate_post_id(conn, project_name, post_id):
                    skipped_count += 1
                    continue
                seen_post_ids.add(post_id)

            item_sched = ""
            item_vis = visibility
            if visibility == "schedule":
                item_dt = start_dt + timedelta(minutes=added_count * schedule_interval_minutes)
                item_sched = item_dt.strftime("%Y-%m-%d %H:%M:%S")

            conn.execute(
                """
                INSERT INTO video_queue (
                    project_name, source_type, platform, post_id, source_url, input_video_path,
                    title, description, tags, duration, view_count, like_count,
                    upload_to, visibility, schedule_time, is_premiere, made_for_kids,
                    age_restriction, altered_content, target_type, target_channel_id,
                    target_group_name, target_group_names_json, group_schema_version,
                    option_group_id, render_mode, resolution,
                    emulate_mobile, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending')
                """,
                (
                    project_name,
                    item["source_type"],
                    item["platform"],
                    post_id,
                    item["source_url"],
                    item["input_video_path"],
                    item["title"],
                    item["description"],
                    item["tags"],
                    item["duration"],
                    item["view_count"],
                    item["like_count"],
                    upload_to or item["upload_to"],
                    item_vis,
                    item_sched,
                    is_premiere,
                    made_for_kids,
                    age_restriction,
                    altered_content,
                    "CHANNEL" if target_channel_id else "GROUP",
                    target_channel_id,
                    group_names[0] if group_names else "",
                    serialize_group_names(group_names),
                    1,
                    option_group_id,
                    render_mode,
                    resolution,
                    emulate_mobile,
                ),
            )
            added_count += 1
    log_msg = f"Đã quét và thêm {added_count} video vào dự án [{project_name}] (Chế độ: {visibility})."
    if skipped_count:
        log_msg += f" Bỏ qua {skipped_count} video đã có trong dự án."
    add_log("Scanner", log_msg, "SUCCESS")
    return {"ok": True, "added": added_count, "skipped": skipped_count}


@app.post("/api/upload-video-files")
def api_upload_video_files(
    project_name: str = Form("Default"),
    upload_to: str = Form("Shorts"),
    render_mode: str = Form("shorts_9_16"),
    resolution: str = Form("1080p"),
    visibility: str = Form("public"),
    schedule_time: str = Form(""),
    schedule_interval_minutes: int = Form(60),
    is_premiere: int = Form(0),
    made_for_kids: int = Form(0),
    age_restriction: int = Form(0),
    altered_content: int = Form(0),
    emulate_mobile: int = Form(0),
    target_group_names: str = Form(""),
    files: List[UploadFile] = File(...),
):
    """Tải lên trực tiếp các file video từ trình duyệt hoặc kéo thả vào thư mục temp/uploads và nạp vào hàng đợi."""
    try:
        final_settings = validate_queue_fields({
            "visibility": visibility,
            "schedule_time": schedule_time,
            "schedule_interval_minutes": schedule_interval_minutes,
            "is_premiere": is_premiere,
            "made_for_kids": made_for_kids,
            "age_restriction": age_restriction,
            "altered_content": altered_content,
            "upload_to": upload_to,
            "render_mode": render_mode,
            "resolution": resolution,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from datetime import timedelta
    start_dt = datetime.fromisoformat(final_settings["schedule_time"]) if final_settings["schedule_time"] else None
    schedule_interval_minutes = max(30, int(schedule_interval_minutes or 30))
    upload_to = final_settings["upload_to"]
    render_mode = final_settings["render_mode"]
    resolution = final_settings["resolution"]
    visibility = final_settings["visibility"]
    upload_dir = APP_TEMP_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    validation_conn = get_conn()
    try:
        group_names = _validated_group_names(validation_conn, target_group_names, default="Mặc định")
    finally:
        validation_conn.close()
    with closing(get_conn()) as project_conn, project_conn:
        project_conn.execute('BEGIN IMMEDIATE')
        project_id, project_name = _capture_import_project(project_conn, project_name)
    conn = get_conn()
    added_count = 0
    skipped_count = 0
    added_titles = []
    added_ids = []
    invalid_files = []
    seen_content_keys = set()
    conn.commit()

    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext and ext not in VIDEO_EXTENSIONS:
            continue

        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(f.filename).stem).strip(" .")[:150] or "video"
        safe_name = f"{safe_stem}_{os.urandom(4).hex()}{ext}"
        target_path = upload_dir / safe_name

        uploaded_size = int(getattr(f, "size", 0) or 0)
        required_space = uploaded_size + 256 * 1024 * 1024 if uploaded_size > 0 else 1024 * 1024 * 1024
        try:
            ensure_disk_space(upload_dir, required_space, f"nhận file {Path(f.filename).name}")
        except OSError as exc:
            conn.close()
            raise HTTPException(507, str(exc)) from exc

        try:
            content_key, bytes_written = copy_stream_and_hash(f.file, target_path)
        except Exception as exc:
            target_path.unlink(missing_ok=True)
            conn.close()
            raise HTTPException(400, f"Không đọc được file {Path(f.filename).name}: {exc}") from exc
        if not bytes_written:
            target_path.unlink(missing_ok=True)
            skipped_count += 1
            continue
        post_id = content_key.partition(":")[2][:16].upper()
        if content_key in seen_content_keys or _is_duplicate_content_key(conn, project_name, content_key):
            # Trùng nội dung với video đã có trong dự án — dọn file tạm và bỏ qua.
            skipped_count += 1
            try:
                target_path.unlink()
            except Exception:
                pass
            continue
        seen_content_keys.add(content_key)

        try:
            probe = probe_video_info(str(target_path))
        except Exception as exc:
            target_path.unlink(missing_ok=True)
            skipped_count += 1
            invalid_files.append({"name": Path(f.filename).name, "reason": str(exc)})
            continue
        title = Path(f.filename).stem

        is_short = probe["seconds"] <= 65 or (probe["height"] > probe["width"] and probe["seconds"] <= 180)
        final_upload_to = upload_to or ("Shorts" if is_short else "Video")
        item_schedule = (start_dt + timedelta(minutes=added_count * schedule_interval_minutes)).strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""

        inserted = False
        try:
            with _import_insert_lock:
                conn.execute("BEGIN IMMEDIATE")
                project_name = _current_import_project(conn, project_id)
                if not _is_duplicate_content_key(conn, project_name, content_key):
                    insert_cursor = conn.execute(
                        """
                        INSERT INTO video_queue (
                            project_name, source_type, platform, post_id, content_key, source_url, input_video_path,
                            title, description, tags, duration, view_count, like_count,
                            upload_to, visibility, schedule_time, is_premiere, made_for_kids,
                            age_restriction, altered_content, target_type, target_channel_id,
                            target_group_name, target_group_names_json, group_schema_version,
                            option_group_id, render_mode, resolution,
                            emulate_mobile, download_progress, status
                        ) VALUES (?, 'File', 'LocalUpload', ?, ?, ?, ?, ?, ?, '#shorts #trending', ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, 'GROUP', NULL, ?, ?, 1, NULL, ?, ?, ?, 100, 'Pending')
                        """,
                        (
                            project_name,
                            post_id,
                            content_key,
                            str(target_path),
                            str(target_path),
                            title,
                            title,
                            probe["duration"],
                            final_upload_to,
                            visibility.lower(),
                            item_schedule,
                            final_settings["is_premiere"],
                            final_settings["made_for_kids"],
                            final_settings["age_restriction"],
                            final_settings["altered_content"],
                            group_names[0],
                            serialize_group_names(group_names),
                            render_mode,
                            resolution,
                            1 if emulate_mobile else 0,
                        ),
                    )
                    conn.commit()
                    inserted = True
                    queue_id = int(insert_cursor.lastrowid)
                else:
                    conn.rollback()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            target_path.unlink(missing_ok=True)
            conn.close()
            raise
        if not inserted:
            skipped_count += 1
            target_path.unlink(missing_ok=True)
            continue
        added_count += 1
        added_titles.append(title)
        added_ids.append(queue_id)
        try:
            register_managed_artifact(str(target_path), "upload", "queue", queue_id)
        except Exception as exc:
            add_log("Storage", f"Không ghi được manifest cho file upload của queue #{queue_id}; file được giữ lại ({type(exc).__name__}).", "WARNING", video_title=title)

    conn.commit()
    conn.close()
    log_msg = f"Đã tải lên và thêm {added_count} video vào dự án [{project_name}]."
    if skipped_count:
        log_msg += f" Bỏ qua {skipped_count} video trùng nội dung."
    add_log("Upload", log_msg, "SUCCESS")
    return {
        "ok": True, "added": added_count, "skipped": skipped_count,
        "titles": added_titles, "added_ids": added_ids, "invalid_files": invalid_files,
    }


@app.post("/api/browse-native-files")
async def api_browse_native_files():
    """Mở cửa sổ File Explorer gốc của Windows để chọn trực tiếp các file video."""
    def _browse():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilenames(
            title="Chọn các tệp video từ máy tính",
            filetypes=[
                ("Video Files (*.mp4;*.mov;*.mkv;*.webm;*.avi)", "*.mp4;*.mov;*.mkv;*.webm;*.avi;*.flv;*.m4v"),
                ("Tất cả tệp", "*.*"),
            ],
        )
        root.destroy()
        return list(selected) if selected else []

    files = await run_in_threadpool(_browse)
    return {"ok": True, "files": files}


@app.post("/api/browse-native-folder")
async def api_browse_native_folder():
    """Mở cửa sổ chọn thư mục gốc của Windows."""
    def _browse():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Chọn thư mục chứa video trên máy tính")
        root.destroy()
        return selected or ""

    folder = await run_in_threadpool(_browse)
    return {"ok": True, "folder": folder}


@app.get("/api/browse-disk")
def api_browse_disk(path: str = "", recursive_count: int = 0):
    """
    Duyệt ổ đĩa / thư mục ngay trong giao diện tool (không cần hộp thoại Windows).
    Trả về danh sách thư mục con + file video của path; recursive_count=1 sẽ đếm thêm
    tổng số video kể cả trong thư mục con (dùng cho nút 'Thêm cả thư mục con').
    """
    import string

    drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    if not path or not Path(path).exists():
        path = drives[0] if drives else str(APP_TEMP_DIR)
    p = Path(path)
    parent = str(p.parent) if p.parent != p else ""
    dirs, vids = [], []
    try:
        entries = sorted(p.iterdir(), key=lambda x: x.name.lower())
        for e in entries:
            if e.name.startswith(".") or e.name.startswith("$"):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": str(e)})
                elif e.is_file() and e.suffix.lower() in VIDEO_EXTENSIONS:
                    vids.append({
                        "name": e.name,
                        "path": str(e),
                        "size_mb": round(e.stat().st_size / 1048576, 1),
                    })
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass

    recursive_video_count = len(vids)
    if recursive_count:
        found = []
        for root, _sd, files in os.walk(p, onerror=lambda _e: None):
            for fn in files:
                if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                    found.append(fn)
        recursive_video_count = len(found)

    return {
        "ok": True,
        "current": str(p),
        "parent": parent,
        "drives": drives,
        "dirs": dirs,
        "videos": vids,
        "recursive_video_count": recursive_video_count,
    }


@app.post("/api/add-local-paths")
def api_add_local_paths(data: Dict[str, Any] = Body(...)):
    """Thêm trực tiếp danh sách file hoặc thư mục từ máy tính vào hàng đợi (recursive=True để lấy cả thư mục con)."""
    paths = data.get("paths") or []
    project_name = data.get("project_name") or "Default"
    upload_to = data.get("upload_to") or "Shorts"
    render_mode = data.get("render_mode") or "shorts_9_16"
    resolution = data.get("resolution") or "1080p"
    visibility = (data.get("visibility") or "public").lower()
    target_group_names = data.get("target_group_names") if "target_group_names" in data else None
    schedule_time = str(data.get("schedule_time") or "").strip()
    try:
        schedule_interval_minutes = int(data.get("schedule_interval_minutes") or 60)
    except (TypeError, ValueError):
        raise HTTPException(400, "Khoảng cách lịch phải là số phút hợp lệ.")
    try:
        final_settings = validate_queue_fields({
            "visibility": visibility,
            "schedule_time": schedule_time,
            "is_premiere": data.get("is_premiere", 0),
            "made_for_kids": data.get("made_for_kids", 0),
            "age_restriction": data.get("age_restriction", 0),
            "altered_content": data.get("altered_content", 0),
            "upload_to": upload_to,
            "render_mode": render_mode,
            "resolution": resolution,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from datetime import timedelta
    start_dt = datetime.fromisoformat(final_settings["schedule_time"]) if final_settings["schedule_time"] else None
    schedule_interval_minutes = max(30, schedule_interval_minutes)
    visibility = final_settings["visibility"]
    upload_to = final_settings["upload_to"]
    render_mode = final_settings["render_mode"]
    resolution = final_settings["resolution"]
    recursive = bool(data.get("recursive", False))

    validation_conn = get_conn()
    try:
        group_names = _validated_group_names(validation_conn, target_group_names, default="Mặc định")
    finally:
        validation_conn.close()

    if not paths:
        raise HTTPException(400, "Không có tệp nào được chọn.")

    pending_files: List[Path] = []
    skipped_count = 0
    seen_post_ids = set()

    with closing(get_conn()) as project_conn, project_conn:
        project_conn.execute('BEGIN IMMEDIATE')
        project_id, project_name = _capture_import_project(project_conn, project_name)
    conn = get_conn()
    try:
        for p_str in paths:
            p = Path(p_str)
            if not p.exists():
                continue

            candidate_files = []
            if p.is_dir():
                if recursive:
                    for root, _sd, files in os.walk(p, onerror=lambda _e: None):
                        for fn in files:
                            fp = Path(root) / fn
                            if fp.suffix.lower() in VIDEO_EXTENSIONS:
                                candidate_files.append(fp)
                else:
                    candidate_files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                candidate_files = [p]

            for f in candidate_files:
                post_id = hashlib.md5(str(f).encode("utf-8")).hexdigest()[:16].upper()
                if post_id in seen_post_ids or _is_duplicate_post_id(conn, project_name, post_id):
                    skipped_count += 1
                    continue
                seen_post_ids.add(post_id)
                pending_files.append(f)
    finally:
        conn.close()

    try:
        probe_map = probe_video_infos([str(f) for f in pending_files])
    except VideoProbeError as exc:
        raise HTTPException(400, str(exc)) from exc

    added_count = 0
    added_titles = []
    added_ids = []
    conn = get_conn()
    try:
        with _import_insert_lock:
            conn.execute('BEGIN IMMEDIATE')
            project_name = _current_import_project(conn, project_id)
            for f in pending_files:
                post_id = hashlib.md5(str(f).encode("utf-8")).hexdigest()[:16].upper()
                if _is_duplicate_post_id(conn, project_name, post_id):
                    skipped_count += 1
                    continue

                probe = probe_map[str(f)]
                title = f.stem
                is_short = probe["seconds"] <= 65 or (probe["height"] > probe["width"] and probe["seconds"] <= 180)
                final_upload_to = upload_to or ("Shorts" if is_short else "Video")
                item_schedule = (start_dt + timedelta(minutes=added_count * schedule_interval_minutes)).strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""
                item_premiere = final_settings["is_premiere"] if start_dt else 0

                cursor = conn.execute(
                    """
                    INSERT INTO video_queue (
                        project_name, source_type, platform, post_id, source_url, input_video_path,
                        title, description, tags, duration, view_count, like_count,
                        upload_to, visibility, schedule_time, is_premiere, made_for_kids,
                        age_restriction, altered_content, target_type, target_channel_id,
                        target_group_name, target_group_names_json, group_schema_version,
                        option_group_id, render_mode, resolution,
                        emulate_mobile, download_progress, status
                    ) VALUES (?, 'File', 'LocalFile', ?, ?, ?, ?, ?, '#shorts #trending', ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, 'GROUP', NULL, ?, ?, 1, NULL, ?, ?, 0, 100, 'Pending')
                    """,
                    (
                        project_name,
                        post_id,
                        str(f),
                        str(f),
                        title,
                        title,
                        probe["duration"],
                        final_upload_to,
                        visibility,
                        item_schedule,
                        item_premiere,
                        final_settings["made_for_kids"],
                        final_settings["age_restriction"],
                        final_settings["altered_content"],
                        group_names[0],
                        serialize_group_names(group_names),
                        render_mode,
                        resolution,
                    ),
                )
                added_count += 1
                added_titles.append(title)
                added_ids.append(int(cursor.lastrowid))

            conn.commit()
    finally:
        conn.close()
    log_msg = f"Đã thêm trực tiếp {added_count} video cục bộ vào dự án [{project_name}]."
    if skipped_count:
        log_msg += f" Bỏ qua {skipped_count} video đã có trong dự án."
    add_log("Scanner", log_msg, "SUCCESS")
    return {"ok": True, "added": added_count, "skipped": skipped_count,
            "titles": added_titles, "added_ids": added_ids}


@app.post("/api/queue/quick-edit")
def api_quick_edit_queue(data: Dict[str, Any] = Body(...)):
    edits = data.get("items")
    project = data.get("project")
    if not isinstance(edits, list) or not edits or not isinstance(project, str) or not project:
        raise HTTPException(400, "Chọn dự án và video cần lưu.")

    changes = {}
    for edit in edits:
        if not isinstance(edit, dict) or type(edit.get("id")) is not int or edit["id"] <= 0:
            raise HTTPException(400, "ID video không hợp lệ.")
        item_id = edit["id"]
        if item_id in changes or not edit.keys() <= {"id", "title", "target_channel_id", "visibility"}:
            raise HTTPException(400, f"Video #{item_id} có thay đổi không hợp lệ.")
        changes[item_id] = {key: value for key, value in edit.items() if key != "id"}

    conn = get_conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _ensure_queue_items_idle(conn, changes, "sửa nhanh video")
            for item_id, updates in changes.items():
                row = conn.execute(
                    "SELECT * FROM video_queue WHERE id = ? AND project_name = ?", (item_id, project),
                ).fetchone()
                if not row:
                    raise HTTPException(404, f"Video #{item_id} không còn trong dự án.")
                current = dict(row)
                if current["status"] == "Completed" or current["published_url"]:
                    raise HTTPException(409, f"Video #{item_id} đã đăng.")
                if "title" in updates:
                    if not isinstance(updates["title"], str):
                        raise HTTPException(400, f"Tiêu đề video #{item_id} không hợp lệ.")
                    try:
                        updates["title"] = validate_metadata_fields(
                            {"title": updates["title"]}, require_title=True,
                        )["title"]
                    except ValueError as exc:
                        raise HTTPException(400, f"Video #{item_id}: {exc}") from exc
                if "visibility" in updates:
                    if updates["visibility"] not in ("private", "unlisted", "public"):
                        raise HTTPException(400, f"Chế độ hiển thị video #{item_id} không hợp lệ.")
                    updates.update(schedule_time="", is_premiere=0)
                if "target_channel_id" in updates:
                    channel_id = updates["target_channel_id"]
                    if type(channel_id) is not int or channel_id <= 0:
                        raise HTTPException(400, f"Kênh đích video #{item_id} không hợp lệ.")
                    updates = _queue_target_updates(conn, updates, current)
                if updates:
                    columns = ", ".join(f"{key} = ?" for key in updates)
                    conn.execute(f"UPDATE video_queue SET {columns} WHERE id = ?", [*updates.values(), item_id])
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/queue/bulk-update")
def api_bulk_update_queue(data: Dict[str, Any] = Body(...)):
    raw_ids = data.get("ids") or []
    updates = data.get("updates") or {}
    if not raw_ids or not isinstance(updates, dict) or not updates:
        return {"ok": False, "updated": 0, "existing": 0}
    try:
        ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        raise HTTPException(400, "Danh sách ID video không hợp lệ.")

    allowed = {
        "title", "description", "tags", "upload_to", "visibility", "schedule_time",
        "target_type", "target_channel_id", "target_group_name", "target_group_names",
        "target_group_names_json", "target_profile_name", "option_group_id", "render_mode",
        "render_config_json", "cut_start", "cut_end", "resolution", "emulate_mobile",
        "made_for_kids", "age_restriction", "altered_content", "is_premiere", "group_schema_version",
    }
    filtered = {key: value for key, value in updates.items() if key in allowed}
    if not filtered:
        return {"ok": False, "updated": 0, "existing": 0}
    if "render_config_json" in filtered:
        raw_config = filtered["render_config_json"]
        try:
            render_config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Cấu hình render phải là JSON hợp lệ.") from exc
        try:
            render_config = validate_render_config(render_config, filtered.get('render_mode'))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        filtered['render_config_json'] = json.dumps(render_config, ensure_ascii=False, allow_nan=False)

    try:
        interval = int(updates.get("schedule_interval_minutes") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Khoảng cách lịch phải là số phút hợp lệ.")
    base_raw = str(updates.get("schedule_time") or "").strip()
    conn = get_conn()
    updated = 0
    existing_ids = set()
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in ids)
        _ensure_queue_items_idle(conn, ids, "sửa hàng loạt")
        rows = conn.execute(f"SELECT * FROM video_queue WHERE id IN ({marks})", ids).fetchall()
        current_by_id = {int(row["id"]): dict(row) for row in rows}
        existing_ids = set(current_by_id)
        missing_ids = [item_id for item_id in ids if item_id not in existing_ids]
        if missing_ids:
            raise HTTPException(404, f'Video không còn tồn tại: {missing_ids}')
        target_keys = {
            "target_type", "target_channel_id", "target_group_name", "target_group_names",
            "target_group_names_json", "target_profile_name",
        }
        dest_changed = any(key in filtered for key in target_keys)
        if dest_changed:
            active = [row for row in current_by_id.values() if (
                str(row.get("claim_token") or "")
                or str(row.get("status") or "").startswith(("Uploading", "Downloading", "Rendering", "Đang đăng"))
            )]
            if active:
                raise HTTPException(409, "Có video đang được worker giữ; không thể đổi đích của lô này.")

        from datetime import timedelta
        base_dt = None
        if base_raw:
            try:
                normalized_base = validate_queue_fields({
                    **{key: current_by_id[ids[0]].get(key) for key in (
                        "upload_to", "render_mode", "resolution", "made_for_kids", "age_restriction"
                    ) if ids[0] in current_by_id},
                    "visibility": "schedule",
                    "schedule_time": base_raw,
                    "is_premiere": 0,
                })
                base_raw = normalized_base["schedule_time"]
                base_dt = datetime.fromisoformat(base_raw)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

        if filtered.get("visibility") in ("public", "unlisted", "private") and base_raw:
            raise HTTPException(400, "Chế độ đăng ngay không được kèm thời gian đặt lịch.")
        wants_schedule = str(filtered.get("visibility") or "").lower() in ("schedule", "scheduled")
        if wants_schedule and not base_dt and len(existing_ids) > 1:
            raise HTTPException(400, "Chọn lịch cho nhiều video cần giờ bắt đầu để phân cách từng lịch.")
        if base_dt and len(existing_ids) > 1:
            interval = max(30, interval or 30)

        planned: Dict[int, Dict[str, Any]] = {}
        for index, item_id in enumerate(ids):
            current = current_by_id.get(item_id)
            if not current:
                continue
            item_updates = dict(filtered)
            item_updates.pop("schedule_time", None)
            if dest_changed:
                item_updates = _queue_target_updates(conn, item_updates, current)

            if base_dt:
                item_updates["schedule_time"] = (base_dt + timedelta(minutes=index * interval)).strftime("%Y-%m-%d %H:%M:%S")
                if not item_updates.get("visibility"):
                    item_updates["visibility"] = "schedule"
            elif "schedule_time" in updates:
                item_updates["schedule_time"] = base_raw
            elif item_updates.get("visibility") in ("public", "unlisted", "private"):
                item_updates["schedule_time"] = ""
                item_updates["is_premiere"] = 0

            if "option_group_id" in item_updates and item_updates["option_group_id"]:
                if not conn.execute("SELECT 1 FROM upload_option_groups WHERE id = ?", (item_updates["option_group_id"],)).fetchone():
                    raise HTTPException(400, f"Không tìm thấy bộ tùy chọn đăng #{item_updates['option_group_id']}.")

            merged = dict(current)
            merged.update(item_updates)
            try:
                final = validate_queue_fields(merged)
                metadata = validate_metadata_fields(merged)
            except ValueError as exc:
                raise HTTPException(400, f"Video #{item_id}: {exc}") from exc
            item_updates.update({
                "visibility": final["visibility"],
                "schedule_time": final["schedule_time"],
                "is_premiere": final["is_premiere"],
                "made_for_kids": final["made_for_kids"],
                "age_restriction": final["age_restriction"],
                "altered_content": final["altered_content"],
                "upload_to": final["upload_to"],
                "render_mode": final["render_mode"],
                "resolution": final["resolution"],
            })
            item_updates.update({key: metadata[key] for key in ("title", "description", "tags") if key in item_updates})
            planned[item_id] = item_updates

        for item_id, item_updates in planned.items():
            safe = {key: value for key, value in item_updates.items() if key in allowed or key in {
                "target_group_names_json", "assigned_channel_id", "assigned_group_names_json"
            }}
            monitor_policy.manual_override(current_by_id[item_id], safe)
            changed = {key: value for key, value in safe.items() if current_by_id[item_id].get(key) != value}
            if not changed:
                continue
            columns = ", ".join(f"{key} = ?" for key in changed)
            cur = conn.execute(
                f"UPDATE video_queue SET {columns} WHERE id = ? AND COALESCE(claim_token, '') = '' "
                "AND status NOT LIKE 'Uploading%' AND status NOT LIKE 'Downloading%' AND status NOT LIKE 'Rendering%'",
                list(changed.values()) + [item_id],
            )
            if cur.rowcount != 1:
                raise HTTPException(409, f"Video #{item_id} đã được một worker giữ; không cập nhật được lô.")
            updated += 1
        conn.commit()
        return {
            "ok": True, "updated": updated, "existing": len(existing_ids),
            "missing_ids": missing_ids, "requested": len(ids),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/queue/shorts-boost")
def api_shorts_boost(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    if not ids:
        return {"ok": False, "updated": 0}
    normalized = _normalize_queue_ids(ids)
    if not normalized:
        return {"ok": False, "updated": 0}
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_queue_items_idle(conn, normalized, "bật Shorts boost")
        updated_count = 0
        for item_id in normalized:
            row = conn.execute("SELECT id, title, render_config_json FROM video_queue WHERE id = ?", (item_id,)).fetchone()
            if not row:
                continue
            title = (row["title"] or "").strip()
            # Đảm bảo có #shorts trong tiêu đề, nếu dài quá thì cắt gọt < 55 ký tự để không bị nút mobile che khuất
            if "#shorts" not in title.lower():
                if len(title) > 45:
                    title = title[:45].strip() + " #shorts"
                else:
                    title = f"{title} #shorts".strip()

            cfg = {}
            try:
                if row["render_config_json"]:
                    cfg = json.loads(row["render_config_json"])
            except Exception:
                cfg = {}

            cfg["enforce_shorts_duration"] = True
            cfg["shorts_visual_pop"] = True
            cfg["shorts_fit_mode"] = "speedup"
            try:
                cfg["speed"] = normalize_render_speed(cfg.get("speed", 1.0))
            except ValueError:
                cfg["speed"] = 1.0
            if cfg["speed"] < 1.05:
                cfg["speed"] = 1.05
            cfg["hflip"] = True
            cfg["audio_pitch_shift"] = True

            cur = conn.execute(
                """
                UPDATE video_queue
                SET upload_to = 'Shorts',
                    render_mode = 'shorts_9_16',
                    emulate_mobile = 0,
                    resolution = '1080p',
                    title = ?,
                    render_config_json = ?
                WHERE id = ? AND COALESCE(claim_token, '') = ''
                """,
                (title, json.dumps(cfg, ensure_ascii=False), item_id)
            )
            if cur.rowcount == 1:
                updated_count += 1
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "updated": updated_count}



@app.post("/api/queue/edit-metadata")
def api_edit_queue_metadata(data: Dict[str, Any] = Body(...)):
    item_id = data.get("id")
    if not item_id:
        raise HTTPException(400, "Thiếu ID video cần chỉnh sửa.")
    item_id = int(item_id)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Không tìm thấy video #{item_id} trong hàng đợi.")
        _ensure_queue_items_idle(conn, [item_id], "sửa metadata")
        current = dict(row)
        updates: Dict[str, Any] = {
            "title": (data.get("title") or "").strip(),
            "description": (data.get("description") or "").strip(),
            "tags": (data.get("tags") or "").strip(),
            "resolution": (data.get("resolution") or "1080p").strip(),
            "emulate_mobile": 1 if data.get("emulate_mobile", False) else 0,
            "render_mode": data.get("render_mode") or "shorts_9_16",
            "cut_start": (data.get("cut_start") or "").strip(),
            "cut_end": (data.get("cut_end") or "").strip(),
            "visibility": data.get("visibility") or "public",
            "schedule_time": (data.get("schedule_time") or "").strip(),
            "is_premiere": 1 if data.get("is_premiere") else 0,
            "made_for_kids": 1 if data.get("made_for_kids") else 0,
            "age_restriction": 1 if data.get("age_restriction") else 0,
            "altered_content": 1 if data.get("altered_content") else 0,
        }
        if "option_group_id" in data:
            option_group_id = data.get("option_group_id") or None
            if option_group_id is not None and not conn.execute(
                "SELECT 1 FROM upload_option_groups WHERE id = ?", (option_group_id,)
            ).fetchone():
                raise HTTPException(400, f"Không tìm thấy bộ tùy chọn đăng #{option_group_id}.")
            updates["option_group_id"] = option_group_id

        destination_changed = False
        if "target_group_names" in data or "target_group_names_json" in data or "target_group_name" in data:
            raw_groups = data.get("target_group_names", data.get("target_group_names_json", data.get("target_group_name")))
            requested_groups = _validated_group_names(conn, raw_groups, default="Mặc định")
            current_groups = target_group_names(current) if str(current.get("target_type") or "GROUP").upper() == "GROUP" else []
            destination_changed = current_groups != requested_groups or str(current.get("target_type") or "").upper() != "GROUP"
            if not destination_changed and current.get("assigned_channel_id"):
                try:
                    from group_distribution import choose_target_channel
                    choose_target_channel(conn, current, validate_existing_assignment=True)
                except ValueError:
                    destination_changed = True
            if destination_changed:
                updates.update(_queue_target_updates(conn, {"target_group_names": requested_groups}, current))
        elif "target_channel_id" in data or "target_profile_name" in data:
            requested_channel = data.get("target_channel_id") or None
            requested_profile = str(data.get("target_profile_name") or "").strip()
            destination_changed = (
                (int(current.get("target_channel_id") or 0) != int(requested_channel or 0))
                or (str(current.get("target_profile_name") or "").strip() != requested_profile)
                or (str(current.get("target_type") or "").upper() == "GROUP")
            )
            if not destination_changed and current.get("assigned_channel_id"):
                try:
                    from group_distribution import choose_target_channel
                    choose_target_channel(conn, current, validate_existing_assignment=True)
                except ValueError:
                    destination_changed = True
            if destination_changed:
                updates.update(_queue_target_updates(conn, {
                    "target_channel_id": requested_channel,
                    "target_profile_name": requested_profile,
                }, current))

        _ensure_destination_not_active(current, destination_changed)
        merged = dict(current)
        merged.update(updates)
        try:
            final = validate_queue_fields(merged)
            metadata = validate_metadata_fields(merged)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        updates.update({
            "visibility": final["visibility"],
            "schedule_time": final["schedule_time"],
            "is_premiere": final["is_premiere"],
            "made_for_kids": final["made_for_kids"],
            "age_restriction": final["age_restriction"],
            "altered_content": final["altered_content"],
            "upload_to": final["upload_to"],
            "render_mode": final["render_mode"],
            "resolution": final["resolution"],
        })
        updates.update({key: metadata[key] for key in ("title", "description", "tags")})
        safe = {key: value for key, value in updates.items() if key in {
            "title", "description", "tags", "resolution", "emulate_mobile", "render_mode",
            "cut_start", "cut_end", "visibility", "schedule_time", "is_premiere",
            "made_for_kids", "age_restriction", "altered_content", "option_group_id",
            "target_channel_id", "target_group_name", "target_group_names_json", "target_type",
            "target_profile_name", "assigned_channel_id", "assigned_group_names_json", "group_schema_version",
        }}
        monitor_policy.manual_override(current, safe)
        changed = {key: value for key, value in safe.items() if current.get(key) != value}
        if changed:
            columns = ", ".join(f"{key} = ?" for key in changed)
            cur = conn.execute(
                f"UPDATE video_queue SET {columns} WHERE id = ? AND COALESCE(claim_token, '') = ''",
                list(changed.values()) + [item_id],
            )
            if cur.rowcount != 1:
                raise HTTPException(409, f"Video #{item_id} đã được worker giữ; không lưu được thay đổi.")
        conn.commit()
        return {"ok": True, "updated": 1 if changed else 0, "destination_changed": destination_changed}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/queue/delete")
def api_delete_queue_items(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    if not ids:
        return {"ok": True, "deleted": 0}

    normalized = _normalize_queue_ids(ids)
    if not normalized:
        return {"ok": True, "deleted": 0}
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # claim_token khác rỗng = một worker (batch/monitor) đang giữ video này —
        # xóa ngay lúc này sẽ khiến worker ghi vào dòng đã mất hoặc đăng video không còn hàng đợi.
        _ensure_queue_items_idle(conn, normalized, "xóa")
        placeholders = ",".join("?" for _ in normalized)
        cur = conn.execute(f"DELETE FROM video_queue WHERE id IN ({placeholders})", normalized)
        deleted = int(cur.rowcount or 0)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted}


@app.get("/api/queue/{item_id}/stream")
def api_stream_video(item_id: int):
    import os
    conn = get_conn()
    row = conn.execute("SELECT input_video_path, output_video_path FROM video_queue WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Không tìm thấy video trong hàng đợi.")
    target_path = row["output_video_path"] if (row["output_video_path"] and os.path.exists(row["output_video_path"])) else row["input_video_path"]
    if not target_path or not os.path.exists(target_path):
        raise HTTPException(404, "File video chưa tải về hoặc chưa được render trên máy tính.")
    return FileResponse(target_path, media_type="video/mp4")


@app.post("/api/queue/auto-schedule")
def api_auto_schedule(data: Dict[str, Any] = Body(...)):
    from datetime import datetime, timedelta
    ids = data.get("ids") or []
    if not ids:
        raise HTTPException(400, "Vui lòng chọn ít nhất 1 video để lên lịch.")
    start_time_str = (data.get("start_time") or "").strip()
    interval_minutes = int(data.get("interval_minutes") or 60)
    is_premiere = 1 if data.get("is_premiere") else 0

    # AN TOÀN TÀI KHOẢN (quy tắc chống spam): ép giãn cách tối thiểu 30 phút giữa các lần phát hành
    if interval_minutes < 30:
        interval_minutes = 30

    if start_time_str:
        try:
            curr = datetime.fromisoformat(start_time_str)
        except Exception:
            raise HTTPException(400, f"Thời gian bắt đầu không đúng định dạng: '{start_time_str}' (VD: 2026-10-25 09:00)")
        if curr <= datetime.now():
            raise HTTPException(400, "Thời gian bắt đầu phải ở TƯƠNG LAI — để kịp upload video trước giờ phát hành.")
    else:
        # Mặc định an toàn cho lô lớn (50 video): bắt đầu sau 60 phút nữa —
        # upload từng video mất thời gian, lịch sát "bây giờ" sẽ bị YouTube từ chối khi đến lượt đăng
        curr = datetime.now() + timedelta(minutes=60)

    normalized = _normalize_queue_ids(ids)
    if not normalized:
        raise HTTPException(400, "Danh sách ID video không hợp lệ.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_queue_items_idle(conn, normalized, "lên lịch")
        first_dt = last_dt = None
        for item_id in normalized:
            curr_str = curr.strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE video_queue SET monitor_metadata_policy_json='', schedule_time = ?, visibility = 'schedule', is_premiere = ? "
                "WHERE id = ? AND COALESCE(claim_token, '') = ''",
                (curr_str, is_premiere, item_id),
            )
            if first_dt is None:
                first_dt = curr
            last_dt = curr
            curr += timedelta(minutes=interval_minutes)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    add_log(
        "Scheduler",
        f"Đã lên lịch giãn cách cho {len(normalized)} video: từ {first_dt:%Y-%m-%d %H:%M} đến {last_dt:%Y-%m-%d %H:%M} (cách nhau {interval_minutes} phút).",
        "SUCCESS",
    )
    return {
        "ok": True,
        "scheduled": len(normalized),
        "interval_minutes": interval_minutes,
        "first_time": first_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "last_time": last_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/queue/export-csv")
def api_export_csv(project: str = "Default"):
    import csv
    import io
    from csv_safety import ENCODING_COLUMN, ENCODING_VERSION, encode_csv_cell
    from fastapi.responses import Response

    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, platform, title, description, tags, duration, source_url, render_mode,
               upload_to, status, published_url, visibility, schedule_time,
               made_for_kids, age_restriction, altered_content
        FROM video_queue WHERE project_name = ? ORDER BY id ASC
        """,
        (project,),
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Platform", "Title", "Description", "Tags", "Duration", "Source_URL",
        "Render_Mode", "Upload_To", "Status", "Published_URL",
        "Visibility", "Schedule_Time", "Made_For_Kids", "Age_Restriction", "Altered_Content",
        ENCODING_COLUMN,
    ])
    for r in rows:
        writer.writerow([encode_csv_cell(value) for value in [
            r["id"], r["platform"], r["title"], r["description"], r["tags"], r["duration"],
            r["source_url"], r["render_mode"], r["upload_to"], r["status"], r["published_url"],
            r["visibility"] or "public", r["schedule_time"] or "",
            r["made_for_kids"] or 0, r["age_restriction"] or 0, r["altered_content"] or 0,
        ]] + [ENCODING_VERSION])

    csv_data = "\ufeff" + output.getvalue()
    from urllib.parse import quote
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", project or "Queue")[:40] or "Queue"
    display_name = f"YouTube_Queue_{project or 'Queue'}.csv"
    return Response(
        content=csv_data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="YouTube_Queue_{safe_name}.csv"; '
                f"filename*=UTF-8''{quote(display_name, safe='')}"
            )
        },
    )


@app.post("/api/queue/import-csv")
def api_import_csv(data: Dict[str, Any] = Body(...)):
    project_name = str(data.get("project_name") or "Default").strip() or "Default"
    csv_text = data.get("csv_text") or ""
    preview_only = bool(data.get("preview"))
    conn = get_conn()
    try:
        if preview_only:
            conn.execute("BEGIN")
        else:
            conn.execute("BEGIN IMMEDIATE")
        existing_urls = {
            normalize_source_url(row[0]) for row in conn.execute(
                "SELECT source_url FROM video_queue WHERE project_name = ? AND COALESCE(source_url, '') != ''",
                (project_name,),
            ).fetchall() if normalize_source_url(row[0])
        }
        existing_groups = {
            str(row[0]) for row in conn.execute("SELECT name FROM channel_groups").fetchall()
        }
        try:
            preview = preview_csv_import(
                csv_text,
                existing_urls=existing_urls,
                existing_groups=existing_groups,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if preview_only:
            conn.rollback()
            return {"ok": True, "preview": True, **{k: v for k, v in preview.items() if k != "valid_rows"}}

        conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (project_name,))
        added = 0
        for row in preview["valid_rows"]:
            item = row["data"]
            groups = item["target_group_names"]
            conn.execute(
                """
                INSERT INTO video_queue (
                    project_name, source_type, platform, source_url, title, description, tags,
                    upload_to, render_mode, resolution, visibility, schedule_time, is_premiere,
                    made_for_kids, age_restriction, altered_content, emulate_mobile,
                    target_type, target_group_name, target_group_names_json, group_schema_version, status
                ) VALUES (?, 'CSV', 'Custom', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                          'GROUP', ?, ?, 1, 'Pending')
                """,
                (
                    project_name, item["source_url"], item["title"], item["description"], item["tags"],
                    item["upload_to"], item["render_mode"], item["resolution"], item["visibility"],
                    item["schedule_time"], item["is_premiere"], item["made_for_kids"],
                    item["age_restriction"], item["altered_content"], groups[0], serialize_group_names(groups),
                ),
            )
            added += 1
        conn.commit()
        skipped = preview["duplicate_count"] + preview["error_count"]
        result = {"ok": True, "preview": False, "added": added, "skipped": skipped, **{
            key: preview[key] for key in ("total_rows", "valid_count", "duplicate_count", "error_count", "rows")
        }}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    add_log(
        "CSV",
        f"Nhập CSV [{project_name}]: thêm {result['added']}, trùng {result['duplicate_count']}, lỗi {result['error_count']} dòng.",
        "SUCCESS" if result["added"] else "WARNING",
    )
    return result


# ==========================================================
# NHẬP / XUẤT EXCEL HÀNG LOẠT (CHUẨN BỊ LÔ)
# ==========================================================
_EXCEL_FIELD_ALIASES = {
    "id": ("id", "video id", "ma video", "ma id", "queue id"),
    "title": ("tieu de", "title", "ten video", "tieu de video", "tieu de youtube"),
    "description": ("mo ta", "description", "desc", "mo ta video", "mo ta youtube"),
    "tags": ("tags", "tag", "the", "tu khoa", "keywords"),
    "upload_to": ("tai dang", "upload to", "upload_to", "loai video", "dang dang", "shorts video"),
    "render_mode": ("che do render", "render mode", "render_mode", "che do xu ly", "kieu render"),
    "resolution": ("do phan giai", "resolution", "chat luong", "quality"),
    "visibility": ("rieng tu", "visibility", "che do dang", "trang thai dang", "privacy"),
    "schedule_time": ("dat lich", "schedule", "schedule time", "schedule_time", "thoi gian dang", "len lich", "gio dang"),
    "target_profile_name": ("profile chrome", "profile", "chrome profile", "tai khoan chrome", "profile dang"),
    "channel": ("kenh youtube", "kenh", "channel", "channel id", "kenh dich", "tai khoan youtube", "youtube channel"),
    "target_group_names": ("nhom kenh", "nhom dich", "target groups", "target_group_names", "groups"),
    "age_restriction": ("gioi han 18+", "18+", "age restriction", "age_restriction", "gioi han do tuoi", "gioi han nguoi xem"),
    "made_for_kids": ("danh cho tre em", "made for kids", "made_for_kids", "tre em", "mfk"),
    "altered_content": ("nhan ai", "altered content", "altered_content", "noi dung ai", "ai label"),
    "is_premiere": ("premiere", "cong chieu", "is premiere", "is_premiere"),
    "cut_start": ("cat dau", "cut start", "cut_start", "bat dau cat"),
    "cut_end": ("cat cuoi", "cut end", "cut_end", "ket thuc cat"),
    "emulate_mobile": ("gia lap mobile", "emulate mobile", "emulate_mobile", "mobile"),
}

_EXCEL_COLUMN_DEFS = [
    ("id", "ID", 7),
    ("title", "Tiêu đề", 46),
    ("description", "Mô tả", 55),
    ("tags", "Tags", 26),
    ("upload_to", "Tải dạng", 11),
    ("render_mode", "Chế độ render", 16),
    ("resolution", "Độ phân giải", 12),
    ("visibility", "Riêng tư", 12),
    ("schedule_time", "Đặt lịch", 20),
    ("target_profile_name", "Profile Chrome", 20),
    ("channel", "Kênh YouTube", 30),
    ("target_group_names", "Nhóm kênh đích", 32),
    ("age_restriction", "Giới hạn 18+", 13),
    ("made_for_kids", "Dành cho trẻ em", 15),
    ("altered_content", "Nhãn AI", 11),
    ("is_premiere", "Premiere", 10),
    ("cut_start", "Cắt đầu", 10),
    ("cut_end", "Cắt cuối", 10),
    ("emulate_mobile", "Giả lập Mobile", 14),
    ("status", "Trạng thái", 20),
    ("published_url", "Link đã đăng", 34),
]

_EXCEL_FIELD_LOOKUP: Optional[Dict[str, str]] = None

_EXCEL_YES = {"1", "true", "yes", "y", "co", "x", "v", "bat", "on", "dong y", "co roi"}
_EXCEL_NO = {"0", "false", "no", "n", "khong", "tat", "off", "khong co"}

_EXCEL_VISIBILITY = {
    "public": "public", "cong khai": "public", "dang cong khai": "public",
    "dang ngay": "public", "public ngay": "public",
    "unlisted": "unlisted", "khong cong khai": "unlisted", "khong ro": "unlisted",
    "an": "unlisted", "hidden": "unlisted",
    "private": "private", "rieng tu": "private", "kin": "private", "private video": "private",
    "schedule": "schedule", "len lich": "schedule", "dat lich": "schedule",
    "hen gio": "schedule", "hen gio dang": "schedule", "scheduled": "schedule",
}

_EXCEL_RENDER_MODES = {
    "shorts 9 16": "shorts_9_16", "shorts": "shorts_9_16", "shorts_9_16": "shorts_9_16",
    "render doc 9 16": "shorts_9_16", "doc 9 16 blur": "shorts_9_16",
    "pip thu nho": "pip_thu_nho", "lach thu nho": "pip_thu_nho",
    "video nen dhb": "pip_thu_nho", "pip": "pip_thu_nho",
    "full 16 9": "full_16_9", "lach full man 16 9": "full_16_9", "full man hinh": "full_16_9",
    "square 1 1": "square_1_1", "vuong 1 1": "square_1_1",
    "audio effect": "audio_effect", "lach am thanh": "audio_effect",
    "md5 cut": "md5_cut", "doi md5": "md5_cut", "xoa tag": "md5_cut",
    "none": "none", "khong render": "none",
    "custom ffmpeg": "custom_ffmpeg",
}

_EXCEL_RESOLUTIONS = {
    "1080p": "1080p", "1080": "1080p", "full hd": "1080p", "fhd": "1080p",
    "2k": "2k", "1440p": "2k", "qhd": "2k", "2k qhd": "2k",
    "4k": "4k", "2160p": "4k", "uhd": "4k", "4k ultra hd": "4k",
    "original": "original", "goc": "original", "nguyen goc": "original",
}


def _excel_key(value: Any) -> str:
    import unicodedata
    text = str(value if value is not None else "").strip().lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    return " ".join(text.split())


def _excel_field_lookup() -> Dict[str, str]:
    global _EXCEL_FIELD_LOOKUP
    if _EXCEL_FIELD_LOOKUP is None:
        _EXCEL_FIELD_LOOKUP = {}
        for field, aliases in _EXCEL_FIELD_ALIASES.items():
            for alias in aliases:
                _EXCEL_FIELD_LOOKUP[_excel_key(alias)] = field
    return _EXCEL_FIELD_LOOKUP


def _excel_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return int(text)
    try:
        number = float(text)
        return int(number) if number.is_integer() else None
    except Exception:
        return None


def _excel_datetime(value: Any) -> Optional[datetime]:
    from datetime import date, time, timedelta
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.now().astimezone().tzinfo).replace(tzinfo=None)
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time(9, 0))
    if isinstance(value, (int, float)):
        serial = float(value)
        if 20000 < serial < 80000:
            return datetime(1899, 12, 30) + timedelta(days=serial)
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: FURB162 - supports Python 3.10
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.now().astimezone().tzinfo).replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M", "%d-%m-%Y",
        "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%H:%M %d/%m/%Y", "%H:%M:%S %d/%m/%Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            if "%H" not in fmt:
                parsed = parsed.replace(hour=9, minute=0)
            return parsed
        except Exception:
            continue
    return None


def _excel_bool(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value != 0 else 0
    key = _excel_key(value)
    if not key:
        return None
    if key in _EXCEL_YES:
        return 1
    if key in _EXCEL_NO:
        return 0
    return None


def _excel_map_choice(value: Any, mapping: Dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    key = _excel_key(value)
    return mapping.get(key) if key else None


def _parse_id_list(raw: Any) -> List[int]:
    if not raw:
        return []
    items: Any
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            items = decoded if isinstance(decoded, list) else [decoded]
        except Exception:
            items = re.split(r"[,;\s]+", text)
    result = []
    for item in items:
        try:
            result.append(int(item))
        except Exception:
            continue
    return result


def _match_excel_channel(value: Any, channels: List[Dict[str, Any]]):
    if value is None or str(value).strip() == "":
        return None, ""
    channel_id = _excel_int(value)
    if channel_id is not None:
        for ch in channels:
            if int(ch["id"]) == channel_id:
                return ch, ""
        return None, f"không tìm thấy kênh có ID {channel_id}"
    key = _excel_key(value)
    if not key:
        return None, ""
    exact = [
        ch for ch in channels
        if _excel_key(ch.get("name")) == key or _excel_key(ch.get("handle")) == key
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, "tên kênh trùng với nhiều kênh"
    partial = [
        ch for ch in channels
        if key in _excel_key(ch.get("name")) or key in _excel_key(ch.get("handle"))
    ]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, f"tên kênh khớp {len(partial)} kênh, hãy ghi rõ tên hoặc ID"
    return None, f"không tìm thấy kênh '{value}'"


def _match_excel_profile(value: Any, profile_names: List[str]) -> Optional[str]:
    key = _excel_key(value)
    if not key:
        return None
    for name in profile_names:
        if _excel_key(name) == key:
            return name
    return None


def _find_excel_header(worksheet, lookup: Dict[str, str]):
    max_col = min(int(worksheet.max_column or 1), 80)
    max_row = min(int(worksheet.max_row or 1), 20)
    best = None
    for row_idx in range(1, max_row + 1):
        mapping: Dict[str, int] = {}
        for col_idx in range(1, max_col + 1):
            header = _excel_key(worksheet.cell(row_idx, col_idx).value)
            field = lookup.get(header)
            if field and field not in mapping:
                mapping[field] = col_idx
        score = len(mapping)
        if "title" in mapping:
            score += 1
        if score >= 2 and (best is None or score > best[0]):
            best = (score, row_idx, mapping)
    return best


@app.get("/api/queue/export-excel")
def api_export_excel(project: str = "Default", ids: str = ""):
    import io
    from urllib.parse import quote
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    from fastapi.responses import Response

    id_list = _parse_id_list(ids)
    conn = get_conn()
    if id_list:
        placeholders = ",".join("?" for _ in id_list)
        rows = conn.execute(
            f"SELECT * FROM video_queue WHERE project_name = ? AND id IN ({placeholders}) ORDER BY id ASC",
            [project] + id_list,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM video_queue WHERE project_name = ? ORDER BY id ASC",
            (project,),
        ).fetchall()
    channels = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM youtube_channels").fetchall()}
    conn.close()

    if id_list and not rows:
        raise HTTPException(404, "Không tìm thấy video nào thuộc dự án với các ID đã chọn.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Danh sách"

    header_fill = PatternFill("solid", fgColor="1E293B")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, (_, header, width) in enumerate(_EXCEL_COLUMN_DEFS, start=1):
        cell = ws.cell(1, col_idx, header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = border
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "A2"

    for row_idx, item in enumerate(rows, start=2):
        item = dict(item)
        ch = channels.get(int(item["target_channel_id"])) if item["target_channel_id"] else None
        values = {
            "id": item["id"],
            "title": item["title"] or "",
            "description": item["description"] or "",
            "tags": item["tags"] or "",
            "upload_to": item["upload_to"] or "Shorts",
            "render_mode": item["render_mode"] or "shorts_9_16",
            "resolution": item["resolution"] or "1080p",
            "visibility": item["visibility"] or "public",
            "schedule_time": item["schedule_time"] or "",
            "target_profile_name": item["target_profile_name"] or "",
            "channel": ch["name"] if ch else "",
            "target_group_names": " | ".join(target_group_names(dict(item)))
            if str(item.get("target_type") or "GROUP").upper() == "GROUP" else "",
            "age_restriction": item["age_restriction"] or 0,
            "made_for_kids": item["made_for_kids"] or 0,
            "altered_content": item["altered_content"] or 0,
            "is_premiere": item["is_premiere"] or 0,
            "cut_start": item["cut_start"] or "",
            "cut_end": item["cut_end"] or "",
            "emulate_mobile": item["emulate_mobile"] or 0,
            "status": item["status"] or "",
            "published_url": item["published_url"] or "",
        }
        for col_idx, (field, _, _) in enumerate(_EXCEL_COLUMN_DEFS, start=1):
            cell = ws.cell(row_idx, col_idx, values.get(field, ""))
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=field in ("title", "description", "tags"))
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
            if field in ("id", "schedule_time", "cut_start", "cut_end", "published_url"):
                cell.number_format = "@"

    last_row = max(2, len(rows) + 1)
    last_col = get_column_letter(len(_EXCEL_COLUMN_DEFS))
    if rows:
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"

    header_col = {field: get_column_letter(idx) for idx, (field, _, _) in enumerate(_EXCEL_COLUMN_DEFS, start=1)}
    validations = [
        ("Shorts,Video", "upload_to"),
        ("public,unlisted,private,schedule", "visibility"),
        ("0,1", "age_restriction"),
        ("0,1", "made_for_kids"),
        ("0,1", "altered_content"),
        ("0,1", "is_premiere"),
        ("0,1", "emulate_mobile"),
        ("1080p,2k,4k,original", "resolution"),
        ("shorts_9_16,pip_thu_nho,full_16_9,square_1_1,audio_effect,md5_cut,none,custom_ffmpeg", "render_mode"),
    ]
    for formula, field in validations:
        dv = DataValidation(type="list", formula1=f'"{formula}"', allow_blank=True)
        ws.add_data_validation(dv)
        col = header_col[field]
        dv.add(f"{col}2:{col}{max(500, last_row)}")

    guide = wb.create_sheet("Hướng dẫn")
    guide.column_dimensions["A"].width = 5
    guide.column_dimensions["B"].width = 30
    guide.column_dimensions["C"].width = 95
    guide_rows = [
        ("", "HƯỚNG DẪN CHUẨN BỊ LÔ VIDEO BẰNG EXCEL", ""),
        ("", "", ""),
        ("1", "Tải danh sách", "Tích chọn các video cần chuẩn bị trong bảng (không tích = cả dự án), rồi bấm nút '📥 Xuất Excel'."),
        ("2", "Sửa trên Excel", "Sửa trực tiếp các cột Tiêu đề, Mô tả, Đặt lịch, Profile Chrome, Kênh YouTube, 18+, trẻ em, nhãn AI..."),
        ("3", "Giữ nguyên cột ID", "Cột ID giúp cập nhật đúng video. Nếu xóa cột ID, dữ liệu sẽ được ghép theo thứ tự các dòng đang chọn."),
        ("4", "Ô để trống", "Ô để trống = giữ nguyên giá trị cũ. Chỉ ô có dữ liệu mới được cập nhật."),
        ("5", "Nhập lại", "Bấm '📤 Nhập Excel' và chọn file này. Kiểm tra thông báo để biết dòng nào được cập nhật hoặc bị bỏ qua."),
        ("6", "Chạy lô", "Chọn Profile Chrome + Kênh YouTube đích ở cột trái (nếu cần), bấm '✔️ Áp Dụng', rồi bấm '▶️ CHẠY TOÀN TRÌNH TỰ ĐỘNG'."),
        ("", "", ""),
        ("!", "Quy tắc an toàn", "Lịch đăng tự động phải cách nhau TỐI THIỂU 30 PHÚT. Hệ thống tự dời lịch nếu file Excel có các mốc quá sát nhau."),
        ("!", "Định dạng lịch", "Ghi 'YYYY-MM-DD HH:MM' hoặc 'DD/MM/YYYY HH:MM' (ví dụ 2026-11-01 09:00). Lịch đã qua sẽ bị bỏ qua."),
        ("!", "Chế độ đăng", "public = Công khai, unlisted = Không công khai, private = Riêng tư, schedule = Lên lịch. Có giờ đặt lịch hợp lệ thì tự chuyển sang Lên lịch."),
        ("!", "Profile & Kênh", "Kênh YouTube thuộc Profile nào thì hệ thống dùng Profile đó để đăng (đảm bảo cùng Proxy). Ghi tên kênh hoặc ID kênh."),
        ("!", "Giá trị 0/1", "0 = Không/Tắt, 1 = Có/Bật. Có thể ghi Có/Không, Yes/No."),
    ]
    title_font = Font(bold=True, size=14, color="1E293B")
    step_font = Font(bold=True, color="2563EB")
    warn_font = Font(bold=True, color="DC2626")
    for r_idx, (mark, label, text) in enumerate(guide_rows, start=1):
        a = guide.cell(r_idx, 1, mark)
        b = guide.cell(r_idx, 2, label)
        c = guide.cell(r_idx, 3, text)
        a.alignment = Alignment(horizontal="center", vertical="top")
        b.alignment = Alignment(vertical="top", wrap_text=True)
        c.alignment = Alignment(vertical="top", wrap_text=True)
        if r_idx == 1:
            b.font = title_font
            c.font = title_font
        else:
            b.font = warn_font if mark == "!" else step_font
            c.font = Font(color="334155")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", project or "Queue")[:40] or "Queue"
    display_name = f"DHB_Queue_{project or 'Queue'}.xlsx"
    headers = {
        "Content-Disposition": (
            f'attachment; filename="DHB_Queue_{safe_name}.xlsx"; '
            f"filename*=UTF-8''{quote(display_name, safe='')}"
        )
    }
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


def _excel_import_worker(raw: bytes, project_name: str, selected_ids: List[int], preview_only: bool = False) -> Dict[str, Any]:
    """Toàn bộ phần nặng của import Excel (parse + ghép dòng + ghi DB) tách thành hàm sync
    để chạy trong threadpool — không giữ event loop khi xử lý file lớn (S4)."""
    import io
    from datetime import timedelta

    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as exc:
        raise HTTPException(400, f"Không đọc được file Excel: {exc}")

    sheet = None
    for name in ("Danh sách", "Danh sach", "Danh sách video", "Queue"):
        if name in wb.sheetnames:
            sheet = wb[name]
            break
    if sheet is None:
        for candidate in wb.worksheets:
            if _excel_key(candidate.title) not in ("huong dan", "guide"):
                sheet = candidate
                break
    if sheet is None:
        raise HTTPException(400, "File Excel không có sheet dữ liệu.")

    found = _find_excel_header(sheet, _excel_field_lookup())
    if not found:
        raise HTTPException(
            400,
            "Không nhận diện được dòng tiêu đề. Hãy dùng file xuất từ nút '📥 Xuất Excel' "
            "hoặc đảm bảo có cột 'Tiêu đề' / 'Title'.",
        )
    _, header_row, columns = found

    excel_rows = []
    max_data_row = min(int(sheet.max_row or header_row), header_row + 5000)
    for r_idx in range(header_row + 1, max_data_row + 1):
        values = {
            field: sheet.cell(r_idx, col_idx).value
            for field, col_idx in columns.items()
        }
        if all(v is None or str(v).strip() == "" for v in values.values()):
            continue
        excel_rows.append({
            "row": r_idx,
            "id": _excel_int(values.get("id")),
            "values": values,
        })

    if not excel_rows:
        raise HTTPException(400, "File Excel không có dòng dữ liệu nào.")

    conn = get_conn()
    try:
        conn.execute("BEGIN" if preview_only else "BEGIN IMMEDIATE")
        project_rows = conn.execute(
            "SELECT * FROM video_queue WHERE project_name = ? ORDER BY id ASC",
            (project_name,),
        ).fetchall()
        if not project_rows:
            raise HTTPException(404, f"Dự án [{project_name}] chưa có video nào.")

        all_project_ids = [int(r["id"]) for r in project_rows]
        current_by_id = {int(r["id"]): dict(r) for r in project_rows}
        if selected_ids:
            target_pool = [i for i in selected_ids if i in current_by_id]
            if not target_pool:
                raise HTTPException(400, "Các dòng đã chọn không thuộc dự án hiện tại.")
        else:
            target_pool = all_project_ids

        channels = [dict(r) for r in conn.execute("SELECT * FROM youtube_channels ORDER BY id ASC").fetchall()]
        profile_names = [p["name"] for p in conn.execute("SELECT name FROM chrome_profiles ORDER BY name ASC").fetchall()]
        for ch in channels:
            pname = (ch.get("profile_name") or "").strip()
            if pname and pname not in profile_names:
                profile_names.append(pname)

        warnings: List[str] = []
        errors: List[str] = []
        updates_by_id: Dict[int, Dict[str, Any]] = {}
        row_results: Dict[int, Dict[str, Any]] = {}
        has_any_id = any(row["id"] is not None for row in excel_rows)

        def warn(message: str):
            if len(warnings) < 80:
                warnings.append(message)

        if has_any_id:
            unmatched_without_id = 0
            matched_ids = set()
            for row in excel_rows:
                excel_id = row["id"]
                if excel_id is None:
                    unmatched_without_id += 1
                    continue
                if excel_id not in current_by_id:
                    message = f"ID #{excel_id} không thuộc dự án [{project_name}] — bỏ qua."
                    warn(f"Dòng {row['row']}: {message}")
                    row_results[row["row"]] = {"line": row["row"], "queue_id": excel_id, "state": "skipped", "reasons": [message], "warnings": []}
                    continue
                if excel_id not in target_pool:
                    message = "ID không nằm trong các dòng đã tích chọn — bỏ qua."
                    warn(f"Dòng {row['row']}: {message}")
                    row_results[row["row"]] = {"line": row["row"], "queue_id": excel_id, "state": "skipped", "reasons": [message], "warnings": []}
                    continue
                if excel_id in matched_ids:
                    message = f"ID #{excel_id} bị lặp trong file Excel — chỉ cập nhật một lần."
                    errors.append(f"Dòng {row['row']}: {message}")
                    row_results[row["row"]] = {"line": row["row"], "queue_id": excel_id, "state": "error", "reasons": [message], "warnings": []}
                    continue
                matched_ids.add(excel_id)
                row["_target_id"] = excel_id
            if unmatched_without_id:
                warn(f"Có {unmatched_without_id} dòng thiếu ID nên không được ghép tự động — bỏ qua.")
        else:
            if len(excel_rows) > len(target_pool):
                warn(
                    f"File có {len(excel_rows)} dòng nhưng chỉ có {len(target_pool)} video được chọn — "
                    f"{len(excel_rows) - len(target_pool)} dòng cuối bị bỏ qua."
                )
            for row, item_id in zip(excel_rows, target_pool):
                if row["row"] not in row_results:
                    row["_target_id"] = item_id

        now = datetime.now().replace(microsecond=0)
        scheduled_entries = []
        for row in excel_rows:
            item_id = row.get("_target_id")
            if item_id is None:
                row_results.setdefault(row["row"], {"line": row["row"], "queue_id": None, "state": "skipped", "reasons": ["Không ghép được dòng Excel với video trong dự án."], "warnings": []})
                continue
            values = row["values"]
            current = current_by_id.get(item_id, {})
            updates: Dict[str, Any] = {}
            row_errors: List[str] = []
            row_warnings: List[str] = []

            def row_error(
                message: str, *, _row_errors=row_errors, _row=row, _item_id=item_id
            ):
                _row_errors.append(message)
                errors.append(f"Dòng {_row['row']} (#{_item_id}): {message}")

            def row_warning(
                message: str, *, _row_warnings=row_warnings, _row=row, _item_id=item_id
            ):
                _row_warnings.append(message)
                warn(f"Dòng {_row['row']} (#{_item_id}): {message}")

            if _blocked_queue_rows(conn, [item_id]):
                row_error("Video đang được giữ hoặc cần đối soát; không thể sửa bằng Excel.")

            title = values.get("title")
            if title is not None and str(title).strip() != "":
                title_text = str(title).strip()
                if len(title_text) > 100:
                    row_error(f"Tiêu đề dài {len(title_text)} ký tự, tối đa 100.")
                updates["title"] = title_text

            description = values.get("description")
            if description is not None and str(description).strip() != "":
                desc_text = str(description).strip()
                if len(desc_text) > 5000:
                    row_error("Mô tả vượt quá 5000 ký tự.")
                updates["description"] = desc_text

            tags = values.get("tags")
            if tags is not None and str(tags).strip() != "":
                updates["tags"] = str(tags).strip()

            upload_to_raw = values.get("upload_to")
            upload_to = _excel_map_choice(
                upload_to_raw,
                {"shorts": "Shorts", "short": "Shorts", "shorts video": "Shorts",
                 "video": "Video", "video dai": "Video", "dai": "Video", "long": "Video"},
            )
            if upload_to:
                updates["upload_to"] = upload_to
            elif upload_to_raw not in (None, ""):
                row_error(f"Định dạng video '{upload_to_raw}' không hợp lệ.")

            render_raw = values.get("render_mode")
            render_mode = _excel_map_choice(render_raw, _EXCEL_RENDER_MODES)
            if render_mode:
                updates["render_mode"] = render_mode
            elif render_raw not in (None, ""):
                row_error(f"Chế độ render '{render_raw}' không hợp lệ.")

            resolution_raw = values.get("resolution")
            resolution = _excel_map_choice(resolution_raw, _EXCEL_RESOLUTIONS)
            if resolution:
                updates["resolution"] = resolution
            elif resolution_raw not in (None, ""):
                row_error(f"Độ phân giải '{resolution_raw}' không hợp lệ.")

            profile_raw = values.get("target_profile_name")
            profile_name = None
            if profile_raw is not None and str(profile_raw).strip() != "":
                profile_name = _match_excel_profile(profile_raw, profile_names)

            channel_raw = values.get("channel")
            channel_input_present = channel_raw is not None and str(channel_raw).strip() != ""
            channel = None
            if channel_input_present:
                channel, reason = _match_excel_channel(channel_raw, channels)
                if not channel:
                    row_error(f"{reason}.")

            groups_raw = values.get("target_group_names")
            groups_input_present = groups_raw is not None and str(groups_raw).strip() != ""
            groups = []
            if groups_input_present:
                try:
                    groups = normalize_group_names(groups_raw)
                    actual_groups = {str(r[0]).casefold(): str(r[0]) for r in conn.execute("SELECT name FROM channel_groups").fetchall()}
                    missing = [name for name in groups if name.casefold() not in actual_groups]
                    if missing:
                        row_error("Nhóm không tồn tại: " + ", ".join(missing))
                    else:
                        groups = [actual_groups[name.casefold()] for name in groups]
                except ValueError as exc:
                    row_error(str(exc))

            if channel_input_present and groups_input_present:
                row_error("Chỉ chọn Kênh YouTube hoặc Nhóm kênh đích, không nhập đồng thời cả hai.")

            if channel:
                channel_profile = (channel.get("profile_name") or "").strip()
                if profile_name and channel_profile and _excel_key(profile_name) != _excel_key(channel_profile):
                    row_warning(f"Profile '{profile_name}' khác Profile của kênh; dùng '{channel_profile}' theo cấu hình kênh.")
                elif profile_raw and not profile_name:
                    row_warning(f"Không khớp Profile '{profile_raw}'; giữ Profile gắn với kênh đích.")
                updates["target_channel_id"] = int(channel["id"])
                updates["target_type"] = "CHANNEL"
                updates["target_group_name"] = ""
                updates["target_group_names_json"] = "[]"
                updates["group_schema_version"] = GROUP_SCHEMA_VERSION
                updates["assigned_channel_id"] = None
                updates["assigned_group_names_json"] = ""
                if channel_profile:
                    updates["target_profile_name"] = channel_profile
                elif profile_name:
                    updates["target_profile_name"] = profile_name
            elif groups_input_present and groups and not row_errors:
                updates["target_type"] = "GROUP"
                updates["target_channel_id"] = None
                updates["target_profile_name"] = ""
                updates["target_group_name"] = groups[0]
                updates["target_group_names_json"] = serialize_group_names(groups)
                updates["group_schema_version"] = GROUP_SCHEMA_VERSION
                updates["assigned_channel_id"] = None
                updates["assigned_group_names_json"] = ""
            elif profile_raw is not None and str(profile_raw).strip() != "" and not channel_input_present and not groups_input_present:
                if not profile_name:
                    row_error(f"Không tìm thấy Profile Chrome '{profile_raw}'.")
                else:
                    profile_channels = [c for c in channels if _excel_key(c.get("profile_name")) == _excel_key(profile_name)]
                    if len(profile_channels) != 1:
                        row_error(f"Profile '{profile_name}' phải có đúng một kênh YouTube; hiện có {len(profile_channels)}.")
                    else:
                        updates["target_profile_name"] = profile_name
                        updates["target_channel_id"] = None
                        updates["target_type"] = "PROFILE"
                        updates["target_group_name"] = ""
                        updates["target_group_names_json"] = "[]"
                        updates["assigned_channel_id"] = None
                        updates["assigned_group_names_json"] = ""

            visibility_raw = values.get("visibility")
            visibility = _excel_map_choice(visibility_raw, _EXCEL_VISIBILITY)
            if visibility_raw not in (None, "") and not visibility:
                row_error(f"Chế độ đăng '{visibility_raw}' không hợp lệ.")

            schedule_raw = values.get("schedule_time")
            schedule_dt = _excel_datetime(schedule_raw) if schedule_raw not in (None, "") else None
            if schedule_raw not in (None, "") and schedule_dt is None:
                row_error(f"Giờ đặt lịch '{schedule_raw}' không đúng định dạng.")
            if schedule_dt and schedule_dt <= now:
                row_error(f"Giờ đặt lịch {schedule_dt:%Y-%m-%d %H:%M} đã qua.")

            if visibility == "schedule":
                if schedule_dt:
                    updates["visibility"] = "schedule"
                    updates["schedule_time"] = schedule_dt.strftime("%Y-%m-%d %H:%M:%S")
                elif not (str(current.get("schedule_time") or "").strip()):
                    row_error("Chọn Lên lịch nhưng thiếu giờ đặt lịch.")
                else:
                    updates["visibility"] = "schedule"
            elif visibility in ("public", "unlisted", "private"):
                if schedule_dt:
                    row_error("Chế độ đăng ngay và giờ đặt lịch không thể cùng được chọn.")
                updates["visibility"] = visibility
                updates["schedule_time"] = ""
                updates["is_premiere"] = 0
            elif schedule_dt:
                updates["visibility"] = "schedule"
                updates["schedule_time"] = schedule_dt.strftime("%Y-%m-%d %H:%M:%S")

            for field in ("age_restriction", "made_for_kids", "altered_content", "is_premiere", "emulate_mobile"):
                raw_bool = values.get(field)
                parsed_bool = _excel_bool(raw_bool)
                if raw_bool not in (None, "") and parsed_bool is None:
                    row_error(f"Giá trị '{raw_bool}' ở cột {field} không phải 0/1 hoặc Có/Không.")
                elif parsed_bool is not None:
                    updates[field] = parsed_bool

            for field in ("cut_start", "cut_end"):
                raw_cut = values.get(field)
                if raw_cut is not None and str(raw_cut).strip() != "":
                    updates[field] = str(raw_cut).strip()

            merged = dict(current)
            merged.update(updates)
            try:
                metadata = validate_metadata_fields(merged)
                final = validate_queue_fields(merged, now=now)
                updates.update({key: final[key] for key in (
                    "visibility", "schedule_time", "is_premiere", "made_for_kids", "age_restriction",
                    "upload_to", "render_mode", "resolution",
                )})
                updates.update({key: metadata[key] for key in ("title", "description", "tags") if key in updates})
            except ValueError as exc:
                row_error(str(exc))

            if str(current.get("status") or "") == "Completed":
                row_warning("Video đã đăng; thay đổi chỉ lưu trong hàng đợi, không sửa video trên YouTube.")

            if row_errors:
                row_results[row["row"]] = {"line": row["row"], "queue_id": item_id, "state": "error", "reasons": row_errors, "warnings": row_warnings}
            elif updates:
                updates = {key: value for key, value in updates.items() if current.get(key) != value}
                if not updates:
                    row_results[row["row"]] = {"line": row["row"], "queue_id": item_id, "state": "skipped", "reasons": ["Giá trị không thay đổi so với hàng đợi."], "warnings": row_warnings}
                    continue
                updates_by_id[item_id] = updates
                row_results[row["row"]] = {"line": row["row"], "queue_id": item_id, "state": "valid", "reasons": [], "warnings": row_warnings, "changes": len(updates)}
                if updates.get("schedule_time") and str(updates.get("visibility") or current.get("visibility")) == "schedule":
                    scheduled_entries.append(
                        (item_id, datetime.fromisoformat(updates["schedule_time"]))
                    )
            else:
                row_results[row["row"]] = {"line": row["row"], "queue_id": item_id, "state": "skipped", "reasons": ["Dòng không có ô nào cần cập nhật."], "warnings": row_warnings}

        adjusted = 0
        scheduled_entries.sort(key=lambda pair: pair[1])
        for idx in range(1, len(scheduled_entries)):
            _prev_id, prev_dt = scheduled_entries[idx - 1]
            item_id, item_dt = scheduled_entries[idx]
            min_dt = prev_dt + timedelta(minutes=30)
            if item_dt < min_dt:
                item_dt = min_dt
                updates_by_id[item_id]["schedule_time"] = item_dt.strftime("%Y-%m-%d %H:%M:%S")
                adjusted += 1
            scheduled_entries[idx] = (item_id, item_dt)
        if adjusted:
            warn(
                f"Đã tự dời {adjusted} lịch để đảm bảo giãn cách tối thiểu 30 phút giữa các lần đăng."
            )

        allowed_cols = {
            "title", "description", "tags", "upload_to", "render_mode", "resolution",
            "visibility", "schedule_time", "target_profile_name", "target_channel_id",
            "target_type", "age_restriction", "made_for_kids", "altered_content",
            "is_premiere", "cut_start", "cut_end", "emulate_mobile", "target_group_name",
            "target_group_names_json", "group_schema_version", "assigned_channel_id", "assigned_group_names_json",
        }
        updated = 0
        for item_id, item_updates in updates_by_id.items():
            safe_updates = {k: v for k, v in item_updates.items() if k in allowed_cols}
            if not safe_updates:
                continue
            destination_changed = any(k in safe_updates for k in (
                "target_type", "target_channel_id", "target_profile_name", "target_group_name", "target_group_names_json"
            ))
            current = current_by_id[item_id]
            _ensure_destination_not_active(current, destination_changed)
            monitor_policy.manual_override(current, safe_updates)
            changed = {k: v for k, v in safe_updates.items() if current.get(k) != v}
            if not changed:
                continue
            cols = ", ".join(f"{k} = ?" for k in changed)
            cur = conn.execute(
                f"UPDATE video_queue SET {cols} WHERE id = ? AND COALESCE(claim_token, '') = '' "
                "AND status NOT LIKE 'Uploading%' AND status NOT LIKE 'Downloading%' "
                "AND status NOT LIKE 'Rendering%' AND status NOT LIKE 'Đang đăng%'",
                list(changed.values()) + [item_id],
            )
            if cur.rowcount != 1:
                raise HTTPException(409, f"Video #{item_id} bắt đầu chạy trong lúc nhập Excel; đã hủy toàn bộ cập nhật.")
            updated += cur.rowcount
        if preview_only:
            conn.rollback()
            updated = len(updates_by_id)
        else:
            conn.commit()
    finally:
        conn.close()

    valid_count = sum(1 for result in row_results.values() if result["state"] == "valid")
    skipped = sum(1 for result in row_results.values() if result["state"] == "skipped")
    error_count = sum(1 for result in row_results.values() if result["state"] == "error")
    result = {
        "ok": True,
        "preview": bool(preview_only),
        "updated": updated,
        "matched": valid_count,
        "skipped": skipped,
        "error_count": error_count,
        "total_rows": len(excel_rows),
        "adjusted_schedules": adjusted,
        "warning_count": len(warnings),
        "warnings": warnings[:50],
        "errors": errors[:100],
        "rows": [row_results[key] for key in sorted(row_results)[:100]],
    }
    if not preview_only:
        add_log(
            "Excel",
            f"Đã nhập Excel cập nhật {updated}/{len(excel_rows)} dòng cho dự án [{project_name}], "
            f"bỏ qua {skipped}, lỗi {error_count}.",
            "SUCCESS" if updated else "WARNING",
        )
    return result


def _description_video_id(value: Any) -> Optional[int]:
    if type(value) is int:
        return value if 0 < value <= 9223372036854775807 else None
    if isinstance(value, str) and re.fullmatch(r"#?[0-9]{1,19}", value.strip()):
        return _description_video_id(int(value.strip().lstrip("#")))
    if type(value) is float and value.is_integer():
        return _description_video_id(int(value))
    return None


def _description_excel_entries(raw: bytes) -> List[Dict[str, Any]]:
    from zipfile import ZipFile
    from openpyxl import load_workbook

    wb = None
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 64 * 1024 * 1024:
                raise HTTPException(400, "File Excel sau giải nén quá lớn (tối đa 64 MB).")
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        sheet = next(
            (wb[name] for name in ("Danh sách", "Danh sach", "Danh sách video", "Queue") if name in wb.sheetnames),
            next((ws for ws in wb.worksheets if _excel_key(ws.title) not in ("huong dan", "guide")), None),
        )
        if sheet is None:
            raise HTTPException(400, "File Excel không có sheet dữ liệu.")
        sheet.reset_dimensions()
        lookup = {
            _excel_key(alias): field
            for field in ("id", "description")
            for alias in _EXCEL_FIELD_ALIASES[field]
        }
        columns = {}
        header_row = 0
        for row_idx, cells in enumerate(sheet.iter_rows(max_row=20, max_col=80), 1):
            columns = {}
            for col_idx, cell in enumerate(cells):
                field = lookup.get(_excel_key(cell.value))
                if field:
                    columns.setdefault(field, col_idx)
            if "id" in columns and "description" in columns:
                header_row = row_idx
                break
        if not header_row:
            raise HTTPException(400, "File cần có cột 'ID' và 'Mô tả' (hoặc 'Description').")

        entries = []
        for row_idx, cells in enumerate(
            sheet.iter_rows(min_row=header_row + 1, max_col=max(columns.values()) + 1), header_row + 1,
        ):
            id_cell, description_cell = cells[columns["id"]], cells[columns["description"]]
            if id_cell.value in (None, "") and description_cell.value in (None, ""):
                continue
            if id_cell.data_type in ("f", "e") or description_cell.data_type in ("f", "e"):
                raise HTTPException(400, f"Dòng {row_idx}: hãy chuyển công thức/lỗi Excel thành giá trị trước khi nhập.")
            entries.append({
                "line": row_idx,
                "id": id_cell.value,
                "description": "" if description_cell.value is None else str(description_cell.value).strip(),
            })
            if len(entries) > 5000:
                raise HTTPException(400, "File có hơn 5000 dòng dữ liệu; hãy chia nhỏ file trước khi nhập.")
        if not entries:
            raise HTTPException(400, "File Excel không có dòng mô tả nào.")
        return entries
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "Không đọc được file Excel. Hãy chọn file .xlsx/.xlsm hợp lệ.") from exc
    finally:
        if wb is not None:
            wb.close()


def _bulk_description_worker(
    entries: List[Dict[str, Any]],
    project_name: str,
    selected_ids: List[int],
    preview_only: bool = True,
    preview_revision: str = "",
) -> Dict[str, Any]:
    if not isinstance(entries, list) or not entries:
        raise HTTPException(400, "Chưa có mô tả nào để xử lý.")
    if len(entries) > 5000:
        raise HTTPException(400, "Mỗi lượt chỉ xử lý tối đa 5000 mô tả.")
    if not isinstance(project_name, str) or not project_name.strip():
        raise HTTPException(400, "Thiếu dự án cần cập nhật.")
    if not isinstance(selected_ids, list) or len(selected_ids) > 5000 or any(
        type(item_id) is not int or _description_video_id(item_id) is None for item_id in selected_ids
    ):
        raise HTTPException(400, "Danh sách video phải gồm ID nguyên dương, tối đa 5000 mục.")
    if type(preview_only) is not bool:
        raise HTTPException(400, "preview_only phải là true hoặc false.")
    if not preview_only and (not isinstance(preview_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_revision)):
        raise HTTPException(400, "Cần xem trước mô tả trước khi lưu.")

    normalized = []
    id_counts: Dict[int, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("description"), str):
            raise HTTPException(400, f"Dữ liệu mô tả dòng {index + 1} phải là văn bản.")
        item_id = _description_video_id(entry.get("id"))
        line = entry.get("line", index + 1)
        if type(line) is not int or line <= 0:
            raise HTTPException(400, "Số dòng mô tả không hợp lệ.")
        normalized.append({"id": item_id, "line": line, "description": entry["description"].strip()})
        if item_id is not None:
            id_counts[item_id] = id_counts.get(item_id, 0) + 1

    conn = get_conn()
    try:
        conn.execute("BEGIN" if preview_only else "BEGIN IMMEDIATE")
        project_rows = conn.execute(
            "SELECT id, description, status, claim_token, "
            "EXISTS(SELECT 1 FROM content_reservations cr WHERE cr.queue_id = video_queue.id) AS reserved "
            "FROM video_queue WHERE project_name = ? ORDER BY id ASC",
            (project_name,),
        ).fetchall()
        if not project_rows:
            raise HTTPException(404, "Dự án chưa có video nào.")
        current_by_id = {int(row["id"]): dict(row) for row in project_rows}
        if selected_ids:
            target_pool = {item_id for item_id in selected_ids if item_id in current_by_id}
            if not target_pool:
                raise HTTPException(400, "Các dòng đã chọn không thuộc dự án hiện tại.")
        else:
            target_pool = set(current_by_id)

        baseline = [
            (item_id, current_by_id[item_id]["description"], current_by_id[item_id]["status"],
             bool(current_by_id[item_id]["claim_token"]), bool(current_by_id[item_id]["reserved"]))
            for item_id in sorted(id_counts) if item_id in target_pool
        ]
        revision = hashlib.sha256(json.dumps(
            [project_name, sorted(selected_ids), normalized, baseline], ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if not preview_only and preview_revision != revision:
            raise HTTPException(409, "Dữ liệu hoặc trạng thái video đã thay đổi. Hãy xem trước lại; chưa lưu mô tả nào.")

        results: List[Dict[str, Any]] = []
        candidates: Dict[int, Dict[str, Any]] = {}
        matched = 0
        unchanged = 0
        for entry in normalized:
            line = entry["line"]
            item_id = entry["id"]
            description = entry["description"]
            result: Dict[str, Any] = {
                "line": line,
                "queue_id": item_id,
                "state": "ready",
                "reason": "",
                "preview": description[:160],
            }
            results.append(result)
            current = current_by_id.get(item_id) if item_id is not None else None
            if item_id is None:
                result.update(state="error", reason="ID video thiếu hoặc không phải số nguyên dương.")
                continue
            if current is None or item_id not in target_pool:
                result.update(state="skipped", reason="ID không thuộc dự án hoặc không nằm trong các video đã chọn.")
                continue
            if id_counts[item_id] > 1:
                result.update(state="error", reason=f"ID #{item_id} bị lặp; không áp dụng các dòng trùng ID.")
                continue
            if not description:
                result.update(state="skipped", reason="Mô tả trống; giữ nguyên mô tả hiện tại.")
                continue
            if len(description) > 5000:
                result.update(state="error", reason="Mô tả vượt quá 5000 ký tự.")
                continue
            status = str(current.get("status") or "").strip()
            if (
                str(current.get("claim_token") or "").strip()
                or status.startswith(_QUEUE_BUSY_STATUS_PREFIXES)
                or status == "NeedsReview"
                or current["reserved"]
            ):
                result.update(state="error", reason="Video đang được xử lý hoặc cần đối soát.")
                continue
            matched += 1
            if str(current.get("description") or "").strip() == description:
                unchanged += 1
                result["state"] = "unchanged"
                continue
            candidates[item_id] = {"result": result, "description": description}

        updated = 0
        if not preview_only and candidates:
            _ensure_queue_items_idle(conn, list(candidates), "ghi mô tả hàng loạt")
            for item_id, candidate in candidates.items():
                cur = conn.execute(
                    "UPDATE video_queue SET description = ? WHERE id = ? AND COALESCE(claim_token, '') = ''",
                    (candidate["description"], item_id),
                )
                row_result = candidate["result"]
                if cur.rowcount == 1:
                    updated += 1
                    row_result["state"] = "updated"
                else:
                    row_result.update(state="error", reason="Video vừa được worker giữ; chưa cập nhật.")
        if not preview_only:
            conn.commit()

        error_count = sum(row["state"] == "error" for row in results)
        skipped = sum(row["state"] == "skipped" for row in results)
        pending = sum(row["state"] == "ready" for row in results)
        response = {
            "ok": pending > 0 if preview_only else updated > 0,
            "preview_only": preview_only,
            "total_rows": len(entries),
            "preview_revision": revision,
            "matched": matched,
            "unchanged": unchanged,
            "pending": pending,
            "updated": updated,
            "skipped": skipped,
            "error_count": error_count,
            "rows": results[:200],
        }
        if not preview_only:
            add_log(
                "Excel",
                f"Ghi mô tả hàng loạt: cập nhật {updated}, giữ nguyên {unchanged}, bỏ qua {skipped}, lỗi {error_count}.",
                "SUCCESS" if updated else "WARNING",
            )
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/queue/bulk-descriptions")
async def api_bulk_descriptions(data: Dict[str, Any] = Body(...)):
    return await run_in_threadpool(
        _bulk_description_worker,
        data.get("items"),
        data.get("project_name"),
        data.get("ids", []),
        data.get("preview_only", True),
        data.get("preview_revision", ""),
    )


@app.post("/api/queue/import-descriptions")
async def api_import_descriptions(
    file: UploadFile = File(...),
    project_name: str = Form(...),
    ids: str = Form("[]"),
    preview_only: bool = Form(True),
    preview_revision: str = Form(""),
):
    filename = (file.filename or "").strip()
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Vui lòng chọn file .xlsx hoặc .xlsm; file .xls cũ chưa được hỗ trợ.")
    try:
        selected_ids = json.loads(ids)
    except ValueError as exc:
        raise HTTPException(400, "Danh sách ID video không hợp lệ.") from exc
    raw = await file.read(20 * 1024 * 1024 + 1)
    if not raw:
        raise HTTPException(400, "File Excel rỗng.")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "File Excel quá lớn (tối đa 20 MB).")
    entries = await run_in_threadpool(_description_excel_entries, raw)
    return await run_in_threadpool(
        _bulk_description_worker, entries, project_name.strip(), selected_ids, preview_only, preview_revision,
    )


@app.post("/api/queue/import-excel")
async def api_import_excel(
    file: UploadFile = File(...),
    project_name: str = Form("Default"),
    ids: str = Form(""),
    preview_only: bool = Form(False),
):
    filename = (file.filename or "").strip()
    if filename.lower().endswith(".xls"):
        raise HTTPException(400, "File .xls cũ chưa được hỗ trợ. Hãy lưu lại thành .xlsx rồi nhập.")
    if filename and not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Vui lòng chọn file Excel .xlsx hoặc .xlsm.")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "File Excel rỗng.")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "File Excel quá lớn (tối đa 20 MB).")

    project_name = (project_name or "Default").strip() or "Default"
    selected_ids = _parse_id_list(ids)
    # S4: parse openpyxl + xử lý tới 5000 dòng là CPU-bound — đẩy sang threadpool
    # để không đóng băng event loop (polling 2.5s của toàn dashboard bị khựng).
    return await run_in_threadpool(_excel_import_worker, raw, project_name, selected_ids, preview_only)


# ==========================================================
# AI DỊCH THUẬT, VIẾT LẠI SEO & BIÊN TẬP HÀNG LOẠT
# ==========================================================
_AI_EDIT_QUEUE_FIELDS = (
    "id", "created_at", "project_name", "source_type", "platform", "source_url", "post_id",
    "target_type", "target_channel_id", "target_profile_name", "target_group_name",
    "target_group_names_json", "group_schema_version", "assigned_channel_id", "assigned_group_names_json",
    "title", "description", "tags", "upload_to", "visibility", "schedule_time", "option_group_id",
    "made_for_kids", "altered_content", "age_restriction", "is_premiere", "render_mode", "render_config_json",
    "cut_start", "cut_end", "resolution", "emulate_mobile", "manual_reupload_authorized",
    "manual_reupload_reason", "monitor_metadata_policy_json", "input_video_path", "output_video_path",
    "thumbnail_path", "content_key", "status", "claim_token", "published_url", "last_attempt_id", "error_message",
)


def _ai_edit_snapshot(conn, item):
    """Compare semantic state, including reused attempts; this is not a lifecycle epoch."""
    attempt = conn.execute(
        "SELECT * FROM upload_attempts WHERE queue_id=? ORDER BY attempt_no DESC,id DESC LIMIT 1",
        (item["id"],),
    ).fetchone()
    return ({field: item[field] for field in _AI_EDIT_QUEUE_FIELDS}, dict(attempt) if attempt else None)


def _ai_edit_failure(item_id, code, message):
    return {"id": int(item_id), "code": code, "error": message}


def _commit_ai_edit(item, snapshot, metadata, fields):
    """Check ownership and compare the original read in the same short write transaction."""
    with closing(get_conn()) as conn:
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM video_queue WHERE id=?", (item["id"],)).fetchone()
            if row is None:
                return _ai_edit_failure(item["id"], "item_missing", "Video không còn trong hàng đợi.")
            try:
                _ensure_queue_items_idle(conn, [item["id"]], "biên tập AI")
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                return _ai_edit_failure(item["id"], "item_guarded", "Video đang xử lý hoặc cần đối soát; kết quả lần này không được ghi.")
            if _ai_edit_snapshot(conn, row) != snapshot:
                return _ai_edit_failure(item["id"], "stale_snapshot", "Dữ liệu video đã thay đổi; hãy tải lại trước khi biên tập tiếp.")
            # fields comes only from the fixed title/description/tags allowlist below.
            assignments = ", ".join(f"{field}=?" for field in fields)
            cur = conn.execute(f"UPDATE video_queue SET {assignments} WHERE id=?",
                               (*[metadata[field] for field in fields], item["id"]))
            if cur.rowcount != 1:
                raise RuntimeError("AI edit lost its locked queue row.")
            conn.commit()
            return None
        except sqlite3.OperationalError as exc:
            # Do not hide disk/schema/corruption errors as ordinary item conflicts.
            if str(exc).lower() not in {"database is locked", "database table is locked", "database is busy"}:
                raise
            return _ai_edit_failure(item["id"], "db_write_failed", "Cơ sở dữ liệu đang bận; kết quả lần này không được ghi.")
        finally:
            if conn.in_transaction:
                conn.rollback()


@app.post("/api/queue/ai-process")
def api_ai_process_queue(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids", [])
    mode = data.get("mode") or "rewrite_seo"  # 'translate' | 'rewrite_seo' | 'batch_edit'
    provider = data.get("provider") or "gemini"
    target_lang = data.get("target_lang") or "Tiếng Việt"
    custom_prompt = data.get("custom_prompt") or ""
    if not isinstance(mode, str) or mode not in {"translate", "rewrite_seo", "shorts_hook", "batch_edit"}:
        raise HTTPException(400, "Chế độ biên tập không hợp lệ.")
    if not isinstance(provider, str) or provider not in {"gemini", "openai", "deepseek", "google_translate"}:
        raise HTTPException(400, "Nhà cung cấp AI không được hỗ trợ.")
    if not isinstance(target_lang, str) or len(target_lang) > 80:
        raise HTTPException(400, "Ngôn ngữ đích không hợp lệ hoặc quá dài.")
    if not isinstance(custom_prompt, str) or len(custom_prompt) > 4000:
        raise HTTPException(400, "Prompt tùy chỉnh không được vượt quá 4000 ký tự.")
    if not isinstance(ids, list) or len(ids) > 5000 or any(type(item_id) is not int or item_id <= 0 for item_id in ids):
        raise HTTPException(400, "Danh sách video phải là mảng ID nguyên dương, tối đa 5000 mục.")
    ids = list(dict.fromkeys(ids))

    # Tham số cho batch_edit
    edit_field = data.get("edit_field") or "title"
    if edit_field not in ("title", "description", "tags"):
        raise HTTPException(400, "Trường biên tập không hợp lệ (chỉ nhận title / description / tags).")
    edit_op = data.get("edit_op") or "append_end"
    find_str = data.get("find_str") or ""
    replace_str = data.get("replace_str") or ""
    if not isinstance(edit_op, str) or edit_op not in {"append_end", "append_first", "replace", "strip_hashtags", "titlecase", "uppercase", "lowercase"}:
        raise HTTPException(400, "Thao tác biên tập không hợp lệ.")
    if not isinstance(find_str, str) or not isinstance(replace_str, str) or len(find_str) > 5000 or len(replace_str) > 5000:
        raise HTTPException(400, "Chuỗi tìm/thay thế phải là văn bản tối đa 5000 ký tự.")

    updated = 0
    provider_counts: Dict[str, int] = {}
    fallback_count = 0
    links_preserved = 0
    failed_items = []
    # Fetch rows and attempt snapshots in one consistent read, then release it before provider work.
    with closing(get_conn()) as conn:
        conn.execute("BEGIN")
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT * FROM video_queue WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
        else:
            project_name = data.get("project_name", "Default")
            if project_name is None or project_name == "":
                project_name = "Default"
            if not isinstance(project_name, str) or len(project_name) > 200 or "\x00" in project_name:
                raise HTTPException(400, "Tên dự án phải là văn bản tối đa 200 ký tự và không chứa NUL.")
            rows = conn.execute("SELECT * FROM video_queue WHERE project_name = ? ORDER BY id", (project_name,)).fetchall()
        _ensure_queue_items_idle(conn, [int(r["id"]) for r in rows], "biên tập AI")
        items = [(dict(row), _ai_edit_snapshot(conn, row)) for row in rows]
    found = {item["id"] for item, _ in items}
    failed_items.extend(_ai_edit_failure(item_id, "item_missing", "Video không còn trong hàng đợi.")
                        for item_id in ids if item_id not in found)

    for item, snapshot in items:
        fields = (edit_field,) if mode == "batch_edit" else ("title", "description", "tags")
        if mode == "batch_edit":
            res = {field: item[field] or "" for field in ("title", "description", "tags")}
            res[edit_field] = apply_batch_text_edit(res[edit_field], edit_op, find_str, replace_str)
        else:
            try:
                res = ai_optimize_youtube_metadata(
                    title=item.get("title") or "", description=item.get("description") or "",
                    action=mode, provider=provider, target_lang=target_lang, custom_prompt=custom_prompt,
                )
            except SourceLinksTooLongError:
                failed_items.append(_ai_edit_failure(item["id"], "source_links_too_long", "Không thể giữ đủ link nguồn trong giới hạn mô tả."))
                continue
            except (sqlite3.Error, AssertionError):
                raise
            except Exception:
                # Provider errors can contain URLs/credentials; keep them out of API responses and logs.
                failed_items.append(_ai_edit_failure(item["id"], "provider_failed", "Nhà cung cấp không xử lý được video; kết quả lần này không được ghi."))
                continue
        try:
            if not isinstance(res, dict) or any(not isinstance(res.get(field), str) for field in ("title", "description", "tags")):
                raise ValueError("Kết quả phải có tiêu đề, mô tả và tags dạng văn bản.")
            metadata = validate_metadata_fields(res)
        except ValueError as exc:
            failed_items.append(_ai_edit_failure(item["id"], "invalid_metadata", str(exc)))
            continue
        failure = _commit_ai_edit(item, snapshot, metadata, fields)
        if failure:
            failed_items.append(failure)
            continue
        updated += 1
        if mode != "batch_edit":
            actual_provider = res.get("provider_used")
            if actual_provider not in ("gemini", "openai", "deepseek", "google_translate"):
                actual_provider = provider
            provider_counts[actual_provider] = provider_counts.get(actual_provider, 0) + 1
            fallback_count += int(bool(res.get("fallback")))
            count = res.get("links_preserved")
            links_preserved += count if type(count) is int and count >= 0 else 0
    if mode == "batch_edit":
        add_log("AI", f"Đã biên tập thủ công {updated} video; không cập nhật {len(failed_items)} video.",
                "WARNING" if failed_items else "SUCCESS")
        return {"ok": not failed_items, "updated": updated, "failed": failed_items}
    summary = ", ".join(f"{name}: {count}" for name, count in sorted(provider_counts.items())) or "không có video"
    add_log(
        "AI",
        f"Đã xử lý {updated} video; yêu cầu {provider}; nhà cung cấp thực tế [{summary}]; fallback {fallback_count}.",
        "WARNING" if fallback_count or failed_items else "SUCCESS",
    )
    return {
        "ok": not failed_items,
        "updated": updated,
        "failed": failed_items,
        "provider_requested": provider,
        "provider_counts": provider_counts,
        "fallback_count": fallback_count,
        "links_preserved": links_preserved,
    }


# ==========================================================
# ĐIỀU KHIỂN PIPELINE (DOWNLOAD -> RENDER -> UPLOAD)
# ==========================================================
def _validated_upload_receipt_and_cycle(conn, item_id: int):
    """Return (receipt, has_cycle_attempts) for the current upload cycle, or reject ambiguous evidence.

    An explicit reupload authorization begins a new cycle. Older upload IDs
    remain in the audit trail without binding a newly authorized upload.
    """
    attempts = conn.execute(
        "SELECT id,attempt_type,outcome,channel_id,evidence_json FROM upload_attempts "
        "WHERE queue_id=? ORDER BY id DESC", (item_id,),
    ).fetchall()
    boundary = next((row["id"] for row in attempts
                     if row["attempt_type"] == "manual_reupload" and row["outcome"] == "Authorized"), 0)
    receipts = []
    has_cycle_attempts = False
    for attempt in attempts:
        if attempt["id"] <= boundary:
            break
        has_cycle_attempts = True
        if attempt["evidence_json"] == "{}":
            continue  # Exact legacy/manual sentinel; malformed documents are not legacy.
        try:
            receipt = read_evidence(attempt["evidence_json"], item_id, attempt["id"], attempt["channel_id"])
        except EvidenceError:
            raise HTTPException(409, {"code": "UPLOAD_RECEIPT_INVALID", "message":
                "Bằng chứng upload không hợp lệ; giữ video để đối soát, chưa thể xác nhận đã đăng."})
        if receipt is None:
            continue
        receipts.append(receipt)
    if not receipts:
        return None, has_cycle_attempts
    if len({(row["video_id"], row["channel_id"]) for row in receipts}) != 1:
        raise HTTPException(409, {"code": "UPLOAD_RECEIPT_CONFLICT", "message":
            "Có nhiều Video ID hoặc kênh trong cùng lượt đối soát; cần kiểm tra bằng chứng trước khi xác nhận."})
    return receipts[0], has_cycle_attempts


def validated_upload_receipt(conn, item_id: int):
    return _validated_upload_receipt_and_cycle(conn, item_id)[0]


@app.post("/api/queue/{item_id}/recheck-receipt")
def api_recheck_upload_receipt(item_id: int):
    """Read-only exact-ID YouTube API recheck on the stored upload receipt."""
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT id, status, claim_token FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy video trong hàng đợi.")
        if str(row["claim_token"] or "").strip():
            raise HTTPException(409, "Video đang được worker xử lý; hãy chờ worker kết thúc.")
        receipt, _ = _validated_upload_receipt_and_cycle(conn, item_id)
        if receipt is None:
            raise HTTPException(409, {"code": "UPLOAD_RECEIPT_MISSING", "message":
                "Lượt này chưa có Video ID trong bằng chứng upload để đọc lại tự động."})
        if receipt["backend"] != "youtube_api":
            raise HTTPException(409, {"code": "RECEIPT_RECHECK_UNSUPPORTED_BACKEND", "message":
                "Đọc lại tự động qua API chỉ hỗ trợ lượt đăng bằng YouTube Data API."})
        channel_row = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (receipt["channel_id"],)).fetchone()
        if not channel_row:
            raise HTTPException(409, {"code": "UPLOAD_RECEIPT_CHANNEL_MISSING", "message":
                "Không tìm thấy kênh đích gắn với receipt này."})
        channel = dict(channel_row)
    try:
        report = recheck_api_video_receipt(channel, receipt["video_id"], receipt["expected"])
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(502, {"code": "RECEIPT_RECHECK_FAILED", "message": redact_secrets(str(exc))}) from None
    return {"ok": True, "queue_id": item_id, "receipt_channel_id": receipt["channel_id"], **report}


@app.post("/api/queue/{item_id}/manual-reupload")
def api_manual_reupload(item_id: int, data: Dict[str, Any] = Body(...)):
    """Ghi nhận quyết định đối soát hoặc cho phép đăng lại đúng một item."""
    action = str(data.get("action") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    if action not in ("reupload", "confirmed_published"):
        raise HTTPException(400, "Thao tác phải là reupload hoặc confirmed_published.")
    if len(reason) < 5 or len(reason) > 1000:
        raise HTTPException(400, "Hãy nhập lý do đối soát từ 5 đến 1000 ký tự.")

    published_url = str(data.get("published_url") or "").strip()
    if action == "confirmed_published":
        published_url = normalize_published_video_url(published_url)
    if action == "confirmed_published" and not published_url:
        raise HTTPException(400, "Cần nhập URL video YouTube hợp lệ để xác nhận đã đăng.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(404, "Không tìm thấy video trong hàng đợi.")
        item = dict(row)
        status = str(item.get("status") or "").strip()
        if str(item.get("claim_token") or "").strip() or status.startswith(("Uploading", "Đang đăng", "Rendering", "Downloading")):
            conn.rollback()
            raise HTTPException(409, "Video đang được worker xử lý; hãy chờ worker kết thúc.")

        reservations = conn.execute(
            "SELECT content_key, channel_id, state FROM content_reservations WHERE queue_id = ?",
            (item_id,),
        ).fetchall()
        if queue_requires_review(conn, item_id):
            status = "NeedsReview"
        if any(str(reservation["state"] or "") == "Active" for reservation in reservations):
            conn.rollback()
            raise HTTPException(409, "Reservation vẫn đang hoạt động; chưa thể thay đổi kết quả upload.")

        receipt, has_cycle_attempts = (
            _validated_upload_receipt_and_cycle(conn, item_id)
            if action == "confirmed_published"
            else (None, False)
        )
        if receipt and published_url != receipt["video_url"]:
            conn.rollback()
            raise HTTPException(409, {"code": "UPLOAD_RECEIPT_URL_MISMATCH", "message":
                "URL xác nhận không khớp Video ID đã nhận từ YouTube. Hãy dùng đúng URL trong bằng chứng upload."})
        if action == "confirmed_published" and item.get("status") == "Completed":
            latest = conn.execute(
                "SELECT id,attempt_type,outcome,published_url,channel_id FROM upload_attempts "
                "WHERE queue_id=? ORDER BY id DESC LIMIT 1", (item_id,),
            ).fetchone()
            if (latest and latest["attempt_type"] == "manual_review" and latest["outcome"] == "ConfirmedPublished"
                    and item.get("last_attempt_id") == latest["id"]
                    and normalize_published_video_url(item.get("published_url") or "") == published_url
                    and normalize_published_video_url(latest["published_url"] or "") == published_url
                    and (not receipt or latest["channel_id"] == receipt["channel_id"])
                    and not reservations):
                conn.rollback()
                return {"ok": True, "replayed": True, "id": item_id,
                        "message": "Quyết định xác nhận này đã được ghi nhận; không tạo thêm lượt đăng hoặc lịch sử."}
        if (action == "confirmed_published" and receipt is None and has_cycle_attempts
                and data.get("acknowledge_legacy_no_receipt") is not True):
            conn.rollback()
            raise HTTPException(400, {"code": "LEGACY_RECEIPT_ACK_REQUIRED", "message":
                "Lượt upload này chưa có receipt Video ID tự động. Hãy kiểm tra đúng video/kênh trên Studio và xác nhận rõ đối soát thủ công."})

        channel_id = next((int(r["channel_id"]) for r in reservations), None)
        if channel_id is None and item.get("target_channel_id"):
            channel_id = int(item["target_channel_id"])
        if channel_id is None and item.get("assigned_channel_id"):
            channel_id = int(item["assigned_channel_id"])
        if receipt:
            reserved_channels = {int(row["channel_id"]) for row in reservations}
            if ((reserved_channels and reserved_channels != {receipt["channel_id"]})
                    or (channel_id is not None and channel_id != receipt["channel_id"])):
                conn.rollback()
                raise HTTPException(409, {"code": "UPLOAD_RECEIPT_CHANNEL_MISMATCH", "message":
                    "Kênh đối soát không khớp kênh đã nhận video; hãy kiểm tra lại assignment và receipt."})
            channel_id = receipt["channel_id"]
        source_post_id = str(item.get("post_id") or "").strip()
        if not source_post_id:
            normalized_url = normalize_source_url(item.get("source_url"))
            source_post_id = f"url:{normalized_url}" if normalized_url else f"queue:{item_id}"
        content_key = str(item.get("content_key") or "").strip()
        historical_channels = conn.execute(
            "SELECT DISTINCT channel_id FROM published_history WHERE channel_id IS NOT NULL "
            "AND (source_post_id = ? OR (? != '' AND content_key = ?))",
            (source_post_id, content_key, content_key),
        ).fetchall()
        prior_channel_ids = {int(r["channel_id"]) for r in historical_channels}
        normalized_source_url = normalize_source_url(item.get("source_url"))
        if normalized_source_url:
            legacy_url_rows = conn.execute(
                "SELECT channel_id, source_url FROM published_history WHERE channel_id IS NOT NULL "
                "AND COALESCE(source_url, '') != ''"
            ).fetchall()
            prior_channel_ids.update(
                int(r["channel_id"])
                for r in legacy_url_rows
                if normalize_source_url(r["source_url"]) == normalized_source_url
            )
        if channel_id is None and len(prior_channel_ids) == 1:
            channel_id = next(iter(prior_channel_ids))
        if action == "reupload" and channel_id is None and len(prior_channel_ids) > 1:
            conn.rollback()
            raise HTTPException(
                409,
                "Video từng đăng lên nhiều kênh; hãy chọn một kênh cụ thể trước khi cho phép đăng lại.",
            )

        if action == "reupload":
            if channel_id is None:
                conn.rollback()
                raise HTTPException(
                    409,
                    "Không xác định được kênh đích của lượt đăng lại. Hãy gán video vào một kênh cụ thể rồi thử lại.",
                )
            if status not in ("Completed", "NeedsReview") and not (item.get("published_url") or "").strip():
                conn.rollback()
                raise HTTPException(409, "Chỉ video đã đăng hoặc cần đối soát mới có thể được cho phép đăng lại.")
            if status == "NeedsReview" and data.get("confirm_not_published") is not True:
                conn.rollback()
                raise HTTPException(400, "Video cần đối soát phải được xác nhận chưa đăng trước khi đăng lại.")
            attempt_no = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM upload_attempts WHERE queue_id = ?",
                (item_id,),
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO upload_attempts "
                "(queue_id, attempt_no, attempt_type, source_post_id, content_key, channel_id, "
                "phase, outcome, reason, published_url) "
                "VALUES (?, ?, 'manual_reupload', ?, ?, ?, 'Authorized', 'Authorized', ?, ?)",
                (
                    item_id,
                    attempt_no,
                    source_post_id,
                    content_key,
                    channel_id,
                    reason,
                    item.get("published_url") or "",
                ),
            )
            conn.execute(
                "DELETE FROM content_reservations WHERE queue_id = ? AND state = 'NeedsReview'",
                (item_id,),
            )
            conn.execute(
                "UPDATE video_queue SET status = 'Pending', published_url = '', error_message = ?, "
                "manual_reupload_authorized = 1, manual_reupload_reason = ?, last_attempt_id = ?, "
                "upload_progress = 0, target_type = CASE WHEN ? IS NOT NULL THEN 'CHANNEL' ELSE target_type END, "
                "target_channel_id = COALESCE(?, target_channel_id) "
                "WHERE id = ? AND COALESCE(claim_token, '') = ''",
                (
                    f"Đã được cho phép đăng lại theo quyết định người dùng: {reason}",
                    reason,
                    int(cur.lastrowid),
                    channel_id,
                    channel_id,
                    item_id,
                ),
            )
            conn.commit()
            return {"ok": True, "message": "Đã ghi quyết định cho item này. Chọn riêng video đó rồi chạy pipeline.", "id": item_id}

        if status != "NeedsReview":
            conn.rollback()
            raise HTTPException(409, "Chỉ video NeedsReview mới được xác nhận kết quả đã đăng.")
        if channel_id is None:
            conn.rollback()
            raise HTTPException(409, "Không xác định được kênh đã đăng; hãy giữ NeedsReview để đối soát thêm.")
        attempt_no = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM upload_attempts WHERE queue_id = ?",
            (item_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO published_history "
            "(source_post_id, source_url, channel_id, content_key, published_url) VALUES (?, ?, ?, ?, ?)",
            (source_post_id, item.get("source_url") or "", channel_id, content_key, published_url),
        )
        conn.execute(
            "UPDATE published_history SET "
            "source_url = CASE WHEN COALESCE(source_url, '') = '' THEN ? ELSE source_url END, "
            "content_key = CASE WHEN COALESCE(content_key, '') = '' THEN ? ELSE content_key END, "
            "published_url = CASE WHEN COALESCE(published_url, '') = '' THEN ? ELSE published_url END "
            "WHERE source_post_id = ? AND channel_id = ?",
            (item.get("source_url") or "", content_key, published_url, source_post_id, channel_id),
        )
        review_attempt = conn.execute(
            "INSERT INTO upload_attempts "
            "(queue_id, attempt_no, attempt_type, source_post_id, content_key, channel_id, "
            "phase, outcome, reason, published_url) "
            "VALUES (?, ?, 'manual_review', ?, ?, ?, 'Resolved', 'ConfirmedPublished', ?, ?)",
            (item_id, attempt_no, source_post_id, content_key, channel_id, reason, published_url),
        )
        conn.execute(
            "UPDATE video_queue SET status = 'Completed', published_url = ?, error_message = '', "
            "manual_reupload_authorized = 0, manual_reupload_reason = '', last_attempt_id = ? WHERE id = ?",
            (published_url, int(review_attempt.lastrowid), item_id),
        )
        conn.execute("DELETE FROM content_reservations WHERE queue_id = ?", (item_id,))
        conn.commit()
        return {"ok": True, "message": "Đã ghi nhận video đã đăng và giữ lịch sử chống trùng.", "id": item_id}
    except HTTPException:
        if conn.in_transaction:
            conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/pipeline/start")
def api_start_pipeline(data: Dict[str, Any] = Body(...)):
    raw_ids = data.get("ids") or []
    if not raw_ids:
        raise HTTPException(400, "Vui lòng chọn ít nhất 1 video để chạy.")
    try:
        ids = list(dict.fromkeys(int(x) for x in raw_ids))
    except (TypeError, ValueError):
        raise HTTPException(400, "Danh sách ID video không hợp lệ.")
    do_dl = bool(data.get("do_download", True))
    do_rd = bool(data.get("do_render", True))
    do_up = bool(data.get("do_upload", True))
    force_reupload = bool(data.get("force_reupload", False))

    conn = get_conn()
    try:
        placeholders = ",".join("?" * len(ids))
        held = [item_id for item_id in ids if queue_requires_review(conn, item_id)]
        if held:
            raise HTTPException(409, f"Video cần đối soát trước khi chạy: {held}")
        rows = conn.execute(
            f"SELECT id, title, status, published_url FROM video_queue WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in rows}

    if force_reupload:
        if any((by_id[item_id]["status"] or "").strip() == "NeedsReview" for item_id in ids if item_id in by_id):
            raise HTTPException(
                400,
                "Video cần đối soát từng item trước khi đăng lại; hãy dùng nút 🔁 hoặc ✅ ở đúng dòng video.",
            )
        raise HTTPException(
            400,
            "Không hỗ trợ bỏ qua chống trùng cho cả batch. Hãy cho phép đăng lại từng item kèm lý do.",
        )

    if do_up:
        preview_token = data.get("preview_id")
        result = pipeline_manager.start_batch(
            ids, do_dl, do_rd, do_up, preview_token=preview_token if isinstance(preview_token, str) else "",
        )
        if result.get("code") in {"PREVIEW_STALE", "REVIEW_REQUIRED"}:
            return JSONResponse({**result, "detail": result["message"]}, status_code=409)
        return result

    run_ids = []
    skipped = []
    for item_id in ids:
        row = by_id.get(item_id)
        if not row:
            skipped.append({"id": item_id, "title": "", "reason": "không còn tồn tại"})
            continue
        status = (row["status"] or "").strip()
        published = (row["published_url"] or "").strip()
        if do_up and status == "NeedsReview":
            skipped.append({"id": item_id, "title": row["title"] or f"#{item_id}", "reason": "cần đối soát YouTube trước khi chạy lại"})
            continue
        if do_up and (status == "Completed" or published):
            skipped.append({"id": item_id, "title": row["title"] or f"#{item_id}", "reason": "đã đăng trước đó"})
            continue
        if do_up and (status.startswith("Uploading") or status.startswith("Đang đăng")):
            skipped.append({"id": item_id, "title": row["title"] or f"#{item_id}", "reason": "đang/đã upload dở"})
            continue
        run_ids.append(item_id)

    if not run_ids:
        detail = "; ".join(f"{s['title'] or '#' + str(s['id'])} ({s['reason']})" for s in skipped[:5])
        raise HTTPException(
            400,
            f"Không có video nào có thể chạy. {detail}. "
            "Nếu thực sự muốn đăng lại video đã hoàn tất, hãy xác nhận 'Đăng lại' khi được hỏi.",
        )

    result = pipeline_manager.start_batch(run_ids, do_dl, do_rd, do_up, force_reupload=False)
    if result.get("code") == "REVIEW_REQUIRED":
        return JSONResponse({**result, "detail": result["message"]}, status_code=409)
    if skipped:
        result["skipped"] = skipped
        result["message"] = (result.get("message") or "") + f" Bỏ qua {len(skipped)} video đã đăng/đang upload dở."
    return result


@app.post("/api/queue/preview-distribution")
def api_preview_distribution(data: Dict[str, Any] = Body(...)):
    raw_ids = data.get("ids") or []
    if not raw_ids:
        raise HTTPException(400, "Chọn ít nhất một video để xem trước phân phối.")
    try:
        ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        raise HTTPException(400, "Danh sách ID video không hợp lệ.")
    preview = pipeline_manager.preview_distribution(ids, [
        bool(data.get(key, True)) for key in ("do_download", "do_render", "do_upload")
    ])
    items = preview["items"]
    blocked = sum(bool(item.get("blocked")) for item in items)
    warnings = sum(len(item.get("warnings") or []) for item in items)
    return {
        "ok": blocked == 0,
        "items": items,
        "preview_id": preview["preview_token"],
        "total": len(items),
        "blocked": blocked,
        "warning_count": warnings,
        "assignment_persists_on_retry": True,
    }


@app.get("/api/pipeline/upload-slot")
def api_upload_slot_status():
    return upload_slot_recovery_status()


@app.post("/api/pipeline/upload-slot/acknowledge")
def api_acknowledge_upload_slot(data: Dict[str, Any] = Body(...)):
    if data.get("previous_session_closed") is not True or not isinstance(data.get("recovery_id"), str):
        raise HTTPException(400, "Hãy xác nhận phiên upload cũ đã kết thúc.")
    if not acknowledge_upload_slot_recovery(data["recovery_id"]):
        raise HTTPException(409, "Trạng thái lượt upload đã thay đổi. Hãy kiểm tra lại.")
    return {"ok": True}


@app.post("/api/pipeline/stop")
def api_stop_pipeline():
    return {"ok": True, "batch_state": pipeline_manager.stop_all()}


# ==========================================================
# QUẢN LÝ PROFILES CHROME & KÊNH YOUTUBE PHÂN CẤP
# ==========================================================
@app.get("/api/antidetect/status")
def api_antidetect_status():
    return AntidetectClient().status()


def sync_antidetect_studio_channels(profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Đồng bộ Antidetect với YouTube Studio: mở TỪNG Profile Antidetect qua lease CDP,
    đọc trạng thái đăng nhập Google + danh sách kênh thật trong Studio (email, handle,
    Channel ID) rồi đưa về youtube_channels — đúng luồng 'refresh-channels' của Profile
    Chrome local. Chạy tuần tự; mỗi profile được ghi log để theo dõi tiến độ ở tab Nhật Ký.
    Không đụng vào fingerprint/proxy — Antidetect vẫn là chủ sở hữu duy nhất của trình duyệt.
    """
    results: List[Dict[str, Any]] = []
    added_total = updated_total = 0
    logged_in_count = 0

    for prof in profiles or []:
        name = str(prof.get("name") or "").strip()
        ad_id = str(prof.get("antidetect_profile_id") or "").strip()
        entry = {
            "name": name, "antidetect_profile_id": ad_id, "logged_in": False,
            "channels": 0, "added": 0, "updated": 0, "error": "",
        }
        if not name or not ad_id:
            entry["error"] = "thiếu tên Profile hoặc ID liên kết Antidetect"
            results.append(entry)
            continue

        add_log("Antidetect", f"[{name}] Đang mở profile qua Antidetect để quét YouTube Studio...", "INFO")
        try:
            probe = probe_profile_login(
                f"antidetect://{ad_id}",
                "",
                grab_channels=True,
                antidetect_profile_id=ad_id,
            )
        except Exception as exc:
            entry["error"] = str(exc)[:300]
            add_log("Antidetect", f"[{name}] Lỗi quét Studio: {exc}", "ERROR")
            results.append(entry)
            continue

        entry["logged_in"] = bool(probe.get("logged_in"))
        new_status = "Đã đăng nhập" if entry["logged_in"] else "Chưa đăng nhập"
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE chrome_profiles SET login_status = ?, last_login_check = ? WHERE antidetect_profile_id = ?",
                (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ad_id),
            )
            conn.execute(
                "UPDATE youtube_channels SET status = ? WHERE profile_name = ?",
                (new_status, name),
            )
            if entry["logged_in"]:
                found = probe.get("channels") or []
                prof_row = conn.execute(
                    "SELECT path, proxy FROM chrome_profiles WHERE antidetect_profile_id = ?",
                    (ad_id,),
                ).fetchone()
                prof_path = ((prof_row["path"] if prof_row else "") or "").strip() or f"antidetect://{ad_id}"
                prof_proxy = (prof_row["proxy"] if prof_row else "") or ""
                added, updated = _upsert_profile_channels(conn, name, prof_path, prof_proxy, found, new_status)
                entry["added"] = added
                entry["updated"] = updated
                entry["channels"] = len(found)
                added_total += added
                updated_total += updated
            conn.commit()
        finally:
            conn.close()

        if entry["logged_in"]:
            logged_in_count += 1
            add_log(
                "Antidetect",
                f"[{name}] {probe.get('message', 'Đã đăng nhập')} → "
                f"+{entry['added']} kênh mới, cập nhật {entry['updated']} kênh.",
                "SUCCESS",
            )
        else:
            add_log("Antidetect", f"[{name}] {probe.get('message', 'Chưa đăng nhập Google.')}", "WARNING")
        results.append(entry)

    return {
        "scanned": len(results),
        "logged_in": logged_in_count,
        "channels_added": added_total,
        "channels_updated": updated_total,
        "profiles": results,
    }


@app.post("/api/antidetect/sync")
def api_antidetect_sync(payload: Optional[Dict[str, Any]] = Body(None)):
    """Kéo danh sách Profile từ Antidetect về chrome_profiles (không lấy mật khẩu proxy).
    Truyền {"scan_channels": true} để quét tiếp YouTube Studio trong từng profile
    (đăng nhập Google + kênh YouTube) và đồng bộ về youtube_channels."""
    client = AntidetectClient()
    try:
        remote = client.list_profiles()
    except AntidetectError as e:
        add_log("Antidetect", f"Không đồng bộ được Profile: {e}", "ERROR")
        raise HTTPException(503, str(e))
    result = sync_antidetect_profiles(remote)
    add_log(
        "Antidetect",
        f"Đồng bộ Profile Antidetect: thêm {result['added']}, cập nhật {result['updated']}, "
        f"bỏ qua {result['skipped']}.",
        "SUCCESS",
    )
    if isinstance(payload, dict) and payload.get("scan_channels"):
        result["studio"] = sync_antidetect_studio_channels(result.get("profiles") or [])
    return result


@app.get("/api/profiles")
def api_list_profiles():
    return get_all_profiles()


@app.get("/api/profiles/{profile_name:path}/channels")
def api_get_profile_channels(profile_name: str):
    return get_channels_by_profile(profile_name)


def _validated_crud_input(schema, data, *, partial=False):
    try:
        return parse_config_input(schema, data, partial=partial)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _binding_references(conn, channel_ids, profile_name=''):
    try:
        return binding_references(conn, channel_ids, profile_name)
    except ValueError as exc:
        raise HTTPException(409, 'Liên kết nhóm không hợp lệ. Hãy kiểm tra đích trong hàng đợi trước.') from exc


def _ensure_binding_deletable(conn, channel_ids, profile_name=''):
    queue_ids, monitors, evidence = _binding_references(conn, channel_ids, profile_name)
    if queue_ids or monitors or evidence:
        details = []
        if queue_ids:
            details.append('video ' + ', '.join(f'#{i}' for i in queue_ids[:5]))
        if monitors:
            details.append('Monitor ' + ', '.join(f"#{m['id']}" for m in monitors[:5]))
        if evidence:
            details.append('lịch sử đăng hoặc lượt duyệt')
        raise HTTPException(409, 'Không thể xóa vì còn liên kết: ' + '; '.join(details) + '. Hãy đối soát hoặc đổi đích trước.')


@app.post('/api/profiles')
def api_create_profile(data: Dict[str, Any] = Body(...)):
    values = _validated_crud_input(ProfileCreate, data)
    name, proxy, note = values['name'], values['proxy'], values['note']
    from config import APP_PROFILES_DIR
    root = APP_PROFILES_DIR.resolve()
    candidate = root / name
    if candidate.resolve().parent != root:
        raise HTTPException(400, 'Thư mục Profile phải nằm trực tiếp trong thư mục Profile của ứng dụng.')
    created_directory = False
    try:
        with closing(get_conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            known_names = [r[0] for r in conn.execute('SELECT name FROM chrome_profiles UNION SELECT profile_name FROM youtube_channels')]
            if any(str(known).casefold() == name.casefold() for known in known_names):
                raise HTTPException(409, 'Tên Profile đã có cấu hình hoặc kênh liên kết. Hãy dùng chức năng sửa hoặc chọn tên khác.')
            known_paths = [r[0] for r in conn.execute('SELECT path FROM chrome_profiles UNION SELECT profile_path FROM youtube_channels')]
            if any(path and not str(path).startswith('antidetect://') and
                   str(Path(path).resolve()).casefold() == str(candidate).casefold() for path in known_paths):
                raise HTTPException(409, 'Thư mục này đã có Profile hoặc kênh sử dụng.')
            root.mkdir(parents=True, exist_ok=True)
            if any(child.name.casefold() == name.casefold() for child in root.iterdir()):
                raise HTTPException(409, 'Thư mục Profile đã tồn tại trên đĩa. Hãy chọn tên khác để bảo toàn phiên Chrome cũ.')
            try:
                candidate.mkdir()
            except FileExistsError as exc:
                raise HTTPException(409, 'Thư mục Profile đã tồn tại trên đĩa.') from exc
            created_directory = True
            conn.execute("INSERT INTO chrome_profiles(name,path,proxy,source,note,antidetect_profile_id) VALUES(?,?,?,'Local',?,'')",
                         (name, str(candidate), proxy, note))
            cursor = conn.execute("""
                INSERT INTO youtube_channels(name,handle,group_name,upload_mode,profile_name,profile_path,proxy,emulate_mobile,status,note)
                VALUES(?,?,'Mặc định','playwright',?,?,?,0,'Ready','Kênh chính theo Profile')
                """, (f'YT - {name}', f"@{name.lower().replace(' ', '')}", name, str(candidate), proxy))
            set_channel_group_memberships(conn, int(cursor.lastrowid), ['Mặc định'])
    except BaseException:
        if created_directory:
            try:
                candidate.rmdir()  # Only the empty directory created by this request.
            except OSError:
                pass
        raise
    return {'ok': True, 'name': name, 'path': str(candidate)}


@app.put('/api/profiles/{profile_name:path}')
def api_update_profile(profile_name: str, data: Dict[str, Any] = Body(...)):
    values = _validated_crud_input(ProfileUpdate, data, partial=True)
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM chrome_profiles WHERE name=?', (profile_name,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Không tìm thấy Profile Chrome này.')
        is_antidetect = str(row['source'] or '').casefold() == 'antidetect' or bool(row['antidetect_profile_id'])
        if 'proxy' in values and is_antidetect:
            raise HTTPException(400, 'Proxy của Profile Antidetect do Antidetect quản lý — hãy sửa trong Antidetect.')
        if 'proxy' in values and '***' not in values['proxy']:
            channel_ids = [r[0] for r in conn.execute('SELECT id FROM youtube_channels WHERE profile_name=?', (profile_name,))]
            queue_ids, monitors, evidence = _binding_references(conn, channel_ids, profile_name)
            _ensure_queue_items_idle(conn, queue_ids, 'đổi proxy Profile')
            if 'content_reservations' in evidence:
                raise HTTPException(409, 'Profile còn reservation cần đối soát trước khi đổi proxy.')
            if any(m['is_running'] or pipeline_manager._monitor_handles_alive(m['id']) for m in monitors):
                raise HTTPException(409, 'Hãy dừng Monitor đang sử dụng Profile trước khi đổi proxy.')
            conn.execute('UPDATE chrome_profiles SET proxy=? WHERE name=?', (values['proxy'], profile_name))
            conn.execute('UPDATE youtube_channels SET proxy=? WHERE profile_name=?', (values['proxy'], profile_name))
        if 'note' in values:
            conn.execute('UPDATE chrome_profiles SET note=? WHERE name=?', (values['note'], profile_name))
    add_log('Profile', f"Đã cập nhật cấu hình Profile Chrome '{profile_name}'.", 'INFO')
    return {'ok': True}


@app.delete('/api/profiles/{profile_name:path}')
def api_delete_profile(profile_name: str):
    """Delete unused DB bindings atomically; retain all Chrome files."""
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM chrome_profiles WHERE name=?', (profile_name,)).fetchone():
            raise HTTPException(404, 'Không tìm thấy Profile Chrome này.')
        channel_ids = [r[0] for r in conn.execute('SELECT id FROM youtube_channels WHERE profile_name=?', (profile_name,))]
        _ensure_binding_deletable(conn, channel_ids, profile_name)
        conn.execute('DELETE FROM channel_group_memberships WHERE channel_id IN (SELECT id FROM youtube_channels WHERE profile_name=?)', (profile_name,))
        conn.execute('DELETE FROM youtube_channels WHERE profile_name=?', (profile_name,))
        conn.execute('DELETE FROM chrome_profiles WHERE name=?', (profile_name,))
    add_log('Profile', f"Đã xóa Profile Chrome '{profile_name}' khỏi hệ thống (giữ nguyên thư mục trên đĩa).", 'WARNING')
    return {'ok': True}


@app.post("/api/profiles/{profile_name:path}/open-browser")
def api_open_profile_browser(profile_name: str):
    prof = get_profile_binding(profile_name)
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    ch_like = {
        "id": 0,
        "name": f"Profile {profile_name}",
        "profile_name": profile_name,
        "profile_path": prof["path"],
        "proxy": prof["proxy"] or "",
        "source": prof.get("source") or "",
        "antidetect_profile_id": prof.get("antidetect_profile_id") or "",
    }
    try:
        return open_profile_for_login(ch_like)
    except Exception as e:
        add_log("Profile", f"Lỗi mở Profile '{profile_name}': {e}", "ERROR")
        raise HTTPException(500, f"Lỗi khi mở Profile: {e}")


def _check_profile_probe_binding(conn, profile_name, before):
    current = conn.execute('SELECT * FROM chrome_profiles WHERE name=?', (profile_name,)).fetchone()
    fields = ('path', 'proxy', 'source', 'antidetect_profile_id')
    if current is None or any((current[key] or '') != (before.get(key) or '') for key in fields):
        raise HTTPException(409, 'Profile đã bị xóa hoặc thay đổi trong lúc kiểm tra. Hãy tải lại và thử lại.')


@app.post("/api/profiles/{profile_name:path}/check-login")
def api_check_profile_login(profile_name: str):
    """Mở Chrome Profile ở vị trí khuất để kiểm tra phiên đăng nhập Google (kèm Proxy nếu có)."""
    prof = get_profile_binding(profile_name)
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    try:
        probe = probe_profile_login(
            prof["path"],
            prof["proxy"] or "",
            grab_channels=True,
            antidetect_profile_id=prof.get("antidetect_profile_id") or "",
        )
    except Exception as e:
        add_log("Profile", f"Lỗi kiểm tra đăng nhập Profile '{profile_name}': {e}", "ERROR")
        raise HTTPException(500, f"Lỗi khi mở Profile để kiểm tra: {e}")

    new_status = "Đã đăng nhập" if probe.get("logged_in") else "Chưa đăng nhập"
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        _check_profile_probe_binding(conn, profile_name, prof)
        conn.execute(
            "UPDATE chrome_profiles SET login_status = ?, last_login_check = ? WHERE name = ?",
            (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile_name),
        )
        conn.execute("UPDATE youtube_channels SET status = ? WHERE profile_name = ?", (new_status, profile_name))
    add_log(
        "Profile",
        f"Kiểm tra đăng nhập Profile '{profile_name}': {new_status}. {probe.get('message', '')}",
        "SUCCESS" if probe.get("logged_in") else "WARNING",
    )
    return {"ok": True, **probe, "login_status": new_status}


def _upsert_profile_channels(
    conn,
    profile_name: str,
    prof_path: str,
    prof_proxy: str,
    found: List[Dict[str, Any]],
    new_status: str,
) -> tuple:
    """Đồng bộ danh sách kênh quét được từ YouTube Studio của một Profile vào
    youtube_channels: khớp theo channel_id → handle → tên; trả về (added, updated).
    Dùng chung cho refresh-channels (Profile Chrome) và sync Antidetect."""
    current = conn.execute('SELECT path,proxy FROM chrome_profiles WHERE name=?', (profile_name,)).fetchone()
    if current is None or (current['path'] or '') != prof_path or (current['proxy'] or '') != prof_proxy:
        raise HTTPException(409, 'Liên kết Profile đã thay đổi. Hãy quét lại danh sách kênh.')
    added, updated = 0, 0
    for ch in found or []:
        handle = (ch.get("handle") or "").strip()
        ch_name = (ch.get("name") or "").strip() or handle
        email = (ch.get("email") or "").strip()
        channel_id = (ch.get("channel_id") or "").strip()
        if not ch_name and not handle and not channel_id:
            continue
        existing = None
        if channel_id:
            existing = conn.execute(
                "SELECT id FROM youtube_channels WHERE profile_name = ? AND channel_id = ?",
                (profile_name, channel_id),
            ).fetchone()
        if not existing and handle:
            existing = conn.execute(
                "SELECT id FROM youtube_channels WHERE profile_name = ? AND handle = ?",
                (profile_name, handle),
            ).fetchone()
        if not existing and ch_name:
            existing = conn.execute(
                "SELECT id FROM youtube_channels WHERE profile_name = ? AND name = ?",
                (profile_name, ch_name),
            ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE youtube_channels
                SET name = ?,
                    handle = COALESCE(NULLIF(?, ''), handle),
                    email = COALESCE(NULLIF(?, ''), email),
                    channel_id = COALESCE(NULLIF(?, ''), channel_id),
                    status = ?
                WHERE id = ?
                """,
                (ch_name, handle, email, channel_id, new_status, existing["id"]),
            )
            updated += 1
        else:
            cur = conn.execute(
                """
                INSERT INTO youtube_channels (
                    name, handle, email, channel_id, group_name, upload_mode, profile_name,
                    profile_path, proxy, emulate_mobile, status, note
                ) VALUES (?, ?, ?, ?, 'Mặc định', 'playwright', ?, ?, ?, 0, ?, 'Kênh quét tự động từ YouTube Studio')
                """,
                (ch_name, handle, email, channel_id, profile_name, prof_path, prof_proxy, new_status),
            )
            set_channel_group_memberships(conn, int(cur.lastrowid), ["Mặc định"])
            added += 1
    return added, updated


@app.post("/api/profiles/{profile_name:path}/refresh-channels")
def api_refresh_profile_channels(profile_name: str):
    """Quét danh sách kênh YouTube thật trong Chrome Profile và cập nhật vào bảng youtube_channels."""
    prof = get_profile_binding(profile_name)
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    try:
        probe = probe_profile_login(
            prof["path"],
            prof["proxy"] or "",
            grab_channels=True,
            antidetect_profile_id=prof.get("antidetect_profile_id") or "",
        )
    except Exception as e:
        add_log("Profile", f"Lỗi quét kênh Profile '{profile_name}': {e}", "ERROR")
        raise HTTPException(500, f"Lỗi khi mở Profile để quét kênh: {e}")

    prof_path = prof["path"]
    prof_proxy = prof["proxy"] or ""
    found = probe.get("channels") or []
    new_status = "Đã đăng nhập" if probe.get("logged_in") else "Chưa đăng nhập"

    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        _check_profile_probe_binding(conn, profile_name, prof)
        conn.execute(
            "UPDATE chrome_profiles SET login_status = ?, last_login_check = ? WHERE name = ?",
            (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile_name),
        )
        conn.execute("UPDATE youtube_channels SET status = ? WHERE profile_name = ?", (new_status, profile_name))

        added, updated = _upsert_profile_channels(conn, profile_name, prof_path, prof_proxy, found, new_status)
    add_log(
        "Profile",
        f"Quét kênh Profile '{profile_name}': {new_status}. Thêm mới {added} kênh, cập nhật {updated} kênh.",
        "SUCCESS" if probe.get("logged_in") else "WARNING",
    )
    return {
        "ok": True,
        "logged_in": probe.get("logged_in", False),
        "email": probe.get("email", ""),
        "channel_id": probe.get("channel_id", ""),
        "message": probe.get("message", ""),
        "added": added,
        "updated": updated,
        "channels": found,
        "login_status": new_status,
    }


@app.get("/api/channels")
def api_list_channels(profile: Optional[str] = None):
    conn = get_conn()
    if profile:
        rows = conn.execute("SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id DESC", (profile,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM youtube_channels ORDER BY id DESC").fetchall()
    ids = [int(row["id"]) for row in rows]
    group_map: Dict[int, List[str]] = {channel_id: [] for channel_id in ids}
    if ids:
        marks = ",".join("?" for _ in ids)
        try:
            membership_rows = conn.execute(
                f"SELECT channel_id, group_name FROM channel_group_memberships "
                f"WHERE channel_id IN ({marks}) ORDER BY sort_order ASC, group_name ASC",
                ids,
            ).fetchall()
        except Exception:
            membership_rows = []
        for membership in membership_rows:
            names = group_map.get(int(membership["channel_id"]))
            if names is not None and membership["group_name"] not in names:
                names.append(str(membership["group_name"]))
    channels = []
    for row in rows:
        channel = public_channel(row)
        channel['metadata_revision'] = metadata_revision(row, 'channel')
        channel["group_names"] = group_map.get(int(row["id"])) or [str(row["group_name"] or "Mặc định")]
        channels.append(channel)
    conn.close()
    return channels


@app.post("/api/channels")
def api_create_channel(data: Dict[str, Any] = Body(...)):
    try:
        supplied_fields = set(data)
        data = validate_config(data, 'channel', create=True)
        data = prepare_channel_update({}, data, supplied_fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    name = data['name']
    prof_name = (data.get("profile_name") or name).strip()
    prof_path = (data.get("profile_path") or "").strip()

    binding = get_profile_binding(prof_name) if prof_name else None
    is_antidetect = bool(
        binding
        and (
            str(binding.get("source") or "") == "Antidetect"
            or str(binding.get("antidetect_profile_id") or "").strip()
        )
    )
    upload_mode = (data.get("upload_mode") or "playwright").strip()
    if upload_mode not in ("playwright", "youtube_api"):
        raise HTTPException(400, "Phương thức đăng không hợp lệ.")
    if is_antidetect:
        if not str(binding.get("antidetect_profile_id") or "").strip():
            raise HTTPException(
                400,
                "Profile Antidetect này thiếu ID liên kết. Hãy bấm 'Đồng Bộ Antidetect' rồi thử lại.",
            )
        prof_path = binding.get("path") or f"antidetect://{binding['antidetect_profile_id']}"

    if not prof_path:
        from config import DHB_PROFILE_CHROME_DIR, APP_PROFILES_DIR
        candidate = DHB_PROFILE_CHROME_DIR / prof_name
        prof_path = str(candidate) if candidate.exists() else str(APP_PROFILES_DIR / prof_name)

    if upload_mode == 'youtube_api':
        _validate_channel_oauth(data)
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        group_names = _validated_group_names(
            conn, data.pop('group_names'), legacy=data.get('group_name'), default='Mặc định'
        )
        data.update(group_name=group_names[0], profile_name=prof_name, profile_path=prof_path)
        if is_antidetect:
            data.update(emulate_mobile=0, proxy='')
        columns = ', '.join(data)
        placeholders = ', '.join('?' for _ in data)
        cur = conn.execute(f'INSERT INTO youtube_channels ({columns}) VALUES ({placeholders})', list(data.values()))
        set_channel_group_memberships(conn, int(cur.lastrowid), group_names)
    add_log("Channel", f"Đã thêm kênh YouTube: {name}", "SUCCESS", channel_name=name)
    return {"ok": True}


def _validate_channel_oauth(values):
    try:
        oauth = get_oauth_token_local_status(values)
    except (ValueError, TypeError, OverflowError, OSError):
        raise HTTPException(400, 'Cấu hình OAuth không hợp lệ. Kiểm tra token và thời gian hết hạn.') from None
    if not oauth['configured'] or oauth['error']:
        raise HTTPException(400, oauth['error'] or 'Kênh chưa có OAuth hợp lệ.')


@app.put("/api/channels/{channel_id}")
def api_update_channel(channel_id: int, data: Dict[str, Any] = Body(...)):
    """Cập nhật thông tin kênh YouTube (tên, handle, nhóm, proxy, chế độ đăng, cấu hình mặc định...)."""
    try:
        updates = validate_config(data, 'channel')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if 'proxy' in updates and '***' in updates['proxy']:
        del updates['proxy']  # Masked GET value means keep the stored password.
    if 'oauth_token_json' in updates and not updates['oauth_token_json']:
        del updates['oauth_token_json']  # An empty editor never erases stored OAuth.

    if not updates:
        return {"ok": False, "message": "Không có trường hợp lệ để cập nhật."}

    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy kênh YouTube này.")
        if ('expected_metadata_revision' in data
                and data['expected_metadata_revision'] != metadata_revision(row, 'channel')):
            raise HTTPException(409, 'Metadata của kênh đã đổi. Tải lại trước khi lưu để không ghi đè lựa chọn mới.')
        try:
            updates = prepare_channel_update(dict(row), updates, set(data))
        except MetadataIntentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        final = {**dict(row), **updates}
        if final['upload_mode'] == 'youtube_api' and {'upload_mode','oauth_token_json'} & updates.keys():
            _validate_channel_oauth(final)

        group_names = None
        if 'group_names' in updates or 'group_name' in updates:
            raw_groups = updates.pop('group_names', updates.get('group_name'))
            group_names = _validated_group_names(
                conn, raw_groups, legacy=updates.get('group_name', row['group_name']) or '', default='Mặc định',
            )
            updates['group_name'] = group_names[0]

        # Đổi Profile: lấy path của binding khi không nhập đường dẫn mới.
        if 'profile_name' in updates and not updates.get('profile_path'):
            if not updates['profile_name']:
                raise HTTPException(400, 'Tên Profile Chrome không được để trống.')
            prof_row = conn.execute('SELECT path FROM chrome_profiles WHERE name=?', (updates['profile_name'],)).fetchone()
            if prof_row and prof_row['path']:
                updates['profile_path'] = prof_row['path']

        final_prof_name = updates.get('profile_name', row['profile_name'] or '')
        prof_binding = conn.execute('SELECT * FROM chrome_profiles WHERE name=?', (final_prof_name,)).fetchone() if final_prof_name else None
        if prof_binding and (str(prof_binding['source'] or '') == 'Antidetect' or str(prof_binding['antidetect_profile_id'] or '').strip()):
            ad_binding_id = str(prof_binding['antidetect_profile_id'] or '').strip()
            if not ad_binding_id:
                raise HTTPException(400, "Profile Antidetect này thiếu ID liên kết. Hãy bấm 'Đồng Bộ Antidetect' rồi thử lại.")
            updates.update(profile_path=prof_binding['path'] or f'antidetect://{ad_binding_id}', proxy='', emulate_mobile=0)

        cols = ', '.join(f'{key} = ?' for key in updates)
        conn.execute(f'UPDATE youtube_channels SET {cols} WHERE id = ?', [*updates.values(), channel_id])
        if group_names is not None:
            set_channel_group_memberships(conn, channel_id, group_names)
    add_log("Channel", f"Đã cập nhật thông tin kênh YouTube #{channel_id}.", "INFO")
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
def api_delete_channel(channel_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        channel = conn.execute('SELECT profile_name FROM youtube_channels WHERE id=?', (channel_id,)).fetchone()
        if channel is None:
            raise HTTPException(404, 'Không tìm thấy kênh.')
        _ensure_binding_deletable(conn, [channel_id], channel['profile_name'] or '')
        conn.execute('DELETE FROM channel_group_memberships WHERE channel_id=?', (channel_id,))
        conn.execute('DELETE FROM youtube_channels WHERE id=?', (channel_id,))
    return {"ok": True}


@app.post("/api/channels/{channel_id}/open-browser")
def api_open_channel_browser(channel_id: int):
    conn = get_conn()
    ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    conn.close()
    if not ch:
        raise HTTPException(404, "Không tìm thấy kênh.")
    channel_data = dict(ch)
    prof = get_profile_binding(channel_data.get("profile_name") or "")
    if prof:
        channel_data["source"] = prof.get("source") or ""
        channel_data["antidetect_profile_id"] = prof.get("antidetect_profile_id") or ""
        if str(prof.get("source") or "") == "Antidetect":
            channel_data["profile_path"] = prof.get("path") or ""
    try:
        return open_profile_for_login(channel_data)
    except Exception as e:
        add_log("Channel", f"Lỗi mở Profile cho kênh #{channel_id}: {e}", "ERROR", channel_name=ch["name"])
        raise HTTPException(500, f"Lỗi khi mở Profile: {e}")


@app.get("/api/channels/{channel_id}/oauth-status")
def api_channel_oauth_status(channel_id: int):
    """Trạng thái OAuth token cục bộ (không gọi mạng): có refresh token, hạn access token..."""
    conn = get_conn()
    ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    conn.close()
    if not ch:
        raise HTTPException(404, "Không tìm thấy kênh.")
    return get_oauth_token_local_status(dict(ch))


@app.post("/api/channels/{channel_id}/check-oauth")
def api_check_channel_oauth(channel_id: int):
    """Làm mới token thật và xác minh kênh YouTube mà token đang đại diện (gọi mạng Google)."""
    conn = get_conn()
    ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    conn.close()
    if not ch:
        raise HTTPException(404, "Không tìm thấy kênh.")
    result = check_oauth_token_status(dict(ch))
    if result.get("ok"):
        add_log(
            "Channel",
            f"OAuth token kênh #{channel_id} hợp lệ — đại diện '{result['identity']['name']}' "
            f"({result['identity']['channel_id']}), khớp kênh đích: {'CÓ' if result.get('matched') else 'KHÔNG'}.",
            "SUCCESS" if result.get("matched") else "WARNING",
            channel_name=ch["name"],
        )
    else:
        add_log("Channel", f"OAuth token kênh #{channel_id} lỗi: {result.get('error')}", "ERROR", channel_name=ch["name"])
    return result


def _find_same_profile_oauth_client(conn, profile_name: str, prefer_channel_id: int) -> Optional[Dict[str, Any]]:
    prof = str(profile_name or "").strip()
    if not prof:
        return None
    rows = conn.execute(
        "SELECT id, name, oauth_token_json FROM youtube_channels "
        "WHERE profile_name = ? AND COALESCE(oauth_token_json, '') != '' "
        "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id ASC",
        (prof, prefer_channel_id),
    ).fetchall()
    for row in rows:
        try:
            parsed = json.loads(row["oauth_token_json"] or "{}")
        except Exception:
            continue
        cid = str(parsed.get("client_id") or "").strip()
        csec = str(parsed.get("client_secret") or "").strip()
        if cid.endswith(".apps.googleusercontent.com") and csec:
            return {
                "source_id": int(row["id"]),
                "source_name": str(row["name"] or ""),
                "client_id": cid,
                "client_secret": csec,
            }
    return None


@app.get("/api/channels/{channel_id}/oauth-setup-info")
def api_channel_oauth_setup_info(channel_id: int):
    from connect_youtube_oauth import extract_antidetect_profile_id

    with closing(get_conn()) as conn:
        ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
        if not ch:
            raise HTTPException(404, "Không tìm thấy kênh.")
        channel_data = dict(ch)
        profile_name = str(channel_data.get("profile_name") or "").strip()
        same_prof = _find_same_profile_oauth_client(conn, profile_name, channel_id)
    prof = get_profile_binding(profile_name) if profile_name else None
    ad_id = extract_antidetect_profile_id(channel_data, prof)
    local_status = get_oauth_token_local_status(channel_data)
    return {
        "ok": True,
        "channel_id": channel_id,
        "channel_name": channel_data.get("name") or "",
        "youtube_channel_id": channel_data.get("channel_id") or "",
        "profile_name": profile_name,
        "antidetect_bound": bool(ad_id),
        "upload_mode": channel_data.get("upload_mode") or "playwright",
        "already_configured": bool(local_status.get("configured") and not local_status.get("error")),
        "same_profile_client_available": same_prof is not None,
        "same_profile_source_id": same_prof["source_id"] if same_prof else None,
        "same_profile_source_name": same_prof["source_name"] if same_prof else "",
    }


@app.post("/api/channels/{channel_id}/open-gcloud-console")
def api_open_channel_gcloud_console(channel_id: int):
    from connect_youtube_oauth import GCLOUD_YOUTUBE_API_URL, open_url_in_channel_browser

    with closing(get_conn()) as conn:
        ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch:
        raise HTTPException(404, "Không tìm thấy kênh.")
    channel_data = dict(ch)
    profile_name = str(channel_data.get("profile_name") or "").strip()
    prof = get_profile_binding(profile_name) if profile_name else None
    try:
        opened = open_url_in_channel_browser(channel_data, GCLOUD_YOUTUBE_API_URL, prof)
        add_log(
            "Channel",
            f"Đã mở Google Cloud Console trong Profile '{profile_name}' cho kênh #{channel_id}.",
            "INFO",
            channel_name=channel_data.get("name") or "",
        )
        return {"ok": True, "browser": opened.get("browser", ""), "profile_name": profile_name}
    except Exception as exc:
        raise HTTPException(400, f"Không mở được Google Cloud Console trong Profile: {exc}") from exc


@app.post("/api/channels/{channel_id}/connect-oauth")
def api_connect_channel_oauth(channel_id: int, data: Dict[str, Any] = Body(...)):
    from connect_youtube_oauth import (
        authorized_credentials,
        open_url_in_channel_browser,
        parse_desktop_client_json,
    )
    from youtube_uploader import _http_session_for_channel

    activate = data.get("activate", True) is not False
    use_same_profile = data.get("use_same_profile_client") is True
    raw_client_json = str(data.get("client_json") or "").strip()

    with closing(get_conn()) as conn:
        ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
        if not ch:
            raise HTTPException(404, "Không tìm thấy kênh.")
        channel_data = dict(ch)
        yt_cid = str(channel_data.get("channel_id") or "").strip()
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", yt_cid):
            raise HTTPException(400, "Kênh chưa có YouTube Channel ID (UC...) hợp lệ. Hãy quét kênh từ Profile trước.")
        dup_count = conn.execute(
            "SELECT COUNT(*) FROM youtube_channels WHERE channel_id = ?", (yt_cid,)
        ).fetchone()[0]
        if dup_count != 1:
            raise HTTPException(409, "Channel ID này đang bị trùng nhiều dòng trong DHB. Hãy giữ đúng 1 dòng trước khi kết nối.")

        profile_name = str(channel_data.get("profile_name") or "").strip()
        if use_same_profile:
            same_prof = _find_same_profile_oauth_client(conn, profile_name, channel_id)
            if not same_prof:
                raise HTTPException(400, "Profile này chưa có sẵn OAuth Client. Hãy chọn file JSON Desktop app từ Google Cloud.")
            client_id, client_secret = same_prof["client_id"], same_prof["client_secret"]
        else:
            if not raw_client_json:
                raise HTTPException(400, "Vui lòng chọn file OAuth Client JSON (Desktop app) tải về từ Google Cloud.")
            try:
                client_id, client_secret = parse_desktop_client_json(raw_client_json)
            except RuntimeError as exc:
                raise HTTPException(400, str(exc)) from exc

    prof = get_profile_binding(profile_name) if profile_name else None
    http = _http_session_for_channel(channel_data)
    try:
        creds, identity = authorized_credentials(
            http,
            client_id,
            client_secret,
            yt_cid,
            open_url=lambda url: open_url_in_channel_browser(channel_data, url, prof),
            timeout_seconds=240,
            return_identity=True,
        )
    except RuntimeError as exc:
        add_log("Channel", f"Kết nối OAuth kênh #{channel_id} chưa hoàn tất: {exc}", "WARNING", channel_name=channel_data.get("name") or "")
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        add_log("Channel", f"Lỗi mạng khi kết nối OAuth kênh #{channel_id}: {exc}", "ERROR", channel_name=channel_data.get("name") or "")
        raise HTTPException(502, "Kết nối mạng tới Google hoặc trình duyệt Antidetect thất bại.") from exc
    finally:
        close_fn = getattr(http, "close", None)
        if callable(close_fn):
            close_fn()

    update_payload: Dict[str, Any] = {"oauth_token_json": json.dumps(creds)}
    if activate:
        update_payload["upload_mode"] = "youtube_api"
    api_update_channel(channel_id, update_payload)
    add_log(
        "Channel",
        f"Đã kết nối OAuth thành công cho kênh #{channel_id} '{identity['name']}' ({yt_cid})"
        + (" và chuyển sang YouTube Data API." if activate else "."),
        "SUCCESS",
        channel_name=channel_data.get("name") or "",
    )
    return {
        "ok": True,
        "channel_id": channel_id,
        "youtube_channel_id": identity["channel_id"],
        "identity_name": identity["name"],
        "upload_mode": "youtube_api" if activate else (channel_data.get("upload_mode") or "playwright"),
    }


@app.get("/api/groups")
def api_list_groups():
    conn = get_conn()
    rows = conn.execute(
        "SELECT g.*, COUNT(m.channel_id) AS channel_count "
        "FROM channel_groups g LEFT JOIN channel_group_memberships m ON m.group_name = g.name "
        "GROUP BY g.name ORDER BY g.name ASC"
    ).fetchall()
    groups = []
    for row in rows:
        item = dict(row)
        item["channel_ids"] = [
            int(member[0]) for member in conn.execute(
                "SELECT channel_id FROM channel_group_memberships WHERE group_name = ? "
                "ORDER BY sort_order ASC, channel_id ASC",
                (row["name"],),
            ).fetchall()
        ]
        groups.append(item)
    conn.close()
    return groups


@app.post("/api/groups")
def api_create_group(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name or len(name) > 80 or any(ch in name for ch in "\r\n\0"):
        raise HTTPException(400, "Tên nhóm phải dài 1–80 ký tự và không chứa xuống dòng.")
    conn = get_conn()
    conn.execute(
        "INSERT INTO channel_groups (name, description) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET description = excluded.description",
        (name, data.get("description") or ""),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/groups/{name}")
def api_rename_group(name: str, data: Dict[str, Any] = Body(...)):
    old_name = str(name or "").strip()
    new_name = str(data.get("name") or "").strip()
    if not new_name or len(new_name) > 80 or any(ch in new_name for ch in "\r\n\0"):
        raise HTTPException(400, "Tên nhóm phải dài 1–80 ký tự và không chứa xuống dòng.")
    if old_name == "Mặc định" and new_name != old_name:
        raise HTTPException(400, "Không thể đổi tên nhóm mặc định.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM channel_groups WHERE name = ?", (old_name,)).fetchone():
            raise HTTPException(404, "Không tìm thấy nhóm kênh.")
        if new_name != old_name and conn.execute(
            "SELECT 1 FROM channel_groups WHERE name = ?", (new_name,)
        ).fetchone():
            raise HTTPException(409, "Tên nhóm mới đã được sử dụng.")
        if new_name != old_name:
            conn.execute(
                "INSERT INTO channel_groups (name, description) VALUES (?, ?)",
                (new_name, data.get("description") or ""),
            )
            conn.execute(
                "INSERT OR IGNORE INTO channel_group_memberships "
                "(group_name, channel_id, sort_order, schema_version) "
                "SELECT ?, channel_id, sort_order, schema_version FROM channel_group_memberships WHERE group_name = ?",
                (new_name, old_name),
            )
            conn.execute("UPDATE youtube_channels SET group_name = ? WHERE group_name = ?", (new_name, old_name))
            conn.execute("DELETE FROM channel_group_memberships WHERE group_name = ?", (old_name,))
            conn.execute("DELETE FROM channel_groups WHERE name = ?", (old_name,))
        else:
            conn.execute(
                "UPDATE channel_groups SET description = ? WHERE name = ?",
                (data.get("description") or "", old_name),
            )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "renamed": new_name != old_name, "stale_queue_targets_require_review": True}


@app.delete("/api/groups/{name}")
def api_delete_group(name: str):
    group_name = str(name or "").strip()
    if group_name == "Mặc định":
        raise HTTPException(400, "Không thể xóa nhóm mặc định.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM channel_groups WHERE name = ?", (group_name,)).fetchone():
            raise HTTPException(404, "Không tìm thấy nhóm kênh.")
        conn.execute("DELETE FROM channel_group_memberships WHERE group_name = ?", (group_name,))
        conn.execute(
            "UPDATE youtube_channels SET group_name = 'Mặc định' "
            "WHERE group_name = ? AND NOT EXISTS ("
            "SELECT 1 FROM channel_group_memberships m WHERE m.channel_id = youtube_channels.id)",
            (group_name,),
        )
        conn.execute("UPDATE youtube_channels SET group_name = ("
                     "SELECT m.group_name FROM channel_group_memberships m WHERE m.channel_id = youtube_channels.id "
                     "ORDER BY m.sort_order, m.group_name LIMIT 1) WHERE group_name = ?", (group_name,))
        conn.execute("DELETE FROM channel_groups WHERE name = ?", (group_name,))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "queued_targets_require_review": True}


# ==========================================================
# QUẢN LÝ TÙY CHỌN ĐĂNG & LINK SHOPEE AFFILIATE
# ==========================================================
@app.get("/api/options")
def api_list_options():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM upload_option_groups ORDER BY id ASC").fetchall()
    conn.close()
    return [{**dict(row), 'metadata_revision': metadata_revision(row, 'option')} for row in rows]


@app.post("/api/options")
def api_create_option(data: Dict[str, Any] = Body(...)):
    try:
        supplied_fields = set(data)
        data = validate_config(data, 'option', create=True)
        data = prepare_option_update({}, data, supplied_fields)
        validate_option_state(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with closing(get_conn()) as conn, conn:
        columns = ', '.join(data)
        placeholders = ', '.join('?' for _ in data)
        cur = conn.execute(f'INSERT INTO upload_option_groups ({columns}) VALUES ({placeholders})', list(data.values()))
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id}


@app.put("/api/options/{opt_id}")
def api_update_option(opt_id: int, data: Dict[str, Any] = Body(...)):
    """Sửa đầy đủ một bộ tùy chọn đăng (affiliate, pin comment, hashtags, tags, danh mục...)."""
    try:
        updates = validate_config(data, 'option')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not updates:
        return {"ok": False, "message": "Không có trường hợp lệ để cập nhật."}

    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (opt_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Không tìm thấy bộ tùy chọn đăng này.')
        if ('expected_metadata_revision' in data
                and data['expected_metadata_revision'] != metadata_revision(row, 'option')):
            raise HTTPException(409, 'Metadata của bộ tùy chọn đã đổi. Tải lại trước khi lưu để không ghi đè lựa chọn mới.')
        try:
            updates = prepare_option_update(dict(row), updates, set(data))
            validate_option_state({**dict(row), **updates})
        except MetadataIntentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        cols = ', '.join(f'{key} = ?' for key in updates)
        conn.execute(f'UPDATE upload_option_groups SET {cols} WHERE id=?', [*updates.values(), opt_id])
    add_log("Options", f"Đã cập nhật bộ tùy chọn đăng #{opt_id}.", "INFO")
    return {"ok": True}


@app.delete("/api/options/{opt_id}")
def api_delete_option(opt_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM upload_option_groups WHERE id=?', (opt_id,)).fetchone():
            raise HTTPException(404, 'Không tìm thấy bộ tùy chọn đăng.')
        if (conn.execute('SELECT 1 FROM video_queue WHERE option_group_id=? LIMIT 1', (opt_id,)).fetchone()
                or conn.execute('SELECT 1 FROM monitors WHERE option_group_id=? LIMIT 1', (opt_id,)).fetchone()):
            raise HTTPException(409, 'Bộ tùy chọn đang được video hoặc Monitor sử dụng. Chọn lại cấu hình và xem trước các mục liên quan trước khi xóa để không mất yêu cầu metadata.')
        conn.execute('DELETE FROM upload_option_groups WHERE id=?', (opt_id,))
    return {"ok": True}


# ==========================================================
# KHO TÀI NGUYÊN RENDER & PRESETS FFMPEG (TỪ DHB)
# ==========================================================
@app.get("/api/render-resources")
def api_get_render_resources():
    return {
        "resources": list_dhb_render_resources(),
        "configs": load_dhb_render_configs(),
    }


# ==========================================================
# PRESET RENDER CÓ TÊN (học quy trình preset JSON của DHB)
# ==========================================================
@app.get('/api/render-presets')
def api_list_render_presets():
    with closing(get_conn()) as conn:
        return [public_preset(row) for row in conn.execute('SELECT * FROM render_presets ORDER BY name')]


def _validated_preset_config(values):
    try:
        return validate_render_config(values['config'], values['render_mode'])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _check_preset_revision(row, expected):
    if not expected or expected != preset_revision(row):
        raise HTTPException(409, 'Preset đã thay đổi hoặc thiếu phiên bản đã đọc. Hãy nạp lại trước khi cập nhật/xóa.')


@app.post('/api/render-presets')
def api_save_render_preset(data: Dict[str, Any] = Body(...)):
    values = _validated_crud_input(PresetCreate, data)
    cfg = _validated_preset_config(values)
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM render_presets WHERE name=?', (values['name'],)).fetchone():
            raise HTTPException(409, 'Tên preset đã tồn tại. Hãy nạp preset và chọn Cập nhật, hoặc nhập tên mới.')
        cursor = conn.execute('INSERT INTO render_presets(name,render_mode,config_json,note) VALUES(?,?,?,?)',
            (values['name'],values['render_mode'],json.dumps(cfg,ensure_ascii=False,allow_nan=False),values['note']))
        row = conn.execute('SELECT * FROM render_presets WHERE id=?', (cursor.lastrowid,)).fetchone()
    return {'ok': True, **public_preset(row)}


@app.put('/api/render-presets/{preset_id}')
def api_update_render_preset(preset_id: int, data: Dict[str, Any] = Body(...)):
    updates = _validated_crud_input(PresetUpdate, data, partial=True)
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM render_presets WHERE id=?', (preset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Không tìm thấy preset này.')
        _check_preset_revision(row, updates.pop('expected_revision', ''))
        values = {key: row[key] for key in ('name','render_mode','note')}
        if 'config' not in updates:
            try:
                values['config'] = json.loads(row['config_json'])
            except (ValueError, TypeError):
                raise HTTPException(400, 'Cấu hình cũ bị lỗi. Cần cung cấp cấu hình thay thế đầy đủ.') from None
        values.update(updates)
        cfg = _validated_preset_config(values)
        if conn.execute('SELECT 1 FROM render_presets WHERE name=? AND id<>?', (values['name'],preset_id)).fetchone():
            raise HTTPException(409, 'Tên preset đã tồn tại; không thể ghi đè preset khác.')
        conn.execute('UPDATE render_presets SET name=?,render_mode=?,config_json=?,note=? WHERE id=?',
            (values['name'],values['render_mode'],json.dumps(cfg,ensure_ascii=False,allow_nan=False),values['note'],preset_id))
        result = public_preset(conn.execute('SELECT * FROM render_presets WHERE id=?', (preset_id,)).fetchone())
    return {'ok': True, **result}


@app.delete('/api/render-presets/{preset_id}')
def api_delete_render_preset(preset_id: int, expected_revision: str = ''):
    with closing(get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM render_presets WHERE id=?', (preset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Không tìm thấy preset này.')
        _check_preset_revision(row, expected_revision)
        conn.execute('DELETE FROM render_presets WHERE id=?', (preset_id,))
    return {'ok': True}


# ==========================================================
# AUTO MONITOR 24/7 (QUÉT & REUP TỰ ĐỘNG)
# ==========================================================
@app.get("/api/monitors")
def api_list_monitors():
    return pipeline_manager.get_monitor_snapshots()


@app.get('/api/upload-capabilities')
def api_upload_capabilities():
    from youtube_uploader import upload_capabilities
    return upload_capabilities()


@app.post("/api/monitors")
def api_create_monitor(data: Dict[str, Any] = Body(...)):
    try:
        data = validate_monitor_create(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        with closing(get_conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if data['target_channel_id'] is not None:
                if not conn.execute('SELECT 1 FROM youtube_channels WHERE id=?', (data['target_channel_id'],)).fetchone():
                    raise HTTPException(400, 'Kênh đích không còn tồn tại; hãy chọn lại kênh.')
            elif not conn.execute('SELECT 1 FROM channel_groups WHERE name=?', (data['target_group_name'],)).fetchone():
                raise HTTPException(400, 'Nhóm kênh đích không còn tồn tại; hãy chọn lại nhóm.')
            if data['option_group_id'] is not None and not conn.execute('SELECT 1 FROM upload_option_groups WHERE id=?', (data['option_group_id'],)).fetchone():
                raise HTTPException(400, 'Bộ tùy chọn không còn tồn tại; hãy chọn lại.')
            columns = ', '.join(data)
            placeholders = ', '.join('?' for _ in data)
            conn.execute(f'INSERT INTO monitors ({columns}) VALUES ({placeholders})', list(data.values()))
    except sqlite3.IntegrityError as exc:
        if 'UNIQUE constraint failed: monitors.name' in str(exc):
            raise HTTPException(409, 'Tên monitor đã tồn tại; hãy chọn tên khác.') from None
        raise
    return {"ok": True}


@app.get("/api/monitors/{monitor_id}/metadata-preview")
def api_monitor_metadata_preview(monitor_id: int):
    conn = get_conn()
    try:
        row = conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Không tìm thấy monitor.')
        mon = dict(row)
        result = monitor_policy.preview(conn, mon, mon.get('metadata_policy_json') or monitor_policy.DEFAULT_POLICY)
        result.pop('envelope')
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/monitors/{monitor_id}/metadata-confirm")
def api_monitor_metadata_confirm(monitor_id: int, data: Dict[str, Any] = Body(...)):
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Không tìm thấy monitor.')
        if row['is_running'] or pipeline_manager._monitor_handles_alive(monitor_id):
            raise HTTPException(409, 'Dừng monitor và đợi worker kết thúc trước khi xác nhận.')
        result = monitor_policy.preview(conn, dict(row), data.get('policy'))
        if result['blocked']:
            raise HTTPException(409, ' '.join(reason for channel in result['channels'] for reason in channel['reasons']))
        if result['revision'] != data.get('revision'):
            raise HTTPException(409, 'Cấu hình đã đổi. Hãy xem trước lại.')
        conn.execute("UPDATE monitors SET metadata_policy_json=?,metadata_revision=?,status='Stopped' WHERE id=?",
                     (monitor_policy.encode(result['policy']), result['revision'], monitor_id))
        conn.commit()
        return {'ok': True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/monitors/{monitor_id}/toggle")
def api_toggle_monitor(monitor_id: int):
    result = pipeline_manager.toggle_monitor(monitor_id)
    if not result.get("ok"):
        if "Không tìm thấy" in str(result.get("message", "")):
            raise HTTPException(404, result["message"])
        raise HTTPException(409, result.get("message", "Monitor đang chuyển trạng thái."))
    return result


@app.delete("/api/monitors/{monitor_id}")
def api_delete_monitor(monitor_id: int):
    result = pipeline_manager.delete_monitor(monitor_id)
    if not result.get("ok"):
        if "Không tìm thấy" in str(result.get("message", "")):
            raise HTTPException(404, result["message"])
        raise HTTPException(409, result.get("message", "Không thể xóa Monitor lúc này."))
    return result


# ==========================================================
# NHẬT KÝ HỆ THỐNG & CÀI ĐẶT TOÀN CỤC
# ==========================================================
def _log_query_parts(module: str = "", status: str = "", q: str = ""):
    clauses = []
    params: List[Any] = []
    if module:
        clauses.append("module = ?")
        params.append(module)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        like = f"%{q.strip()}%"
        clauses.append("(message LIKE ? OR channel_name LIKE ? OR video_title LIKE ?)")
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


@app.get("/api/logs")
def api_get_logs(limit: int = 100, module: str = "", status: str = "", q: str = ""):
    limit = max(1, min(limit, 2000))
    where_sql, params = _log_query_parts(module, status, q)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM system_logs{where_sql} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    secrets = collect_secret_values()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("module", "channel_name", "video_title", "status", "message", "run_id", "phase", "error_code"):
            item[field] = redact_secrets(item.get(field), secrets)
        result.append(item)
    return result


@app.get("/api/logs/modules")
def api_log_modules():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT module FROM system_logs WHERE module <> '' ORDER BY module"
    ).fetchall()
    conn.close()
    return [r["module"] for r in rows]


@app.get("/api/logs/export")
def api_export_logs(module: str = "", status: str = "", q: str = ""):
    from csv_safety import ENCODING_COLUMN, ENCODING_VERSION, encode_csv_cell
    where_sql, params = _log_query_parts(module, status, q)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM system_logs{where_sql} ORDER BY id DESC LIMIT 20000",
        params,
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "created_at", "module", "status", "channel_name", "video_title", "message",
        "run_id", "queue_id", "attempt_id", "phase", "error_code",
        ENCODING_COLUMN,
    ])
    secrets = collect_secret_values()
    for row in rows:
        writer.writerow([encode_csv_cell(value) for value in [
            row["id"], row["created_at"], row["module"], row["status"],
            redact_secrets(row["channel_name"], secrets), redact_secrets(row["video_title"], secrets),
            redact_secrets(row["message"], secrets), redact_secrets(row["run_id"], secrets),
            row["queue_id"], row["attempt_id"], redact_secrets(row["phase"], secrets),
            redact_secrets(row["error_code"], secrets),
        ]] + [ENCODING_VERSION])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    add_log("System", f"Đã xuất {len(rows)} dòng nhật ký ra file CSV.", "SUCCESS")
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="dhb_logs_{stamp}.csv"'},
    )


@app.delete("/api/logs")
def api_clear_logs():
    conn = get_conn()
    conn.execute("DELETE FROM system_logs")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings():
    return public_settings(get_settings())


@app.get("/api/storage/overview")
def api_storage_overview(retention_days: int = DEFAULT_RETENTION_DAYS):
    try:
        return storage_overview(retention_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/storage/cleanup-preview")
def api_storage_cleanup_preview(data: Dict[str, Any] = Body(...)):
    try:
        retention_days = int(data.get("retention_days", DEFAULT_RETENTION_DAYS))
        return storage_overview(retention_days)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/storage/cleanup")
def api_storage_cleanup(data: Dict[str, Any] = Body(...)):
    artifact_ids = data.get("artifact_ids")
    if not isinstance(artifact_ids, list):
        raise HTTPException(400, "Danh sách artifact_ids phải là một mảng.")
    try:
        retention_days = int(data.get("retention_days", DEFAULT_RETENTION_DAYS))
        return cleanup_managed_artifacts(artifact_ids, retention_days)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/settings")
def api_save_settings(data: Dict[str, Any] = Body(...)):
    try:
        payload = validate_public_settings(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if "download_cookies_file" in payload:
        raw_cookie_path = str(payload.get("download_cookies_file") or "").strip()
        if raw_cookie_path:
            cookie_path = Path(raw_cookie_path).expanduser()
            if not cookie_path.is_file():
                raise HTTPException(400, "File cookies Netscape cấu hình cho yt-dlp không tồn tại.")
            payload["download_cookies_file"] = str(cookie_path.resolve())
    save_settings(payload)
    add_log("Settings", "Đã lưu cấu hình hệ thống.", "SUCCESS")
    return {"ok": True}


@app.post("/api/settings/secrets")
def api_save_secret_settings(data: Dict[str, Any] = Body(...)):
    try:
        payload = validate_secret_settings(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not payload:
        raise HTTPException(400, "Không có khóa bí mật mới để lưu.")
    save_settings(payload)
    add_log("Settings", f"Đã lưu {len(payload)} khóa bí mật qua endpoint riêng.", "SUCCESS")
    return {"ok": True, "saved": sorted(payload)}


@app.post("/api/backup/export-now")
def api_export_backup_now():
    settings = get_settings()
    target = str(settings.get("external_backup_dir") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="Chưa cấu hình thư mục export CSV trong Cài Đặt.")
    keep = int(settings.get("external_backup_keep", 10) or 10)
    try:
        result = export_all_tables_csv(target, keep=keep)
    except Exception as exc:
        add_log("Housekeeping", f"Export CSV thủ công thất bại: {exc}", "ERROR", error_code="ERR_EXPORT")
        raise HTTPException(status_code=500, detail="Export thất bại. Kiểm tra quyền thư mục và Nhật Ký Hệ Thống.") from exc
    add_log(
        "Housekeeping",
        f"Đã export CSV thủ công {result['tables']} bảng ({result['rows']} dòng) → {result['path']}",
        "SUCCESS",
    )
    return {"ok": True, **result}


if __name__ == "__main__":
    import uvicorn

    # Truyền thẳng app object để Uvicorn không import lại app.py (tránh khởi động trùng thread/DB).
    uvicorn.run(app, host="127.0.0.1", port=8686, reload=False, access_log=False)
