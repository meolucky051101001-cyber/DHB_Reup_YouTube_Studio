import sys
import os
import re
import csv
import io
import json
import shutil
import hashlib
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body, File, UploadFile, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import (
    APP_ROOT,
    APP_TEMP_DIR,
    DHB_ROOT,
    FFMPEG_EXE,
    YTDL_EXE,
    list_dhb_render_resources,
    load_dhb_render_configs,
)
from api_security import (
    generate_api_token,
    is_allowed_host,
    is_allowed_origin,
    token_matches,
)
from database import (
    init_db,
    get_conn,
    get_settings,
    save_settings,
    sync_from_dhb_facebook,
    add_log,
    get_all_profiles,
    get_channels_by_profile,
    run_housekeeping,
    export_all_tables_csv,
    collect_secret_values,
    is_secret_setting_key,
    public_channel,
    public_settings,
    redact_secrets,
)
from scanner_downloader import scan_social_urls, probe_video_info, probe_video_infos, VIDEO_EXTENSIONS
from ai_engine import ai_optimize_youtube_metadata, apply_batch_text_edit
from youtube_uploader import (
    open_profile_for_login,
    probe_profile_login,
    get_oauth_token_local_status,
    check_oauth_token_status,
)
from pipeline_manager import pipeline_manager

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


threading.Thread(target=_housekeeping_worker, daemon=True, name="DHB-Housekeeping").start()
pipeline_manager.start_monitor_supervisor()

app = FastAPI(title="DHB Reup YouTube Studio", version="2.0.0")

API_TOKEN = generate_api_token()
TOKEN_EXEMPT_PATHS = {"/api/auth/token"}
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_api_token(request: Request) -> str:
    token = request.headers.get("x-dhb-token") or ""
    if not token and request.url.path.endswith("/stream"):
        token = request.query_params.get("token") or ""
    return token


@app.middleware("http")
async def guard_local_api(request: Request, call_next):
    if not is_allowed_host(request.headers.get("host", "")):
        return JSONResponse({"detail": "Host không hợp lệ — tool chỉ phục vụ localhost."}, status_code=403)

    path = request.url.path
    if path.startswith("/api/") and path not in TOKEN_EXEMPT_PATHS:
        if request.method in STATE_CHANGING_METHODS and not is_allowed_origin(request.headers.get("origin", "")):
            return JSONResponse({"detail": "Origin không hợp lệ — chặn yêu cầu cross-site."}, status_code=403)
        token = _extract_api_token(request)
        if not token_matches(token, API_TOKEN):
            return JSONResponse({"detail": "Thiếu hoặc sai API token cục bộ."}, status_code=401)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.get("/api/auth/token")
def api_auth_token():
    return JSONResponse({"token": API_TOKEN}, headers={"Cache-Control": "no-store"})


STATIC_DIR = APP_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


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
        "dhb_connected": DHB_ROOT.exists(),
        "ffmpeg_path": FFMPEG_EXE,
        "ytdl_path": YTDL_EXE,
    }


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
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên dự án không được để trống.")
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    return {"ok": True, "name": name}


@app.delete("/api/projects/{name}")
def api_delete_project(name: str):
    conn = get_conn()
    total_projects = conn.execute("SELECT COUNT(*) as c FROM projects").fetchone()["c"]
    if total_projects <= 1:
        conn.close()
        raise HTTPException(400, "Cần giữ lại ít nhất 1 dự án để làm việc.")
    conn.execute("DELETE FROM video_queue WHERE project_name = ?", (name,))
    conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/queue")
