"""Isolated yt-dlp worker so the parent can stop its process tree reliably."""

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import yt_dlp


def _emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _safe_error(value, source_url):
    message = str(value or "")
    if source_url:
        message = message.replace(source_url, "[nguồn video]")
    message = re.sub(r"https?://[^\s?]+\?[^\s]+", "[URL đã ẩn query]", message)
    return message[:600]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    payload = json.loads(base64.urlsafe_b64decode(sys.argv[1].encode("ascii")).decode("utf-8"))
    source_url = str(payload["source_url"])
    started_at = time.monotonic()
    timeout_sec = max(60, int(payload.get("timeout_sec") or 1800))

    def progress_hook(data):
        if time.monotonic() - started_at > timeout_sec:
            raise TimeoutError("Tải video vượt quá thời hạn tổng.")
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = min(99, int(downloaded * 100 / total)) if total else 0
            _emit({"status": "progress", "percent": percent, "message": f"Đang tải... {percent}%"})
        elif status == "finished":
            _emit({"status": "progress", "percent": 99, "message": "Đang ghép luồng video/audio..."})

    options = {
        "outtmpl": str(payload["out_template"]),
        "format": str(payload["format"]),
        "merge_output_format": "mp4",
        "ffmpeg_location": str(payload["ffmpeg_location"]),
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
    }
    if payload.get("cookies_file"):
        options["cookiefile"] = str(payload["cookies_file"])

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=True)
            if not info:
                raise RuntimeError("Không nhận được thông tin video từ yt-dlp.")
            output = Path(ydl.prepare_filename(info))
            mp4_candidate = output.with_suffix(".mp4")
            if mp4_candidate.is_file():
                output = mp4_candidate
        _emit({"status": "done", "video_path": str(output), "title": str(info.get("title") or "")[:300]})
        return 0
    except BaseException as exc:
        _emit({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": _safe_error(exc, source_url),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
