"""Kiểm tra dung lượng và dọn riêng artifact do ứng dụng quản lý."""

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import APP_OUTPUT_DIR, APP_TEMP_DIR
from database import get_conn

DEFAULT_RETENTION_DAYS = 30
MIN_DOWNLOAD_HEADROOM_BYTES = 1024 * 1024 * 1024
RENDER_HEADROOM_BYTES = 512 * 1024 * 1024


def _storage_roots() -> List[Path]:
    return [APP_TEMP_DIR.resolve(), APP_OUTPUT_DIR.resolve()]


def _canonical(path: str, strict: bool = True) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=strict)))


def _within_roots(path: Path, roots: Iterable[Path]) -> bool:
    target = os.path.normcase(str(path))
    for root in roots:
        try:
            if os.path.commonpath([target, os.path.normcase(str(root))]) == os.path.normcase(str(root)):
                return True
        except (OSError, ValueError):
            continue
    return False


def ensure_disk_space(target_dir: Path, required_bytes: int, operation: str) -> Dict[str, int]:
    """Fail trước khi job chiếm thêm dung lượng; nguồn hiện có không bị đụng tới."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    free_bytes = int(shutil.disk_usage(str(target)).free)
    required = max(0, int(required_bytes))
    if free_bytes < required:
        raise OSError(
            f"Không đủ dung lượng trống để {operation}: cần dự phòng {required / (1024 ** 3):.2f} GiB, "
            f"còn {free_bytes / (1024 ** 3):.2f} GiB. File nguồn được giữ nguyên."
        )
    return {"free_bytes": free_bytes, "required_bytes": required}


def estimate_render_bytes(input_path: str) -> int:
    try:
        source_size = max(0, int(Path(input_path).stat().st_size))
    except OSError:
        source_size = 0
    return max(RENDER_HEADROOM_BYTES, source_size * 2 + RENDER_HEADROOM_BYTES)


def _directory_size(root: Path) -> Dict[str, int]:
    total = count = 0
    if not root.exists():
        return {"bytes": 0, "files": 0}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        for name in files:
            item = base / name
            try:
                if item.is_symlink() or not item.is_file():
                    continue
                total += item.stat().st_size
                count += 1
            except OSError:
                continue
    return {"bytes": total, "files": count}


def _queue_references(conn) -> Dict[str, List[Dict[str, Any]]]:
    references: Dict[str, List[Dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT id, status, input_video_path, output_video_path, thumbnail_path FROM video_queue"
    ).fetchall()
    for row in rows:
        item = dict(row)
        for field in ("input_video_path", "output_video_path", "thumbnail_path"):
            raw = str(item.get(field) or "").strip()
            if not raw:
                continue
            try:
                canonical = _canonical(raw, strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            references.setdefault(canonical, []).append(
                {"queue_id": int(item["id"]), "status": str(item.get("status") or ""), "field": field}
            )
    return references


def _eligible_artifact_path(raw_path: str, roots: List[Path]) -> Optional[Path]:
    candidate = Path(raw_path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if _within_roots(resolved, roots) else None


def storage_overview(retention_days: int = DEFAULT_RETENTION_DAYS) -> Dict[str, Any]:
    retention = int(retention_days)
    if retention < 1 or retention > 365:
        raise ValueError("Thời hạn lưu phải nằm trong khoảng 1–365 ngày.")
    roots = _storage_roots()
    temp_usage = _directory_size(roots[0])
    output_usage = _directory_size(roots[1])
    free_values = [int(shutil.disk_usage(str(root)).free) for root in roots]
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention)
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    try:
        references = _queue_references(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM managed_artifacts ORDER BY registered_at DESC, id DESC")]
        logs_count = int(conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0])
    finally:
        conn.close()

    candidates = []
    protected_references = 0
    stale_records = 0
    for row in rows:
        registered_at = str(row.get("registered_at") or "")
        if registered_at > cutoff_text:
            continue
        path = _eligible_artifact_path(str(row.get("path") or ""), roots)
        if path is None:
            stale_records += 1
            continue
        canonical = _canonical(str(path), strict=True)
        if references.get(canonical):
            protected_references += 1
            continue
        candidates.append({
            "id": int(row["id"]),
            "name": path.name,
            "path": str(path),
            "kind": str(row.get("kind") or "file"),
            "owner_type": str(row.get("owner_type") or ""),
            "owner_id": row.get("owner_id"),
            "size_bytes": int(row.get("size_bytes") or path.stat().st_size),
            "registered_at": registered_at,
        })
    return {
        "free_bytes": min(free_values) if free_values else 0,
        "temp": temp_usage,
        "output": output_usage,
        "retention_days": retention,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(item["size_bytes"] for item in candidates),
        "protected_references": protected_references,
        "stale_manifest_records": stale_records,
        "logs_count": logs_count,
        "logs_retained_limit": 5000,
        "candidates": candidates,
    }


def cleanup_managed_artifacts(
    artifact_ids: List[int], retention_days: int = DEFAULT_RETENTION_DAYS
) -> Dict[str, Any]:
    ids = list(dict.fromkeys(int(value) for value in artifact_ids))
    if not ids:
        raise ValueError("Chưa chọn artifact nào để dọn.")
    if len(ids) > 200:
        raise ValueError("Mỗi lượt chỉ được chọn tối đa 200 artifact.")
    retention = int(retention_days)
    if retention < 1 or retention > 365:
        raise ValueError("Thời hạn lưu phải nằm trong khoảng 1–365 ngày.")
    cutoff_text = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    roots = _storage_roots()
    conn = get_conn()
    deleted = []
    skipped = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM managed_artifacts WHERE id IN ({marks})", ids).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        references = _queue_references(conn)
        for artifact_id in ids:
            row = by_id.get(artifact_id)
            if not row:
                skipped.append({"id": artifact_id, "reason": "Không còn trong manifest."})
                continue
            if str(row.get("registered_at") or "") > cutoff_text:
                skipped.append({"id": artifact_id, "reason": f"Chưa đủ tuổi lưu {retention} ngày."})
                continue
            raw_path = str(row.get("path") or "")
            path = _eligible_artifact_path(raw_path, roots)
            if path is None:
                skipped.append({"id": artifact_id, "reason": "File không tồn tại, là symlink hoặc nằm ngoài thư mục được quản lý."})
                continue
            canonical = _canonical(str(path), strict=True)
            refs = references.get(canonical, [])
            if refs:
                review = any(ref["status"].casefold() == "needsreview" for ref in refs)
                reason = "Được giữ bởi hàng đợi NeedsReview." if review else "Đang được một hàng đợi tham chiếu."
                skipped.append({"id": artifact_id, "reason": reason})
                continue
            # Kiểm tra lại ngay trước unlink, sau khi đã khóa ghi SQLite.
            if path.is_symlink() or not _within_roots(path.resolve(strict=True), roots):
                skipped.append({"id": artifact_id, "reason": "Đường dẫn đã đổi hoặc ra ngoài thư mục được quản lý."})
                continue
            path.unlink()
            conn.execute("DELETE FROM managed_artifacts WHERE id = ?", (artifact_id,))
            deleted.append({"id": artifact_id, "name": path.name, "size_bytes": int(row.get("size_bytes") or 0)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "deleted_count": len(deleted),
        "deleted_bytes": sum(item["size_bytes"] for item in deleted),
        "deleted": deleted,
        "skipped": skipped,
    }


def delete_completed_temp_artifact(path: str, queue_id: int) -> bool:
    """Xóa file tạm của lượt đã xác minh Completed, chỉ khi manifest cùng queue sở hữu."""
    roots = [_storage_roots()[0]]
    try:
        canonical = _canonical(path, strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    candidate = _eligible_artifact_path(canonical, roots)
    if candidate is None:
        return False
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queue = conn.execute(
            "SELECT status, published_url FROM video_queue WHERE id = ?", (int(queue_id),)
        ).fetchone()
        if not queue or str(queue["status"] or "").casefold() != "completed" or not str(queue["published_url"] or "").strip():
            conn.rollback()
            return False
        artifact = conn.execute(
            "SELECT id, owner_type, owner_id FROM managed_artifacts WHERE path = ?", (canonical,)
        ).fetchone()
        if not artifact or artifact["owner_type"] != "queue" or int(artifact["owner_id"] or -1) != int(queue_id):
            conn.rollback()
            return False
        refs = _queue_references(conn).get(canonical, [])
        if any(int(ref["queue_id"]) != int(queue_id) for ref in refs):
            conn.rollback()
            return False
        if candidate.is_symlink() or not _within_roots(candidate.resolve(strict=True), roots):
            conn.rollback()
            return False
        candidate.unlink()
        conn.execute("DELETE FROM managed_artifacts WHERE id = ?", (int(artifact["id"]),))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
