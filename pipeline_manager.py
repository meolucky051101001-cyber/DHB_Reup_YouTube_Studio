import json
import hashlib
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from database import (
    get_conn, add_log, get_settings, register_managed_artifact,
    set_log_context, clear_log_context, queue_requires_review,
)
from config import APP_OUTPUT_DIR, APP_TEMP_DIR
from scanner_downloader import download_video, scan_social_urls
from render_engine import render_video, generate_thumbnail_from_video
from ai_engine import ai_optimize_youtube_metadata
from youtube_uploader import (
    UploadNotSent, upload_video_to_youtube, validate_upload_preflight, normalize_published_video_url,
)
from content_identity import normalize_source_url, sha256_file, source_dedupe_key
from group_distribution import choose_target_channel, target_group_names
from upload_approval import PreviewStale, approval_item, asset_stamps, encode, upload_source_path
from antidetect_client import AntidetectNotReady
from phase4_rules import (
    GROUP_SCHEMA_VERSION, serialize_group_names, validate_metadata_fields,
    validate_queue_fields, validate_source_url,
)
from storage_manager import (
    MIN_DOWNLOAD_HEADROOM_BYTES,
    delete_completed_temp_artifact,
    ensure_disk_space,
    estimate_render_bytes,
)


MONITOR_HEARTBEAT_SECONDS = 30
MONITOR_HEALTH_CHECK_SECONDS = 60
MONITOR_STALE_SECONDS = 180
MONITOR_RESTART_WINDOW_SECONDS = 900
MONITOR_MAX_RESTARTS = 3

# Giãn cách tối thiểu bắt buộc giữa 2 lượt upload thật (anti-checkpoint, bảo vệ kênh).
UPLOAD_MIN_GAP_SECONDS = 30 * 60
_LAST_UPLOAD_SETTING_KEY = "last_upload_started_epoch"
_LAST_UPLOAD_FINISHED_SETTING_KEY = "last_upload_finished_epoch"
_UPLOAD_GAP_LOCK = threading.Lock()
_COOLDOWN_ANCHOR = None
# Fail-closed trong tiến trình hiện tại: nếu không ghi được mốc kết thúc upload,
# mọi lượt đăng sau bị chặn cho tới khi khởi động lại tool và kiểm tra lại.
_UPLOAD_GAP_STATE_BROKEN = False

RENDER_QUEUE_DEFAULT_WORKERS = 2


def _log_run_id(batch_token: str) -> str:
    """Derive a correlation ID without exposing the claim token in API-visible logs."""
    return hashlib.sha256(str(batch_token or "").encode("utf-8")).hexdigest()[:16]


def _history_contains_item(conn, item, channel_id, content_key):
    """Match current identity to both new fingerprints and legacy source IDs."""
    rows = conn.execute(
        "SELECT content_key, source_post_id, source_url FROM published_history "
        "WHERE channel_id = ? OR channel_id IS NULL",
        (channel_id,),
    ).fetchall()
    post_id = str(item.get("post_id") or "").strip()
    normalized_url = normalize_source_url(item.get("source_url"))
    for row in rows:
        if content_key and str(row["content_key"] or "").strip() == content_key:
            return True
        if post_id and str(row["source_post_id"] or "").strip() == post_id:
            return True
        old_url = normalize_source_url(row["source_url"])
        if normalized_url and old_url == normalized_url:
            return True
    return False


def _store_content_fingerprint(queue_id, claim_token, content_key, allow_duplicate=False):
    """Persist a file fingerprint and serialize same-project queue dedupe."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT project_name, claim_token FROM video_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not row or str(row["claim_token"] or "") != claim_token:
            conn.rollback()
            return "claim_lost"
        duplicate = conn.execute(
            "SELECT id FROM video_queue WHERE project_name = ? AND content_key = ? AND id != ? LIMIT 1",
            (row["project_name"], content_key, queue_id),
        ).fetchone()
        if duplicate and not allow_duplicate:
            conn.execute(
                "UPDATE video_queue SET status = 'SkippedDuplicate', "
                "error_message = ? WHERE id = ? AND claim_token = ?",
                (f"Nội dung trùng với video #{duplicate['id']} trong cùng dự án.", queue_id, claim_token),
            )
            conn.commit()
            return "duplicate"
        conn.execute(
            "UPDATE video_queue SET content_key = ? WHERE id = ? AND claim_token = ?",
            (content_key, queue_id, claim_token),
        )
        conn.commit()
        return "stored"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _acquire_content_reservation(queue_id, claim_token, item, channel_id, force_reupload=False):
    """Atomically reserve one identity for one channel before the upload wait."""
    content_key = source_dedupe_key(item, queue_id)
    if not content_key:
        raise RuntimeError("Không xác định được khóa nội dung để chống đăng trùng.")
    channel_id = int(channel_id)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT claim_token FROM video_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not owner or str(owner["claim_token"] or "") != claim_token:
            conn.rollback()
            return content_key, "claim_lost"

        if not force_reupload and _history_contains_item(
            conn, item, channel_id, str(item.get("content_key") or "").strip()
        ):
            conn.commit()
            return content_key, "published"

        existing = conn.execute(
            "SELECT queue_id, claim_token, state FROM content_reservations "
            "WHERE content_key = ? AND channel_id = ?",
            (content_key, channel_id),
        ).fetchone()
        if existing:
            if (
                int(existing["queue_id"]) == int(queue_id)
                and str(existing["claim_token"] or "") == claim_token
                and str(existing["state"] or "Active") == "Active"
            ):
                conn.commit()
                return content_key, "acquired"
            state = str(existing["state"] or "Active")
            conn.commit()
            return content_key, "needs_review" if state == "NeedsReview" else "busy"

        conn.execute(
            "INSERT INTO content_reservations "
            "(content_key, channel_id, queue_id, claim_token, state) VALUES (?, ?, ?, ?, 'Active')",
            (content_key, channel_id, queue_id, claim_token),
        )
        conn.commit()
        return content_key, "acquired"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_content_reservation_state(content_key, channel_id, queue_id, claim_token, state):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE content_reservations SET state = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE content_key = ? AND channel_id = ? AND queue_id = ? AND claim_token = ?",
            (state, content_key, channel_id, queue_id, claim_token),
        )
        conn.commit()
    finally:
        conn.close()


def _validate_content_reservation(content_key, channel_id, queue_id, claim_token, item, force_reupload=False):
    """Recheck lock ownership and history immediately before invoking the uploader."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        reservation = conn.execute(
            "SELECT state FROM content_reservations WHERE content_key = ? AND channel_id = ? "
            "AND queue_id = ? AND claim_token = ?",
            (content_key, int(channel_id), queue_id, claim_token),
        ).fetchone()
        if not reservation or str(reservation["state"] or "Active") != "Active":
            conn.rollback()
            return "reservation_lost"
        if not force_reupload and _history_contains_item(
            conn, item, int(channel_id), str(item.get("content_key") or "").strip()
        ):
            conn.commit()
            return "published"
        conn.commit()
        return "ready"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_content_reservation(content_key, channel_id, queue_id, claim_token):
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM content_reservations WHERE content_key = ? AND channel_id = ? "
            "AND queue_id = ? AND claim_token = ? AND state = 'Active'",
            (content_key, channel_id, queue_id, claim_token),
        )
        conn.commit()
    finally:
        conn.close()


def _history_source_post_id(item, queue_id):
    post_id = str(item.get("post_id") or "").strip()
    if post_id:
        return post_id
    normalized_url = normalize_source_url(item.get("source_url"))
    return f"url:{normalized_url}" if normalized_url else f"queue:{queue_id}"