def api_get_queue(project: str = ""):
    conn = get_conn()
    if not project or project == "Default":
        p_row = conn.execute("SELECT name FROM projects ORDER BY id ASC LIMIT 1").fetchone()
        project = p_row["name"] if p_row else "Dự án YouTube #1"
    rows = conn.execute(
        "SELECT * FROM video_queue WHERE project_name = ? ORDER BY id DESC",
        (project,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/queue/scan")
def api_scan_and_add(data: Dict[str, Any] = Body(...)):
    project_name = data.get("project_name") or "Default"
    raw_urls = data.get("urls") or ""
    urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not urls:
        raise HTTPException(400, "Vui lòng nhập ít nhất 1 đường dẫn URL hoặc đường dẫn thư mục.")

    max_items = int(data.get("max_items", 20) or 20)
    min_views = int(data.get("min_views", 0) or 0)
    min_likes = int(data.get("min_likes", 0) or 0)
    min_comments = int(data.get("min_comments", 0) or 0)
    only_shorts = bool(data.get("only_shorts", False))
    recursive = bool(data.get("recursive", False))
    target_channel_id = data.get("target_channel_id") or None
    target_group_name = data.get("target_group_name") or "Mặc định"
    option_group_id = data.get("option_group_id") or None
    render_mode = data.get("render_mode") or "shorts_9_16"
    upload_to = data.get("upload_to") or "Shorts"
    resolution = data.get("resolution") or "1080p"
    emulate_mobile = 1 if data.get("emulate_mobile", False) else 0
    visibility = (data.get("visibility") or "public").lower()
    if visibility == "scheduled":
        visibility = "schedule"
    if visibility not in ("public", "unlisted", "private", "schedule"):
        raise HTTPException(400, "Chế độ hiển thị không hợp lệ (chỉ nhận public / unlisted / private / schedule).")
    base_schedule_time = (data.get("schedule_time") or "").strip()
    schedule_interval_minutes = int(data.get("schedule_interval_minutes") or 0)
    is_premiere = 1 if data.get("is_premiere") else 0
    made_for_kids = 1 if data.get("made_for_kids") else 0
    age_restriction = 1 if data.get("age_restriction") else 0
    altered_content = 1 if data.get("altered_content") else 0

    from datetime import datetime, timedelta
    start_dt = None
    if visibility == "schedule":
        if not base_schedule_time:
            raise HTTPException(
                400,
                "Chế độ Lên lịch bắt buộc phải nhập thời gian đăng cụ thể trong tương lai. "
                "Hệ thống không tự chuyển sang Công khai khi thiếu giờ.",
            )
        try:
            start_dt = datetime.fromisoformat(base_schedule_time.replace(" ", "T"))
        except Exception:
            raise HTTPException(400, "Thời gian bắt đầu đặt lịch không đúng định dạng (ví dụ đúng: 2026-10-25 19:30).")
        if start_dt <= datetime.now():
            raise HTTPException(400, "Thời gian bắt đầu đặt lịch phải ở TƯƠNG LAI.")
        schedule_interval_minutes = max(30, schedule_interval_minutes)

    scanned = scan_social_urls(
        urls=urls,
        max_items_per_url=max_items,
        min_views=min_views,
        min_likes=min_likes,
        min_comments=min_comments,
        only_shorts=only_shorts,
        recursive_local=recursive,
    )

    conn = get_conn()
    with _import_insert_lock:
        conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (project_name,))
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
                    target_group_name, option_group_id, render_mode, resolution,
                    emulate_mobile, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending')
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
                    target_group_name,
                    option_group_id,
                    render_mode,
                    resolution,
                    emulate_mobile,
                ),
            )
            added_count += 1
        conn.commit()
    conn.close()
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
    made_for_kids: int = Form(0),
    age_restriction: int = Form(0),
    altered_content: int = Form(0),
    emulate_mobile: int = Form(0),
    files: List[UploadFile] = File(...),
):
    """Tải lên trực tiếp các file video từ trình duyệt hoặc kéo thả vào thư mục temp/uploads và nạp vào hàng đợi."""
    upload_dir = APP_TEMP_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (project_name,))

    added_count = 0
    added_titles = []

    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext and ext not in VIDEO_EXTENSIONS:
            continue

        safe_name = f"{Path(f.filename).stem}_{os.urandom(4).hex()}{ext}"
        target_path = upload_dir / safe_name

        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(f.file, buffer)

        probe = probe_video_info(str(target_path))
        title = Path(f.filename).stem
        post_id = hashlib.md5(str(target_path).encode("utf-8")).hexdigest()[:16].upper()

        is_short = probe["seconds"] <= 65 or (probe["height"] > probe["width"] and probe["seconds"] <= 180)
        final_upload_to = upload_to or ("Shorts" if is_short else "Video")

        conn.execute(
            """
            INSERT INTO video_queue (
                project_name, source_type, platform, post_id, source_url, input_video_path,
                title, description, tags, duration, view_count, like_count,
                upload_to, visibility, schedule_time, is_premiere, made_for_kids,
                age_restriction, altered_content, target_type, target_channel_id,
                target_group_name, option_group_id, render_mode, resolution,
                emulate_mobile, download_progress, status
            ) VALUES (?, 'File', 'LocalUpload', ?, ?, ?, ?, ?, '#shorts #trending', ?, 0, 0, ?, ?, '', 0, ?, ?, ?, 'GROUP', NULL, 'Mặc định', NULL, ?, ?, ?, 100, 'Pending')
            """,
            (
                project_name,
                post_id,
                str(target_path),
                str(target_path),
                title,
                title,
                probe["duration"],
                final_upload_to,
                visibility.lower(),
                1 if made_for_kids else 0,
                1 if age_restriction else 0,
                1 if altered_content else 0,
                render_mode,
                resolution,
                1 if emulate_mobile else 0,
            ),
        )
        added_count += 1
        added_titles.append(title)

    conn.commit()
    conn.close()
    add_log("Upload", f"Đã tải lên và thêm {added_count} video vào dự án [{project_name}].", "SUCCESS")
    return {"ok": True, "added": added_count, "titles": added_titles}


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
    recursive = bool(data.get("recursive", False))

    if not paths:
        raise HTTPException(400, "Không có tệp nào được chọn.")

    pending_files: List[Path] = []
    skipped_count = 0
    seen_post_ids = set()

    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (project_name,))
    conn.commit()
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

    probe_map = probe_video_infos([str(f) for f in pending_files])

    added_count = 0
    added_titles = []
    conn = get_conn()
    try:
        with _import_insert_lock:
            for f in pending_files:
                post_id = hashlib.md5(str(f).encode("utf-8")).hexdigest()[:16].upper()
                if _is_duplicate_post_id(conn, project_name, post_id):
                    skipped_count += 1
                    continue

                probe = probe_map[str(f)]
                title = f.stem
                is_short = probe["seconds"] <= 65 or (probe["height"] > probe["width"] and probe["seconds"] <= 180)
                final_upload_to = upload_to or ("Shorts" if is_short else "Video")

                conn.execute(
                    """
                    INSERT INTO video_queue (
                        project_name, source_type, platform, post_id, source_url, input_video_path,
                        title, description, tags, duration, view_count, like_count,
                        upload_to, visibility, schedule_time, is_premiere, made_for_kids,
                        age_restriction, altered_content, target_type, target_channel_id,
                        target_group_name, option_group_id, render_mode, resolution,
                        emulate_mobile, download_progress, status
                    ) VALUES (?, 'File', 'LocalFile', ?, ?, ?, ?, ?, '#shorts #trending', ?, 0, 0, ?, ?, '', 0, 0, 0, 0, 'GROUP', NULL, 'Mặc định', NULL, ?, ?, 0, 100, 'Pending')
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
                        render_mode,
                        resolution,
                    ),
                )
                added_count += 1
                added_titles.append(title)

            conn.commit()
    finally:
        conn.close()
    log_msg = f"Đã thêm trực tiếp {added_count} video cục bộ vào dự án [{project_name}]."
    if skipped_count:
        log_msg += f" Bỏ qua {skipped_count} video đã có trong dự án."
    add_log("Scanner", log_msg, "SUCCESS")
    return {"ok": True, "added": added_count, "skipped": skipped_count, "titles": added_titles}


@app.post("/api/queue/bulk-update")
def api_bulk_update_queue(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    updates = data.get("updates") or {}
    if not ids or not updates:
        return {"ok": False}

    allowed = {
        "title", "description", "tags", "upload_to", "visibility", "schedule_time",
        "target_type", "target_channel_id", "target_group_name", "option_group_id",
        "render_mode", "render_config_json", "cut_start", "cut_end", "status",
        "resolution", "emulate_mobile", "target_profile_name",
        "made_for_kids", "age_restriction", "altered_content", "is_premiere",
    }
    
    interval = int(updates.get("schedule_interval_minutes") or 0)
    base_sched = (updates.get("schedule_time") or "").strip()
    from datetime import datetime, timedelta
    base_dt = None
    if base_sched:
        try:
            base_dt = datetime.fromisoformat(base_sched.replace(" ", "T"))
        except Exception:
            raise HTTPException(400, f"Thời gian đặt lịch không đúng định dạng: '{base_sched}'.")
        if base_dt <= datetime.now():
            raise HTTPException(400, "Thời gian bắt đầu đặt lịch phải ở TƯƠNG LAI.")
    start_dt = base_dt if (base_dt and interval > 0) else None
    if start_dt:
        interval = max(30, interval)

    filtered = {k: v for k, v in updates.items() if k in allowed and k != "schedule_time"}
    if filtered.get("visibility") == "scheduled":
        filtered["visibility"] = "schedule"
    immediate_mode = filtered.get("visibility") in ("public", "unlisted", "private")
    if immediate_mode and base_sched:
        raise HTTPException(
            400,
            "Chế độ đăng ngay (Công khai/Không công khai/Riêng tư) không đi kèm giờ đặt lịch. "
            "Hãy chọn 'Lên lịch' nếu muốn hẹn giờ.",
        )

    conn = get_conn()
    if filtered.get("visibility") == "schedule" and not base_sched:
        missing = []
        for item_id in ids:
            row = conn.execute("SELECT schedule_time FROM video_queue WHERE id = ?", (item_id,)).fetchone()
            existing = (row["schedule_time"] or "").strip() if row else ""
            valid = False
            if existing:
                try:
                    valid = datetime.fromisoformat(existing.replace(" ", "T")) > datetime.now()
                except Exception:
                    valid = False
            if not valid:
                missing.append(item_id)
        if missing:
            conn.close()
            raise HTTPException(
                400,
                f"{len(missing)} video chưa có thời gian đăng hợp lệ trong TƯƠNG LAI. "
                "Chế độ Lên lịch bắt buộc phải có giờ đăng — hệ thống không tự chuyển sang Công khai.",
            )

    for idx, item_id in enumerate(ids):
        item_updates = dict(filtered)
        if start_dt:
            item_dt = start_dt + timedelta(minutes=idx * interval)
            item_updates["schedule_time"] = item_dt.strftime("%Y-%m-%d %H:%M:%S")
        elif "schedule_time" in updates and base_sched:
            item_updates["schedule_time"] = base_sched
        if immediate_mode:
            item_updates["schedule_time"] = ""
            item_updates["is_premiere"] = 0

        if item_updates:
            cols = ", ".join(f"{k} = ?" for k in item_updates.keys())
            conn.execute(
                f"UPDATE video_queue SET {cols} WHERE id = ?",
                list(item_updates.values()) + [item_id],
            )
    conn.commit()
    conn.close()
    return {"ok": True, "updated": len(ids)}


@app.post("/api/queue/shorts-boost")
def api_shorts_boost(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    if not ids:
        return {"ok": False, "updated": 0}
    conn = get_conn()
    updated_count = 0
    for item_id in ids:
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
        if "speed" not in cfg or cfg["speed"] < 1.05:
            cfg["speed"] = 1.05
        cfg["hflip"] = True
        cfg["audio_pitch_shift"] = True

        conn.execute(
            """
            UPDATE video_queue
            SET upload_to = 'Shorts',
                render_mode = 'shorts_9_16',
                emulate_mobile = 0,
                resolution = '1080p',
                title = ?,
                render_config_json = ?
            WHERE id = ?
            """,
            (title, json.dumps(cfg, ensure_ascii=False), item_id)
        )
        updated_count += 1
    conn.commit()
    conn.close()
    return {"ok": True, "updated": updated_count}



@app.post("/api/queue/edit-metadata")
def api_edit_queue_metadata(data: Dict[str, Any] = Body(...)):
    item_id = data.get("id")
    if not item_id:
        raise HTTPException(400, "Thiếu ID video cần chỉnh sửa.")
    title = (data.get("title") or "").strip()
    desc = (data.get("description") or "").strip()
    tags = (data.get("tags") or "").strip()
    resolution = (data.get("resolution") or "1080p").strip()
    emulate_mobile = 1 if data.get("emulate_mobile", False) else 0
    render_mode = data.get("render_mode") or "shorts_9_16"
    option_group_id = data.get("option_group_id") or None
    cut_start = (data.get("cut_start") or "").strip()
    cut_end = (data.get("cut_end") or "").strip()
    visibility = (data.get("visibility") or "public").lower()
    if visibility == "scheduled":
        visibility = "schedule"
    if visibility not in ("public", "unlisted", "private", "schedule"):
        raise HTTPException(400, "Chế độ hiển thị không hợp lệ (chỉ nhận public / unlisted / private / schedule).")
    schedule_time = (data.get("schedule_time") or "").strip()
    is_premiere = 1 if data.get("is_premiere") else 0
    if visibility in ("public", "unlisted", "private"):
        schedule_time = ""
        is_premiere = 0
    elif visibility == "schedule":
        if not schedule_time:
            raise HTTPException(400, "Vui lòng nhập thời gian đặt lịch phát hành.")
        try:
            sched_dt = datetime.fromisoformat(schedule_time.replace(" ", "T"))
        except Exception:
            raise HTTPException(400, "Thời gian đặt lịch không đúng định dạng.")
        if sched_dt <= datetime.now():
            raise HTTPException(400, "Thời gian đặt lịch phải ở TƯƠNG LAI.")
    made_for_kids = 1 if data.get("made_for_kids") else 0
    age_restriction = 1 if data.get("age_restriction") else 0
    altered_content = 1 if data.get("altered_content") else 0

    # C1: các cột targeting chỉ được ghi khi request GỬI KÈM khóa tương ứng —
    # modal sửa video không gửi target_group_name/target_profile_name nên tuyệt đối
    # không được xóa trắng cấu hình kênh đích sẵn có của video.
    conn = get_conn()
    if option_group_id is not None:
        exists = conn.execute("SELECT 1 FROM upload_option_groups WHERE id = ?", (option_group_id,)).fetchone()
        if not exists:
            conn.close()
            raise HTTPException(400, f"Không tìm thấy bộ tùy chọn đăng #{option_group_id}.")

    set_sql = """
        title = ?, description = ?, tags = ?, resolution = ?,
        emulate_mobile = ?, render_mode = ?, cut_start = ?, cut_end = ?,
        visibility = ?, schedule_time = ?, is_premiere = ?, made_for_kids = ?,
        age_restriction = ?, altered_content = ?
    """
    set_vals = [
        title, desc, tags, resolution, emulate_mobile,
        render_mode, cut_start, cut_end,
        visibility, schedule_time, is_premiere, made_for_kids,
        age_restriction, altered_content,
    ]
    if "option_group_id" in data:
        set_sql += ", option_group_id = ?"
        set_vals.append(option_group_id)
    if "target_channel_id" in data:
        set_sql += ", target_channel_id = ?"
        set_vals.append(data.get("target_channel_id") or None)
    if "target_group_name" in data:
        set_sql += ", target_group_name = ?"
        set_vals.append((data.get("target_group_name") or "").strip())
    if "target_profile_name" in data:
        set_sql += ", target_profile_name = ?"
        set_vals.append((data.get("target_profile_name") or "").strip() or None)

    cur = conn.execute(
        f"UPDATE video_queue SET {set_sql} WHERE id = ?",
        (*set_vals, item_id),
    )
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, f"Không tìm thấy video #{item_id} trong hàng đợi.")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/queue/delete")
def api_delete_queue_items(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    if not ids:
        return {"ok": True, "deleted": 0}
    conn = get_conn()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM video_queue WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": len(ids)}


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

    conn = get_conn()
    first_dt = last_dt = None
    for item_id in ids:
        curr_str = curr.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE video_queue SET schedule_time = ?, visibility = 'schedule', is_premiere = ? WHERE id = ?",
            (curr_str, is_premiere, item_id),
        )
        if first_dt is None:
            first_dt = curr
        last_dt = curr
        curr += timedelta(minutes=interval_minutes)
    conn.commit()
    conn.close()
    add_log(
        "Scheduler",
        f"Đã lên lịch giãn cách cho {len(ids)} video: từ {first_dt:%Y-%m-%d %H:%M} đến {last_dt:%Y-%m-%d %H:%M} (cách nhau {interval_minutes} phút).",
        "SUCCESS",
    )
    return {
        "ok": True,
        "scheduled": len(ids),
        "interval_minutes": interval_minutes,
        "first_time": first_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "last_time": last_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/queue/export-csv")
def api_export_csv(project: str = "Default"):
    import csv
    import io
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
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["platform"], r["title"], r["description"], r["tags"], r["duration"],
            r["source_url"], r["render_mode"], r["upload_to"], r["status"], r["published_url"],
            r["visibility"] or "public", r["schedule_time"] or "",
            r["made_for_kids"] or 0, r["age_restriction"] or 0, r["altered_content"] or 0,
        ])

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
    import csv
    import io

    project_name = data.get("project_name") or "Default"
    csv_text = data.get("csv_text") or ""
    if not csv_text.strip():
        raise HTTPException(400, "Nội dung CSV không được để trống.")

    f = io.StringIO(csv_text.strip())
    reader = csv.DictReader(f)
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (project_name,))
    added = 0
    def _csv_int(row: Dict[str, Any], *names: str) -> int:
        for n in names:
            v = (str(row.get(n) or "")).strip()
            if v:
                try:
                    return 1 if float(v) >= 1 else 0
                except Exception:
                    pass
        return 0

    for row in reader:
        url = row.get("Source_URL") or row.get("source_url") or row.get("URL") or row.get("url") or ""
        title = (row.get("Title") or row.get("title") or "").strip()
        if not title:
            title = (Path(url).stem if url else "") or "Video"
        desc = row.get("Description") or row.get("description") or ""
        tags = row.get("Tags") or row.get("tags") or ""
        upload_to = row.get("Upload_To") or row.get("upload_to") or "Shorts"
        render_mode = row.get("Render_Mode") or row.get("render_mode") or "shorts_9_16"
        visibility = (row.get("Visibility") or row.get("visibility") or "public").lower()
        schedule_time = (row.get("Schedule_Time") or row.get("schedule_time") or "").strip()
        made_for_kids = _csv_int(row, "Made_For_Kids", "made_for_kids")
        age_restriction = _csv_int(row, "Age_Restriction", "age_restriction")
        altered_content = _csv_int(row, "Altered_Content", "altered_content")

        conn.execute(
            """
            INSERT INTO video_queue (
                project_name, source_type, platform, source_url, title, description, tags,
                upload_to, render_mode, visibility, schedule_time,
                made_for_kids, age_restriction, altered_content, emulate_mobile, status
            ) VALUES (?, 'CSV', 'Custom', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'Pending')
            """,
            (
                project_name, url, title, desc, tags, upload_to, render_mode,
                visibility, schedule_time, made_for_kids, age_restriction, altered_content,
            ),
        )
        added += 1
    conn.commit()
    conn.close()
    add_log("CSV", f"Đã nhập {added} video từ CSV vào dự án [{project_name}].", "SUCCESS")
    return {"ok": True, "added": added}


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
        return value.replace(tzinfo=None, microsecond=0)
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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None, microsecond=0)
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


@app.post("/api/queue/import-excel")
async def api_import_excel(
    file: UploadFile = File(...),
    project_name: str = Form("Default"),
    ids: str = Form(""),
):
    import io
    from datetime import timedelta

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

    project_name = (project_name or "Default").strip() or "Default"
    selected_ids = _parse_id_list(ids)

    conn = get_conn()
    try:
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
        updates_by_id: Dict[int, Dict[str, Any]] = {}
        matched = 0
        has_any_id = any(row["id"] is not None for row in excel_rows)

        def warn(message: str):
            if len(warnings) < 80:
                warnings.append(message)

        if has_any_id:
            unmatched_without_id = 0
            for row in excel_rows:
                excel_id = row["id"]
                if excel_id is None:
                    unmatched_without_id += 1
                    continue
                if excel_id not in current_by_id:
                    warn(f"Dòng {row['row']}: ID #{excel_id} không thuộc dự án [{project_name}] — bỏ qua.")
                    continue
                if excel_id not in target_pool:
                    warn(f"Dòng {row['row']}: ID #{excel_id} không nằm trong các dòng đã tích chọn — bỏ qua.")
                    continue
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
                row["_target_id"] = item_id

        now = datetime.now()
        scheduled_entries = []
        for row in excel_rows:
            item_id = row.get("_target_id")
            if item_id is None:
                continue
            matched += 1
            values = row["values"]
            current = current_by_id.get(item_id, {})
            updates: Dict[str, Any] = {}

            title = values.get("title")
            if title is not None and str(title).strip() != "":
                title_text = str(title).strip()
                if len(title_text) > 100:
                    warn(f"Dòng {row['row']}: tiêu đề dài {len(title_text)} ký tự, đã cắt còn 100 ký tự.")
                    title_text = title_text[:100].rstrip()
                updates["title"] = title_text

            description = values.get("description")
            if description is not None and str(description).strip() != "":
                desc_text = str(description).strip()
                if len(desc_text) > 5000:
                    warn(f"Dòng {row['row']}: mô tả dài quá 5000 ký tự, đã cắt bớt.")
                    desc_text = desc_text[:5000]
                updates["description"] = desc_text

            tags = values.get("tags")
            if tags is not None and str(tags).strip() != "":
                updates["tags"] = str(tags).strip()

            upload_to = _excel_map_choice(
                values.get("upload_to"),
                {"shorts": "Shorts", "short": "Shorts", "shorts video": "Shorts",
                 "video": "Video", "video dai": "Video", "dai": "Video", "long": "Video"},
            )
            if upload_to:
                updates["upload_to"] = upload_to

            render_mode = _excel_map_choice(values.get("render_mode"), _EXCEL_RENDER_MODES)
            if render_mode:
                updates["render_mode"] = render_mode

            resolution = _excel_map_choice(values.get("resolution"), _EXCEL_RESOLUTIONS)
            if resolution:
                updates["resolution"] = resolution

            profile_raw = values.get("target_profile_name")
            profile_name = None
            if profile_raw is not None and str(profile_raw).strip() != "":
                profile_name = _match_excel_profile(profile_raw, profile_names)
                if not profile_name:
                    warn(f"Dòng {row['row']}: không tìm thấy Profile Chrome '{profile_raw}' — bỏ qua cột Profile.")

            channel_raw = values.get("channel")
            channel_input_present = channel_raw is not None and str(channel_raw).strip() != ""
            channel = None
            if channel_input_present:
                channel, reason = _match_excel_channel(channel_raw, channels)
                if not channel:
                    warn(f"Dòng {row['row']}: {reason} — bỏ qua cột Kênh.")

            if channel:
                channel_profile = (channel.get("profile_name") or "").strip()
                if profile_name and channel_profile and _excel_key(profile_name) != _excel_key(channel_profile):
                    warn(
                        f"Dòng {row['row']}: Profile '{profile_name}' khác Profile của kênh "
                        f"(dùng '{channel_profile}' để giữ cùng Proxy)."
                    )
                updates["target_channel_id"] = int(channel["id"])
                updates["target_type"] = "CHANNEL"
                if channel_profile:
                    updates["target_profile_name"] = channel_profile
                elif profile_name:
                    updates["target_profile_name"] = profile_name
            elif profile_name and not channel_input_present:
                updates["target_profile_name"] = profile_name
                updates["target_channel_id"] = None
                updates["target_type"] = "PROFILE"
            elif profile_name and not current.get("target_channel_id"):
                updates["target_profile_name"] = profile_name

            visibility = _excel_map_choice(values.get("visibility"), _EXCEL_VISIBILITY)
            if values.get("visibility") not in (None, "") and not visibility:
                warn(f"Dòng {row['row']}: chế độ đăng '{values.get('visibility')}' không hợp lệ — bỏ qua.")

            schedule_raw = values.get("schedule_time")
            schedule_dt = _excel_datetime(schedule_raw) if schedule_raw not in (None, "") else None
            if schedule_raw not in (None, "") and schedule_dt is None:
                warn(f"Dòng {row['row']}: giờ đặt lịch '{schedule_raw}' không đúng định dạng — bỏ qua.")
            if schedule_dt and schedule_dt <= now:
                warn(f"Dòng {row['row']}: giờ đặt lịch {schedule_dt:%Y-%m-%d %H:%M} đã qua — bỏ qua.")
                schedule_dt = None

            current_sched_dt = _excel_datetime(current.get("schedule_time"))
            schedule_changed = schedule_dt is not None and current_sched_dt != schedule_dt

            if visibility == "schedule":
                if schedule_dt:
                    updates["visibility"] = "schedule"
                    updates["schedule_time"] = schedule_dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    if current_sched_dt and current_sched_dt > now:
                        updates["visibility"] = "schedule"
                    else:
                        updates["visibility"] = "schedule"
                        updates["schedule_time"] = ""
                        warn(
                            f"Dòng {row['row']}: chọn Lên lịch nhưng thiếu giờ hợp lệ — "
                            "video sẽ bị chặn khi chạy cho tới khi bổ sung giờ (không tự đăng Công khai)."
                        )
            elif visibility in ("public", "unlisted", "private"):
                if schedule_changed:
                    updates["visibility"] = "schedule"
                    updates["schedule_time"] = schedule_dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    updates["visibility"] = visibility
                    if schedule_dt or current.get("schedule_time"):
                        updates["schedule_time"] = ""
                        updates["is_premiere"] = 0
            elif schedule_dt:
                updates["visibility"] = "schedule"
                updates["schedule_time"] = schedule_dt.strftime("%Y-%m-%d %H:%M:%S")
            elif visibility:
                updates["visibility"] = visibility

            for field in ("age_restriction", "made_for_kids", "altered_content", "is_premiere", "emulate_mobile"):
                parsed_bool = _excel_bool(values.get(field))
                if parsed_bool is not None:
                    updates[field] = parsed_bool

            final_visibility = updates.get("visibility", (current.get("visibility") or "").lower())
            final_schedule = updates.get("schedule_time", (current.get("schedule_time") or "").strip())
            if updates.get("is_premiere") and final_visibility != "schedule" and not final_schedule:
                warn(f"Dòng {row['row']}: Premiere chỉ áp dụng khi Lên lịch — đã tắt Premiere.")
                updates["is_premiere"] = 0

            for field in ("cut_start", "cut_end"):
                raw_cut = values.get(field)
                if raw_cut is not None and str(raw_cut).strip() != "":
                    updates[field] = str(raw_cut).strip()

            if str(current.get("status") or "") == "Completed":
                warn(f"Dòng {row['row']} (#{item_id}): video đã đăng — thay đổi chỉ lưu trong hàng đợi, không sửa video trên YouTube.")

            if updates:
                updates_by_id[item_id] = updates
                if updates.get("schedule_time"):
                    scheduled_entries.append(
                        (item_id, datetime.fromisoformat(updates["schedule_time"]))
                    )

        adjusted = 0
        scheduled_entries.sort(key=lambda pair: pair[1])
        for idx in range(1, len(scheduled_entries)):
            prev_id, prev_dt = scheduled_entries[idx - 1]
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
            "is_premiere", "cut_start", "cut_end", "emulate_mobile",
        }
        updated = 0
        for item_id, item_updates in updates_by_id.items():
            safe_updates = {k: v for k, v in item_updates.items() if k in allowed_cols}
            if not safe_updates:
                continue
            cols = ", ".join(f"{k} = ?" for k in safe_updates)
            conn.execute(
                f"UPDATE video_queue SET {cols} WHERE id = ?",
                list(safe_updates.values()) + [item_id],
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()

    skipped = len(excel_rows) - matched
    add_log(
        "Excel",
        f"Đã nhập Excel cập nhật {updated}/{len(excel_rows)} dòng cho dự án [{project_name}]"
        + (f", bỏ qua {skipped} dòng." if skipped else "."),
        "SUCCESS" if updated else "WARNING",
    )
    return {
        "ok": True,
        "updated": updated,
        "matched": matched,
        "skipped": skipped,
        "total_rows": len(excel_rows),
        "adjusted_schedules": adjusted,
        "warning_count": len(warnings),
        "warnings": warnings[:30],
    }


# ==========================================================
# AI DỊCH THUẬT, VIẾT LẠI SEO & BIÊN TẬP HÀNG LOẠT
# ==========================================================
@app.post("/api/queue/ai-process")
def api_ai_process_queue(data: Dict[str, Any] = Body(...)):
    ids = data.get("ids") or []
    mode = data.get("mode") or "rewrite_seo"  # 'translate' | 'rewrite_seo' | 'batch_edit'
    provider = data.get("provider") or "gemini"
    target_lang = data.get("target_lang") or "Tiếng Việt"
    custom_prompt = data.get("custom_prompt") or ""

    # Tham số cho batch_edit
    edit_field = data.get("edit_field") or "title"
    if edit_field not in ("title", "description", "tags"):
        raise HTTPException(400, "Trường biên tập không hợp lệ (chỉ nhận title / description / tags).")
    edit_op = data.get("edit_op") or "append_end"
    find_str = data.get("find_str") or ""
    replace_str = data.get("replace_str") or ""

    conn = get_conn()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM video_queue WHERE id IN ({placeholders})", ids).fetchall()
    else:
        project_name = data.get("project_name") or "Default"
        rows = conn.execute("SELECT * FROM video_queue WHERE project_name = ?", (project_name,)).fetchall()

    updated = 0
    for r in rows:
        item = dict(r)
        if mode == "batch_edit":
            old_val = item.get(edit_field) or ""
            new_val = apply_batch_text_edit(old_val, edit_op, find_str, replace_str)
            conn.execute(
                f"UPDATE video_queue SET {edit_field} = ? WHERE id = ?",
                (new_val, item["id"]),
            )
            updated += 1
        else:
            res = ai_optimize_youtube_metadata(
                title=item.get("title") or "",
                description=item.get("description") or "",
                action=mode,
                provider=provider,
                target_lang=target_lang,
                custom_prompt=custom_prompt,
            )
            conn.execute(
                "UPDATE video_queue SET title = ?, description = ?, tags = ? WHERE id = ?",
                (res["title"], res["description"], res["tags"], item["id"]),
            )
            updated += 1

    conn.commit()
    conn.close()
    add_log("AI", f"Đã xử lý AI / Biên tập cho {updated} video (chế độ: {mode}).", "SUCCESS")
    return {"ok": True, "updated": updated}


# ==========================================================
# ĐIỀU KHIỂN PIPELINE (DOWNLOAD -> RENDER -> UPLOAD)
# ==========================================================
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
        rows = conn.execute(
            f"SELECT id, title, status, published_url FROM video_queue WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: r for r in rows}

    run_ids = []
    skipped = []
    for item_id in ids:
        row = by_id.get(item_id)
        if not row:
            skipped.append({"id": item_id, "title": "", "reason": "không còn tồn tại"})
            continue
        status = (row["status"] or "").strip()
        published = (row["published_url"] or "").strip()
        if do_up and not force_reupload and (status == "Completed" or published):
            skipped.append({"id": item_id, "title": row["title"] or f"#{item_id}", "reason": "đã đăng trước đó"})
            continue
        if do_up and not force_reupload and (status.startswith("Uploading") or status.startswith("Đang đăng")):
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

    result = pipeline_manager.start_batch(run_ids, do_dl, do_rd, do_up, force_reupload=force_reupload)
    if skipped:
        result["skipped"] = skipped
        result["message"] = (result.get("message") or "") + f" Bỏ qua {len(skipped)} video đã đăng/đang upload dở."
    return result


@app.post("/api/pipeline/stop")
def api_stop_pipeline():
    pipeline_manager.stop_all()
    return {"ok": True}


# ==========================================================
# QUẢN LÝ PROFILES CHROME & KÊNH YOUTUBE PHÂN CẤP
# ==========================================================
@app.get("/api/profiles")
def api_list_profiles():
    return get_all_profiles()


@app.get("/api/profiles/{profile_name}/channels")
def api_get_profile_channels(profile_name: str):
    return get_channels_by_profile(profile_name)


@app.post("/api/profiles")
def api_create_profile(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên Profile không được để trống.")
    from config import APP_PROFILES_DIR
    p_path = str(APP_PROFILES_DIR / name)
    Path(p_path).mkdir(parents=True, exist_ok=True)
    proxy = (data.get("proxy") or "").strip()
    note = (data.get("note") or "").strip()

    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chrome_profiles (name, path, proxy, source, note) VALUES (?, ?, ?, 'Local', ?)",
        (name, p_path, proxy, note),
    )
    chk = conn.execute("SELECT id FROM youtube_channels WHERE profile_name = ?", (name,)).fetchone()
    if not chk:
        conn.execute(
            """
            INSERT INTO youtube_channels (name, handle, group_name, upload_mode, profile_name, profile_path, proxy, emulate_mobile, status, note)
            VALUES (?, ?, 'Default', 'playwright', ?, ?, ?, 0, 'Ready', 'Kênh chính theo Profile')
            """,
            (f"YT - {name}", f"@{name.lower().replace(' ', '')}", name, p_path, proxy),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "name": name, "path": p_path}


@app.put("/api/profiles/{profile_name}")
def api_update_profile(profile_name: str, data: Dict[str, Any] = Body(...)):
    conn = get_conn()
    row = conn.execute("SELECT name FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    if "proxy" in data:
        proxy = (data.get("proxy") or "").strip()
        conn.execute("UPDATE chrome_profiles SET proxy = ? WHERE name = ?", (proxy, profile_name))
        # Quy tắc an toàn: kênh thuộc Profile phải đi cùng Proxy với lúc đăng nhập thủ công
        conn.execute("UPDATE youtube_channels SET proxy = ? WHERE profile_name = ?", (proxy, profile_name))
    if "note" in data:
        conn.execute(
            "UPDATE chrome_profiles SET note = ? WHERE name = ?",
            ((data.get("note") or "").strip(), profile_name),
        )
    conn.commit()
    conn.close()
    add_log("Profile", f"Đã cập nhật cấu hình Profile Chrome '{profile_name}'.", "INFO")
    return {"ok": True}


@app.delete("/api/profiles/{profile_name}")
def api_delete_profile(profile_name: str):
    """Xóa Profile khỏi hệ thống (CHỈ xóa bản ghi database, tuyệt đối không xóa thư mục Chrome trên ổ đĩa)."""
    conn = get_conn()
    row = conn.execute("SELECT path FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    conn.execute("DELETE FROM youtube_channels WHERE profile_name = ?", (profile_name,))
    conn.execute("DELETE FROM chrome_profiles WHERE name = ?", (profile_name,))
    conn.commit()
    conn.close()
    add_log("Profile", f"Đã xóa Profile Chrome '{profile_name}' khỏi hệ thống (giữ nguyên thư mục trên đĩa).", "WARNING")
    return {"ok": True}


@app.post("/api/profiles/{profile_name}/open-browser")
def api_open_profile_browser(profile_name: str):
    conn = get_conn()
    prof = conn.execute("SELECT * FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    conn.close()
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    ch_like = {
        "id": 0,
        "name": f"Profile {profile_name}",
        "profile_name": profile_name,
        "profile_path": prof["path"],
        "proxy": prof["proxy"] or "",
    }
    return open_profile_for_login(ch_like)


@app.post("/api/profiles/{profile_name}/check-login")
def api_check_profile_login(profile_name: str):
    """Mở Chrome Profile ở vị trí khuất để kiểm tra phiên đăng nhập Google (kèm Proxy nếu có)."""
    conn = get_conn()
    prof = conn.execute("SELECT * FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    conn.close()
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    try:
        probe = probe_profile_login(prof["path"], prof["proxy"] or "", grab_channels=True)
    except Exception as e:
        add_log("Profile", f"Lỗi kiểm tra đăng nhập Profile '{profile_name}': {e}", "ERROR")
        raise HTTPException(500, f"Lỗi khi mở Profile để kiểm tra: {e}")

    new_status = "Đã đăng nhập" if probe.get("logged_in") else "Chưa đăng nhập"
    conn = get_conn()
    conn.execute(
        "UPDATE chrome_profiles SET login_status = ?, last_login_check = ? WHERE name = ?",
        (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile_name),
    )
    conn.execute("UPDATE youtube_channels SET status = ? WHERE profile_name = ?", (new_status, profile_name))
    conn.commit()
    conn.close()
    add_log(
        "Profile",
        f"Kiểm tra đăng nhập Profile '{profile_name}': {new_status}. {probe.get('message', '')}",
        "SUCCESS" if probe.get("logged_in") else "WARNING",
    )
    return {"ok": True, **probe, "login_status": new_status}


@app.post("/api/profiles/{profile_name}/refresh-channels")
def api_refresh_profile_channels(profile_name: str):
    """Quét danh sách kênh YouTube thật trong Chrome Profile và cập nhật vào bảng youtube_channels."""
    conn = get_conn()
    prof = conn.execute("SELECT * FROM chrome_profiles WHERE name = ?", (profile_name,)).fetchone()
    conn.close()
    if not prof:
        raise HTTPException(404, "Không tìm thấy Profile Chrome này.")
    try:
        probe = probe_profile_login(prof["path"], prof["proxy"] or "", grab_channels=True)
    except Exception as e:
        add_log("Profile", f"Lỗi quét kênh Profile '{profile_name}': {e}", "ERROR")
        raise HTTPException(500, f"Lỗi khi mở Profile để quét kênh: {e}")

    prof_path = prof["path"]
    prof_proxy = prof["proxy"] or ""
    found = probe.get("channels") or []
    new_status = "Đã đăng nhập" if probe.get("logged_in") else "Chưa đăng nhập"

    conn = get_conn()
    conn.execute(
        "UPDATE chrome_profiles SET login_status = ?, last_login_check = ? WHERE name = ?",
        (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile_name),
    )
    conn.execute("UPDATE youtube_channels SET status = ? WHERE profile_name = ?", (new_status, profile_name))

    added, updated = 0, 0
    for ch in found:
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
            conn.execute(
                """
                INSERT INTO youtube_channels (
                    name, handle, email, channel_id, group_name, upload_mode, profile_name,
                    profile_path, proxy, emulate_mobile, status, note
                ) VALUES (?, ?, ?, ?, 'Mặc định', 'playwright', ?, ?, ?, 0, ?, 'Kênh quét tự động từ Chrome Profile')
                """,
                (ch_name, handle, email, channel_id, profile_name, prof_path, prof_proxy, new_status),
            )
            added += 1
    conn.commit()
    conn.close()
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
    conn.close()
    return [public_channel(row) for row in rows]


@app.post("/api/channels")
def api_create_channel(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên kênh không được để trống.")
    prof_name = (data.get("profile_name") or name).strip()
    prof_path = (data.get("profile_path") or "").strip()
    if not prof_path:
        from config import DHB_PROFILE_CHROME_DIR, APP_PROFILES_DIR
        candidate = DHB_PROFILE_CHROME_DIR / prof_name
        prof_path = str(candidate) if candidate.exists() else str(APP_PROFILES_DIR / prof_name)

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO youtube_channels (
            name, channel_id, handle, group_name, upload_mode, profile_name,
            profile_path, emulate_mobile, use_hidden_desktop, proxy, oauth_token_json,
            default_category_id, default_privacy, made_for_kids, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            data.get("channel_id") or "",
            data.get("handle") or "",
            data.get("group_name") or "Mặc định",
            data.get("upload_mode") or "playwright",
            prof_name,
            prof_path,
            1 if data.get("emulate_mobile", False) else 0,
            1 if data.get("use_hidden_desktop") else 0,
            data.get("proxy") or "",
            data.get("oauth_token_json") or "",
            data.get("default_category_id") or "22",
            data.get("default_privacy") or "public",
            1 if data.get("made_for_kids") else 0,
            data.get("note") or "",
        ),
    )
    conn.commit()
    conn.close()
    add_log("Channel", f"Đã thêm kênh YouTube: {name}", "SUCCESS", channel_name=name)
    return {"ok": True}


@app.put("/api/channels/{channel_id}")
def api_update_channel(channel_id: int, data: Dict[str, Any] = Body(...)):
    """Cập nhật thông tin kênh YouTube (tên, handle, nhóm, proxy, chế độ đăng, cấu hình mặc định...)."""
    text_fields = (
        "name", "channel_id", "handle", "email", "group_name", "upload_mode", "proxy",
        "oauth_token_json", "default_category_id", "default_privacy", "note", "status",
        "profile_name", "profile_path",
    )
    bool_fields = ("emulate_mobile", "use_hidden_desktop", "made_for_kids", "altered_content")

    if "name" in data and not (data.get("name") or "").strip():
        raise HTTPException(400, "Tên kênh không được để trống.")
    if "upload_mode" in data and data.get("upload_mode") not in ("playwright", "youtube_api"):
        raise HTTPException(400, "Phương thức đăng không hợp lệ (chỉ nhận playwright / youtube_api).")
    if "default_privacy" in data and data.get("default_privacy") not in ("public", "unlisted", "private"):
        raise HTTPException(400, "Chế độ hiển thị mặc định không hợp lệ (public / unlisted / private).")

    updates: Dict[str, Any] = {}
    for k in text_fields:
        if k in data:
            v = data.get(k)
            if k == "oauth_token_json" and not str(v or "").strip():
                # Ô token để trống = giữ nguyên token đã lưu (API không trả lại secret).
                continue
            updates[k] = "" if v is None else str(v).strip()
    for k in bool_fields:
        if k in data:
            updates[k] = 1 if data.get(k) else 0

    if not updates:
        return {"ok": False, "message": "Không có trường hợp lệ để cập nhật."}

    conn = get_conn()
    row = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Không tìm thấy kênh YouTube này.")

    # Đổi Profile: tự đồng bộ profile_path từ bảng chrome_profiles nếu người dùng không nhập đường dẫn mới
    if "profile_name" in updates and not updates.get("profile_path"):
        prof_name = updates["profile_name"]
        if not prof_name:
            conn.close()
            raise HTTPException(400, "Tên Profile Chrome không được để trống.")
        prof_row = conn.execute("SELECT path FROM chrome_profiles WHERE name = ?", (prof_name,)).fetchone()
        if prof_row and prof_row["path"]:
            updates["profile_path"] = prof_row["path"]

    cols = ", ".join(f"{k} = ?" for k in updates.keys())
    conn.execute(f"UPDATE youtube_channels SET {cols} WHERE id = ?", list(updates.values()) + [channel_id])
    conn.commit()
    conn.close()
    add_log("Channel", f"Đã cập nhật thông tin kênh YouTube #{channel_id}.", "INFO")
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
def api_delete_channel(channel_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM youtube_channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/open-browser")
def api_open_channel_browser(channel_id: int):
    conn = get_conn()
    ch = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel_id,)).fetchone()
    conn.close()
    if not ch:
        raise HTTPException(404, "Không tìm thấy kênh.")
    return open_profile_for_login(dict(ch))


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


@app.get("/api/groups")
def api_list_groups():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM channel_groups ORDER BY name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/groups")
def api_create_group(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên nhóm không được để trống.")
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO channel_groups (name, description) VALUES (?, ?)",
        (name, data.get("description") or ""),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ==========================================================
# QUẢN LÝ TÙY CHỌN ĐĂNG & LINK SHOPEE AFFILIATE
# ==========================================================
@app.get("/api/options")
def api_list_options():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM upload_option_groups ORDER BY id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/options")
def api_create_option(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên bộ tùy chọn không được để trống.")
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO upload_option_groups (
            name, affiliate_enabled, affiliate_url, affiliate_CTA,
            pin_comment_enabled, pin_comment_text, append_hashtags,
            default_tags, category_id, language, made_for_kids, ai_label,
            disable_shorts_remix
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            1 if data.get("affiliate_enabled") else 0,
            data.get("affiliate_url") or "",
            data.get("affiliate_CTA") or "👉 Mua ngay sản phẩm chính hãng tại đây:",
            1 if data.get("pin_comment_enabled") else 0,
            data.get("pin_comment_text") or "",
            data.get("append_hashtags") or "#shorts #trending #viral",
            data.get("default_tags") or "",
            data.get("category_id") or "22",
            data.get("language") or "vi",
            1 if data.get("made_for_kids") else 0,
            1 if data.get("ai_label") else 0,
            1 if data.get("disable_shorts_remix") else 0,
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": new_id}


@app.put("/api/options/{opt_id}")
def api_update_option(opt_id: int, data: Dict[str, Any] = Body(...)):
    """Sửa đầy đủ một bộ tùy chọn đăng (affiliate, pin comment, hashtags, tags, danh mục...)."""
    if "name" in data and not (data.get("name") or "").strip():
        raise HTTPException(400, "Tên bộ tùy chọn không được để trống.")

    text_fields = (
        "name", "affiliate_url", "affiliate_CTA", "pin_comment_text",
        "append_hashtags", "default_tags", "category_id", "language",
    )
    bool_fields = ("affiliate_enabled", "pin_comment_enabled", "made_for_kids", "ai_label", "disable_shorts_remix")

    updates: Dict[str, Any] = {}
    for k in text_fields:
        if k in data:
            v = data.get(k)
            updates[k] = ("" if v is None else str(v)).strip()
    for k in bool_fields:
        if k in data:
            updates[k] = 1 if data.get(k) else 0

    if not updates:
        return {"ok": False, "message": "Không có trường hợp lệ để cập nhật."}

    conn = get_conn()
    row = conn.execute("SELECT id FROM upload_option_groups WHERE id = ?", (opt_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Không tìm thấy bộ tùy chọn đăng này.")
    cols = ", ".join(f"{k} = ?" for k in updates.keys())
    conn.execute(f"UPDATE upload_option_groups SET {cols} WHERE id = ?", list(updates.values()) + [opt_id])
    conn.commit()
    conn.close()
    add_log("Options", f"Đã cập nhật bộ tùy chọn đăng #{opt_id}.", "INFO")
    return {"ok": True}


@app.delete("/api/options/{opt_id}")
def api_delete_option(opt_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM upload_option_groups WHERE id = ?", (opt_id,))
    conn.commit()
    conn.close()
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
@app.get("/api/render-presets")
def api_list_render_presets():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM render_presets ORDER BY name ASC").fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config_json") or "{}")
        except Exception:
            d["config"] = {}
        res.append(d)
    return res


@app.post("/api/render-presets")
def api_save_render_preset(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Tên preset không được để trống.")
    render_mode = data.get("render_mode") or "shorts_9_16"
    cfg = data.get("config") or {}
    note = (data.get("note") or "").strip()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO render_presets (name, render_mode, config_json, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            render_mode = excluded.render_mode,
            config_json = excluded.config_json,
            note = excluded.note
        """,
        (name, render_mode, json.dumps(cfg, ensure_ascii=False), note),
    )
    conn.commit()
    conn.close()
    add_log("Render", f"Đã lưu Preset Render '{name}' (chế độ {render_mode}).", "SUCCESS")
    return {"ok": True}


