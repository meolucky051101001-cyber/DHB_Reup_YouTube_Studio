import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from database import get_conn, add_log, get_settings, save_settings
from config import APP_TEMP_DIR
from scanner_downloader import download_video, scan_social_urls
from render_engine import render_video, generate_thumbnail_from_video
from ai_engine import ai_optimize_youtube_metadata
from youtube_uploader import upload_video_to_youtube


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
# Fail-closed trong tiến trình hiện tại: nếu không ghi được mốc kết thúc upload,
# mọi lượt đăng sau bị chặn cho tới khi khởi động lại tool và kiểm tra lại.
_UPLOAD_GAP_STATE_BROKEN = False

RENDER_QUEUE_DEFAULT_WORKERS = 2


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
    if value < 0:
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
    current = time.time() if now is None else float(now)
    return read_last_upload_epoch() + UPLOAD_MIN_GAP_SECONDS - current


def mark_upload_finished(when: Optional[float] = None) -> None:
    """Ghi mốc kết thúc lượt upload thật; raise UploadGapStateError nếu không ghi được."""
    timestamp = time.time() if when is None else float(when)
    try:
        save_settings({_LAST_UPLOAD_FINISHED_SETTING_KEY: str(timestamp)})
    except Exception as exc:
        raise UploadGapStateError(f"Không ghi được mốc kết thúc upload: {exc}") from exc


def _record_upload_finished_safely(title: str) -> None:
    """Ghi mốc kết thúc sau khi uploader trả về; lỗi ghi sẽ chặn mọi lượt đăng sau."""
    global _UPLOAD_GAP_STATE_BROKEN
    try:
        mark_upload_finished()
    except Exception as exc:
        _UPLOAD_GAP_STATE_BROKEN = True
        try:
            add_log(
                "Upload",
                f"❌ Không ghi được mốc kết thúc upload của '{title}': {exc}. "
                "Các lượt đăng tiếp theo bị chặn để bảo vệ kênh; hãy khởi động lại tool sau khi kiểm tra ổ đĩa/DB.",
                "ERROR",
                video_title=title,
            )
        except Exception:
            pass


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
    except Exception:
        pass


