"""Deterministic, versioned group-to-channel distribution."""

import sqlite3
from typing import Any, Dict, List, Tuple

from phase4_rules import GROUP_SCHEMA_VERSION, normalize_group_names


def target_group_names(item: Dict[str, Any]) -> List[str]:
    group_target = str(item.get("target_type") or "GROUP").upper() == "GROUP"
    if not group_target:
        return []
    try:
        schema_version = int(item.get("group_schema_version") or 0)
    except (TypeError, ValueError):
        raise ValueError("Phiên bản dữ liệu nhóm kênh không hợp lệ; hãy mở và lưu lại đích.")
    if schema_version > GROUP_SCHEMA_VERSION:
        raise ValueError(
            f"Dữ liệu nhóm kênh phiên bản {schema_version} chưa được hỗ trợ; cần nâng cấp ứng dụng trước khi chạy."
        )
    names = normalize_group_names(
        item.get("target_group_names_json"),
        legacy_name=item.get("target_group_name") or "",
    )
    if not names:
        raise ValueError("Video chưa có nhóm kênh đích; hãy chọn lại nhóm trước khi chạy.")
    return names


def group_channel_pool(conn: sqlite3.Connection, group_names: List[str]) -> List[Dict[str, Any]]:
    names = normalize_group_names(group_names)
    if not names:
        raise ValueError("Chưa chọn nhóm kênh đích.")

    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "channel_groups" in tables:
        marks = ",".join("?" for _ in names)
        found = {
            str(row[0]) for row in conn.execute(
                f"SELECT name FROM channel_groups WHERE name IN ({marks})", names
            ).fetchall()
        }
        missing = [name for name in names if name not in found]
        if missing:
            raise ValueError("Nhóm kênh không còn tồn tại: " + ", ".join(missing))

    pool: List[Dict[str, Any]] = []
    seen = set()
    for group_name in names:
        if "channel_group_memberships" in tables:
            rows = conn.execute(
                "SELECT c.* FROM channel_group_memberships m "
                "JOIN youtube_channels c ON c.id = m.channel_id "
                "WHERE m.group_name = ? ORDER BY m.sort_order ASC, c.id ASC",
                (group_name,),
            ).fetchall()
        else:
            # Compatibility for older isolated DB fixtures; production migration creates memberships.
            rows = conn.execute(
                "SELECT * FROM youtube_channels WHERE group_name = ? ORDER BY id ASC",
                (group_name,),
            ).fetchall()
        for row in rows:
            channel = dict(row)
            channel_id = int(channel["id"])
            if channel_id in seen:
                continue
            seen.add(channel_id)
            channel["group_names"] = [group_name]
            pool.append(channel)
    if not pool:
        raise ValueError(f"Nhóm kênh {', '.join(names)} chưa có kênh nào.")
    return pool


def resolve_target_candidates(conn: sqlite3.Connection, item: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    if item.get("target_channel_id"):
        row = conn.execute(
            "SELECT * FROM youtube_channels WHERE id = ?", (item["target_channel_id"],)
        ).fetchone()
        if not row:
            raise ValueError(f"Kênh đích #{item['target_channel_id']} không còn tồn tại; hãy chọn lại kênh.")
        return [dict(row)], []

    profile_name = str(item.get("target_profile_name") or "").strip()
    if profile_name:
        rows = conn.execute(
            "SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC", (profile_name,)
        ).fetchall()
        if not rows:
            raise ValueError(f"Profile '{profile_name}' không còn kênh hợp lệ; hãy chọn lại đích.")
        if len(rows) > 1:
            raise ValueError(
                f"Profile '{profile_name}' có {len(rows)} kênh; cần chọn kênh cụ thể để tránh đăng nhầm."
            )
        return [dict(rows[0])], []

    if str(item.get("target_type") or "GROUP").upper() == "CHANNEL":
        raise ValueError("Đích kênh cụ thể không còn hợp lệ; hãy chọn lại kênh.")

    groups = target_group_names(item)
    return group_channel_pool(conn, groups), groups


def choose_target_channel(
    conn: sqlite3.Connection,
    item: Dict[str, Any],
    *,
    validate_existing_assignment: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    assigned_id = item.get("assigned_channel_id")
    if assigned_id not in (None, "", 0, "0") and not validate_existing_assignment:
        # The batch froze this destination before worker threads started. A later group
        # edit/delete must not reroute work already in flight.
        row = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (int(assigned_id),)).fetchone()
        if not row:
            raise ValueError(f"Kênh đã phân công #{assigned_id} đã bị xóa; không thể tự chuyển kênh.")
        groups = target_group_names(item) if str(item.get("target_type") or "GROUP").upper() == "GROUP" else []
        return dict(row), groups

    candidates, groups = resolve_target_candidates(conn, item)
    if assigned_id not in (None, "", 0, "0"):
        assigned_id = int(assigned_id)
        assigned = next((c for c in candidates if int(c["id"]) == assigned_id), None)
        if assigned is None:
            if validate_existing_assignment:
                raise ValueError(
                    f"Kênh đã phân công #{assigned_id} không còn thuộc đích hiện tại. "
                    "Hãy xem lại preview và sửa đích trước khi chạy lại."
                )
            row = conn.execute("SELECT * FROM youtube_channels WHERE id = ?", (assigned_id,)).fetchone()
            if not row:
                raise ValueError(f"Kênh đã phân công #{assigned_id} đã bị xóa; hãy chọn lại đích.")
            assigned = dict(row)
        return assigned, groups

    try:
        rotation_key = int(item.get("id") or 0)
    except (TypeError, ValueError):
        rotation_key = 0
    return candidates[rotation_key % len(candidates)], groups


def freeze_target_assignment(conn: sqlite3.Connection, item: Dict[str, Any]) -> Tuple[int, List[str]]:
    channel, groups = choose_target_channel(conn, item, validate_existing_assignment=True)
    channel_id = int(channel["id"])
    return channel_id, groups