@app.delete("/api/render-presets/{preset_id}")
def api_delete_render_preset(preset_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM render_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ==========================================================
# AUTO MONITOR 24/7 (QUÉT & REUP TỰ ĐỘNG)
# ==========================================================
@app.get("/api/monitors")
def api_list_monitors():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/monitors")
def api_create_monitor(data: Dict[str, Any] = Body(...)):
    name = (data.get("name") or "").strip()
    source_urls = (data.get("source_urls") or "").strip()
    if not name or not source_urls:
        raise HTTPException(400, "Vui lòng nhập Tên luồng Monitor và Danh sách link kênh nguồn.")
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO monitors (
            name, source_urls, target_channel_id, target_group_name,
            upload_to, render_mode, option_group_id, ai_Action, ai_provider,
            ai_target_lang, min_views, check_interval_minutes, max_videos_per_scan
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            source_urls,
            data.get("target_channel_id") or None,
            data.get("target_group_name") or "Mặc định",
            data.get("upload_to") or "Shorts",
            data.get("render_mode") or "shorts_9_16",
            data.get("option_group_id") or None,
            data.get("ai_Action") or "rewrite_seo",
            data.get("ai_provider") or "gemini",
            data.get("ai_target_lang") or "Tiếng Việt",
            int(data.get("min_views") or 0),
            int(data.get("check_interval_minutes") or 30),
            int(data.get("max_videos_per_scan") or 3),
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/monitors/{monitor_id}/toggle")
def api_toggle_monitor(monitor_id: int):
    conn = get_conn()
    row = conn.execute("SELECT is_running FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Không tìm thấy Monitor.")
    if row["is_running"]:
        return pipeline_manager.stop_monitor(monitor_id)
    else:
        return pipeline_manager.start_monitor(monitor_id)


@app.delete("/api/monitors/{monitor_id}")
def api_delete_monitor(monitor_id: int):
    pipeline_manager.stop_monitor(monitor_id)
    conn = get_conn()
    conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


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
        item["message"] = redact_secrets(item.get("message"), secrets)
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
    where_sql, params = _log_query_parts(module, status, q)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM system_logs{where_sql} ORDER BY id DESC LIMIT 20000",
        params,
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "module", "status", "channel_name", "video_title", "message"])
    secrets = collect_secret_values()
    for row in rows:
        writer.writerow([
            row["id"], row["created_at"], row["module"], row["status"],
            row["channel_name"], row["video_title"], redact_secrets(row["message"], secrets),
        ])
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


@app.post("/api/settings")
def api_save_settings(data: Dict[str, Any] = Body(...)):
    payload = {
        key: value
        for key, value in data.items()
        if not (is_secret_setting_key(key) and not str(value or "").strip())
    }
    save_settings(payload)
    add_log("Settings", "Đã lưu cấu hình hệ thống.", "SUCCESS")
    return {"ok": True}


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
        add_log("Housekeeping", f"Export CSV thủ công thất bại: {exc}", "ERROR")
        raise HTTPException(status_code=500, detail=f"Export thất bại: {exc}")
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
