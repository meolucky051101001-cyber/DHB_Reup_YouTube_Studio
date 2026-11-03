"""Shared validation and group-selection rules for queue preparation."""

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from content_identity import normalize_source_url


GROUP_SCHEMA_VERSION = 1
VALID_VISIBILITY = {"public", "unlisted", "private", "schedule"}
VALID_UPLOAD_TO = {"Shorts", "Video"}
VALID_RENDER_MODES = {
    "none", "md5_cut", "shorts_9_16", "pip_thu_nho", "full_16_9",
    "square_1_1", "audio_effect", "dhb_audio_master", "custom_ffmpeg",
}
VALID_RESOLUTIONS = {"1080p", "2k", "4k", "original"}


def validate_source_url(value: Any) -> str:
    """Return a canonical HTTP(S) URL or reject the source before it reaches the queue."""
    normalized = normalize_source_url(value)
    if not normalized:
        raise ValueError("URL nguồn phải là địa chỉ HTTP/HTTPS hợp lệ.")
    return normalized


def validate_metadata_fields(values: Dict[str, Any], *, require_title: bool = False) -> Dict[str, str]:
    """Normalize title/description/tags consistently for CSV, Excel and queue edits."""
    title = str(values.get("title") or "").strip()
    description = str(values.get("description") or "").strip()
    tags = str(values.get("tags") or "").strip()
    if require_title and not title:
        raise ValueError("Thiếu tiêu đề video.")
    if len(title) > 100:
        raise ValueError("Tiêu đề không được vượt quá 100 ký tự.")
    if len(description) > 5000:
        raise ValueError("Mô tả không được vượt quá 5000 ký tự.")
    return {"title": title, "description": description, "tags": tags}


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "có", "co"}:
            return True
        if text in {"0", "false", "no", "off", "không", "khong", ""}:
            return False
    raise ValueError("Cờ bật/tắt chỉ nhận 0/1, true/false, Có/Không.")


def normalize_group_names(value: Any, legacy_name: Any = "", default: Optional[str] = None) -> List[str]:
    """Parse a versioned JSON list or legacy single/delimited group string."""
    raw: Iterable[Any]
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        text = str(value or "").strip()
        parsed = None
        if text.startswith("["):
            try:
                candidate = json.loads(text)
                if isinstance(candidate, list):
                    parsed = candidate
            except (TypeError, ValueError):
                parsed = None
        if parsed is not None:
            raw = parsed
        elif text:
            # Legacy single names stay intact; multi-group text uses explicit separators.
            separator = "|" if "|" in text else (";" if ";" in text else ("\n" if "\n" in text else None))
            raw = text.split(separator) if separator else [text]
        elif legacy_name:
            raw = [legacy_name]
        elif default:
            raw = [default]
        else:
            raw = []

    result: List[str] = []
    seen = set()
    for item in raw:
        name = str(item or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        if len(name) > 80 or any(ch in name for ch in "\r\n\0"):
            raise ValueError("Tên nhóm phải dài tối đa 80 ký tự và không chứa xuống dòng.")
        seen.add(key)
        result.append(name)
    if default and not result:
        result.append(default)
    return result


def serialize_group_names(names: Any, legacy_name: Any = "") -> str:
    return json.dumps(
        normalize_group_names(names, legacy_name=legacy_name),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_schedule_time(value: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Thời gian đặt lịch không đúng định dạng ISO hoặc ngày/giờ hợp lệ.") from exc
    if parsed.tzinfo is not None:
        local_tz = (now or datetime.now().astimezone()).astimezone().tzinfo
        parsed = parsed.astimezone(local_tz).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def validate_queue_fields(values: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Validate final, already-merged queue values and return normalized publish fields."""
    now = now or datetime.now()
    visibility = str(values.get("visibility") or "public").strip().lower()
    if visibility == "scheduled":
        visibility = "schedule"
    if visibility not in VALID_VISIBILITY:
        raise ValueError("Chế độ đăng chỉ nhận public, unlisted, private hoặc schedule.")

    upload_to = str(values.get("upload_to") or "Shorts").strip()
    if upload_to not in VALID_UPLOAD_TO:
        raise ValueError("Định dạng đăng chỉ nhận Shorts hoặc Video.")
    render_mode = str(values.get("render_mode") or "shorts_9_16").strip()
    if render_mode not in VALID_RENDER_MODES:
        raise ValueError(f"Chế độ render '{render_mode}' không hợp lệ.")
    resolution = str(values.get("resolution") or "1080p").strip().lower()
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(f"Độ phân giải '{resolution}' không hợp lệ.")

    schedule = parse_schedule_time(values.get("schedule_time"), now=now)
    premiere = _truthy(values.get("is_premiere"))
    made_for_kids = _truthy(values.get("made_for_kids"))
    age_restriction = _truthy(values.get("age_restriction"))
    altered_content = _truthy(values.get("altered_content"))
    if visibility == "schedule":
        if schedule is None:
            raise ValueError("Chế độ Lên lịch bắt buộc có ngày giờ đăng.")
        if schedule <= now.replace(tzinfo=None):
            raise ValueError("Thời gian đặt lịch phải ở tương lai theo giờ địa phương.")
    elif schedule is not None:
        raise ValueError("Chế độ đăng ngay không được kèm thời gian đặt lịch.")
    if premiere and visibility != "schedule":
        raise ValueError("Premiere chỉ dùng được khi video ở chế độ Lên lịch.")
    if made_for_kids and age_restriction:
        raise ValueError("Không thể đồng thời chọn Dành cho trẻ em và Giới hạn 18+.")

    return {
        "visibility": visibility,
        "schedule_time": schedule.strftime("%Y-%m-%d %H:%M:%S") if schedule else "",
        "is_premiere": int(premiere),
        "made_for_kids": int(made_for_kids),
        "age_restriction": int(age_restriction),
        "altered_content": int(altered_content),
        "upload_to": upload_to,
        "render_mode": render_mode,
        "resolution": resolution,
    }