def _start_upload_attempt(queue_id, claim_token, item, channel_id):
    """Create an attempt or advance a previously authorized single-item retry."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queue = conn.execute(
            "SELECT claim_token, manual_reupload_authorized, manual_reupload_reason, last_attempt_id "
            "FROM video_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not queue or str(queue["claim_token"] or "") != claim_token:
            conn.rollback()
            return None

        authorized = bool(queue["manual_reupload_authorized"])
        attempt = None
        if authorized and queue["last_attempt_id"]:
            attempt = conn.execute(
                "SELECT id, outcome FROM upload_attempts WHERE id = ? AND queue_id = ?",
                (queue["last_attempt_id"], queue_id),
            ).fetchone()
            if attempt and attempt["outcome"] != "Authorized":
                attempt = None

        if attempt:
            attempt_id = int(attempt["id"])
            conn.execute(
                "UPDATE upload_attempts SET phase = 'Uploading', outcome = 'InProgress', "
                "channel_id = ?, content_key = ?, source_post_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (
                    channel_id,
                    str(item.get("content_key") or "").strip(),
                    _history_source_post_id(item, queue_id),
                    attempt_id,
                ),
            )
        else:
            attempt_no = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM upload_attempts WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO upload_attempts "
                "(queue_id, attempt_no, attempt_type, source_post_id, content_key, channel_id, phase, outcome, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Uploading', 'InProgress', ?)",
                (
                    queue_id,
                    attempt_no,
                    "manual_reupload" if authorized else "upload",
                    _history_source_post_id(item, queue_id),
                    str(item.get("content_key") or "").strip(),
                    channel_id,
                    str(queue["manual_reupload_reason"] or "") if authorized else "",
                ),
            )
            attempt_id = int(cur.lastrowid)

        conn.execute(
            "UPDATE video_queue SET last_attempt_id = ?, manual_reupload_authorized = 0, "
            "manual_reupload_reason = '' WHERE id = ? AND claim_token = ?",
            (attempt_id, queue_id, claim_token),
        )
        conn.commit()
        return attempt_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_upload_attempt(attempt_id, outcome, published_url="", error_message=""):
    if not attempt_id:
        return
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE upload_attempts SET phase = 'Finished', outcome = ?, published_url = ?, "
            "error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (outcome, published_url or "", error_message or "", attempt_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_upload_attempt_phase(attempt_id, phase):
    if not attempt_id or not phase:
        return
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE upload_attempts SET phase = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND outcome = 'InProgress'",
            (phase, attempt_id),
        )
        conn.commit()
    finally:
        conn.close()


def _monitor_source_already_published(conn, source_post_id: str, target_channel_id: Optional[int]) -> bool:
    """Kiểm tra trùng theo kênh đích; monitor nhóm vẫn dedupe toàn cục như trước."""
    if not source_post_id:
        return False
    try:
        channel_id = int(target_channel_id) if target_channel_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        channel_id = None
    if channel_id is None:
        row = conn.execute(
            "SELECT id FROM published_history WHERE source_post_id = ? LIMIT 1",
            (source_post_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM published_history WHERE source_post_id = ? AND channel_id = ? LIMIT 1",
            (source_post_id, channel_id),
        ).fetchone()
    return row is not None


# Điều kiện "nguồn này đã nằm trong hàng đợi của monitor, cùng đích đăng" — dùng ở cả
# bước lọc khi quét LẪN trong chính câu INSERT (chống TOCTOU: monitor quét xong phải
# gọi AI hàng chục giây rồi mới insert, monitor khác cùng đích kịp chen hàng giống hệt).
# Cùng nhóm kênh = cùng vòng xoay kênh (chia theo id video) nên coi là cùng đích.
_MONITOR_QUEUED_PREDICATE = """
        post_id = :pid
        AND (
            project_name = :own_project
            OR (
                project_name LIKE 'Monitor -%'
                AND COALESCE(target_channel_id, -1) = COALESCE(:chan, -1)
                AND COALESCE(target_group_name, '') = COALESCE(:grp, '')
            )
        )
    """


def _monitor_source_queued(conn, pid: str, own_project: str, chan, grp) -> bool:
    """Nguồn đã được monitor nào đó cùng đích (hoặc chính monitor này) đưa vào hàng đợi?"""
    if not pid:
        return False
    grp_norm = grp or "Mặc định"  # chuẩn hóa như insert_monitor_queue_item
    row = conn.execute(
        f"SELECT id FROM video_queue WHERE {_MONITOR_QUEUED_PREDICATE} LIMIT 1",
        {"pid": pid, "own_project": own_project, "chan": chan, "grp": grp_norm},
    ).fetchone()
    return row is not None


def insert_monitor_queue_item(
    conn,
    proj_name: str,
    mon: Dict[str, Any],
    item_data: Dict[str, Any],
    title: str,
    desc: str,
    tags: str,
) -> Optional[int]:
    """Insert hàng monitor vào video_queue một cách NGUYÊN TỨC: chỉ chèn khi chưa có
    dòng nào trùng nguồn (cùng đích đăng). Trả về id dòng mới, hoặc None nếu bị chặn."""
    grp_norm = mon.get("target_group_name") or "Mặc định"
    cur = conn.execute(
        f"""
        INSERT INTO video_queue (
            project_name, source_type, platform, post_id, source_url, input_video_path,
            title, description, tags, duration, view_count, like_count,
            upload_to, visibility, target_type, target_channel_id, target_group_name,
            target_group_names_json, group_schema_version,
            option_group_id, render_mode, render_config_json, emulate_mobile, status
        )
        SELECT
            :proj, :src_type, :platform, :pid, :src_url, :input_path,
            :title, :desc, :tags, :duration, :views, :likes,
            :upload_to, 'public', :target_type, :chan, :grp, :groups_json, :group_schema_version,
            :opt_id, :render_mode, :render_cfg, 0, 'Pending'
        WHERE NOT EXISTS (
            SELECT 1 FROM video_queue WHERE {_MONITOR_QUEUED_PREDICATE}
        )
        """,
        {
            "proj": proj_name,
            "src_type": item_data["source_type"],
            "platform": item_data["platform"],
            "pid": item_data["post_id"],
            "src_url": item_data["source_url"],
            "input_path": item_data["input_video_path"],
            "title": title,
            "desc": desc,
            "tags": tags,
            "duration": item_data["duration"],
            "views": item_data["view_count"],
            "likes": item_data["like_count"],
            "upload_to": mon.get("upload_to") or "Shorts",
            "target_type": "CHANNEL" if mon.get("target_channel_id") else "GROUP",
            "chan": mon.get("target_channel_id"),
            "grp": grp_norm,
            "groups_json": serialize_group_names([grp_norm]) if not mon.get("target_channel_id") else "[]",
            "group_schema_version": GROUP_SCHEMA_VERSION,
            "opt_id": mon.get("option_group_id"),
            "render_mode": mon.get("render_mode") or "shorts_9_16",
            "render_cfg": mon.get("render_config_json") or "{}",
            "own_project": proj_name,
        },
    )
    if cur.rowcount != 1:
        return None
    return cur.lastrowid


class RenderQueue:
    """Pool render riêng: FFmpeg đã chạy ở process con, lớp này giới hạn số render đồng thời
    độc lập với số luồng tải/đăng của pipeline."""

    def __init__(self):
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._workers = 0
        self._active = 0

    def configure(self, workers: int):
        workers = max(1, min(8, int(workers or RENDER_QUEUE_DEFAULT_WORKERS)))
        with self._lock:
            if self._active > 0:
                return
            if self._executor is not None and workers == self._workers:
                return
            old = self._executor
            self._workers = workers
            self._executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="DHB-Render"
            )
        if old is not None:
            old.shutdown(wait=False, cancel_futures=True)

    def _run(self, fn, args, kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            if self._executor is None:
                try:
                    configured = int(get_settings().get("max_concurrent_renders", RENDER_QUEUE_DEFAULT_WORKERS) or RENDER_QUEUE_DEFAULT_WORKERS)
                except Exception:
                    configured = RENDER_QUEUE_DEFAULT_WORKERS
                self._workers = max(1, min(8, configured))
                self._executor = ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix="DHB-Render"
                )
            self._active += 1
            executor = self._executor
        return executor.submit(self._run, fn, args, kwargs)

    def shutdown(self):
        with self._lock:
            executor = self._executor
            self._executor = None
            self._workers = 0
            self._active = 0
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


render_queue = RenderQueue()


class UploadGapStateError(RuntimeError):
    """Không xác minh được trạng thái giãn cách upload — tuyệt đối không đăng."""


def _parse_upload_epoch(raw, key: str) -> float:
    if raw in (None, ""):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise UploadGapStateError(
            f"Mốc giãn cách '{key}' không hợp lệ ({raw!r}) — không thể xác minh an toàn."
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise UploadGapStateError(
            f"Mốc giãn cách '{key}' âm ({raw!r}) — không thể xác minh an toàn."
        )
    return value


def read_last_upload_epoch() -> float:
    """Mốc upload gần nhất (bắt đầu hoặc kết thúc).

    Fail closed: không đọc/không parse được mốc thì raise UploadGapStateError
    thay vì trả 0.0 (lỗi đọc từng bị hiểu nhầm là "chưa từng đăng").
    """
    if _UPLOAD_GAP_STATE_BROKEN:
        raise UploadGapStateError(
            "Trạng thái giãn cách upload đang không xác định (lần trước không ghi được mốc kết thúc). "
            "Hãy khởi động lại tool và kiểm tra lại trước khi đăng."
        )
    try:
        settings = get_settings()
    except Exception as exc:
        raise UploadGapStateError(f"Không đọc được cài đặt giãn cách upload: {exc}") from exc
    started = _parse_upload_epoch(settings.get(_LAST_UPLOAD_SETTING_KEY), _LAST_UPLOAD_SETTING_KEY)
    finished = _parse_upload_epoch(settings.get(_LAST_UPLOAD_FINISHED_SETTING_KEY), _LAST_UPLOAD_FINISHED_SETTING_KEY)
    return max(started, finished)


def read_upload_gap_remaining(now: Optional[float] = None) -> float:
    """Số giây còn phải chờ trước lượt upload kế tiếp (âm = đã đủ giãn cách)."""
    with _UPLOAD_GAP_LOCK, closing(get_conn()) as conn:
        row = conn.execute("SELECT cooldown_until FROM upload_slot WHERE id = 1").fetchone()
        current = time.time() if now is None else float(now)
        if row and row["cooldown_until"] is not None:
            return _cooldown_remaining_locked(row["cooldown_until"], current)
    return read_last_upload_epoch() + UPLOAD_MIN_GAP_SECONDS - current


def _cooldown_remaining_locked(until, now):
    global _COOLDOWN_ANCHOR
    if until is None:
        return 0
    monotonic_now = time.monotonic()
    if _COOLDOWN_ANCHOR is None or _COOLDOWN_ANCHOR[0] != until:
        # After a restart, wait a full interval on the monotonic clock.
        _COOLDOWN_ANCHOR = (until, monotonic_now + UPLOAD_MIN_GAP_SECONDS)
    return max(until - now, _COOLDOWN_ANCHOR[1] - monotonic_now)


def _slot_connection():
    conn = get_conn()
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except Exception:
        conn.close()
        raise
    return conn


def _slot_finished(conn, owner, when, *, recovery=False):
    state = "recovery_required" if recovery else "active"
    return conn.execute(
        "UPDATE upload_slot SET state = 'cooldown', owner = NULL, finished_at = ?, cooldown_until = ? "
        "WHERE id = 1 AND state = ? AND owner = ?",
        (when, when + UPLOAD_MIN_GAP_SECONDS, state, owner),
    ).rowcount == 1


def _record_upload_finished_safely(title, owner, *, entered_uploader, cleanup_unconfirmed=False):
    global _UPLOAD_GAP_STATE_BROKEN, _COOLDOWN_ANCHOR
    try:
        with _UPLOAD_GAP_LOCK, closing(_slot_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if cleanup_unconfirmed:
                conn.execute(
                    "UPDATE upload_slot SET state = 'recovery_required' WHERE id = 1 AND state = 'active' AND owner = ?",
                    (owner,),
                )
            elif entered_uploader:
                now = time.time()
                if _slot_finished(conn, owner, now):
                    _COOLDOWN_ANCHOR = (now + UPLOAD_MIN_GAP_SECONDS, time.monotonic() + UPLOAD_MIN_GAP_SECONDS)
            else:
                conn.execute(
                    "UPDATE upload_slot SET state = CASE WHEN cooldown_until IS NULL THEN 'idle' ELSE 'cooldown' END, "
                    "owner = NULL WHERE id = 1 AND state = 'active' AND owner = ? AND attempt_id IS NULL", (owner,),
                )
    except Exception as exc:
        _UPLOAD_GAP_STATE_BROKEN = True
        try:
            add_log("Upload", f"Không ghi được kết thúc lượt upload: {exc}", "ERROR", video_title=title)
        except Exception:
            pass


def _mark_slot_entered(owner, attempt_id):
    with closing(_slot_connection()) as conn, conn:
        if not conn.execute(
            "UPDATE upload_slot SET attempt_id = ? WHERE id = 1 AND state = 'active' AND owner = ?",
            (attempt_id, owner),
        ).rowcount:
            raise UploadGapStateError("Không còn quyền sử dụng lượt upload.")


def _recover_upload_slot(conn):
    global _COOLDOWN_ANCHOR
    # Called at startup while the application holds its data-directory singleton.
    conn.execute("UPDATE upload_approvals SET state = 'interrupted' WHERE state = 'accepted'")
    row = conn.execute("SELECT * FROM upload_slot WHERE id = 1").fetchone()
    if row["state"] == "active":
        if row["attempt_id"] is None:
            conn.execute("UPDATE upload_slot SET state = 'idle', owner = NULL WHERE id = 1")
        else:
            conn.execute("UPDATE upload_slot SET state = 'recovery_required' WHERE id = 1")
    elif row["state"] == "idle" and row["started_at"] is None:
        settings = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN (?, ?)",
            (_LAST_UPLOAD_SETTING_KEY, _LAST_UPLOAD_FINISHED_SETTING_KEY),
        ).fetchall())
        started = _parse_upload_epoch(settings.get(_LAST_UPLOAD_SETTING_KEY), _LAST_UPLOAD_SETTING_KEY)
        finished = _parse_upload_epoch(settings.get(_LAST_UPLOAD_FINISHED_SETTING_KEY), _LAST_UPLOAD_FINISHED_SETTING_KEY)
        interrupted = conn.execute(
            "SELECT id, last_attempt_id FROM video_queue WHERE status LIKE 'Uploading%' "
            "OR status LIKE 'Đang đăng%' ORDER BY id LIMIT 1"
        ).fetchone()
        if interrupted:
            conn.execute(
                "UPDATE upload_slot SET state = 'recovery_required', owner = ?, queue_id = ?, attempt_id = ?, started_at = ? "
                "WHERE id = 1",
                (uuid.uuid4().hex, interrupted[0], interrupted[1], started),
            )
        elif started or finished:
            marker = max(started, finished)
            conn.execute(
                "UPDATE upload_slot SET state = 'cooldown', started_at = ?, finished_at = ?, cooldown_until = ? WHERE id = 1",
                (started, finished, marker + UPLOAD_MIN_GAP_SECONDS),
            )
    until = conn.execute("SELECT cooldown_until FROM upload_slot WHERE id = 1 AND state = 'cooldown'").fetchone()
    if until and until[0] is not None:
        _COOLDOWN_ANCHOR = (until[0], time.monotonic() + UPLOAD_MIN_GAP_SECONDS)


def upload_slot_recovery_status():
    with _UPLOAD_GAP_LOCK, closing(get_conn()) as conn:
        row = conn.execute("SELECT state, owner, queue_id, attempt_id, cooldown_until FROM upload_slot WHERE id = 1").fetchone()
        return {
            "state": row["state"], "queue_id": row["queue_id"], "attempt_id": row["attempt_id"],
            "cooldown_until": row["cooldown_until"],
            "remaining_seconds": max(0, math.ceil(_cooldown_remaining_locked(row["cooldown_until"], time.time())))
            if row["state"] == "cooldown" else None,
            "available": not _UPLOAD_GAP_STATE_BROKEN,
            "recovery_id": _log_run_id(row["owner"]) if row["state"] == "recovery_required" else None,
        }


def acknowledge_upload_slot_recovery(recovery_id):
    global _COOLDOWN_ANCHOR
    with _UPLOAD_GAP_LOCK, closing(_slot_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT owner FROM upload_slot WHERE id = 1 AND state = 'recovery_required'").fetchone()
        if not row or _log_run_id(row["owner"]) != recovery_id:
            return False
        now = time.time()
        if not _slot_finished(conn, row["owner"], now, recovery=True):
            return False
        _COOLDOWN_ANCHOR = (now + UPLOAD_MIN_GAP_SECONDS, time.monotonic() + UPLOAD_MIN_GAP_SECONDS)
        return True


def release_item_claim(item_id: int, token: str):
    if not token:
        return
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE video_queue SET claim_token = '' WHERE id = ? AND claim_token = ?",
            (item_id, token),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Claim bị rò rỉ khiến các lượt chạy sau bỏ qua video này ("đang được tiến trình
        # khác xử lý") cho tới khi restart — bắt buộc ghi log để nhận diện được.
        try:
            add_log("Pipeline", f"Không giải phóng được claim của video #{item_id}: {e}", "WARNING")
        except Exception:
            pass


def reserve_upload_slot(stop_event: threading.Event, queue_id=None):
    """Return a durable ownership token. Elapsed time cannot expire an active owner."""
    while not stop_event.is_set():
        try:
            with _UPLOAD_GAP_LOCK, closing(_slot_connection()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                if _UPLOAD_GAP_STATE_BROKEN:
                    raise UploadGapStateError("Trạng thái lượt upload chưa được xác nhận.")
                row = conn.execute("SELECT * FROM upload_slot WHERE id = 1").fetchone()
                if row["state"] == "recovery_required":
                    raise UploadGapStateError("Cần đối soát và xác nhận phiên upload cũ đã dừng.")
                if row["state"] != "active":
                    now = time.time()
                    remaining = _cooldown_remaining_locked(row["cooldown_until"], now)
                    if remaining <= 0 and not stop_event.is_set():
                        owner = uuid.uuid4().hex
                        conn.execute(
                            "UPDATE upload_slot SET state = 'active', owner = ?, process_id = ?, queue_id = ?, "
                            "attempt_id = NULL, started_at = ?, cooldown_until = NULL WHERE id = 1",
                            (owner, os.getpid(), queue_id, now),
                        )
                        return owner
        except UploadGapStateError:
            raise
        except Exception as exc:
            raise UploadGapStateError(f"Không xác minh được lượt upload: {exc}") from exc
        stop_event.wait(0.5)
    return False


def _check_upload_slot_preflight(conn):
    row = conn.execute("SELECT state FROM upload_slot WHERE id = 1").fetchone()
    if _UPLOAD_GAP_STATE_BROKEN or not row:
        raise PreviewStale("Chưa xác nhận được trạng thái lượt upload. Kiểm tra Nhật ký và slot.")
    if row["state"] == "recovery_required":
        raise PreviewStale("Lượt upload cũ cần đối soát. Mở Kiểm tra lượt upload trước khi chạy.")


def _preview_upload_file(item, options):
    do_download, do_render, _ = options
    local = str(item.get("source_type") or "").lower() in {"file", "localfile"} or (
        str(item.get("platform") or "").lower() in {"localfile", "localupload"}
    )
    output = item.get("output_video_path") or ""
    source = item.get("input_video_path") or ""
    if do_download and local and (not source or not os.path.isfile(source)):
        raise PreviewStale("File nguồn local không còn. Chọn lại file hoặc dùng Chỉ Đăng YT nếu còn bản render.")
    path = source if do_render else upload_source_path(item)
    file_kind = "render mới" if do_render else "bản render có sẵn" if output else "file nguồn"
    if do_download and not local and (do_render or not output) and (not path or not os.path.isfile(path)):
        return {"file_kind": "tải nguồn rồi render" if do_render else "tải từ nguồn", "file_name": ""}
    try:
        if not path or not os.path.isfile(path):
            raise FileNotFoundError
        with open(path, "rb") as stream:
            if not stream.read(1):
                raise OSError
    except (OSError, ValueError) as exc:
        raise PreviewStale(f"Không tìm thấy hoặc không đọc được {file_kind}. Kiểm tra lại file đã chọn.") from exc
    return {"file_kind": file_kind, "file_name": Path(path).name}


class PipelineManager:
    def __init__(self):
        self._stop_event = threading.Event()
        self._is_running = False
        self._active_threads = 0
        self._lock = threading.Lock()
        # Bộ đếm giới hạn max_uploads_per_run theo TỪNG làn (batch / mỗi monitor) —
        # tránh việc bật lại monitor giữa chừng xóa bộ đếm của batch đang chạy (S3).
        self._batch_upload_box = {"n": 0}
        self._monitor_upload_counts: Dict[int, Dict[str, int]] = {}
        self._monitor_threads: Dict[int, threading.Event] = {}
        self._monitor_workers: Dict[int, threading.Thread] = {}
        self._monitor_heartbeats: Dict[int, threading.Thread] = {}
        self._monitor_states: Dict[int, str] = {}
        self._monitor_restart_history: Dict[int, List[float]] = {}
        self._monitor_lock = threading.Lock()
        self._monitor_lifecycle_lock = threading.RLock()
        self._monitor_supervisor: Optional[threading.Thread] = None
        self._monitor_supervisor_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def batch_state(self) -> str:
        if not self._is_running:
            return "idle"
        return "stopping" if self._stop_event.is_set() else "running"

    def clear_stale_claims(self):
        """Phục hồi job mồ côi sau khi app đã giữ singleton lock.

        Một job giữa bước upload có thể đã được nhận bởi YouTube trước khi tiến
        trình cũ dừng. Job đó cần đối soát thay vì retry tự động. Download/render
        bị ngắt có thể được chạy lại từ file nguồn còn lưu.
        """
        global _UPLOAD_GAP_STATE_BROKEN
        try:
            conn = get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _recover_upload_slot(conn)
                except Exception:
                    _UPLOAD_GAP_STATE_BROKEN = True
                    raise
                cur = conn.execute("""
                    UPDATE video_queue
                    SET status = CASE
                            WHEN status LIKE 'Uploading%'
                              OR status LIKE 'Đang đăng%'
                              OR status LIKE 'Verifying%'
                              OR status LIKE 'Đang xác minh%'
                              OR (
                                  COALESCE(claim_token, '') != ''
                                  AND status NOT IN ('Pending', 'Downloaded', 'Rendered', 'Error', 'Stopped', 'Completed')
                                  AND status NOT LIKE 'Downloading%'
                                  AND status NOT LIKE 'Đang tải%'
                                  AND status NOT LIKE 'Rendering%'
                                  AND status NOT LIKE 'Đang Render%'
                                  AND status NOT LIKE 'Chờ giãn cách đăng%'
                                  AND status NOT LIKE '%Bản Quyền%'
                              )
                              OR (status = 'Completed' AND COALESCE(published_url, '') = '' AND COALESCE(claim_token, '') != '')
                            THEN 'NeedsReview'
                            WHEN status LIKE 'Downloading%'
                              OR status LIKE 'Đang tải%'
                              OR status LIKE 'Rendering%'
                              OR status LIKE 'Đang Render%'
                              OR status LIKE 'Chờ giãn cách đăng%'
                            THEN 'Pending'
                            ELSE status
                        END,
                        error_message = CASE
                            WHEN status LIKE 'Uploading%'
                              OR status LIKE 'Đang đăng%'
                              OR status LIKE 'Verifying%'
                              OR status LIKE 'Đang xác minh%'
                              OR (
                                  COALESCE(claim_token, '') != ''
                                  AND status NOT IN ('Pending', 'Downloaded', 'Rendered', 'Error', 'Stopped', 'Completed')
                                  AND status NOT LIKE 'Downloading%'
                                  AND status NOT LIKE 'Đang tải%'
                                  AND status NOT LIKE 'Rendering%'
                                  AND status NOT LIKE 'Đang Render%'
                                  AND status NOT LIKE 'Chờ giãn cách đăng%'
                                  AND status NOT LIKE '%Bản Quyền%'
                              )
                              OR (status = 'Completed' AND COALESCE(published_url, '') = '' AND COALESCE(claim_token, '') != '')
                            THEN 'Tiến trình cũ dừng khi đang đăng hoặc xác minh. Hãy kiểm tra YouTube Studio trước khi chạy lại.'
                            WHEN status LIKE 'Downloading%'
                              OR status LIKE 'Đang tải%'
                              OR status LIKE 'Rendering%'
                              OR status LIKE 'Đang Render%'
                              OR status LIKE 'Chờ giãn cách đăng%'
                            THEN 'Tiến trình cũ bị ngắt trước bước đăng; có thể tiếp tục xử lý.'
                            ELSE error_message
                        END,
                        claim_token = ''
                    WHERE COALESCE(claim_token, '') != ''
                       OR status LIKE 'Uploading%'
                       OR status LIKE 'Đang đăng%'
                       OR status LIKE 'Verifying%'
                       OR status LIKE 'Đang xác minh%'
                       OR status LIKE 'Downloading%'
                       OR status LIKE 'Đang tải%'
                       OR status LIKE 'Rendering%'
                       OR status LIKE 'Đang Render%'
                       OR status LIKE 'Chờ giãn cách đăng%'
                       OR (status = 'Completed' AND COALESCE(published_url, '') = '' AND COALESCE(claim_token, '') != '')
                """)
                reservations_exist = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'content_reservations'"
                ).fetchone()
                if reservations_exist:
                    # The singleton lock guarantees these Active reservations belong
                    # to a dead process. Ambiguous uploads remain reserved for review;
                    # work known to be safe to retry can release its reservation.
                    conn.execute("""
                        UPDATE content_reservations
                        SET state = 'NeedsReview', updated_at = CURRENT_TIMESTAMP
                        WHERE state = 'Active' AND (
                            NOT EXISTS (
                                SELECT 1 FROM video_queue q
                                WHERE q.id = content_reservations.queue_id
                            )
                            OR EXISTS (
                                SELECT 1 FROM video_queue q
                                WHERE q.id = content_reservations.queue_id
                                  AND q.status = 'NeedsReview'
                            )
                        )
                    """)
                    conn.execute("""
                        DELETE FROM content_reservations
                        WHERE state = 'Active' AND EXISTS (
                            SELECT 1 FROM video_queue q
                            WHERE q.id = content_reservations.queue_id
                              AND q.status != 'NeedsReview'
                        )
                    """)
                attempts_exist = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'upload_attempts'"
                ).fetchone()
                queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(video_queue)").fetchall()}
                if attempts_exist and "last_attempt_id" in queue_columns:
                    conn.execute("""
                        UPDATE upload_attempts
                        SET phase = 'Recovered', outcome = 'Completed',
                            published_url = (
                                SELECT published_url FROM video_queue q
                                WHERE q.last_attempt_id = upload_attempts.id
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE outcome = 'InProgress' AND id IN (
                            SELECT last_attempt_id FROM video_queue
                            WHERE status = 'Completed' AND COALESCE(published_url, '') != ''
                              AND last_attempt_id IS NOT NULL
                        )
                    """)
                    conn.execute("""
                        UPDATE upload_attempts
                        SET phase = 'Recovered', outcome = 'NeedsReview',
                            error_message = 'Ứng dụng dừng sau khi bắt đầu upload; cần đối soát YouTube.',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE outcome = 'InProgress' AND id IN (
                            SELECT last_attempt_id FROM video_queue
                            WHERE status = 'NeedsReview' AND last_attempt_id IS NOT NULL
                        )
                    """)
                    conn.execute("""
                        UPDATE upload_attempts
                        SET phase = 'Recovered', outcome = 'NotSent',
                            error_message = 'Ứng dụng dừng trước khi gửi yêu cầu upload.',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE outcome = 'InProgress' AND id IN (
                            SELECT last_attempt_id FROM video_queue
                            WHERE status != 'NeedsReview' AND last_attempt_id IS NOT NULL
                        )
                    """)
                conn.commit()
                recovered = cur.rowcount
            finally:
                conn.close()
            if recovered:
                add_log(
                    "Pipeline",
                    f"Đã phục hồi {recovered} job bị ngắt; lượt upload chưa xác minh được giữ ở Cần đối soát.",
                    "WARNING",
                )
        except Exception as e:
            try:
                add_log("Pipeline", f"Không dọn được claim_token cũ lúc khởi động: {e}", "WARNING")
            except Exception:
                pass

    def stop_all(self):
        with self._lifecycle_lock:
            if self.batch_state != "running":
                return self.batch_state
            self._stop_event.set()
            # Each worker updates only its own claimed row. Work the batch has
            # not started keeps its previous state, including other projects.
            try:
                from render_engine import kill_all_render_processes
                kill_all_render_processes(owner="batch")
            except Exception:
                pass

        add_log(
            "Pipeline",
            "Đã yêu cầu dừng lô. Video đang đăng (nếu có) sẽ hoàn tất trước khi dừng hẳn.",
            "WARNING",
        )
        return "stopping"

    def start_batch(
        self,
        item_ids: List[int],
        do_download: bool = True,
        do_render: bool = True,
        do_upload: bool = True,
        force_reupload: bool = False,
        preview_token: str = "",
    ):
        item_ids = list(dict.fromkeys(item_ids))
        options = [do_download, do_render, do_upload]
        with self._lifecycle_lock:
            if do_upload:
                with closing(get_conn()) as conn:
                    approval = conn.execute("SELECT * FROM upload_approvals WHERE token = ?", (preview_token,)).fetchone()
                    if approval and approval["state"] in ("accepted", "finished", "interrupted"):
                        accepted_ids = {row[0] for row in conn.execute(
                            "SELECT queue_id FROM upload_approval_items WHERE token = ?", (preview_token,)
                        )}
                        if approval["options_json"] == encode(options) and accepted_ids == set(item_ids):
                            return {
                                "ok": True, "batch_id": _log_run_id(approval["run_token"]),
                                "replayed": True, "state": approval["state"],
                            }
                        return {"ok": False, "code": "PREVIEW_STALE", "message": "Lựa chọn khác bản đã xác nhận."}
            if self._is_running or self._active_threads > 0:
                return {"ok": False, "message": "Hệ thống đang chạy hoặc đang dừng một tiến trình khác."}

            with closing(get_conn()) as conn:
                held = [item_id for item_id in item_ids if queue_requires_review(conn, item_id)]
            if held:
                return {"ok": False, "code": "REVIEW_REQUIRED", "message": f"Video cần đối soát: {held}"}

            batch_token = uuid.uuid4().hex
            if do_upload:
                try:
                    self._freeze_batch_assignments(item_ids, preview_token, options, batch_token)
                except PreviewStale as exc:
                    return {"ok": False, "code": "PREVIEW_STALE", "message": str(exc)}
                except Exception as exc:
                    return {"ok": False, "message": f"Không thể xác nhận kênh đích cho batch: {exc}"}

            self._stop_event.clear()
            self._is_running = True
            self._batch_upload_box = {"n": 0}
            self._active_threads = 1
            try:
                render_queue.configure(
                    int(get_settings().get("max_concurrent_renders", RENDER_QUEUE_DEFAULT_WORKERS) or RENDER_QUEUE_DEFAULT_WORKERS)
                )
            except Exception:
                pass

            try:
                t = threading.Thread(
                    target=self._batch_worker_guarded,
                    args=(item_ids, do_download, do_render, do_upload, batch_token, force_reupload),
                    daemon=True,
                )
                t.start()
            except Exception as exc:
                try:
                    if do_upload:
                        with closing(get_conn()) as conn, conn:
                            conn.execute("UPDATE video_queue SET claim_token = '' WHERE claim_token = ?", (batch_token,))
                            conn.execute(
                                "UPDATE upload_approvals SET state = 'launch_failed', run_token = NULL WHERE run_token = ?",
                                (batch_token,),
                            )
                finally:
                    self._is_running = False
                    self._active_threads = 0
                return {"ok": False, "message": f"Không thể khởi chạy tiến trình: {exc}"}
        return {"ok": True, "batch_id": _log_run_id(batch_token), "message": f"Đã bắt đầu xử lý {len(item_ids)} video."}

    @staticmethod
    def _freeze_batch_assignments(item_ids: List[int], preview_token: str, options: list, batch_token: str):
        """Validate destinations and persist channel assignments before worker threads start."""
        conn = get_conn()
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            _check_upload_slot_preflight(conn)
            approval = conn.execute("SELECT * FROM upload_approvals WHERE token = ?", (preview_token,)).fetchone()
            snapshots = dict(conn.execute(
                "SELECT queue_id, snapshot_json FROM upload_approval_items WHERE token = ?", (preview_token,)
            ).fetchall())
            if (
                not approval or approval["state"] not in ("preview", "launch_failed")
                or not 0 <= time.time() - approval["created_at"] < 900
                or approval["options_json"] != encode(options) or set(snapshots) != set(item_ids)
            ):
                raise PreviewStale("Bản xem trước hết hạn hoặc khác lựa chọn. Hãy xem trước lại.")
            for item_id in item_ids:
                row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    raise PreviewStale(f"Video #{item_id} không còn trong hàng đợi.")
                item = dict(row)
                status = str(item.get("status") or "").strip()
                if (queue_requires_review(conn, item_id) or item.get("claim_token") or item.get("published_url") or status in ("Completed", "NeedsReview")
                        or status.startswith(("Uploading", "Downloading", "Rendering", "Đang đăng"))):
                    raise PreviewStale(f"Video #{item_id} đã đổi trạng thái. Hãy xem trước lại.")

                validate_queue_fields(item)
                validate_metadata_fields(item, require_title=True)
                source_type = str(item.get("source_type") or "").lower()
                platform = str(item.get("platform") or "").lower()
                if source_type not in {"file", "localfile"} and platform not in {"localfile", "localupload"}:
                    item["source_url"] = validate_source_url(item.get("source_url"))
                channel, frozen_groups = choose_target_channel(conn, item, validate_existing_assignment=True)
                if snapshots[item_id] != encode(approval_item(conn, item, channel, frozen_groups)):
                    raise PreviewStale(f"Video #{item_id} đã đổi đích hoặc cấu hình. Hãy xem trước lại.")
                _preview_upload_file(item, options)
                channel_id = int(channel["id"])
                groups = frozen_groups

                if not str(channel.get("channel_id") or "").strip():
                    raise ValueError(
                        f"Kênh '{channel.get('name') or channel_id}' chưa có Channel ID; "
                        "hãy bổ sung ID trước khi chạy."
                    )
                legacy_group = groups[0] if groups else str(item.get("target_group_name") or "")
                conn.execute(
                    "UPDATE video_queue SET assigned_channel_id = ?, assigned_group_names_json = ?, claim_token = ?, "
                    "target_group_names_json = CASE WHEN ? = 'GROUP' THEN ? ELSE target_group_names_json END, "
                    "target_group_name = CASE WHEN ? = 'GROUP' THEN ? ELSE target_group_name END, "
                    "group_schema_version = 1 WHERE id = ?",
                    (
                        channel_id,
                        serialize_group_names(frozen_groups),
                        batch_token,
                        str(item.get("target_type") or "GROUP").upper(),
                        serialize_group_names(groups),
                        str(item.get("target_type") or "GROUP").upper(),
                        legacy_group,
                        item_id,
                    ),
                )
            conn.execute("UPDATE upload_approvals SET state = 'accepted', run_token = ? WHERE token = ?", (batch_token, preview_token))
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            raise PreviewStale(str(exc)) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def preview_distribution(item_ids: List[int], options=None):
        options = options if options is not None else [True, True, True]
        conn = get_conn()
        previews: List[Dict[str, Any]] = []
        snapshots = {}
        token = ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item_id in item_ids:
                row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    previews.append({"id": item_id, "blocked": True, "reasons": ["video không còn tồn tại"]})
                    continue
                item = dict(row)
                entry: Dict[str, Any] = {
                    "id": int(item_id),
                    "title": item.get("title") or f"#{item_id}",
                    "visibility": item.get("visibility") or "public",
                    "schedule_time": item.get("schedule_time") or "",
                    "groups": [],
                    "channel_id": None,
                    "channel_name": "",
                    "warnings": [],
                    "reasons": [],
                    "blocked": False,
                }
                try:
                    _check_upload_slot_preflight(conn)
                    normalized = validate_queue_fields(item)
                    validate_metadata_fields(item, require_title=True)
                    source_type = str(item.get("source_type") or "").lower()
                    platform = str(item.get("platform") or "").lower()
                    if source_type not in {"file", "localfile"} and platform not in {"localfile", "localupload"}:
                        validate_source_url(item.get("source_url"))
                    entry["visibility"] = normalized["visibility"]
                    entry["schedule_time"] = normalized["schedule_time"]
                    channel, groups = choose_target_channel(conn, item, validate_existing_assignment=True)
                    entry["groups"] = groups
                    entry["channel_id"] = int(channel["id"])
                    entry["channel_name"] = str(channel.get("name") or "")
                    snapshot = approval_item(conn, item, channel, groups)
                    snapshots[item_id] = encode(snapshot)
                    for key in ("youtube_channel_id", "profile_name", "antidetect_profile_id"):
                        entry[key] = snapshot[key]
                    entry["upload_mode"] = channel.get("upload_mode") or "playwright"
                    option_id = item.get("option_group_id")
                    option = conn.execute("SELECT * FROM upload_option_groups WHERE id = ?", (option_id,)).fetchone() if option_id else None
                    metadata = validate_upload_preflight(item, channel, dict(option) if option else None)
                    entry["title"] = metadata["title"]
                    entry["visibility"] = metadata["privacy_status"]
                    entry["schedule_time"] = metadata["schedule_time"]
                    entry.update(_preview_upload_file(item, options))
                    if entry["upload_mode"] == "youtube_api":
                        entry["warnings"].append("DHB sẽ kiểm tra Channel ID của OAuth trước khi gửi file qua YouTube Data API.")
                    elif snapshot["antidetect_profile_id"]:
                        entry["warnings"].append("Đăng nhập và đúng kênh sẽ được xác minh trong Studio trước khi gửi file.")
                    try:
                        if _history_contains_item(
                            conn, item, int(channel["id"]), str(item.get("content_key") or "").strip()
                        ):
                            entry["warnings"].append("nội dung đã có trong lịch sử đăng ở kênh này")
                    except Exception:
                        entry["warnings"].append("không kiểm tra được lịch sử đăng trùng")
                    if queue_requires_review(conn, item_id):
                        entry["reasons"].append("cần đối soát lượt đăng trước")
                    status = str(item.get("status") or "")
                    if status.strip() == "Completed" or item.get("published_url"):
                        entry["reasons"].append("video đã đăng trước đó")
                    if item.get("claim_token") or status.startswith(("Uploading", "Downloading", "Rendering", "Đang đăng")):
                        entry["reasons"].append("đang được worker khác giữ")
                except Exception as exc:
                    entry["reasons"].append(str(exc))
                entry["blocked"] = bool(entry["reasons"])
                previews.append(entry)
            if previews and not any(item["blocked"] for item in previews):
                token = uuid.uuid4().hex
                now = time.time()
                conn.execute("DELETE FROM upload_approvals WHERE state IN ('preview','launch_failed') AND created_at < ?", (now - 900,))
                conn.execute("DELETE FROM upload_approval_items WHERE token NOT IN (SELECT token FROM upload_approvals)")
                conn.execute(
                    "INSERT INTO upload_approvals (token, created_at, state, options_json) VALUES (?, ?, 'preview', ?)",
                    (token, now, encode(options)),
                )
                conn.executemany("INSERT INTO upload_approval_items VALUES (?, ?, ?)", (
                    (token, item_id, snapshot) for item_id, snapshot in snapshots.items()
                ))
                conn.commit()
        finally:
            conn.close()
        return {"items": previews, "preview_token": token}

    def _batch_worker_guarded(
        self,
        item_ids: List[int],
        do_download: bool,
        do_render: bool,
        do_upload: bool,
        batch_token: str = "",
        force_reupload: bool = False,
    ):
        try:
            self._batch_worker(item_ids, do_download, do_render, do_upload, batch_token, force_reupload)
        except Exception as exc:
            try:
                add_log("Pipeline", f"Tiến trình hàng đợi gặp lỗi: {exc}", "ERROR",
                        run_id=_log_run_id(batch_token), phase="batch", error_code="ERR_BATCH")
            except Exception:
                pass
        finally:
            try:
                with closing(get_conn()) as conn:
                    conn.execute("UPDATE video_queue SET claim_token = '' WHERE claim_token = ?", (batch_token,))
                    if do_upload:
                        conn.execute(
                            "UPDATE upload_approvals SET state = 'finished' WHERE run_token = ? AND state = 'accepted'",
                            (batch_token,),
                        )
                    conn.commit()
            except Exception:
                pass
            finally:
                with self._lifecycle_lock:
                    self._is_running = False
                    self._active_threads = 0

    def _batch_worker(
        self,
        item_ids: List[int],
        do_download: bool,
        do_render: bool,
        do_upload: bool,
        batch_token: str = "",
        force_reupload: bool = False,
    ):
        settings = get_settings()
        max_workers = max(1, int(settings.get("max_concurrent_threads", 2) or 2))
        delay_value = settings.get("upload_delay_seconds", 5)
        delay_sec = max(0, int(5 if delay_value in (None, "") else delay_value))
        sem = threading.Semaphore(max_workers)
        threads = []
        launch_failure = None

        add_log(
            "Pipeline",
            f"Khởi chạy hàng đợi ({len(item_ids)} video) với tối đa {max_workers} luồng song song.",
            "INFO",
            run_id=_log_run_id(batch_token),
            phase="batch",
        )

        for idx, item_id in enumerate(item_ids):
            acquired = False
            try:
                while not self._stop_event.is_set():
                    if sem.acquire(timeout=0.25):
                        acquired = True
                        break
            except Exception as exc:
                launch_failure = (item_id, type(exc).__name__)
                break
            if not acquired:
                break
            if self._stop_event.is_set():
                sem.release()
                break

            worker_t = None
            try:
                worker_t = threading.Thread(
                    target=self._process_single_item_safe,
                    args=(item_id, idx, do_download, do_render, do_upload, sem, batch_token, force_reupload),
                    daemon=True,
                )
                worker_t.start()
            except Exception as exc:
                worker_started = False
                if worker_t is not None:
                    try:
                        worker_started = worker_t.ident is not None or worker_t.is_alive()
                    except Exception:
                        worker_started = False
                if worker_started:
                    threads.append(worker_t)
                else:
                    sem.release()
                launch_failure = (item_id, type(exc).__name__)
                break
            threads.append(worker_t)

            if delay_sec > 0 and idx < len(item_ids) - 1:
                import random
                actual_delay = delay_sec + random.randint(2, 6)
                if self._stop_event.wait(actual_delay):
                    break

        # A launch/stop must not clear claims while any already-started child is still alive.
        for wt in threads:
            wt.join()

        if launch_failure:
            failed_item_id, error_type = launch_failure
            add_log(
                "Pipeline",
                f"Không khởi chạy được worker cho queue #{failed_item_id} ({error_type}); lô đã dừng nhận item mới.",
                "ERROR",
                run_id=_log_run_id(batch_token),
                queue_id=failed_item_id,
                phase="worker_start",
                error_code="ERR_WORKER_START",
            )
            return
        if self._stop_event.is_set():
            add_log(
                "Pipeline",
                "Đã dừng nhận item mới; các worker đã bắt đầu hoàn tất trước khi đóng lô.",
                "WARNING",
                run_id=_log_run_id(batch_token),
                phase="batch",
            )
            return

        add_log("Pipeline", "Hoàn tất xử lý hàng đợi video.", "SUCCESS", run_id=_log_run_id(batch_token), phase="batch")

    def _process_single_item_safe(
        self,
        item_id: int,
        row_idx: int,
        do_download: bool,
        do_render: bool,
        do_upload: bool,
        sem: threading.Semaphore,
        batch_token: str = "",
        force_reupload: bool = False,
        stop_event: Optional[threading.Event] = None,
        upload_counter: Optional[Dict[str, int]] = None,
        owner: str = "batch",
    ):
        clear_log_context()
        set_log_context(run_id=_log_run_id(batch_token), queue_id=item_id, phase="queued")
        try:
            self.process_single_item(
                item_id, row_idx, do_download, do_render, do_upload,
                claim_token=batch_token, force_reupload=force_reupload,
                stop_event=stop_event, upload_counter=upload_counter, owner=owner,
            )
        except Exception as exc:
            try:
                add_log(
                    "Pipeline",
                    f"Worker queue #{item_id} exited unexpectedly ({type(exc).__name__}).",
                    "ERROR",
                    run_id=_log_run_id(batch_token),
                    queue_id=item_id,
                    phase="worker",
                    error_code="ERR_WORKER",
                )
            except Exception:
                pass
        finally:
            try:
                release_item_claim(item_id, batch_token)
            finally:
                try:
                    sem.release()
                finally:
                    clear_log_context()

    def process_single_item(
        self,
        item_id: int,
        row_idx: int = 0,
        do_download: bool = True,
        do_render: bool = True,
        do_upload: bool = True,
        claim_token: str = "",
        force_reupload: bool = False,
        stop_event: Optional[threading.Event] = None,
        upload_counter: Optional[Dict[str, int]] = None,
        owner: str = "batch",
    ):
        conn = get_conn()
        row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        if not row:
            return

        item = dict(row)
        title = item.get("title") or f"Video #{item_id}"
        manual_reupload_authorized = bool(item.get("manual_reupload_authorized"))
        # Mỗi làn (batch / monitor) mang stop-event và bộ đếm riêng: bấm Dừng batch
        # không được phép giết item của monitor (S1), bộ đếm giới hạn cũng tách làn (S3).
        se = stop_event if stop_event is not None else self._stop_event
        counter = upload_counter if upload_counter is not None else self._batch_upload_box

        existing_status = (item.get("status") or "").strip()
        published_url = (item.get("published_url") or "").strip()
        # A publish request may already have reached YouTube even when Studio did
        # not return a verifiable URL. Never turn that ambiguous result into an
        # automatic retry, including when force_reupload was requested.
        if existing_status == "NeedsReview":
            add_log(
                "Pipeline",
                f"Giữ '{title}' ở trạng thái cần đối soát: lượt đăng trước chưa xác minh. "
                "Kiểm tra YouTube Studio trước khi quyết định đăng lại.",
                "WARNING",
                video_title=title,
            )
            return
        if do_upload and not force_reupload:
            if existing_status == "Completed" or published_url:
                add_log(
                    "Pipeline",
                    f"Bỏ qua '{title}': video đã đăng trước đó ({published_url or existing_status}). "
                    "Chỉ chạy lại khi người dùng xác nhận đăng lại.",
                    "WARNING",
                    video_title=title,
                )
                return
            if existing_status.startswith("Uploading") or existing_status.startswith("Đang đăng"):
                add_log(
                    "Pipeline",
                    f"Bỏ qua '{title}': video đang/đã upload dở từ phiên trước ({existing_status}). "
                    "Kiểm tra Studio trước khi quyết định đăng lại.",
                    "WARNING",
                    video_title=title,
                )
                return

        token = claim_token or uuid.uuid4().hex
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        if queue_requires_review(conn, item_id):
            conn.rollback()
            conn.close()
            return
        # Read under the same write lock as the claim; API checks can be stale.
        fresh = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        if not fresh:
            conn.rollback()
            conn.close()
            return
        item = dict(fresh)
        cur = conn.execute(
            "UPDATE video_queue SET claim_token = ? WHERE id = ? AND COALESCE(claim_token, '') = ''",
            (token, item_id),
        )
        claimed = cur.rowcount == 1 or bool(do_upload and claim_token and conn.execute(
            "SELECT 1 FROM upload_approvals a JOIN upload_approval_items i ON i.token = a.token "
            "JOIN video_queue q ON q.id = i.queue_id AND q.claim_token = a.run_token "
            "WHERE a.state = 'accepted' AND a.run_token = ? AND q.id = ?",
            (claim_token, item_id),
        ).fetchone())
        conn.commit()
        conn.close()
        if not claimed:
            add_log(
                "Pipeline",
                f"Bỏ qua '{title}': video đang được một tiến trình khác xử lý.",
                "WARNING",
                video_title=title,
            )
            return

        def _update_row(**kwargs):
            if not kwargs:
                return False
            cols = ", ".join(f"{k} = ?" for k in kwargs.keys())
            vals = list(kwargs.values()) + [item_id, token]
            c = get_conn()
            try:
                cur = c.execute(
                    f"UPDATE video_queue SET {cols} WHERE id = ? AND COALESCE(claim_token, '') = ?",
                    vals,
                )
                c.commit()
                updated = cur.rowcount == 1
            finally:
                c.close()
            return updated

        def _make_progress_cb(progress_col: str):
            state = {"last_pct": -1, "last_time": 0.0, "stage": ""}

            def _cb(pct: int, msg: str):
                if se.is_set():
                    return
                if progress_col == "upload_progress" and upload_attempt_id:
                    text = str(msg or "").casefold()
                    if "checks" in text or "kiểm duyệt" in text or "bản quyền" in text:
                        stage = "Checks"
                    elif "hiển thị" in text or "visibility" in text or "lịch đăng" in text:
                        stage = "Visibility"
                    elif "đang gửi đăng" in text or "xuất bản" in text or "publish" in text:
                        stage = "Publishing"
                    elif (
                        "xác minh video" in text
                        or "xác minh url video" in text
                        or "đọc lại metadata" in text
                        or "verifying" in text
                    ):
                        stage = "VerifyingResult"
                    elif "đang xử lý video" in text or "processing" in text:
                        stage = "Processing"
                    elif "điền" in text or "metadata" in text or "tiêu đề" in text:
                        stage = "Metadata"
                    elif "chuyển file" in text or "upload" in text or "tải lên" in text:
                        stage = "Transferring"
                    elif "kênh" in text or "studio" in text or "profile" in text:
                        stage = "VerifyingChannel"
                    else:
                        stage = ""
                    if stage and stage != state["stage"]:
                        state["stage"] = stage
                        try:
                            _set_upload_attempt_phase(upload_attempt_id, stage)
                            set_log_context(attempt_id=upload_attempt_id, phase=stage)
                            _update_row(status=f"Uploading -> {stage}")
                        except Exception as phase_err:
                            add_log(
                                "Pipeline",
                                f"Không lưu được giai đoạn upload '{stage}': {phase_err}",
                                "WARNING",
                                video_title=title,
                            )
                now = time.time()
                if pct >= 100 or abs(pct - state["last_pct"]) >= 3 or (now - state["last_time"]) >= 0.8:
                    state["last_pct"] = pct
                    state["last_time"] = now
                    # Progress messages are not lifecycle states. Keeping status
                    # stable also prevents a late callback from reviving a stopped row.
                    _update_row(**{progress_col: pct})

            return _cb

        reservation_key = ""
        reservation_channel_id = None
        reservation_owned = False
        reservation_keep = False
        upload_started = False
        slot_owner = None
        upload_attempt_id = None
        channel = None
        option_group = None
        approved_assets = None

        try:
            if do_upload:
                item["claim_token"] = token
                item["_upload_origin"] = owner
                channel = self._resolve_target_channel(item, row_idx)
                if not channel:
                    raise RuntimeError(
                        "Chưa chọn Kênh YouTube đích hoặc Nhóm Kênh chưa có kênh nào. "
                        "Hãy thêm/chọn kênh trước khi chạy video."
                    )
                if channel.get("_approval_snapshot"):
                    option_group = channel["_approval_options"]
                    approved_assets = list(channel["_approval_snapshot"]["asset_stamps"])
                elif item.get("option_group_id"):
                    c_opt = get_conn()
                    try:
                        og_row = c_opt.execute(
                            "SELECT * FROM upload_option_groups WHERE id = ?",
                            (item["option_group_id"],),
                        ).fetchone()
                    finally:
                        c_opt.close()
                    if og_row:
                        option_group = dict(og_row)
                validate_upload_preflight(item, channel, option_group)

            input_path = item.get("input_video_path") or ""
            thumb_path = item.get("thumbnail_path") or ""

            # BƯỚC 1: TẢI VIDEO (DOWNLOAD)
            if do_download and (not input_path or not os.path.exists(input_path)):
                if se.is_set():
                    _update_row(status="Stopped")
                    return
                ensure_disk_space(APP_TEMP_DIR, MIN_DOWNLOAD_HEADROOM_BYTES, "tải video")
                set_log_context(phase="download")
                _update_row(status="Downloading...", download_progress=5, error_message="")
                dl_res = download_video(
                    source_url=item.get("source_url") or "",
                    post_id=item.get("post_id") or str(item_id),
                    progress_callback=_make_progress_cb("download_progress"),
                    stop_event=se,
                    owner_id=item_id,
                )
                input_path = dl_res["video_path"]
                downloaded_thumb = dl_res.get("thumbnail_path")
                thumb_path = downloaded_thumb or thumb_path
                if approved_assets is not None:
                    approved_assets[0] = asset_stamps([input_path])[0]
                    if downloaded_thumb:
                        approved_assets[2] = asset_stamps([thumb_path])[0]
                _update_row(
                    input_video_path=input_path,
                    thumbnail_path=thumb_path,
                    duration=dl_res.get("duration") or item.get("duration") or "00:00:00",
                    download_progress=100,
                    status="Downloaded",
                )
                add_log("Download", f"Đã tải xong video: {title}", "SUCCESS", video_title=title)
            elif input_path and os.path.exists(input_path):
                _update_row(download_progress=100)

            # BƯỚC 2: RENDER & LÁCH BẢN QUYỀN FFMPEG
            output_path = item.get("output_video_path") or ""
            # Hash file trong worker để không chặn request thêm local path. Ưu tiên
            # file nguồn; chỉ dùng output cũ khi queue không còn file nguồn.
            identity_path = input_path if input_path and os.path.isfile(input_path) else ""
            if not identity_path and output_path and os.path.isfile(output_path):
                identity_path = output_path
            if identity_path:
                content_key = sha256_file(identity_path)
                stored = _store_content_fingerprint(
                    item_id,
                    token,
                    content_key,
                    allow_duplicate=manual_reupload_authorized or force_reupload,
                )
                if stored == "duplicate":
                    add_log(
                        "Pipeline",
                        f"Bỏ qua '{title}': nội dung trùng với video khác trong cùng dự án.",
                        "WARNING",
                        video_title=title,
                    )
                    return
                if stored == "claim_lost":
                    add_log(
                        "Pipeline",
                        f"Bỏ qua '{title}': worker không còn sở hữu hàng đợi khi lưu fingerprint.",
                        "WARNING",
                        video_title=title,
                    )
                    return
                item["content_key"] = content_key

            if do_render:
                if se.is_set():
                    _update_row(status="Stopped")
                    return
                if not input_path or not os.path.exists(input_path):
                    raise FileNotFoundError("Chưa có file video gốc để Render.")

                ensure_disk_space(
                    APP_OUTPUT_DIR, estimate_render_bytes(input_path), "render video"
                )

                set_log_context(phase="render")
                _update_row(status="Rendering...", render_progress=5, error_message="")
                try:
                    render_cfg = json.loads(item.get("render_config_json") or "{}")
                except Exception:
                    render_cfg = {}

                if item.get("resolution"):
                    render_cfg["resolution"] = item["resolution"]

                try:
                    output_path = render_queue.submit(
                        render_video,
                        input_path=input_path,
                        render_mode=item.get("render_mode") or "shorts_9_16",
                        render_config=render_cfg,
                        cut_start=item.get("cut_start") or "00:00:00",
                        cut_end=item.get("cut_end") or "00:00:00",
                        progress_callback=_make_progress_cb("render_progress"),
                        stop_event=se,
                        owner=owner,
                        owner_id=item_id,
                    ).result()
                except InterruptedError:
                    _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                    add_log("Render", f"Đã dừng Render video: {title}", "WARNING", video_title=title)
                    return

                if se.is_set():
                    _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                    return

                if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
                    raise RuntimeError(
                        f"Render xong nhưng file đầu ra không hợp lệ hoặc rỗng: {output_path or '(không có đường dẫn)'}"
                    )
                if approved_assets is not None:
                    approved_assets[1] = asset_stamps([output_path])[0]

                # Nguồn thumbnail: 'reup' = dùng ảnh gốc của nguồn nếu có (học ReupThumbnail của DHB),
                # 'generate' = luôn tự chụp từ video render. Mặc định reup.
                thumb_source = str(get_settings().get("thumbnail_source") or "reup").lower()
                if thumb_source == "generate" or not thumb_path or not os.path.exists(thumb_path):
                    gen_thumb = generate_thumbnail_from_video(output_path or input_path, owner_id=item_id)
                    if gen_thumb:
                        thumb_path = gen_thumb
                        if approved_assets is not None:
                            approved_assets[2] = asset_stamps([thumb_path])[0]

                _update_row(
                    output_video_path=output_path,
                    thumbnail_path=thumb_path,
                    render_progress=100,
                    status="Rendered",
                )
                add_log("Render", f"Đã Render xong ({item.get('render_mode')}): {output_path}", "SUCCESS", video_title=title)

            # BƯỚC 3: ĐĂNG LÊN YOUTUBE (UPLOAD)
            if do_upload:
                if se.is_set():
                    _update_row(status="Stopped")
                    return

                # GIỚI HẠN SỐ VIDEO ĐĂNG MỖI PHIÊN (học LimitUpload của DHB) — 0 = không giới hạn
                max_uploads = int(get_settings().get("max_uploads_per_run", 0) or 0)
                if max_uploads > 0:
                    with self._lock:
                        if counter["n"] >= max_uploads:
                            _update_row(
                                status="Pending",
                                error_message=f"Đã đạt giới hạn {max_uploads} video đăng mỗi phiên (Cài đặt). Video được giữ lại để chạy lần sau.",
                            )
                            add_log(
                                "Pipeline",
                                f"⏸️ Giữ lại video '{title}' — đã đạt giới hạn {max_uploads} lần đăng mỗi phiên (bảo vệ tài khoản).",
                                "WARNING",
                                video_title=title,
                            )
                            return
                        counter["n"] += 1

                if not channel:
                    raise RuntimeError(
                        "Chưa chọn Kênh YouTube đích hoặc Nhóm Kênh chưa có kênh nào. Hãy thêm/chọn kênh ở tab Quản Lý Kênh."
                    )

                conn_f = get_conn()
                f_row = conn_f.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
                conn_f.close()
                fresh_item = dict(f_row) if f_row else item
                if channel.get("_approval_snapshot"):
                    expected = dict(channel["_approval_snapshot"])
                    expected["paths"] = [input_path, output_path, thumb_path]
                    expected["asset_stamps"] = approved_assets

                    def verify_approval():
                        with closing(get_conn()) as check_conn:
                            check_conn.execute("BEGIN")
                            current_item = check_conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
                            current_channel = check_conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (channel["id"],)).fetchone()
                            if (not current_item or current_item["claim_token"] != token or not current_channel
                                    or approval_item(check_conn, dict(current_item), dict(current_channel), expected["groups"]) != expected):
                                raise PreviewStale("Đích, file hoặc cấu hình đã thay đổi. Hãy xem trước lại.")

                    verify_approval()
                    channel["_verify_approval"] = verify_approval

                # Radio 18+ của Studio chưa được xác minh (HANDOVER v7). Chặn trước
                # khi giữ reservation/gọi uploader để không tạo bản nháp rác.
                if bool(fresh_item.get("age_restriction")):
                    raise RuntimeError(
                        "Chưa hỗ trợ xác minh giới hạn độ tuổi 18+ trên YouTube Studio. "
                        "Đã dừng trước khi nạp file. Hãy tắt giới hạn tuổi hoặc probe lại DOM/selector trước."
                    )

                reservation_key, reservation_result = _acquire_content_reservation(
                    item_id,
                    token,
                    fresh_item,
                    channel["id"],
                    force_reupload=force_reupload or manual_reupload_authorized,
                )
                reservation_channel_id = int(channel["id"])
                if reservation_result == "published":
                    _update_row(
                        status="SkippedDuplicate",
                        error_message=f"Nguồn này đã được đăng lên kênh {channel['name']} trước đó.",
                    )
                    add_log(
                        "Pipeline",
                        f"Bỏ qua '{title}': nội dung/nguồn đã có trong lịch sử kênh [{channel['name']}].",
                        "WARNING",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                    return
                if reservation_result == "claim_lost":
                    return
                if reservation_result in ("busy", "needs_review"):
                    message = (
                        "Nội dung này đang có lượt đăng cần đối soát trên kênh đích."
                        if reservation_result == "needs_review"
                        else "Nội dung này đang được một worker khác đăng lên kênh đích; có thể chạy lại sau."
                    )
                    _update_row(status="Pending", error_message=message)
                    add_log(
                        "Pipeline",
                        f"Tạm hoãn '{title}': {message}",
                        "WARNING",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                    return
                reservation_owned = reservation_result == "acquired"

                # P0-7: giãn cách bắt buộc >= 30 phút, tính từ mốc muộn hơn giữa
                # lúc bắt đầu và kết thúc lượt upload trước. Fail closed nếu mốc không đọc được.
                wait_seconds = read_upload_gap_remaining()
                if wait_seconds > 0:
                    wait_min = max(1, int((wait_seconds + 59) // 60))
                    _update_row(status=f"Chờ giãn cách đăng (~{wait_min} phút)...", upload_progress=1)
                    add_log(
                        "Upload",
                        f"⏳ Giữ giãn cách an toàn: chờ ~{wait_min} phút trước khi đăng '{title}' "
                        f"(tối thiểu 30 phút giữa 2 lần đăng).",
                        "INFO",
                        channel_name=channel["name"],
                        video_title=title,
                    )

                _update_row(status="Chờ lượt upload...")
                slot_owner = reserve_upload_slot(se, item_id)
                if not slot_owner:
                    _update_row(status="Stopped", error_message="Đã dừng khi đang chờ giãn cách đăng.")
                    return
                if se.is_set():
                    _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                    return

                reservation_check = _validate_content_reservation(
                    reservation_key,
                    reservation_channel_id,
                    item_id,
                    token,
                    fresh_item,
                    force_reupload=force_reupload or manual_reupload_authorized,
                )
                if reservation_check == "published":
                    _update_row(
                        status="SkippedDuplicate",
                        error_message=f"Nguồn này đã được đăng lên kênh {channel['name']} trong lúc chờ giãn cách.",
                    )
                    return
                if reservation_check != "ready":
                    _update_row(
                        status="Pending",
                        error_message="Reservation chống trùng không còn thuộc worker này; lượt đăng chưa được gửi.",
                    )
                    return

                if channel.get("_verify_approval"):
                    channel["_verify_approval"]()

                upload_attempt_id = _start_upload_attempt(
                    item_id,
                    token,
                    fresh_item,
                    reservation_channel_id,
                )
                if not upload_attempt_id:
                    _update_row(
                        status="Pending",
                        error_message="Worker không còn quyền sở hữu lượt đăng; chưa gửi yêu cầu lên YouTube.",
                    )
                    return
                if channel.get("_verify_approval"):
                    # The attempt consumed the one-time reupload permission.
                    expected["video"] = {**expected["video"], "manual_reupload_authorized": 0, "manual_reupload_reason": ""}
                set_log_context(attempt_id=upload_attempt_id, phase="Uploading")
                if not _update_row(status=f"Uploading -> {channel['name']}", upload_progress=10, error_message=""):
                    _finish_upload_attempt(upload_attempt_id, "NotSent", error_message="Claim đã đổi trước khi gọi uploader.")
                    return
                with self._lifecycle_lock:
                    if se.is_set():
                        _finish_upload_attempt(upload_attempt_id, "NotSent", error_message="Đã dừng trước khi gửi video.")
                        _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                        return
                    _mark_slot_entered(slot_owner, upload_attempt_id)
                    upload_started = True
                pub_res = upload_video_to_youtube(
                    video_item=fresh_item,
                    channel=channel,
                    option_group=option_group,
                    progress_callback=_make_progress_cb("upload_progress"),
                )

                if isinstance(pub_res, dict):
                    pub_url = pub_res.get("url", "")
                    is_cancelled = bool(pub_res.get("cancelled", False))
                    is_flagged = bool(pub_res.get("flagged_copyright", False))
                    is_uncertain = bool(pub_res.get("published_uncertain", False))
                    warning_msg = pub_res.get("warning_message", "")
                    discard_status = str(pub_res.get("discard_status") or "").strip().lower()
                else:
                    pub_url = str(pub_res)
                    is_cancelled = False
                    is_flagged = False
                    is_uncertain = False
                    warning_msg = ""
                    discard_status = ""

                raw_pub_url = str(pub_url or "").strip()
                pub_url = normalize_published_video_url(raw_pub_url)
                invalid_url_warning = ""
                if raw_pub_url and not pub_url:
                    invalid_url_warning = (
                        "Uploader trả URL không phải URL video YouTube hợp lệ; không ghi Completed. "
                        "Hãy đối soát kết quả trong YouTube Studio."
                    )
                result_unverified = is_uncertain or not pub_url

                if is_cancelled and discard_status == "deleted":
                    _update_row(
                        upload_progress=100,
                        status="Đã Hủy (Dính Bản Quyền)",
                        error_message=warning_msg,
                    )
                    _finish_upload_attempt(upload_attempt_id, "Cancelled", error_message=warning_msg)
                    add_log(
                        "Upload",
                        f"🛑 ĐÃ HỦY ĐĂNG lên kênh [{channel['name']}] do CẢNH BÁO VI PHẠM BẢN QUYỀN: {warning_msg}",
                        "WARNING",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                elif is_cancelled:
                    # Đã hủy thao tác đăng nhưng chưa chứng minh được bản nháp đã bị xóa.
                    # Không báo "Đã Hủy" sạch sẽ; giữ reservation để đối soát thủ công.
                    unverified_msg = warning_msg or (
                        "Đã hủy thao tác đăng do cảnh báo bản quyền nhưng chưa xác minh được "
                        "việc xóa bản nháp trên YouTube Studio."
                    )
                    _update_row(
                        upload_progress=100,
                        status="NeedsReview",
                        error_message=unverified_msg,
                    )
                    reservation_keep = True
                    _finish_upload_attempt(upload_attempt_id, "NeedsReview", error_message=unverified_msg)
                    add_log(
                        "Upload",
                        f"⚠️ Đã hủy thao tác đăng do cảnh báo bản quyền nhưng CHƯA xác minh được việc xóa "
                        f"bản nháp trên kênh [{channel['name']}]; cần kiểm tra Studio: {unverified_msg}",
                        "ERROR",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                elif is_flagged:
                    _update_row(
                        upload_progress=100,
                        status="Warning: Copyright",
                        published_url=pub_url,
                        error_message=warning_msg,
                    )
                    _finish_upload_attempt(upload_attempt_id, "Warning", pub_url, warning_msg)
                    add_log(
                        "Upload",
                        f"⚠️ Cảnh báo bản quyền: {warning_msg}",
                        "WARNING",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                elif result_unverified:
                    # Đã bấm Xuất bản nhưng không xác minh được URL video.
                    # Không được báo Completed và không tự xóa/move file nguồn.
                    uncertain_msg = warning_msg or invalid_url_warning or (
                        "Không xác minh được video đã đăng thành công (thiếu URL từ YouTube Studio). "
                        "Có thể video đã lên Studio — hãy kiểm tra trực tiếp trước khi chạy lại để tránh đăng trùng."
                    )
                    _update_row(
                        upload_progress=100,
                        status="NeedsReview",
                        error_message=uncertain_msg,
                    )
                    reservation_keep = True
                    _finish_upload_attempt(upload_attempt_id, "NeedsReview", error_message=uncertain_msg)
                    add_log(
                        "Upload",
                        f"❌ KHÔNG xác minh được kết quả đăng lên kênh [{channel['name']}]: {uncertain_msg}",
                        "ERROR",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                else:
                    completed_saved = _update_row(
                        upload_progress=100,
                        status="Completed",
                        published_url=pub_url,
                        error_message="",
                    )
                    if not completed_saved:
                        reservation_keep = True
                        raise RuntimeError("Không lưu được trạng thái Completed sau khi YouTube trả URL.")
                    # Ghi lịch sử cho MỌI nguồn (link, kéo-thả, local path), không chỉ Monitor.
                    if not self._record_published_history_for_item(item_id, channel):
                        reservation_keep = True
                        raise RuntimeError("Không ghi được published_history sau khi YouTube trả URL.")
                    _finish_upload_attempt(upload_attempt_id, "Completed", pub_url)
                    add_log(
                        "Upload",
                        f"✅ Đăng thành công lên kênh [{channel['name']}]: {pub_url}",
                        "SUCCESS",
                        channel_name=channel["name"],
                        video_title=title,
                    )

                # Xóa file tạm nếu cài đặt yêu cầu — chỉ xóa file nằm trong thư mục temp/ của tool
                settings = get_settings()
                if (
                    settings.get("delete_temp_after_upload") and input_path and pub_url
                    and not is_cancelled and not is_flagged and not result_unverified
                ):
                    try:
                        delete_completed_temp_artifact(input_path, item_id)
                    except Exception:
                        add_log("Storage", f"Không xóa artifact tạm thuộc queue #{item_id}; file được giữ để kiểm tra.", "WARNING", video_title=title)

        except InterruptedError:
            if upload_started:
                reservation_keep = True
                _finish_upload_attempt(
                    upload_attempt_id,
                    "NeedsReview",
                    error_message="Worker bị ngắt sau khi gọi uploader; cần đối soát Studio.",
                )
                _update_row(
                    status="NeedsReview",
                    error_message="Worker bị ngắt trong lượt upload; hãy kiểm tra YouTube Studio trước khi chạy lại.",
                )
                add_log(
                    "Pipeline",
                    f"Giữ '{title}' để đối soát vì worker bị ngắt sau khi bắt đầu upload.",
                    "WARNING",
                    video_title=title,
                )
            else:
                _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                add_log("Pipeline", f"Đã dừng xử lý video: {title}", "WARNING", video_title=title)
        except Exception as e:
            if upload_started and isinstance(e, UploadNotSent):
                if owner == "batch":
                    self._stop_event.set()
                _finish_upload_attempt(upload_attempt_id, "NotSent", error_message=str(e))
                _update_row(status="Error", error_message=str(e))
                add_log("Upload", str(e), "ERROR", video_title=title, error_code="ERR_STUDIO_BASELINE")
                return
            if upload_started and isinstance(e, AntidetectNotReady):
                _finish_upload_attempt(upload_attempt_id, "NotSent", error_message=str(e))
                _update_row(status="Error", error_message=str(e))
                return
            if upload_started:
                reservation_keep = True
                err_msg = str(e)
                try:
                    _finish_upload_attempt(upload_attempt_id, "NeedsReview", error_message=err_msg)
                except Exception:
                    pass
                _update_row(
                    status="NeedsReview",
                    error_message=(
                        "Không xác định được kết quả sau khi bắt đầu upload. Hãy kiểm tra YouTube Studio "
                        f"trước khi chạy lại. Chi tiết: {err_msg}"
                    ),
                )
                add_log(
                    "Error",
                    f"Lỗi sau khi bắt đầu upload '{title}'; giữ reservation để đối soát: {err_msg}",
                    "ERROR",
                    video_title=title,
                )
                return
            if se.is_set():
                _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                return
            err_msg = str(e)
            _update_row(status="Error", error_message=err_msg)
            add_log("Error", f"Lỗi xử lý '{title}': {err_msg}", "ERROR", video_title=title,
                    error_code="ERR_JOB")
        finally:
            if reservation_owned:
                try:
                    _set_content_reservation_state(
                        reservation_key,
                        reservation_channel_id,
                        item_id,
                        token,
                        "NeedsReview",
                    ) if reservation_keep else _release_content_reservation(
                        reservation_key,
                        reservation_channel_id,
                        item_id,
                        token,
                    )
                except Exception as reservation_error:
                    try:
                        add_log(
                            "Pipeline",
                            f"Không cập nhật được reservation chống trùng cho '{title}': {reservation_error}",
                            "ERROR",
                            video_title=title,
                        )
                    except Exception:
                        pass
            if slot_owner:
                _record_upload_finished_safely(
                    title, slot_owner, entered_uploader=upload_started,
                    cleanup_unconfirmed=bool(channel and channel.get("_upload_cleanup_unconfirmed")),
                )


    def _resolve_target_channel(self, item: Dict[str, Any], row_idx: int) -> Dict[str, Any]:
        conn = get_conn()
        try:
            token = item.get("claim_token")
            if token:
                conn.execute("BEGIN IMMEDIATE")
            selected, group_names = choose_target_channel(
                conn, item, validate_existing_assignment=False
            )
            if not group_names and item.get("assigned_channel_id"):
                group_names = target_group_names(item)
            if token:
                approved = conn.execute(
                    "SELECT i.snapshot_json FROM upload_approvals a JOIN upload_approval_items i ON i.token = a.token "
                    "WHERE a.run_token = ? AND a.state = 'accepted' AND i.queue_id = ?",
                    (token, item["id"]),
                ).fetchone()
                current = approval_item(conn, item, selected, group_names)
                if approved:
                    snapshot = json.loads(approved[0])
                    current["groups"] = snapshot["groups"]
                    if current != snapshot:
                        raise PreviewStale("Đích hoặc cấu hình đã thay đổi sau khi xác nhận.")
                elif item.get("_upload_origin") == "monitor":
                    snapshot = current
                    approval_token = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO upload_approvals VALUES (?, ?, 'accepted', ?, ?, 'monitor')",
                        (approval_token, time.time(), encode([True, True, True]), token),
                    )
                    conn.execute("INSERT INTO upload_approval_items VALUES (?, ?, ?)", (approval_token, item["id"], encode(snapshot)))
                    conn.execute("UPDATE video_queue SET assigned_channel_id = ?, assigned_group_names_json = ? "
                                 "WHERE id = ? AND claim_token = ?",
                                 (snapshot["channel_id"], serialize_group_names(group_names), item["id"], token))
                else:
                    raise PreviewStale("Lượt upload thiếu bản xác nhận. Hãy xem trước lại.")
                selected["_approval_snapshot"] = snapshot
                option_row = conn.execute(
                    "SELECT * FROM upload_option_groups WHERE id = ?", (item.get("option_group_id"),)
                ).fetchone()
                selected["_approval_options"] = dict(option_row) if option_row else None
                selected["channel_id"] = snapshot["youtube_channel_id"]
                selected["antidetect_profile_id"] = snapshot["antidetect_profile_id"]
                conn.commit()
            add_log(
                "Pipeline",
                f"Chọn kênh video #{item.get('id')} → '{selected['name']}' (kênh #{selected['id']})"
                + (f" từ nhóm: {', '.join(group_names)}." if group_names else "."),
                "INFO",
                video_title=item.get("title") or "",
            )
            return selected
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            conn.close()

    # ==========================================================
    # CHẾ ĐỘ AUTO MONITOR 24/7 (Tương đương ZhScDd của DHB Reup)
    # ==========================================================
    def _monitor_handles_alive(self, monitor_id: int) -> bool:
        with self._monitor_lock:
            worker = self._monitor_workers.get(monitor_id)
            heartbeat = self._monitor_heartbeats.get(monitor_id)
            return bool(
                (worker is not None and worker.is_alive())
                or (heartbeat is not None and heartbeat.is_alive())
            )

    def _reap_monitor_locked(self, monitor_id: int) -> Optional[str]:
        """Reap handles only after both threads have actually exited.

        Caller holds `_monitor_lifecycle_lock`; this never joins a thread or waits
        for scanner/uploader I/O.
        """
        with self._monitor_lock:
            stop_ev = self._monitor_threads.get(monitor_id)
            worker = self._monitor_workers.get(monitor_id)
            heartbeat = self._monitor_heartbeats.get(monitor_id)
            if ((worker is not None and worker.is_alive())
                    or (heartbeat is not None and heartbeat.is_alive())):
                return self._monitor_states.get(monitor_id, "Running")
            previous_state = self._monitor_states.get(monitor_id)

        # A requested stop is not complete until the desired state is durable.
        # Keep the Stopping generation registered if SQLite is temporarily busy;
        # otherwise the supervisor could mistake it for a crashed, desired worker.
        if previous_state == "Stopping":
            conn = None
            try:
                conn = get_conn()
                cursor = conn.execute(
                    "UPDATE monitors SET is_running = 0, status = 'Stopped' WHERE id = ?",
                    (monitor_id,),
                )
                exists = cursor.rowcount > 0
                conn.commit()
            except Exception:
                return "Stopping"
            finally:
                if conn is not None:
                    conn.close()

            with self._monitor_lock:
                if self._monitor_workers.get(monitor_id) is worker and self._monitor_heartbeats.get(monitor_id) is heartbeat:
                    if ((worker is not None and worker.is_alive())
                            or (heartbeat is not None and heartbeat.is_alive())):
                        return "Stopping"
                    if self._monitor_threads.get(monitor_id) is stop_ev:
                        self._monitor_threads.pop(monitor_id, None)
                    self._monitor_workers.pop(monitor_id, None)
                    self._monitor_heartbeats.pop(monitor_id, None)
                    self._monitor_states[monitor_id] = "Stopped"
            return "Stopped" if exists else None

        with self._monitor_lock:
            if self._monitor_threads.get(monitor_id) is stop_ev:
                self._monitor_threads.pop(monitor_id, None)
            if self._monitor_workers.get(monitor_id) is worker:
                self._monitor_workers.pop(monitor_id, None)
            if self._monitor_heartbeats.get(monitor_id) is heartbeat:
                self._monitor_heartbeats.pop(monitor_id, None)

            if previous_state in {"Starting", "Running"}:
                # The worker exited without an accepted Stop request. Keep the
                # DB desired-running flag so the supervisor can recover it.
                self._monitor_states[monitor_id] = "Error"
                final_status = "Luồng đã dừng bất ngờ; đang chờ tự khôi phục."
            else:
                final_status = None
            state = self._monitor_states.get(monitor_id)

        if final_status:
            try:
                conn = get_conn()
                if state == "Stopped":
                    conn.execute("UPDATE monitors SET is_running = 0, status = 'Stopped' WHERE id = ?", (monitor_id,))
                else:
                    conn.execute(
                        "UPDATE monitors SET status = ? WHERE id = ? AND is_running = 1",
                        (final_status, monitor_id),
                    )
                conn.commit()
                conn.close()
            except Exception:
                pass
        return state

    def refresh_monitor_states(self, monitor_ids: List[int]) -> Dict[int, str]:
        with self._monitor_lifecycle_lock:
            states = {}
            for monitor_id in monitor_ids:
                monitor_id = int(monitor_id)
                state = self._reap_monitor_locked(monitor_id)
                if state:
                    states[monitor_id] = state
            return states

    def get_monitor_snapshots(self) -> List[Dict[str, Any]]:
        """Reconcile completed stops before reading one lifecycle-consistent view."""
        with self._monitor_lifecycle_lock:
            conn = get_conn()
            try:
                monitor_ids = [int(row[0]) for row in conn.execute("SELECT id FROM monitors")]
            finally:
                conn.close()
            for monitor_id in monitor_ids:
                self._reap_monitor_locked(monitor_id)

            conn = get_conn()
            try:
                items = [dict(row) for row in conn.execute("SELECT * FROM monitors ORDER BY id DESC")]
            finally:
                conn.close()
            for item in items:
                monitor_id = int(item["id"])
                running = bool(item.get("is_running"))
                with self._monitor_lock:
                    state = self._monitor_states.get(monitor_id) or ("Running" if running else "Stopped")
                active = self._monitor_handles_alive(monitor_id)
                item["lifecycle_state"] = state
                item["can_start"] = not running and not active and state not in {"Starting", "Stopping"}
                item["can_delete"] = not running and not active and state not in {"Starting", "Running", "Stopping"}
            return items

    def start_monitor(self, monitor_id: int, _auto_restart: bool = False) -> Dict[str, Any]:
        with self._monitor_lifecycle_lock:
            state = self._reap_monitor_locked(monitor_id)
            with self._monitor_lock:
                worker = self._monitor_workers.get(monitor_id)
                heartbeat = self._monitor_heartbeats.get(monitor_id)
                alive = bool((worker and worker.is_alive()) or (heartbeat and heartbeat.is_alive()))
            if alive:
                if state == "Stopping":
                    return {"ok": False, "lifecycle_state": state,
                            "message": f"Monitor #{monitor_id} đang dừng; hãy đợi lượt hiện tại kết thúc rồi bật lại."}
                return {"ok": True, "lifecycle_state": state or "Running",
                        "message": f"Luồng Monitor #{monitor_id} đang chạy."}
            if state == "Stopping":
                # A Stop transition is cleared only after the old handles are reaped.
                state = self._reap_monitor_locked(monitor_id)
                if state == "Stopping":
                    return {"ok": False, "lifecycle_state": state,
                            "message": f"Monitor #{monitor_id} đang dừng; chưa thể tạo lượt mới."}

            stop_ev = threading.Event()
            with self._monitor_lock:
                self._monitor_states[monitor_id] = "Starting"
                self._monitor_threads[monitor_id] = stop_ev

            conn = None
            try:
                conn = get_conn()
                mon_row = conn.execute("SELECT name FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
                if not mon_row:
                    stop_ev.set()
                    with self._monitor_lock:
                        self._monitor_threads.pop(monitor_id, None)
                        self._monitor_states[monitor_id] = "Stopped"
                    return {"ok": False, "lifecycle_state": "Stopped",
                            "message": f"Không tìm thấy Monitor #{monitor_id}."}

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "UPDATE monitors SET is_running = 1, status = 'Starting', last_checked_at = ?, last_heartbeat_at = ? WHERE id = ?",
                    (now_str, now_str, monitor_id),
                )
                conn.commit()
            except Exception as exc:
                stop_ev.set()
                with self._monitor_lock:
                    if self._monitor_threads.get(monitor_id) is stop_ev:
                        self._monitor_threads.pop(monitor_id, None)
                    self._monitor_states[monitor_id] = "Error"
                return {"ok": False, "lifecycle_state": "Error",
                        "message": f"Không thể khởi động Monitor #{monitor_id}: {exc}"}
            finally:
                if conn is not None:
                    conn.close()

            if not _auto_restart:
                self._monitor_restart_history.pop(monitor_id, None)
                self._monitor_upload_counts[monitor_id] = {"n": 0}
            else:
                self._monitor_upload_counts.setdefault(monitor_id, {"n": 0})

            worker = threading.Thread(
                target=self._monitor_worker,
                args=(monitor_id, stop_ev),
                daemon=True,
                name=f"DHB-Monitor-{monitor_id}",
            )
            with self._monitor_lock:
                self._monitor_workers[monitor_id] = worker
            try:
                worker.start()
            except Exception as exc:
                stop_ev.set()
                try:
                    worker_started = worker.ident is not None or worker.is_alive()
                except Exception:
                    worker_started = False
                with self._monitor_lock:
                    if worker_started:
                        self._monitor_states[monitor_id] = "Stopping"
                    else:
                        if self._monitor_workers.get(monitor_id) is worker:
                            self._monitor_workers.pop(monitor_id, None)
                        if self._monitor_threads.get(monitor_id) is stop_ev:
                            self._monitor_threads.pop(monitor_id, None)
                        self._monitor_states[monitor_id] = "Error"
                try:
                    conn = get_conn()
                    status = "Stopping" if worker_started else "Không thể tạo worker monitor."
                    conn.execute("UPDATE monitors SET is_running = 0, status = ? WHERE id = ?", (status, monitor_id))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                add_log("Monitor", f"Không thể tạo worker Monitor #{monitor_id}: {type(exc).__name__}", "ERROR")
                return {"ok": False, "lifecycle_state": "Stopping" if worker_started else "Error",
                        "message": f"Không thể khởi chạy Monitor #{monitor_id}."}

            with self._monitor_lock:
                if self._monitor_states.get(monitor_id) == "Starting":
                    self._monitor_states[monitor_id] = "Running"

            heartbeat_warning = ""
            try:
                if worker.is_alive():
                    self._start_monitor_heartbeat(monitor_id, stop_ev)
            except Exception as exc:
                heartbeat_warning = " Worker đang chạy nhưng heartbeat chưa khởi động; supervisor sẽ thử lại."
                add_log("Monitor", f"Không thể tạo heartbeat Monitor #{monitor_id}: {type(exc).__name__}", "WARNING")
            self.start_monitor_supervisor()

            if not worker.is_alive():
                state = self._reap_monitor_locked(monitor_id)
                return {"ok": False, "lifecycle_state": state,
                        "message": f"Worker Monitor #{monitor_id} đã thoát ngay sau khi khởi chạy."}
            if _auto_restart:
                add_log("Monitor", f"Đã tự khôi phục luồng Auto Monitor #{monitor_id} [{mon_row['name']}].", "WARNING")
                return {"ok": True, "lifecycle_state": "Running",
                        "message": f"Đã tự khôi phục Monitor #{monitor_id}.{heartbeat_warning}"}
            add_log("Monitor", f"Đã bật luồng Auto Monitor #{monitor_id}", "INFO")
            return {"ok": True, "lifecycle_state": "Running",
                    "message": f"Đã bật theo dõi tự động #{monitor_id}.{heartbeat_warning}"}

    def stop_monitor(self, monitor_id: int) -> Dict[str, Any]:
        with self._monitor_lifecycle_lock:
            with self._monitor_lock:
                stop_ev = self._monitor_threads.get(monitor_id)
                if stop_ev is not None:
                    stop_ev.set()
                worker = self._monitor_workers.get(monitor_id)
                heartbeat = self._monitor_heartbeats.get(monitor_id)
                alive = bool((worker and worker.is_alive()) or (heartbeat and heartbeat.is_alive()))
                self._monitor_states[monitor_id] = "Stopping"

            try:
                conn = get_conn()
                cursor = conn.execute(
                    "UPDATE monitors SET is_running = 0, status = ? WHERE id = ?",
                    ("Stopping" if alive else "Stopped", monitor_id),
                )
                conn.commit()
                exists = cursor.rowcount > 0
                conn.close()
            except Exception as exc:
                add_log("Monitor", f"Đã gửi tín hiệu dừng Monitor #{monitor_id}, nhưng chưa cập nhật được DB: {type(exc).__name__}", "ERROR")
                return {"ok": False, "lifecycle_state": "Stopping" if alive else "Error",
                        "message": f"Đã gửi tín hiệu dừng nhưng trạng thái DB chưa đồng bộ: {exc}"}

            if not exists:
                if not alive:
                    with self._monitor_lock:
                        self._monitor_states.pop(monitor_id, None)
                        self._monitor_threads.pop(monitor_id, None)
                        self._monitor_workers.pop(monitor_id, None)
                        self._monitor_heartbeats.pop(monitor_id, None)
                return {"ok": False, "lifecycle_state": "Stopped", "message": f"Không tìm thấy Monitor #{monitor_id}."}
            state = "Stopping" if alive else self._reap_monitor_locked(monitor_id)
            self._monitor_restart_history.pop(monitor_id, None)

        add_log("Monitor", f"Đã yêu cầu dừng luồng Auto Monitor #{monitor_id}", "WARNING")
        if state == "Stopping":
            message = f"Đã yêu cầu dừng Monitor #{monitor_id}; cần chờ worker kết thúc và đồng bộ trạng thái."
        else:
            message = f"Đã dừng theo dõi tự động #{monitor_id}"
        return {"ok": True, "lifecycle_state": state, "message": message}

    def toggle_monitor(self, monitor_id: int) -> Dict[str, Any]:
        with self._monitor_lifecycle_lock:
            try:
                conn = get_conn()
                row = conn.execute("SELECT is_running FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
                conn.close()
            except Exception as exc:
                return {"ok": False, "message": f"Không thể đọc Monitor #{monitor_id}: {exc}"}
            if not row:
                return {"ok": False, "message": f"Không tìm thấy Monitor #{monitor_id}."}
            if row["is_running"]:
                return self.stop_monitor(monitor_id)
            return self.start_monitor(monitor_id)

    def delete_monitor(self, monitor_id: int) -> Dict[str, Any]:
        with self._monitor_lifecycle_lock:
            state = self._reap_monitor_locked(monitor_id)
            with self._monitor_lock:
                worker = self._monitor_workers.get(monitor_id)
                heartbeat = self._monitor_heartbeats.get(monitor_id)
                alive = bool((worker and worker.is_alive()) or (heartbeat and heartbeat.is_alive()))
            conn = get_conn()
            row = conn.execute("SELECT is_running FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
            if not row:
                conn.close()
                return {"ok": False, "lifecycle_state": "Stopped", "message": "Không tìm thấy Monitor."}
            if alive or row["is_running"] or state in {"Starting", "Running", "Stopping"}:
                conn.close()
                return {"ok": False, "lifecycle_state": "Stopping" if state == "Stopping" else state,
                        "message": "Hãy dừng Monitor và đợi worker kết thúc trước khi xóa."}
            conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            conn.commit()
            conn.close()
            with self._monitor_lock:
                self._monitor_states.pop(monitor_id, None)
                self._monitor_threads.pop(monitor_id, None)
                self._monitor_workers.pop(monitor_id, None)
                self._monitor_heartbeats.pop(monitor_id, None)
            self._monitor_upload_counts.pop(monitor_id, None)
            self._monitor_restart_history.pop(monitor_id, None)
            return {"ok": True, "lifecycle_state": "Stopped", "message": "Đã xóa Monitor."}

    def start_monitor_supervisor(self):
        with self._monitor_supervisor_lock:
            if self._monitor_supervisor is not None and self._monitor_supervisor.is_alive():
                return
            self._monitor_supervisor = threading.Thread(
                target=self._monitor_supervisor_loop,
                daemon=True,
                name="DHB-Monitor-Supervisor",
            )
            self._monitor_supervisor.start()
        add_log("Monitor", "Đã bật bộ giám sát Auto Monitor (heartbeat).", "INFO")

    def _monitor_supervisor_loop(self):
        while True:
            try:
                self._check_monitors_health()
            except Exception as e:
                try:
                    add_log("Monitor", f"Lỗi bộ giám sát Auto Monitor: {e}", "ERROR")
                except Exception:
                    pass
            time.sleep(MONITOR_HEALTH_CHECK_SECONDS)

    def _check_monitors_health(self):
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, status, last_heartbeat_at, is_running FROM monitors ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        self.refresh_monitor_states([int(row["id"]) for row in rows])
        now = time.time()
        for row in rows:
            if not row["is_running"]:
                continue
            monitor_id = int(row["id"])
            monitor_name = row["name"] or f"#{monitor_id}"
            with self._monitor_lifecycle_lock:
                state = self._monitor_states.get(monitor_id)
                if state == "Stopping":
                    continue
                with self._monitor_lock:
                    worker = self._monitor_workers.get(monitor_id)
                    heartbeat = self._monitor_heartbeats.get(monitor_id)
                    worker_alive = bool(worker and worker.is_alive())
                    heartbeat_alive = bool(heartbeat and heartbeat.is_alive())
                if not worker_alive:
                    self._restart_dead_monitor(monitor_id, monitor_name)
                    continue

                if not heartbeat_alive:
                    try:
                        self._start_monitor_heartbeat(monitor_id)
                        add_log("Monitor", f"[{monitor_name}] Bộ đếm heartbeat đã dừng, đã khởi động lại.", "WARNING")
                    except Exception as exc:
                        add_log("Monitor", f"[{monitor_name}] Không thể khởi động heartbeat: {type(exc).__name__}.", "ERROR")

                last_hb = self._parse_monitor_ts(row["last_heartbeat_at"])
                if last_hb is None or now - last_hb > MONITOR_STALE_SECONDS:
                    status = row["status"] or ""
                    if not status.startswith("⚠️"):
                        stale_for = int(now - last_hb) if last_hb else 0
                        self._set_monitor_status_raw(
                            monitor_id,
                            f"⚠️ Luồng không phản hồi {stale_for}s — đang theo dõi, không tự khởi động lại để tránh đăng trùng.",
                            only_running=True,
                        )
                        add_log(
                            "Monitor",
                            f"[{monitor_name}] Không có heartbeat trong {stale_for}s. Giữ nguyên luồng để tránh đăng trùng; kiểm tra log nếu kéo dài.",
                            "WARNING",
                        )

    def _restart_dead_monitor(self, monitor_id: int, monitor_name: str):
        with self._monitor_lifecycle_lock:
            conn = get_conn()
            row = conn.execute("SELECT is_running FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
            conn.close()
            if not row or not row["is_running"]:
                return

            state = self._reap_monitor_locked(monitor_id)
            if state == "Stopping" or self._monitor_handles_alive(monitor_id):
                return

            now = time.time()
            history = [
                t for t in self._monitor_restart_history.get(monitor_id, [])
                if now - t < MONITOR_RESTART_WINDOW_SECONDS
            ]
            if len(history) >= MONITOR_MAX_RESTARTS:
                conn = get_conn()
                conn.execute(
                    "UPDATE monitors SET is_running = 0, status = ? WHERE id = ?",
                    (f"Đã dừng sau {MONITOR_MAX_RESTARTS} lần lỗi liên tiếp — kiểm tra log rồi bật lại.", monitor_id),
                )
                conn.commit()
                conn.close()
                self._monitor_restart_history.pop(monitor_id, None)
                with self._monitor_lock:
                    self._monitor_states[monitor_id] = "Error"
                add_log(
                    "Monitor",
                    f"[{monitor_name}] Luồng dừng lặp lại {MONITOR_MAX_RESTARTS} lần trong "
                    f"{MONITOR_RESTART_WINDOW_SECONDS // 60} phút — đã tắt tự khôi phục để tránh vòng lặp lỗi.",
                    "ERROR",
                )
                return

            history.append(now)
            self._monitor_restart_history[monitor_id] = history
            add_log(
                "Monitor",
                f"[{monitor_name}] Phát hiện luồng đã dừng, tự khởi động lại "
                f"(lần {len(history)}/{MONITOR_MAX_RESTARTS} trong {MONITOR_RESTART_WINDOW_SECONDS // 60} phút).",
                "WARNING",
            )
            result = self.start_monitor(monitor_id, _auto_restart=True)
            if not result.get("ok"):
                add_log("Monitor", f"[{monitor_name}] Không thể tự khởi động lại: {result.get('message')}", "ERROR")

    def _start_monitor_heartbeat(self, monitor_id: int, stop_ev: Optional[threading.Event] = None):
        with self._monitor_lock:
            existing = self._monitor_heartbeats.get(monitor_id)
            if existing is not None and existing.is_alive():
                return
            if stop_ev is None:
                stop_ev = self._monitor_threads.get(monitor_id)
            if (stop_ev is None or stop_ev.is_set()
                    or self._monitor_threads.get(monitor_id) is not stop_ev
                    or self._monitor_states.get(monitor_id) == "Stopping"):
                return
            heartbeat = threading.Thread(
                target=self._monitor_heartbeat_loop,
                args=(monitor_id, stop_ev),
                daemon=True,
                name=f"DHB-Monitor-HB-{monitor_id}",
            )
            self._monitor_heartbeats[monitor_id] = heartbeat
        try:
            heartbeat.start()
        except Exception:
            with self._monitor_lock:
                if self._monitor_heartbeats.get(monitor_id) is heartbeat:
                    self._monitor_heartbeats.pop(monitor_id, None)
            raise

    def _monitor_heartbeat_loop(self, monitor_id: int, stop_ev: threading.Event):
        while not stop_ev.wait(MONITOR_HEARTBEAT_SECONDS):
            try:
                conn = get_conn()
                conn.execute(
                    "UPDATE monitors SET last_heartbeat_at = ? WHERE id = ? AND is_running = 1",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), monitor_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    def _monitor_worker(self, monitor_id: int, stop_ev: threading.Event):
        error = None
        try:
            self._monitor_loop(monitor_id, stop_ev)
        except Exception as e:
            error = e
            try:
                add_log("Monitor", f"Luồng Monitor #{monitor_id} gặp lỗi ngoài dự kiến: {e}", "ERROR")
            except Exception:
                pass
        finally:
            with self._monitor_lock:
                current = self._monitor_threads.get(monitor_id) is stop_ev
                stopping = current and self._monitor_states.get(monitor_id) == "Stopping"
                if current and not stopping:
                    self._monitor_states[monitor_id] = "Error"
            stop_ev.set()
            if not stopping:
                try:
                    status = (
                        f"Luồng gặp lỗi, chờ tự phục hồi: {error}"
                        if error is not None
                        else "Luồng đã thoát ngoài yêu cầu; đang chờ tự khôi phục."
                    )
                    self._set_monitor_status_raw(
                        monitor_id, status, only_running=True,
                    )
                except Exception:
                    pass

    @staticmethod
    def _parse_monitor_ts(value: Any) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    def _set_monitor_status_raw(self, monitor_id: int, status_text: str, only_running: bool = False):
        conn = get_conn()
        if only_running:
            conn.execute("UPDATE monitors SET status = ? WHERE id = ? AND is_running = 1", (status_text, monitor_id))
        else:
            conn.execute("UPDATE monitors SET status = ? WHERE id = ?", (status_text, monitor_id))
        conn.commit()
        conn.close()

    @staticmethod
    def _record_published_history_for_item(queue_id: int, channel: Dict[str, Any]) -> bool:
        """Ghi published_history cho lượt đăng Completed có link thật.

        Trước đây chỉ Auto Monitor ghi bảng này, nên video Completed từ link /
        kéo-thả / đường dẫn local không được lưu lịch sử. Giữ source_post_id để
        tương thích dữ liệu cũ và lưu content_key riêng cho file đã nhận diện.
        """
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status, published_url, post_id, source_url, content_key FROM video_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if not row:
                return False
            if (row["status"] or "").strip() != "Completed" or not (row["published_url"] or "").strip():
                return False
            post_id = _history_source_post_id(dict(row), queue_id)
            conn.execute(
                "INSERT OR IGNORE INTO published_history "
                "(source_post_id, source_url, channel_id, content_key, published_url) VALUES (?, ?, ?, ?, ?)",
                (
                    post_id,
                    row["source_url"] or "",
                    channel.get("id"),
                    (row["content_key"] or "").strip(),
                    (row["published_url"] or "").strip(),
                ),
            )
            conn.execute(
                "UPDATE published_history SET "
                "source_url = CASE WHEN COALESCE(source_url, '') = '' THEN ? ELSE source_url END, "
                "content_key = CASE WHEN COALESCE(content_key, '') = '' THEN ? ELSE content_key END, "
                "published_url = CASE WHEN COALESCE(published_url, '') = '' THEN ? ELSE published_url END "
                "WHERE source_post_id = ? AND channel_id = ?",
                (
                    row["source_url"] or "",
                    (row["content_key"] or "").strip(),
                    (row["published_url"] or "").strip(),
                    post_id,
                    channel.get("id"),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def _record_monitor_published(self, queue_id: int, item_data: Dict[str, Any], mon: Dict[str, Any]) -> bool:
        """Ghi published_history CHỈ khi video đã Completed và có link thật. Trả về True nếu đã ghi."""
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status, published_url, content_key FROM video_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if not row:
                return False
            if (row["status"] or "").strip() != "Completed" or not (row["published_url"] or "").strip():
                return False
            conn.execute(
                "INSERT OR IGNORE INTO published_history "
                "(source_post_id, source_url, channel_id, content_key, published_url) VALUES (?, ?, ?, ?, ?)",
                (
                    item_data.get("post_id"),
                    item_data.get("source_url"),
                    mon.get("target_channel_id"),
                    (row["content_key"] or "").strip(),
                    (row["published_url"] or "").strip(),
                ),
            )
            conn.execute(
                "UPDATE published_history SET "
                "source_url = CASE WHEN COALESCE(source_url, '') = '' THEN ? ELSE source_url END, "
                "content_key = CASE WHEN COALESCE(content_key, '') = '' THEN ? ELSE content_key END, "
                "published_url = CASE WHEN COALESCE(published_url, '') = '' THEN ? ELSE published_url END "
                "WHERE source_post_id = ? AND channel_id = ?",
                (
                    item_data.get("source_url") or "",
                    (row["content_key"] or "").strip(),
                    (row["published_url"] or "").strip(),
                    item_data.get("post_id"),
                    mon.get("target_channel_id"),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def _monitor_loop(self, monitor_id: int, stop_ev: threading.Event):
        while not stop_ev.is_set():
            with self._monitor_lock:
                if self._monitor_threads.get(monitor_id) is not stop_ev:
                    break
            conn = get_conn()
            mon_row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
            conn.close()
            if not mon_row or not mon_row["is_running"]:
                break

            mon = dict(mon_row)
            mon_name = mon["name"]
            urls = [u.strip() for u in (mon.get("source_urls") or "").splitlines() if u.strip()]
            max_vids = max(1, int(mon.get("max_videos_per_scan") or 3))
            min_views = int(mon.get("min_views") or 0)
            interval_min = max(1, int(mon.get("check_interval_minutes") or 30))

            try:
                self._update_monitor_status(monitor_id, f"Đang quét {len(urls)} nguồn...")
                scanned = scan_social_urls(
                    urls,
                    max_items_per_url=max_vids * 2,
                    min_views=min_views,
                    stop_event=stop_ev,
                )

                proj_name = f"Monitor - {mon_name}"
                new_items = []
                conn = get_conn()
                for s in scanned:
                    pid = s.get("post_id") or s.get("source_url")
                    # Đã đăng thành công trước đó trên đúng kênh đích (history chỉ ghi SAU khi upload xong — S2)
                    already = _monitor_source_already_published(conn, pid, mon.get("target_channel_id"))
                    if not already:
                        # Đã nằm trong hàng đợi của monitor này HOẶC monitor khác cùng đích đăng
                        # (chống đăng trùng khi 2 monitor cùng theo dõi một nguồn — F1).
                        if _monitor_source_queued(
                            conn, pid, proj_name, mon.get("target_channel_id"), mon.get("target_group_name") or "Mặc định",
                        ):
                            continue
                        new_items.append(s)
                        if len(new_items) >= max_vids:
                            break
                conn.close()

                if not new_items:
                    self._update_monitor_status(monitor_id, f"Không có video mới. Chờ {interval_min} phút...")
                else:
                    add_log(
                        "Monitor",
                        f"[{mon_name}] Phát hiện {len(new_items)} video mới! Bắt đầu xử lý tự động...",
                        "INFO",
                    )
                    for idx, item_data in enumerate(new_items):
                        if stop_ev.is_set():
                            break
                        title = item_data["title"]
                        desc = item_data["description"]
                        tags = item_data["tags"]

                        ai_act = mon.get("ai_Action") or "none"
                        if ai_act in ("translate", "rewrite_seo"):
                            self._update_monitor_status(monitor_id, f"Đang tối ưu AI cho: {title[:30]}...")
                            ai_res = ai_optimize_youtube_metadata(
                                title=title,
                                description=desc,
                                action=ai_act,
                                provider=mon.get("ai_provider") or "gemini",
                                target_lang=mon.get("ai_target_lang") or "Tiếng Việt",
                                custom_prompt=mon.get("ai_prompt") or "",
                            )
                            title = ai_res["title"]
                            desc = ai_res["description"]
                            tags = ai_res["tags"]

                        conn = get_conn()
                        conn.execute("INSERT OR IGNORE INTO projects (name) VALUES (?)", (proj_name,))
                        queue_id = insert_monitor_queue_item(conn, proj_name, mon, item_data, title, desc, tags)
                        conn.commit()
                        conn.close()
                        if queue_id is None:
                            # Monitor khác cùng đích đã chen nguồn này vào hàng đợi trong lúc AI xử lý —
                            # insert nguyên tử (WHERE NOT EXISTS) chặn tại DB nên không bao giờ đăng trùng.
                            add_log(
                                "Monitor",
                                f"[{mon_name}] Bỏ qua '{title[:40]}': nguồn đã nằm trong hàng đợi của "
                                "một monitor khác cùng đích đăng.",
                                "WARNING",
                            )
                            continue

                        self._update_monitor_status(monitor_id, f"Đang xử lý video ({idx+1}/{len(new_items)}): {title[:30]}")
                        monitor_token = uuid.uuid4().hex
                        try:
                            self.process_single_item(
                                queue_id, idx, True, True, True,
                                claim_token=monitor_token,
                                stop_event=stop_ev,
                                upload_counter=self._monitor_upload_counts.setdefault(monitor_id, {"n": 0}),
                                owner="monitor",
                            )
                        finally:
                            release_item_claim(queue_id, monitor_token)
                            with closing(get_conn()) as approval_conn, approval_conn:
                                approval_conn.execute(
                                    "UPDATE upload_approvals SET state = 'finished' WHERE run_token = ? AND state = 'accepted'",
                                    (monitor_token,),
                                )

                        # S2: chỉ đánh dấu "đã xử lý" KHI video thực sự đăng thành công —
                        # video lỗi/dừng sẽ được quét lại ở chu kỳ sau thay vì bị bỏ vĩnh viễn.
                        published_now = self._record_monitor_published(queue_id, item_data, mon)
                        if not published_now:
                            add_log(
                                "Monitor",
                                f"[{mon_name}] Video '{title[:40]}' chưa đăng xong — giữ trong hàng đợi để chạy lại, chu kỳ sau sẽ quét tiếp nguồn mới.",
                                "WARNING",
                            )

                        # CHỐNG SPAM: giãn cách bắt buộc giữa các lần đăng tự động của Monitor
                        if idx < len(new_items) - 1:
                            gap_min = max(0, int(get_settings().get("monitor_upload_gap_minutes", 30) or 30))
                            if gap_min > 0:
                                add_log(
                                    "Monitor",
                                    f"[{mon_name}] Chờ giãn cách {gap_min} phút trước khi đăng video tiếp theo (chống spam, bảo vệ kênh).",
                                    "INFO",
                                )
                                for _ in range(gap_min * 60):
                                    if stop_ev.is_set():
                                        break
                                    time.sleep(1)
                                if stop_ev.is_set():
                                    break

                    self._update_monitor_status(monitor_id, f"Hoàn tất chu kỳ. Quét tiếp sau {interval_min} phút.")

            except Exception as e:
                self._update_monitor_status(monitor_id, f"Lỗi: {e}")
                add_log("Monitor", f"Lỗi luồng Monitor [{mon_name}]: {e}", "ERROR")

            for _ in range(interval_min * 60):
                if stop_ev.is_set():
                    break
                time.sleep(1)

    def _update_monitor_status(self, monitor_id: int, status_text: str):
        with self._monitor_lock:
            if self._monitor_states.get(monitor_id) == "Stopping":
                return
        conn = get_conn()
        conn.execute(
            "UPDATE monitors SET status = ?, last_checked_at = ? WHERE id = ? AND is_running = 1",
            (status_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), monitor_id),
        )
        conn.commit()
        conn.close()


pipeline_manager = PipelineManager()
