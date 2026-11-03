import hashlib
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Any, Optional
import yt_dlp

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
    sec = int(round(seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def probe_video_info(file_path: str) -> Dict[str, Any]:
    """Đọc thời lượng, độ phân giải của file video bằng ffprobe.exe."""
    info = {"duration": "00:00:00", "seconds": 0.0, "width": 0, "height": 0, "has_audio": False}
    if not file_path or not os.path.exists(file_path):
        # Mặc định giả định có tiếng để không làm thay đổi hành vi render khi chưa probe được
        info["has_audio"] = True
        return info
    try:
        cmd = [
            FFPROBE_EXE,
            "-v",
            "quiet",
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
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = json.loads(proc.stdout or "{}")
        dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
        info["seconds"] = dur
        info["duration"] = format_duration(dur)
        for st in data.get("streams", []):
            if st.get("codec_type") == "video":
                info["width"] = int(st.get("width", 0) or 0)
                info["height"] = int(st.get("height", 0) or 0)
            elif st.get("codec_type") == "audio":
                info["has_audio"] = True
    except Exception:
        pass
    return info


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
    stop_event: khi được truyền (làn Monitor), kiểm tra giữa các URL để bấm Dừng có hiệu lực
    ngay thay vì quét hết toàn bộ nguồn mới thoát.
    """
    results: List[Dict[str, Any]] = []
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": max_items_per_url,
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for raw_url in urls:
            if stop_event is not None and stop_event.is_set():
                break
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
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                entries = info.get("entries") if "entries" in info else [info]
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
            except Exception as e:
                add_log("Scanner", f"Lỗi quét link {url}: {e}", "ERROR")

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


def download_video(
    source_url: str,
    post_id: str = "",
    progress_callback: Optional[Callable[[int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
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

    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", post_id) or hashlib.md5(source_url.encode("utf-8")).hexdigest()[:12]
    out_template = str(APP_TEMP_DIR / f"dl_{safe_id}.%(ext)s")

    # Độ phân giải tải theo nền tảng (học download_Resolution_TikTok của DHB): TikTok/Douyin mặc định 720p
    u = (source_url or "").lower()
    if "tiktok.com" in u or "douyin.com" in u or "iesdouyin" in u:
        height_cap = _height_cap(str(get_settings().get("download_resolution_tiktok", "720p") or "720p"))
    else:
        height_cap = _height_cap(str(get_settings().get("download_resolution", "1080p FullHD") or "1080p"))

    def _hook(d):
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Người dùng đã bấm Dừng khi đang tải video.")
        if d.get("status") == "downloading" and progress_callback:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                pct = min(99, int(downloaded * 100 / total))
                progress_callback(pct, f"Đang tải... {pct}%")
        elif d.get("status") == "finished" and progress_callback:
            progress_callback(99, "Đang ghép luồng video/audio...")

    ydl_opts = {
        "outtmpl": out_template,
        "format": f"bestvideo[height<={height_cap}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height_cap}][ext=mp4]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": str(Path(FFMPEG_EXE).parent),
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=True)
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Đã dừng ngay sau khi tải xong luồng video.")
            downloaded_file = ydl.prepare_filename(info)
            mp4_candidate = Path(downloaded_file).with_suffix(".mp4")
            if mp4_candidate.exists():
                downloaded_file = str(mp4_candidate)

            # Tìm thumbnail nếu có
            thumb_path = ""
            for ext in (".jpg", ".webp", ".png"):
                t_cand = Path(downloaded_file).with_suffix(ext)
                if t_cand.exists():
                    thumb_path = str(t_cand)
                    break

            probe = probe_video_info(downloaded_file)
            if progress_callback:
                progress_callback(100, "Tải hoàn tất")
            return {
                "video_path": downloaded_file,
                "thumbnail_path": thumb_path,
                "title": info.get("title") or safe_id,
                "duration": probe["duration"],
            }
    except InterruptedError:
        # Dừng theo yêu cầu người dùng — KHÔNG được rơi xuống nhánh fallback youtube-dl.
        raise
    except Exception as first_err:
        if stop_event is not None and stop_event.is_set():
            # Lỗi do người dùng bấm Dừng trong lúc tải (hook raise) — coi như ngắt, không fallback.
            raise InterruptedError("Đã dừng trong khi tải video.") from first_err
        # Fallback sang youtube-dl.exe tại C:\DHB Reup Facebook\youtube-dl.exe
        out_mp4 = str(APP_TEMP_DIR / f"dl_{safe_id}.mp4")
        cmd = [
            YTDL_EXE,
            "--ffmpeg-location",
            str(Path(FFMPEG_EXE).parent),
            "-f",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "-o",
            out_mp4,
            source_url,
        ]
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Đã dừng trong khi tải video.")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                # youtube-dl.exe cũ không có cơ chế ngắt — chặn trên bằng timeout cứng.
                timeout=1800,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as timeout_err:
            raise RuntimeError(
                f"youtube-dl.exe vượt quá 30 phút không xong — đã hủy tải từ {source_url}."
            ) from timeout_err
        if proc.returncode == 0 and os.path.exists(out_mp4):
            probe = probe_video_info(out_mp4)
            if progress_callback:
                progress_callback(100, "Tải hoàn tất (DHB youtube-dl)")
            return {
                "video_path": out_mp4,
                "thumbnail_path": "",
                "title": safe_id,
                "duration": probe["duration"],
            }
        raise RuntimeError(f"Không thể tải video từ {source_url}: {first_err}")
