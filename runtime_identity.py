"""Identify the source snapshot served by this process."""
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


APPLICATION_ID = "dhb-reup-youtube-studio"


def source_fingerprint(root: Path) -> str:
    files = [*root.glob("*.py"), *(root / "static").rglob("*")]
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_file() and path.suffix in {".py", ".js", ".css", ".html"}:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def capture_runtime(root: Path) -> dict:
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL,
            timeout=2, encoding="ascii",
        ).strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        revision = ""
    return {
        "application": APPLICATION_ID,
        "source_root": str(root),
        "revision": revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "startup_fingerprint": source_fingerprint(root),
    }


def runtime_status(root: Path, startup: dict) -> dict:
    try:
        current = source_fingerprint(root)
    except OSError:
        current = None
    return {
        **startup,
        "disk_fingerprint": current,
        "restart_required": current != startup["startup_fingerprint"] if current else None,
    }