def reserve_upload_slot(stop_event: threading.Event) -> bool:
    """
    Giữ chỗ một lượt upload và bảo đảm cách lượt trước >= 30 phút, tính từ
    mốc muộn hơn giữa lúc bắt đầu và lúc kết thúc upload trước.
    Mốc lưu trong app_settings nên đúng cả khi restart hoặc chạy nhiều luồng.
    Trả về False nếu bị dừng trong lúc chờ; raise UploadGapStateError nếu
    không đọc/ghi được mốc (fail closed — tuyệt đối không đăng).
    """
    while True:
        with _UPLOAD_GAP_LOCK:
            now = time.time()
            remaining = read_upload_gap_remaining(now)
            if remaining <= 0:
                try:
                    save_settings({_LAST_UPLOAD_SETTING_KEY: str(now)})
                except Exception as exc:
                    raise UploadGapStateError(f"Không ghi được mốc bắt đầu upload: {exc}") from exc
                return True

        deadline = time.time() + remaining
        while time.time() < deadline:
            if stop_event.is_set():
                return False
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
        if stop_event.is_set():
            return False


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
        self._monitor_restart_history: Dict[int, List[float]] = {}
        self._monitor_lock = threading.Lock()
        self._monitor_supervisor: Optional[threading.Thread] = None
        self._monitor_supervisor_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def clear_stale_claims(self):
        """Dọn claim còn sót từ phiên app trước (gọi một lần lúc khởi động)."""
        try:
            conn = get_conn()
            conn.execute("UPDATE video_queue SET claim_token = ''")
            conn.commit()
            conn.close()
        except Exception:
            pass

    def stop_all(self):
        self._stop_event.set()

        # 1. Hủy diệt tức thì toàn bộ tiến trình FFmpeg đang chạy trên máy
        try:
            from render_engine import kill_all_render_processes
            kill_all_render_processes()
        except Exception:
            pass

        # 2. Chỉ dừng các video CHƯA đăng. Video đang Uploading phải chạy xong
        # (người dùng chọn "chờ upload hiện tại xong rồi dừng") — đánh dấu
        # Stopped ngay lúc này sẽ khiến UI báo dừng trong khi Chrome vẫn đăng.
        # _is_running giữ nguyên True cho tới khi batch worker kết thúc, nhờ đó
        # không thể start thêm batch mới trong lúc upload cuối đang hoàn tất.
        try:
            conn = get_conn()
            conn.execute("""
                UPDATE video_queue
                SET status = 'Stopped',
                    error_message = 'Đã dừng theo yêu cầu của người dùng'
                WHERE (status LIKE 'Rendering%'
                   OR status LIKE 'Đang Render%'
                   OR status LIKE 'Downloading%'
                   OR status LIKE 'Đang tải%'
                   OR status = 'Pending')
                   AND project_name NOT LIKE 'Monitor -%'
            """)
            conn.commit()
            conn.close()
        except Exception:
            pass

        add_log(
            "Pipeline",
            "Đã yêu cầu dừng. Video đang đăng (nếu có) sẽ được hoàn tất trước khi dừng hẳn; "
            "các video còn lại chưa đăng đã chuyển Stopped.",
            "WARNING",
        )

    def start_batch(
        self,
        item_ids: List[int],
        do_download: bool = True,
        do_render: bool = True,
        do_upload: bool = True,
        force_reupload: bool = False,
    ):
        item_ids = list(dict.fromkeys(item_ids))
        with self._lifecycle_lock:
            if self._is_running or self._active_threads > 0:
                return {"ok": False, "message": "Hệ thống đang chạy hoặc đang dừng một tiến trình khác."}

            self._stop_event.clear()
            self._is_running = True
            self._batch_upload_box = {"n": 0}
            self._active_threads = 1
            batch_token = uuid.uuid4().hex
            try:
                render_queue.configure(
                    int(get_settings().get("max_concurrent_renders", RENDER_QUEUE_DEFAULT_WORKERS) or RENDER_QUEUE_DEFAULT_WORKERS)
                )
            except Exception:
                pass

        t = threading.Thread(
            target=self._batch_worker_guarded,
            args=(item_ids, do_download, do_render, do_upload, batch_token, force_reupload),
            daemon=True,
        )
        try:
            t.start()
        except Exception as exc:
            with self._lifecycle_lock:
                self._is_running = False
                self._active_threads = 0
            return {"ok": False, "message": f"Không thể khởi chạy tiến trình: {exc}"}
        return {"ok": True, "message": f"Đã bắt đầu xử lý {len(item_ids)} video."}

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
                add_log("Pipeline", f"Tiến trình hàng đợi gặp lỗi: {exc}", "ERROR")
            except Exception:
                pass
        finally:
            with self._lifecycle_lock:
                self._is_running = False
                self._active_threads = 0
            try:
                conn = get_conn()
                conn.execute(
                    "UPDATE video_queue SET claim_token = '' WHERE claim_token = ?",
                    (batch_token,),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

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
        delay_sec = max(0, int(settings.get("upload_delay_seconds", 5) or 5))
        sem = threading.Semaphore(max_workers)
        threads = []

        add_log(
            "Pipeline",
            f"Khởi chạy hàng đợi ({len(item_ids)} video) với tối đa {max_workers} luồng song song.",
            "INFO",
        )

        for idx, item_id in enumerate(item_ids):
            if self._stop_event.is_set():
                break
            sem.acquire()
            if self._stop_event.is_set():
                sem.release()
                break

            worker_t = threading.Thread(
                target=self._process_single_item_safe,
                args=(item_id, idx, do_download, do_render, do_upload, sem, batch_token, force_reupload),
                daemon=True,
            )
            threads.append(worker_t)
            worker_t.start()
            if delay_sec > 0 and idx < len(item_ids) - 1:
                import random
                actual_delay = delay_sec + random.randint(2, 6)
                time.sleep(actual_delay)

        for wt in threads:
            wt.join()

        add_log("Pipeline", "Hoàn tất xử lý hàng đợi video.", "SUCCESS")

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
    ):
        try:
            self.process_single_item(
                item_id, row_idx, do_download, do_render, do_upload,
                claim_token=batch_token, force_reupload=force_reupload,
                stop_event=stop_event, upload_counter=upload_counter,
            )
        finally:
            release_item_claim(item_id, batch_token)
            sem.release()

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
    ):
        conn = get_conn()
        row = conn.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        if not row:
            return

        item = dict(row)
        title = item.get("title") or f"Video #{item_id}"
        # Mỗi làn (batch / monitor) mang stop-event và bộ đếm riêng: bấm Dừng batch
        # không được phép giết item của monitor (S1), bộ đếm giới hạn cũng tách làn (S3).
        se = stop_event if stop_event is not None else self._stop_event
        counter = upload_counter if upload_counter is not None else self._batch_upload_box

        existing_status = (item.get("status") or "").strip()
        published_url = (item.get("published_url") or "").strip()
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
        cur = conn.execute(
            "UPDATE video_queue SET claim_token = ? WHERE id = ? AND COALESCE(claim_token, '') = ''",
            (token, item_id),
        )
        conn.commit()
        claimed = cur.rowcount == 1
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
                return
            cols = ", ".join(f"{k} = ?" for k in kwargs.keys())
            vals = list(kwargs.values()) + [item_id]
            c = get_conn()
            c.execute(f"UPDATE video_queue SET {cols} WHERE id = ?", vals)
            c.commit()
            c.close()

        def _make_progress_cb(progress_col: str):
            state = {"last_pct": -1, "last_time": 0.0}

            def _cb(pct: int, msg: str):
                if se.is_set():
                    return
                now = time.time()
                if pct >= 100 or abs(pct - state["last_pct"]) >= 3 or (now - state["last_time"]) >= 0.8:
                    state["last_pct"] = pct
                    state["last_time"] = now
                    _update_row(**{progress_col: pct, "status": msg})

            return _cb

        try:
            input_path = item.get("input_video_path") or ""
            thumb_path = item.get("thumbnail_path") or ""

            # BƯỚC 1: TẢI VIDEO (DOWNLOAD)
            if do_download and (not input_path or not os.path.exists(input_path)):
                if se.is_set():
                    _update_row(status="Stopped")
                    return
                _update_row(status="Downloading...", download_progress=5, error_message="")
                dl_res = download_video(
                    source_url=item.get("source_url") or "",
                    post_id=item.get("post_id") or str(item_id),
                    progress_callback=_make_progress_cb("download_progress"),
                )
                input_path = dl_res["video_path"]
                thumb_path = dl_res.get("thumbnail_path") or thumb_path
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
            if do_render:
                if se.is_set():
                    _update_row(status="Stopped")
                    return
                if not input_path or not os.path.exists(input_path):
                    raise FileNotFoundError("Chưa có file video gốc để Render.")

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

                # Nguồn thumbnail: 'reup' = dùng ảnh gốc của nguồn nếu có (học ReupThumbnail của DHB),
                # 'generate' = luôn tự chụp từ video render. Mặc định reup.
                thumb_source = str(get_settings().get("thumbnail_source") or "reup").lower()
                if thumb_source == "generate" or not thumb_path or not os.path.exists(thumb_path):
                    gen_thumb = generate_thumbnail_from_video(output_path or input_path)
                    if gen_thumb:
                        thumb_path = gen_thumb

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

                channel = self._resolve_target_channel(item, row_idx)
                if not channel:
                    raise RuntimeError(
                        "Chưa chọn Kênh YouTube đích hoặc Nhóm Kênh chưa có kênh nào. Hãy thêm/chọn kênh ở tab Quản Lý Kênh."
                    )

                conn_f = get_conn()
                f_row = conn_f.execute("SELECT * FROM video_queue WHERE id = ?", (item_id,)).fetchone()
                conn_f.close()
                fresh_item = dict(f_row) if f_row else item

                option_group = None
                if fresh_item.get("option_group_id"):
                    c_opt = get_conn()
                    og_row = c_opt.execute(
                        "SELECT * FROM upload_option_groups WHERE id = ?",
                        (fresh_item["option_group_id"],),
                    ).fetchone()
                    c_opt.close()
                    if og_row:
                        option_group = dict(og_row)

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

                if not reserve_upload_slot(se):
                    _update_row(status="Stopped", error_message="Đã dừng khi đang chờ giãn cách đăng.")
                    return
                if se.is_set():
                    _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                    return

                _update_row(status=f"Uploading -> {channel['name']}", upload_progress=10, error_message="")
                try:
                    pub_res = upload_video_to_youtube(
                        video_item=fresh_item,
                        channel=channel,
                        option_group=option_group,
                        progress_callback=_make_progress_cb("upload_progress"),
                    )
                finally:
                    # Mốc kết thúc là chuẩn giãn cách cho lượt kế tiếp, kể cả khi upload lỗi giữa chừng.
                    _record_upload_finished_safely(title)

                if isinstance(pub_res, dict):
                    pub_url = pub_res.get("url", "")
                    is_cancelled = bool(pub_res.get("cancelled", False))
                    is_flagged = bool(pub_res.get("flagged_copyright", False))
                    is_uncertain = bool(pub_res.get("published_uncertain", False))
                    warning_msg = pub_res.get("warning_message", "")
                else:
                    pub_url = str(pub_res)
                    is_cancelled = False
                    is_flagged = False
                    is_uncertain = False
                    warning_msg = ""

                if is_cancelled:
                    _update_row(
                        upload_progress=100,
                        status="Đã Hủy (Dính Bản Quyền)",
                        error_message=warning_msg,
                    )
                    add_log(
                        "Upload",
                        f"🛑 ĐÃ HỦY ĐĂNG lên kênh [{channel['name']}] do CẢNH BÁO VI PHẠM BẢN QUYỀN: {warning_msg}",
                        "WARNING",
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
                    add_log(
                        "Upload",
                        f"⚠️ Cảnh báo bản quyền: {warning_msg}",
                        "WARNING",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                elif is_uncertain or not (pub_url or "").strip():
                    # Đã bấm Xuất bản nhưng không xác minh được URL video.
                    # Không được báo Completed và không tự xóa/move file nguồn.
                    uncertain_msg = warning_msg or (
                        "Không xác minh được video đã đăng thành công (thiếu URL từ YouTube Studio). "
                        "Có thể video đã lên Studio — hãy kiểm tra trực tiếp trước khi chạy lại để tránh đăng trùng."
                    )
                    _update_row(
                        upload_progress=100,
                        status="Error",
                        error_message=uncertain_msg,
                    )
                    add_log(
                        "Upload",
                        f"❌ KHÔNG xác minh được kết quả đăng lên kênh [{channel['name']}]: {uncertain_msg}",
                        "ERROR",
                        channel_name=channel["name"],
                        video_title=title,
                    )
                else:
                    _update_row(
                        upload_progress=100,
                        status="Completed",
                        published_url=pub_url,
                        error_message="",
                    )
                    add_log(
                        "Upload",
                        f"✅ Đăng thành công lên kênh [{channel['name']}]: {pub_url}",
                        "SUCCESS",
                        channel_name=channel["name"],
                        video_title=title,
                    )

                # Tự động di chuyển video nguồn trên máy vào thư mục uploaded/ (chỉ khi đăng thành công)
                is_local_file = item.get("source_type") in ("File", "LocalFile") or item.get("platform") == "LocalFile"
                if not is_cancelled and not is_uncertain and pub_url and is_local_file and input_path and os.path.exists(input_path):
                    try:
                        p = Path(input_path)
                        if p.parent.name != "uploaded":
                            uploaded_dir = p.parent / "uploaded"
                            uploaded_dir.mkdir(exist_ok=True)
                            dest_file = uploaded_dir / p.name
                            if not dest_file.exists():
                                shutil.move(str(p), str(dest_file))
                                _update_row(input_video_path=str(dest_file))
                                add_log("Pipeline", f"Đã lưu trữ video gốc vào: {dest_file}", "INFO")
                    except Exception as mv_err:
                        add_log("Pipeline", f"Không thể di chuyển file vào uploaded/: {mv_err}", "WARNING")

                # Xóa file tạm nếu cài đặt yêu cầu — chỉ xóa file nằm trong thư mục temp/ của tool
                settings = get_settings()
                if settings.get("delete_temp_after_upload") and input_path and not is_uncertain:
                    try:
                        real_input = os.path.realpath(input_path)
                        temp_root = os.path.realpath(str(APP_TEMP_DIR))
                        if real_input.startswith(temp_root):
                            os.remove(real_input)
                    except Exception:
                        pass

        except InterruptedError:
            _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
            add_log("Pipeline", f"Đã dừng xử lý video: {title}", "WARNING", video_title=title)
        except Exception as e:
            if se.is_set():
                _update_row(status="Stopped", error_message="Đã dừng theo yêu cầu của người dùng.")
                return
            err_msg = str(e)
            _update_row(status="Error", error_message=err_msg)
            add_log("Error", f"Lỗi xử lý '{title}': {err_msg}", "ERROR", video_title=title)

    def _resolve_target_channel(self, item: Dict[str, Any], row_idx: int) -> Dict[str, Any]:
        conn = get_conn()
        try:
            if item.get("target_channel_id"):
                ch = conn.execute(
                    "SELECT * FROM youtube_channels WHERE id = ?",
                    (item["target_channel_id"],),
                ).fetchone()
                if not ch:
                    raise RuntimeError(
                        f"Kênh đích đã chọn (ID {item['target_channel_id']}) không còn tồn tại hoặc đã bị xóa. "
                        "Hãy chọn lại kênh cụ thể trước khi bắt đầu — hệ thống không tự đăng sang kênh khác."
                    )
                return dict(ch)

            target_prof = (item.get("target_profile_name") or "").strip()
            if target_prof:
                channels_in_prof = conn.execute(
                    "SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC",
                    (target_prof,),
                ).fetchall()
                if not channels_in_prof:
                    raise RuntimeError(
                        f"Profile '{target_prof}' chưa có kênh YouTube nào. Hãy thêm kênh hoặc chọn kênh cụ thể."
                    )
                if len(channels_in_prof) > 1:
                    names = ", ".join(c["name"] for c in channels_in_prof)
                    raise RuntimeError(
                        f"Profile '{target_prof}' có {len(channels_in_prof)} kênh ({names}). "
                        "Hãy chọn kênh cụ thể để tránh đăng nhầm kênh."
                    )
                return dict(channels_in_prof[0])

            if (item.get("target_type") or "").upper() == "CHANNEL":
                raise RuntimeError(
                    "Video được gán chế độ 'kênh cụ thể' nhưng không còn kênh hợp lệ (có thể kênh đã bị xóa). "
                    "Hãy chọn lại kênh trước khi bắt đầu."
                )

            group_name = (item.get("target_group_name") or "").strip() or "Mặc định"
            channels = conn.execute(
                "SELECT * FROM youtube_channels WHERE group_name = ? ORDER BY id ASC",
                (group_name,),
            ).fetchall()
            if not channels:
                raise RuntimeError(
                    f"Nhóm kênh '{group_name}' chưa có kênh nào. Hãy thêm kênh vào nhóm hoặc chọn kênh cụ thể."
                )
            # Chia vòng theo ID video (không theo thứ tự xử lý) để một video luôn nhận
            # đúng một kênh dù chạy lại hay batch đổi thứ tự.
            try:
                rotation_key = int(item.get("id") or 0)
            except (TypeError, ValueError):
                rotation_key = row_idx
            selected = channels[rotation_key % len(channels)]
            add_log(
                "Pipeline",
                f"Chia kênh theo nhóm '{group_name}': video #{item.get('id')} → '{selected['name']}' "
                f"(kênh #{selected['id']}).",
                "INFO",
                video_title=item.get("title") or "",
            )
            return dict(selected)
        finally:
            conn.close()

    # ==========================================================
    # CHẾ ĐỘ AUTO MONITOR 24/7 (Tương đương ZhScDd của DHB Reup)
    # ==========================================================
    def start_monitor(self, monitor_id: int, _auto_restart: bool = False) -> Dict[str, Any]:
        with self._monitor_lock:
            existing_worker = self._monitor_workers.get(monitor_id)
            if existing_worker is not None and existing_worker.is_alive():
                return {"ok": True, "message": f"Luồng Monitor #{monitor_id} đang chạy."}
            stop_ev = threading.Event()
            self._monitor_threads[monitor_id] = stop_ev

        conn = get_conn()
        mon_row = conn.execute("SELECT name FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if not mon_row:
            with self._monitor_lock:
                self._monitor_threads.pop(monitor_id, None)
            conn.close()
            return {"ok": False, "message": f"Không tìm thấy Monitor #{monitor_id}."}

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE monitors SET is_running = 1, status = 'Running', last_checked_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (now_str, now_str, monitor_id),
        )
        conn.commit()
        conn.close()

        if not _auto_restart:
            self._monitor_restart_history.pop(monitor_id, None)
            # Chỉ reset bộ đếm giới hạn khi NGƯỜI DÙNG chủ động bật lại monitor;
            # tự khôi phục sau crash phải giữ nguyên bộ đếm để không lách giới hạn.
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
        worker.start()
        self._start_monitor_heartbeat(monitor_id, stop_ev)
        self.start_monitor_supervisor()

        if _auto_restart:
            add_log("Monitor", f"Đã tự khôi phục luồng Auto Monitor #{monitor_id} [{mon_row['name']}].", "WARNING")
            return {"ok": True, "message": f"Đã tự khôi phục Monitor #{monitor_id}"}
        add_log("Monitor", f"Đã bật luồng Auto Monitor #{monitor_id}", "INFO")
        return {"ok": True, "message": f"Đã bật theo dõi tự động #{monitor_id}"}

    def stop_monitor(self, monitor_id: int) -> Dict[str, Any]:
        # Cập nhật DB trước khi gỡ thread để supervisor không khởi động lại nhầm
        conn = get_conn()
        conn.execute(
            "UPDATE monitors SET is_running = 0, status = 'Stopped' WHERE id = ?",
            (monitor_id,),
        )
        conn.commit()
        conn.close()

        with self._monitor_lock:
            stop_ev = self._monitor_threads.pop(monitor_id, None)
            if stop_ev is not None:
                stop_ev.set()
            self._monitor_workers.pop(monitor_id, None)
            self._monitor_heartbeats.pop(monitor_id, None)
            self._monitor_restart_history.pop(monitor_id, None)

        add_log("Monitor", f"Đã dừng luồng Auto Monitor #{monitor_id}", "WARNING")
        return {"ok": True, "message": f"Đã dừng theo dõi tự động #{monitor_id}"}

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
                "SELECT id, name, status, last_heartbeat_at FROM monitors WHERE is_running = 1 ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        now = time.time()
        for row in rows:
            monitor_id = int(row["id"])
            monitor_name = row["name"] or f"#{monitor_id}"
            worker = self._monitor_workers.get(monitor_id)
            if worker is None or not worker.is_alive():
                self._restart_dead_monitor(monitor_id, monitor_name)
                continue

            heartbeat = self._monitor_heartbeats.get(monitor_id)
            if heartbeat is None or not heartbeat.is_alive():
                self._start_monitor_heartbeat(monitor_id)
                add_log("Monitor", f"[{monitor_name}] Bộ đếm heartbeat đã dừng, đã khởi động lại.", "WARNING")

            last_hb = self._parse_monitor_ts(row["last_heartbeat_at"])
            if last_hb is None or now - last_hb > MONITOR_STALE_SECONDS:
                status = row["status"] or ""
                if not status.startswith("⚠️"):
                    stale_for = int(now - last_hb) if last_hb else 0
                    self._set_monitor_status_raw(
                        monitor_id,
                        f"⚠️ Luồng không phản hồi {stale_for}s — đang theo dõi, không tự khởi động lại để tránh đăng trùng.",
                    )
                    add_log(
                        "Monitor",
                        f"[{monitor_name}] Không có heartbeat trong {stale_for}s. Giữ nguyên luồng để tránh đăng trùng; kiểm tra log nếu kéo dài.",
                        "WARNING",
                    )

    def _restart_dead_monitor(self, monitor_id: int, monitor_name: str):
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
                self._monitor_threads.pop(monitor_id, None)
                self._monitor_workers.pop(monitor_id, None)
                self._monitor_heartbeats.pop(monitor_id, None)
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
            if stop_ev is None:
                return
            heartbeat = threading.Thread(
                target=self._monitor_heartbeat_loop,
                args=(monitor_id, stop_ev),
                daemon=True,
                name=f"DHB-Monitor-HB-{monitor_id}",
            )
            self._monitor_heartbeats[monitor_id] = heartbeat
        heartbeat.start()

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
        try:
            self._monitor_loop(monitor_id, stop_ev)
        except Exception as e:
            try:
                add_log("Monitor", f"Luồng Monitor #{monitor_id} gặp lỗi ngoài dự kiến: {e}", "ERROR")
            except Exception:
                pass
            try:
                self._set_monitor_status_raw(monitor_id, f"Luồng gặp lỗi, chờ tự phục hồi: {e}")
            except Exception:
                pass
        finally:
            stop_ev.set()
            with self._monitor_lock:
                if self._monitor_threads.get(monitor_id) is stop_ev:
                    self._monitor_threads.pop(monitor_id, None)
                    self._monitor_workers.pop(monitor_id, None)
                    self._monitor_heartbeats.pop(monitor_id, None)

    @staticmethod
    def _parse_monitor_ts(value: Any) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    def _set_monitor_status_raw(self, monitor_id: int, status_text: str):
        conn = get_conn()
        conn.execute("UPDATE monitors SET status = ? WHERE id = ?", (status_text, monitor_id))
        conn.commit()
        conn.close()

    def _record_monitor_published(self, queue_id: int, item_data: Dict[str, Any], mon: Dict[str, Any]) -> bool:
        """Ghi published_history CHỈ khi video đã Completed và có link thật. Trả về True nếu đã ghi."""
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status, published_url FROM video_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if not row:
                return False
            if (row["status"] or "").strip() != "Completed" or not (row["published_url"] or "").strip():
                return False
            conn.execute(
                "INSERT OR IGNORE INTO published_history (source_post_id, source_url, channel_id) VALUES (?, ?, ?)",
                (item_data.get("post_id"), item_data.get("source_url"), mon.get("target_channel_id")),
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
                scanned = scan_social_urls(urls, max_items_per_url=max_vids * 2, min_views=min_views)

                proj_name = f"Monitor - {mon_name}"
                new_items = []
                conn = get_conn()
                for s in scanned:
                    pid = s.get("post_id") or s.get("source_url")
                    # Đã đăng thành công trước đó trên đúng kênh đích (history chỉ ghi SAU khi upload xong — S2)
                    already = _monitor_source_already_published(conn, pid, mon.get("target_channel_id"))
                    if not already:
                        # Đã nằm trong hàng đợi của monitor này (đang chờ / từng lỗi) — không thêm lại
                        queued = conn.execute(
                            "SELECT id FROM video_queue WHERE post_id = ? AND project_name = ? LIMIT 1",
                            (pid, proj_name),
                        ).fetchone()
                        if not queued:
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
                        cur = conn.execute(
                            """
                            INSERT INTO video_queue (
                                project_name, source_type, platform, post_id, source_url, input_video_path,
                                title, description, tags, duration, view_count, like_count,
                                upload_to, visibility, target_type, target_channel_id, target_group_name,
                                option_group_id, render_mode, render_config_json, emulate_mobile, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'public', ?, ?, ?, ?, ?, ?, 0, 'Pending')
                            """,
                            (
                                proj_name,
                                item_data["source_type"],
                                item_data["platform"],
                                item_data["post_id"],
                                item_data["source_url"],
                                item_data["input_video_path"],
                                title,
                                desc,
                                tags,
                                item_data["duration"],
                                item_data["view_count"],
                                item_data["like_count"],
                                mon.get("upload_to") or "Shorts",
                                "CHANNEL" if mon.get("target_channel_id") else "GROUP",
                                mon.get("target_channel_id"),
                                mon.get("target_group_name") or "Mặc định",
                                mon.get("option_group_id"),
                                mon.get("render_mode") or "shorts_9_16",
                                mon.get("render_config_json") or "{}",
                            ),
                        )
                        queue_id = cur.lastrowid
                        conn.commit()
                        conn.close()

                        self._update_monitor_status(monitor_id, f"Đang xử lý video ({idx+1}/{len(new_items)}): {title[:30]}")
                        monitor_token = uuid.uuid4().hex
                        try:
                            self.process_single_item(
                                queue_id, idx, True, True, True,
                                claim_token=monitor_token,
                                stop_event=stop_ev,
                                upload_counter=self._monitor_upload_counts.setdefault(monitor_id, {"n": 0}),
                            )
                        finally:
                            release_item_claim(queue_id, monitor_token)

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
        conn = get_conn()
        conn.execute(
            "UPDATE monitors SET status = ?, last_checked_at = ? WHERE id = ?",
            (status_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), monitor_id),
        )
        conn.commit()
        conn.close()


pipeline_manager = PipelineManager()