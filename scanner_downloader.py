import hashlib
import base64
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Any, Optional

from config import (
    APP_TEMP_DIR,
    FFMPEG_EXE,
    FFPROBE_EXE,
    YTDL_EXE,
)
from database import get_settings, add_log

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}

# Giới hạn số ffprobe chạy song song để không nghẽn đĩa/CPU khi quét thư mục lớn
PROBE_MAX_WORKERS = max(1, min(8, os.cpu_count() or 4))
DOWNLOAD_TIMEOUT_SEC = 30 * 60
SCAN_TIMEOUT_SEC = 5 * 60
MAX_SCAN_ITEMS = 1000


class ScanSourceError(RuntimeError):
    """A source could not be scanned; do not insert an incomplete selection."""


def _scan_remote_source(url, limit, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError('Đã dừng quét nguồn.')
    started = time.monotonic()
    # Job files keep signed source URLs out of the OS process command line.
    with tempfile.TemporaryDirectory(prefix='scan-', dir=APP_TEMP_DIR) as folder:
        request = Path(folder) / 'request.json'
        request.write_text(json.dumps({'url':url,'limit':limit}),encoding='utf-8')
        result_path = request.with_name('result.json')
        proc = subprocess.Popen(
            [sys.executable,str(Path(__file__).with_name('scan_worker.py')),str(request)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            shell=False,creationflags=subprocess.CREATE_NO_WINDOW | getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
            if os.name == 'nt' else 0,start_new_session=os.name != 'nt',
        )
        try:
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError('Đã dừng quét nguồn.')
                if time.monotonic()-started >= SCAN_TIMEOUT_SEC:
                    raise TimeoutError(f'Quét một nguồn vượt quá {SCAN_TIMEOUT_SEC} giây; đã dừng tiến trình quét.')
                if stop_event is not None:
                    stop_event.wait(.1)
                else:
                    time.sleep(.1)
            proc.wait(timeout=5)
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError('Đã dừng quét nguồn.')
            if time.monotonic()-started >= SCAN_TIMEOUT_SEC:
                raise TimeoutError(f'Quét một nguồn vượt quá {SCAN_TIMEOUT_SEC} giây; đã dừng tiến trình quét.')
            if not result_path.is_file() or result_path.stat().st_size > 32*1024*1024:
                raise ScanSourceError('Tiến trình quét không trả kết quả hợp lệ.')
            try:
                result = json.loads(result_path.read_text(encoding='utf-8'))
            except (ValueError, OSError) as exc:
                raise ScanSourceError('Không đọc được kết quả quét nguồn.') from exc
            if proc.returncode != 0 or not isinstance(result,dict) or result.get('ok') is not True:
                raise ScanSourceError('Không lấy được metadata nguồn. Kiểm tra đường dẫn hoặc quyền truy cập rồi thử lại.')
            entries = result.get('entries')
            if not isinstance(entries,list) or len(entries)>limit or any(not isinstance(entry,dict) for entry in entries):
                raise ScanSourceError('Kết quả quét nguồn sai định dạng.')
            return entries
        finally:
            if proc.poll() is None:
                _terminate_download_process(proc)
            if proc.poll() is None:
                raise ScanSourceError('Chưa xác nhận được tiến trình quét đã dừng.')


class VideoProbeError(RuntimeError):
    """Probe không thể chứng minh file là video hợp lệ."""


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "TikTok"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "Douyin"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "Facebook"
    if "instagram.com" in u:
        return "Instagram"
    if "threads.net" in u:
        return "Threads"
    if "twitter.com" in u or "x.com" in u:
        return "Twitter/X"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "Xiaohongshu"
    if "kuaishou.com" in u:
        return "Kuaishou"
    return "Web/Social"


def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "00:00:00"
    sec = round(seconds)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def probe_video_info(file_path: str) -> Dict[str, Any]:
    """Đọc thời lượng, độ phân giải của file video bằng ffprobe.exe."""
    path = str(file_path or "").strip()
    label = os.path.basename(path) or "(không có đường dẫn)"
    if not path or not os.path.isfile(path):
        raise VideoProbeError(f"Không tìm thấy file video để probe: {label}")
    try:
        cmd = [
            FFPROBE_EXE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15,
            shell=False,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProbeError(f"ffprobe quá thời hạn 15 giây khi đọc '{label}'.") from exc
    except OSError as exc:
        raise VideoProbeError(f"Không chạy được ffprobe cho '{label}': {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-500:]
        raise VideoProbeError(
            f"ffprobe thất bại cho '{label}' (mã {proc.returncode})"
            + (f": {detail}" if detail else ".")
        )
    try:
        data = json.loads(proc.stdout or "")
    except (TypeError, ValueError) as exc:
        raise VideoProbeError(f"ffprobe trả JSON không hợp lệ cho '{label}'.") from exc

    streams = data.get("streams")
    streams = streams if isinstance(streams, list) else []
    video_streams = [st for st in streams if isinstance(st, dict) and st.get("codec_type") == "video"]
    if not video_streams:
        raise VideoProbeError(f"File '{label}' không có video stream.")
    video = video_streams[0]
    try:
        width = int(video.get("width", 0) or 0)
        height = int(video.get("height", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise VideoProbeError(f"ffprobe trả kích thước video không hợp lệ cho '{label}'.") from exc
    if width <= 0 or height <= 0:
        raise VideoProbeError(f"Video stream trong '{label}' thiếu kích thước hợp lệ.")

    raw_duration = (data.get("format") or {}).get("duration")
    if raw_duration in (None, ""):
        raw_duration = video.get("duration")
    try:
        seconds = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise VideoProbeError(f"Không xác định được thời lượng video trong '{label}'.") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise VideoProbeError(f"Thời lượng video trong '{label}' không hợp lệ ({raw_duration!r}).")

    return {
        "duration": format_duration(seconds),
        "seconds": seconds,
        "width": width,
        "height": height,
        "has_audio": any(isinstance(st, dict) and st.get("codec_type") == "audio" for st in streams),
        "has_video": True,
    }


def probe_video_infos(file_paths: List[str], max_workers: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """Probe nhiều file song song; trả về map đường dẫn -> thông tin, giữ nguyên kết quả theo từng file."""
    paths: List[str] = []
    seen = set()
    for raw in file_paths or []:
        if not raw:
            continue
        path = str(raw)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    if not paths:
        return {}

    workers = max(1, int(max_workers or PROBE_MAX_WORKERS))
    workers = min(workers, len(paths))
    if workers <= 1:
        return {path: probe_video_info(path) for path in paths}

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ffprobe") as pool:
        return dict(zip(paths, pool.map(probe_video_info, paths)))


def scan_local_folder(
    folder_path: str,
    max_items: int = 100,
    sort_by: str = "name",
    recursive: bool = False,
) -> List[Dict[str, Any]]:
    """Quét danh sách file video từ một thư mục trên máy tính (recursive=True để quét cả thư mục con)."""
    p = Path(folder_path)
    if not p.exists() or not p.is_dir():
        return []

    if recursive:
        found = []
        for root, _subdirs, files in os.walk(p):
            for fn in files:
                fp = Path(root) / fn
                if fp.suffix.lower() in VIDEO_EXTENSIONS:
                    found.append(fp)
    else:
        found = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]

    if sort_by == "time":
        files_sorted = sorted(found, key=lambda x: x.stat().st_mtime, reverse=True)
    else:
        files_sorted = sorted(found, key=lambda x: x.name.lower())

    selected = files_sorted[:max_items]
    probe_map = probe_video_infos([str(f) for f in selected])

    results = []
    for f in selected:
        probe = probe_map[str(f)]
        post_id = hashlib.md5(str(f).encode("utf-8")).hexdigest()[:16].upper()
        is_short = probe["seconds"] <= 65 or (probe["height"] > probe["width"] and probe["seconds"] <= 180)
        results.append({
            "source_type": "File",
            "platform": "LocalFile",
            "post_id": post_id,
            "source_url": str(f),
            "input_video_path": str(f),
            "title": f.stem,
            "description": f.stem,
            "tags": "#shorts #trending",
            "duration": probe["duration"],
            "view_count": 0,
            "like_count": 0,
            "upload_to": "Shorts" if is_short else "Video",
        })
    return results


def scan_social_urls(
    urls: List[str],
    max_items_per_url: int = 20,
    min_views: int = 0,
    min_likes: int = 0,
    min_comments: int = 0,
    only_shorts: bool = False,
    recursive_local: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> List[Dict[str, Any]]:
    """
    Quét danh sách video từ link kênh hoặc link video lẻ (TikTok, Douyin, YouTube, FB, IG, Twitter...).
    Làn Monitor có thể dừng ngay cả khi extractor đang chờ mạng.
    Mỗi URL mạng có deadline riêng và worker riêng; không gửi video.
    """
    if not 1 <= max_items_per_url <= MAX_SCAN_ITEMS:
        raise ValueError(f'Số video mỗi nguồn phải từ 1 đến {MAX_SCAN_ITEMS}.')
    results: List[Dict[str, Any]] = []
    for raw_url in urls:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError('Đã dừng quét nguồn.')
        url = raw_url.strip()
        if not url:
            continue
        # Nếu người dùng nhập đường dẫn thư mục hoặc file nội bộ
        if os.path.exists(url):
            if os.path.isdir(url):
                results.extend(scan_local_folder(url, max_items=max_items_per_url, recursive=recursive_local))
            elif os.path.isfile(url):
                probe = probe_video_info(url)
                f = Path(url)
                results.append({
                    "source_type": "File",
                    "platform": "LocalFile",
                    "post_id": hashlib.md5(url.encode("utf-8")).hexdigest()[:16].upper(),
                    "source_url": url,
                    "input_video_path": url,
                    "title": f.stem,
                    "description": f.stem,
                    "tags": "#shorts",
                    "duration": probe["duration"],
                    "view_count": 0,
                    "like_count": 0,
                    "upload_to": "Shorts" if probe["seconds"] <= 65 else "Video",
                })
            continue

        platform = detect_platform(url)
        try:
            entries = _scan_remote_source(url, max_items_per_url, stop_event)
            for entry in entries:
                if not entry:
                    continue
                vid_id = str(entry.get("id") or hashlib.md5(url.encode()).hexdigest()[:12])
                vid_url = entry.get("webpage_url") or entry.get("url") or url
                if vid_url and not vid_url.startswith("http"):
                    if platform == "YouTube":
                        vid_url = f"https://www.youtube.com/watch?v={vid_id}"
                    elif platform == "TikTok":
                        vid_url = url

                views = int(entry.get("view_count") or 0)
                likes = int(entry.get("like_count") or 0)
                dur_sec = float(entry.get("duration") or 0)
                comments = entry.get("comment_count")

                if min_views > 0 and views < min_views:
                    continue
                if min_likes > 0 and likes < min_likes:
                    continue
                # Lọc theo bình luận (học IsCmt của filter.json DHB) — chỉ lọc khi yt-dlp trả được số liệu
                if min_comments > 0 and comments is not None and int(comments or 0) < min_comments:
                    continue
                if only_shorts and dur_sec > 90:
                    continue

                title = (entry.get("title") or entry.get("description") or f"{platform} Video {vid_id}").strip()
                desc = (entry.get("description") or title).strip()
                tags_list = entry.get("tags") or []
                tags_str = ", ".join(tags_list[:15]) if isinstance(tags_list, list) else ""

                results.append({
                    "source_type": "URL",
                    "platform": platform,
                    "post_id": vid_id,
                    "source_url": vid_url,
                    "input_video_path": "",
                    "title": title[:100],
                    "description": desc,
                    "tags": tags_str,
                    "duration": format_duration(dur_sec),
                    "view_count": views,
                    "like_count": likes,
                    "upload_to": "Shorts" if (dur_sec <= 65 or "/shorts/" in vid_url or platform in ("TikTok", "Douyin")) else "Video",
                })
        except InterruptedError:
            raise
        except (TimeoutError, ScanSourceError):
            add_log('Scanner', f'Không hoàn tất quét nguồn {platform}; không thêm hàng đợi từ lần quét này.', 'ERROR')
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise ScanSourceError('Metadata nguồn có giá trị không hợp lệ.') from exc

    return results


def _height_cap(res_str: str) -> int:
    """Đọc chuỗi cài đặt độ phân giải (VD: '1080p FullHD', '720p', '4K 2160p') thành giới hạn chiều cao."""
    s = (res_str or "").lower()
    if "2160" in s or "4k" in s:
        return 2160
    if "1440" in s or "2k" in s:
        return 1440
    if "720" in s:
        return 720
    if "480" in s:
        return 480
    return 1080


def _run_yt_dlp_worker(payload: Dict[str, Any], progress_callback, stop_event, started_at: float) -> Dict[str, Any]:
    """Chạy yt-dlp trong process riêng để Dừng/timeout giết được FFmpeg ghép luồng bên dưới."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=True).encode("utf-8")).decode("ascii")
    worker_script = str(Path(__file__).with_name("download_worker.py"))
    proc = subprocess.Popen(
        [sys.executable, worker_script, encoded],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt" else 0,
        start_new_session=(os.name != "nt"),
    )
    lines: queue.Queue = queue.Queue(maxsize=20)
    reader_done = threading.Event()

    def _read_output():
        try:
            if proc.stdout:
                for line in proc.stdout:
                    try:
                        lines.put_nowait(line)
                    except queue.Full:
                        try:
                            lines.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            lines.put_nowait(line)
                        except queue.Full:
                            pass
        finally:
            reader_done.set()

    reader = threading.Thread(target=_read_output, daemon=True, name="DHB-Downloader-Output")
    result = None
    worker_error = None
    reader.start()
    try:
        while proc.poll() is None or not reader_done.is_set():
            if stop_event is not None and stop_event.is_set():
                _terminate_download_process(proc)
                raise InterruptedError("Người dùng đã dừng tải video.")
            if time.monotonic() - started_at > DOWNLOAD_TIMEOUT_SEC:
                _terminate_download_process(proc)
                raise TimeoutError(f"Tải video vượt quá {DOWNLOAD_TIMEOUT_SEC // 60} phút; đã dừng cây tiến trình.")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if message.get("status") == "progress" and progress_callback:
                progress_callback(int(message.get("percent") or 0), str(message.get("message") or "Đang tải..."))
            elif message.get("status") == "done":
                result = message
            elif message.get("status") == "error":
                worker_error = f"{message.get('error_type') or 'DownloadError'}: {message.get('error') or 'yt-dlp thất bại.'}"
        proc.wait(timeout=5)
        if proc.returncode != 0 or not result:
            detail = worker_error or f"yt-dlp worker thoát với mã {proc.returncode} mà không trả file."
            raise RuntimeError(detail)
        return result
    except BaseException:
        if proc.poll() is None:
            _terminate_download_process(proc)
        raise
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        reader.join(timeout=1.0)


def _download_ffmpeg_location() -> Optional[str]:
    """Leave normal PATH discovery to yt-dlp; validate explicit overrides once."""
    configured = str(FFMPEG_EXE or '').strip()
    if configured.casefold() in ('ffmpeg', 'ffmpeg.exe'):
        return None
    executable = Path(configured).expanduser()
    if not executable.is_absolute() and not any(separator in configured for separator in ('/', '\\')):
        found = shutil.which(configured) if configured else None
        if found:
            executable = Path(found)
        else:
            raise FileNotFoundError('Không tìm thấy executable FFmpeg đã cấu hình trong PATH.')
    if not executable.is_file():
        raise FileNotFoundError('Executable FFmpeg đã cấu hình không tồn tại.')
    return str(executable.resolve())


def download_video(
    source_url: str,
    post_id: str = "",
    progress_callback: Optional[Callable[[int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    owner_id: Optional[int] = None,
) -> Dict[str, str]:
    """
    Tải video và thumbnail về thư mục temp (kết hợp yt-dlp và youtube-dl.exe của DHB).
    stop_event: cho phép làn batch/monitor ngắt giữa chừng khi người dùng bấm Dừng
    (yt-dlp không có timeout dựng sẵn — cơ chế ngắt là raise trong progress hook).
    Trả về dict: {"video_path": ..., "thumbnail_path": ..., "title": ..., "duration": ...}
    """
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("Đã dừng trước khi bắt đầu tải video.")

    if os.path.exists(source_url) and os.path.isfile(source_url):
        if progress_callback:
            progress_callback(100, "Đã có sẵn file trên máy")
        probe = probe_video_info(source_url)
        return {
            "video_path": source_url,
            "thumbnail_path": "",
            "title": Path(source_url).stem,
            "duration": probe["duration"],
        }

    from storage_manager import MIN_DOWNLOAD_HEADROOM_BYTES, ensure_disk_space
    ensure_disk_space(APP_TEMP_DIR, MIN_DOWNLOAD_HEADROOM_BYTES, "tải video")

    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", post_id) or hashlib.md5(source_url.encode("utf-8")).hexdigest()[:12]
    job_prefix = f"dl_{uuid.uuid4().hex}"
    out_template = str(APP_TEMP_DIR / f"{job_prefix}.%(ext)s")

    def _owned_files():
        root = APP_TEMP_DIR.resolve()
        for candidate in APP_TEMP_DIR.glob(f"{job_prefix}.*"):
            try:
                if candidate.parent.resolve() == root and candidate.is_file():
                    yield candidate
            except OSError:
                continue

    def _cleanup_owned_files():
        for candidate in list(_owned_files()):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_owned_partials():
        for candidate in list(_owned_files()):
            if candidate.name.casefold().endswith((".part", ".ytdl", ".temp")):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def _register_download_artifacts(video_path: str, thumbnail_path: str):
        if owner_id is None:
            return
        from database import register_managed_artifact
        try:
            register_managed_artifact(video_path, "download", "queue", int(owner_id))
            if thumbnail_path:
                register_managed_artifact(thumbnail_path, "download_thumbnail", "queue", int(owner_id))
        except Exception as exc:
            add_log("Storage", f"Không ghi được manifest cho file tải của queue #{owner_id}; file được giữ lại ({type(exc).__name__}).", "WARNING")

    # Độ phân giải tải theo nền tảng (học download_Resolution_TikTok của DHB): TikTok/Douyin mặc định 720p
    u = (source_url or "").lower()
    if "tiktok.com" in u or "douyin.com" in u or "iesdouyin" in u:
        height_cap = _height_cap(str(get_settings().get("download_resolution_tiktok", "720p") or "720p"))
    else:
        height_cap = _height_cap(str(get_settings().get("download_resolution", "1080p FullHD") or "1080p"))

    started_at = time.monotonic()
    cookies_file = str(get_settings().get("download_cookies_file") or "").strip()
    if cookies_file:
        cookie_path = Path(cookies_file).expanduser()
        if not cookie_path.is_file():
            raise FileNotFoundError("File cookies Netscape cấu hình cho yt-dlp không tồn tại.")
        cookies_file = str(cookie_path.resolve())

    ffmpeg_location = _download_ffmpeg_location()
    worker_payload = {
        "source_url": source_url,
        "out_template": out_template,
        "format": f"bestvideo[height<={height_cap}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height_cap}][ext=mp4]/best",
        "cookies_file": cookies_file,
        "timeout_sec": DOWNLOAD_TIMEOUT_SEC,
    }
    if ffmpeg_location is not None:
        worker_payload['ffmpeg_location'] = ffmpeg_location

    try:
        result = _run_yt_dlp_worker(
            worker_payload,
            progress_callback,
            stop_event,
            started_at,
        )
        downloaded = Path(str(result.get("video_path") or ""))
        owned = {os.path.normcase(str(candidate.resolve(strict=True))) for candidate in _owned_files()}
        downloaded_canonical = os.path.normcase(str(downloaded.resolve(strict=True)))
        if downloaded_canonical not in owned or downloaded.suffix.lower() not in VIDEO_EXTENSIONS:
            raise RuntimeError("yt-dlp trả đường dẫn file không thuộc artifact của job.")
        downloaded_file = str(downloaded)
        if not os.path.isfile(downloaded_file) or os.path.getsize(downloaded_file) <= 0:
            raise RuntimeError("yt-dlp không tạo được file video hoàn chỉnh.")

        thumb_path = ""
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            t_cand = downloaded.with_suffix(ext)
            if t_cand.is_file():
                thumb_path = str(t_cand)
                break

        probe = probe_video_info(downloaded_file)
        _cleanup_owned_partials()
        _register_download_artifacts(downloaded_file, thumb_path)
        if progress_callback:
            progress_callback(100, "Tải hoàn tất")
        return {
            "video_path": downloaded_file,
            "thumbnail_path": thumb_path,
            "title": result.get("title") or safe_id,
            "duration": probe["duration"],
        }
    except InterruptedError:
        # Dừng theo yêu cầu người dùng — KHÔNG được rơi xuống nhánh fallback youtube-dl.
        _cleanup_owned_files()
        raise
    except Exception as first_err:
        if stop_event is not None and stop_event.is_set():
            # Lỗi do người dùng bấm Dừng trong lúc tải (hook raise) — coi như ngắt, không fallback.
            _cleanup_owned_files()
            raise InterruptedError("Đã dừng trong khi tải video.") from first_err
        # Dọn riêng phần dở của lượt yt-dlp trước khi thử fallback.
        _cleanup_owned_files()
        # Fallback sang youtube-dl.exe tại C:\DHB Reup Facebook\youtube-dl.exe
        fallback_template = str(APP_TEMP_DIR / f"{job_prefix}.%(ext)s")
        cmd = [
            YTDL_EXE,
            "-f",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "2",
            "-o",
            fallback_template,
            source_url,
        ]
        if ffmpeg_location is not None:
            cmd[1:1] = ['--ffmpeg-location', ffmpeg_location]
        if cookies_file:
            cmd[-1:-1] = ["--cookies", str(Path(cookies_file).expanduser().resolve())]
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Đã dừng trong khi tải video.")
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt" else 0,
                start_new_session=(os.name != "nt"),
            )
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    _terminate_download_process(proc)
                    raise InterruptedError("Người dùng đã dừng tải bằng youtube-dl.")
                if time.monotonic() - started_at > DOWNLOAD_TIMEOUT_SEC:
                    _terminate_download_process(proc)
                    raise TimeoutError("youtube-dl.exe vượt quá 30 phút; đã dừng tiến trình tải.")
                time.sleep(0.25)
            if proc.returncode == 0:
                candidates = sorted(
                    (p for p in _owned_files() if p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.endswith(".part")),
                    key=lambda p: (p.suffix.lower() != ".mp4", p.name.casefold()),
                )
                for candidate in candidates:
                    if candidate.stat().st_size <= 0:
                        continue
                    probe = probe_video_info(str(candidate))
                    _cleanup_owned_partials()
                    _register_download_artifacts(str(candidate), "")
                    if progress_callback:
                        progress_callback(100, "Tải hoàn tất (DHB youtube-dl)")
                    return {
                        "video_path": str(candidate),
                        "thumbnail_path": "",
                        "title": safe_id,
                        "duration": probe["duration"],
                    }
            raise RuntimeError(f"youtube-dl.exe kết thúc với mã {proc.returncode} hoặc không tạo video hợp lệ.")
        except InterruptedError:
            _cleanup_owned_files()
            raise
        except Exception as fallback_err:
            _cleanup_owned_files()
            raise RuntimeError(f"Không thể tải video: yt-dlp: {first_err}; fallback: {fallback_err}") from fallback_err
        except BaseException:
            _cleanup_owned_files()
            raise
        finally:
            if proc is not None and proc.poll() is None:
                _terminate_download_process(proc)


def _terminate_download_process(proc: subprocess.Popen, timeout: float = 5.0):
    """Dừng và reap youtube-dl cùng tiến trình con của job."""
    if proc.poll() is None and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                shell=False,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    if proc.poll() is None and os.name != "nt":
        try:
            os.killpg(proc.pid, 9)
        except Exception:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass
