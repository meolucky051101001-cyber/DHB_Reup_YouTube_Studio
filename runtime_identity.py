"""Identify the source snapshot served by this process."""
import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


APPLICATION_ID = "dhb-reup-youtube-studio"


def git_environment(root: Path, *, trust_root=False) -> dict:
    """Scope child Git to this checkout without changing global configuration."""
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE'):
        env.pop(key, None)
    if trust_root:
        count = int(env.get('GIT_CONFIG_COUNT', '0'))
        if count < 0:
            raise ValueError('Invalid Git environment configuration count.')
        env[f'GIT_CONFIG_KEY_{count}'] = 'safe.directory'
        env[f'GIT_CONFIG_VALUE_{count}'] = Path(root).resolve().as_posix()
        env['GIT_CONFIG_COUNT'] = str(count + 1)
    return env


def git_output(root: Path, *args, timeout=2) -> str:
    root = Path(root).resolve()
    return subprocess.check_output(
        ['git', '-c', f'safe.directory={root.as_posix()}', '-C', str(root), *args],
        cwd=root, env=git_environment(root), stderr=subprocess.DEVNULL,
        timeout=timeout, encoding='utf-8',
    ).strip()


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
    root = Path(root).resolve()
    try:
        revision = git_output(root, 'rev-parse', 'HEAD')
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
