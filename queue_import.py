"""CSV queue import preview and row validation."""

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from phase4_rules import normalize_group_names, validate_metadata_fields, validate_queue_fields, validate_source_url


_ALIASES = {
    "source_url": {"sourceurl", "url", "source", "nguon", "linknguon"},
    "title": {"title", "tieude", "tenvideo"},
    "description": {"description", "mota"},
    "tags": {"tags", "tag", "tukhoa"},
    "upload_to": {"uploadto", "dangkieu", "loaivideo"},
    "render_mode": {"rendermode", "chedorender", "cheduxuly"},
    "resolution": {"resolution", "dophangiai", "chatluong"},
    "visibility": {"visibility", "chedodang", "privacy", "riengtu"},
    "schedule_time": {"scheduletime", "datlich", "thoigiandang", "giohen"},
    "target_groups": {"targetgroups", "targetgroupnames", "groups", "nhomkenh", "nhomdich"},
    "made_for_kids": {"madeforkids", "danhchotreem", "treem", "mfk"},
    "age_restriction": {"agerestriction", "gioihan18", "gioihandoituoi"},
    "altered_content": {"alteredcontent", "noidungai", "nhanai"},
    "is_premiere": {"ispremiere", "premiere", "congchieu"},
}
_BOOL_TRUE = {"1", "true", "yes", "y", "on", "co", "có", "bat", "bật"}
_BOOL_FALSE = {"0", "false", "no", "n", "off", "khong", "không", "tat", "tắt"}


def _header_key(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def _bool_value(value: Any, default: int = 0) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in _BOOL_TRUE:
        return 1
    if text in _BOOL_FALSE:
        return 0
    raise ValueError(f"giá trị bật/tắt không hợp lệ: {value}")


def preview_csv_import(
    csv_text: str,
    *,
    existing_urls: Optional[Set[str]] = None,
    existing_groups: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    text = str(csv_text or "").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("Nội dung CSV không được để trống.")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise ValueError("CSV cần có dòng tiêu đề và dữ liệu.") from exc
    lookup: Dict[int, str] = {}
    aliases = {alias: field for field, values in _ALIASES.items() for alias in values}
    for index, header in enumerate(headers):
        field = aliases.get(_header_key(header))
        if field and field not in lookup.values():
            lookup[index] = field
    if "source_url" not in lookup.values():
        raise ValueError("CSV cần có cột Source_URL hoặc URL.")

    existing = set(existing_urls or ())
    groups = set(existing_groups or ())
    rows: List[Dict[str, Any]] = []
    seen_urls = set(existing)
    for line_no, raw in enumerate(reader, start=2):
        if not raw or not any(str(value or "").strip() for value in raw):
            continue
        values = {field: (raw[index] if index < len(raw) else "") for index, field in lookup.items()}
        item: Dict[str, Any] = {"line": line_no, "state": "valid", "title": "", "reasons": []}
        try:
            source_url = validate_source_url(values.get("source_url"))
        except ValueError:
            source_url = ""
        title = str(values.get("title") or "").strip()
        if not title and source_url:
            title = Path(source_url.split("?", 1)[0]).stem.strip()
        if not source_url:
            item["reasons"].append("Source_URL phải là URL HTTP/HTTPS hợp lệ.")
        if source_url and source_url in seen_urls:
            item["state"] = "duplicate"
            item["reasons"].append("URL đã có trong hàng đợi hoặc lặp trong file.")
        elif source_url:
            seen_urls.add(source_url)

        try:
            metadata = validate_metadata_fields({
                "title": title,
                "description": values.get("description"),
                "tags": values.get("tags"),
            }, require_title=True)
            group_raw = values.get("target_groups")
            groups_value = normalize_group_names(group_raw, default="Mặc định" if not str(group_raw or "").strip() else None)
            if not groups_value:
                raise ValueError("Phải chọn ít nhất một nhóm kênh đích.")
            group_lookup = {name.casefold(): name for name in groups}
            missing_groups = [name for name in groups_value if groups and name.casefold() not in group_lookup]
            if missing_groups:
                item["reasons"].append("Nhóm không tồn tại: " + ", ".join(missing_groups))
            elif groups:
                groups_value = [group_lookup[name.casefold()] for name in groups_value]
            visibility = str(values.get("visibility") or "public").strip().lower()
            if visibility == "scheduled":
                visibility = "schedule"
            upload_to = str(values.get("upload_to") or "Shorts").strip()
            render_mode = str(values.get("render_mode") or "shorts_9_16").strip()
            resolution = str(values.get("resolution") or "1080p").strip().lower()
            flags = {
                "made_for_kids": _bool_value(values.get("made_for_kids")),
                "age_restriction": _bool_value(values.get("age_restriction")),
                "altered_content": _bool_value(values.get("altered_content")),
                "is_premiere": _bool_value(values.get("is_premiere")),
            }
            final = validate_queue_fields({
                "visibility": visibility,
                "schedule_time": values.get("schedule_time") or "",
                "upload_to": upload_to,
                "render_mode": render_mode,
                "resolution": resolution,
                **flags,
            })
            item["data"] = {
                "source_url": source_url,
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "upload_to": final["upload_to"],
                "render_mode": final["render_mode"],
                "resolution": final["resolution"],
                "visibility": final["visibility"],
                "schedule_time": final["schedule_time"],
                "is_premiere": final["is_premiere"],
                "made_for_kids": final["made_for_kids"],
                "age_restriction": final["age_restriction"],
                "altered_content": flags["altered_content"],
                "target_group_names": groups_value,
            }
            item["title"] = title
        except ValueError as exc:
            item["reasons"].append(str(exc))

        if item["reasons"] and item["state"] != "duplicate":
            item["state"] = "error"
        elif item["state"] == "duplicate" and any(
            reason not in ("URL đã có trong hàng đợi hoặc lặp trong file.",) for reason in item["reasons"]
        ):
            item["state"] = "error"
        rows.append(item)

    valid = [row for row in rows if row["state"] == "valid"]
    duplicates = [row for row in rows if row["state"] == "duplicate"]
    errors = [row for row in rows if row["state"] == "error"]
    return {
        "total_rows": len(rows),
        "valid_count": len(valid),
        "duplicate_count": len(duplicates),
        "error_count": len(errors),
        "valid_rows": valid,
        "rows": [
            {key: value for key, value in row.items() if key != "data"}
            for row in rows[:100]
        ],
    }
