"""Stable identities for uploaded files and source URLs."""

import hashlib
import ipaddress
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def sha256_file(path, chunk_size=1024 * 1024):
    """Return a full-file SHA-256 key, failing if the file changes while read."""
    file_path = Path(path)
    before = file_path.stat()
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    after = file_path.stat()
    if (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None)) != (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", None),
    ):
        raise RuntimeError(f"File changed while calculating its fingerprint: {file_path}")
    return f"sha256:{digest.hexdigest()}"


def copy_stream_and_hash(source, destination, chunk_size=1024 * 1024):
    """Copy a binary upload stream to disk while calculating its full SHA-256."""
    digest = hashlib.sha256()
    total = 0
    with open(destination, "wb") as target:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            target.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return f"sha256:{digest.hexdigest()}", total


def normalize_source_url(url):
    """Normalize URL spelling without dropping source-identifying query values."""
    try:
        parts = urlsplit(str(url or "").strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".").lower()
        if scheme not in ("http", "https") or not host or parts.username or parts.password:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return ""
        try:
            host = host.encode("idna").decode("ascii")
            port = parts.port
        except (UnicodeError, ValueError):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = host if port is None or default_port else f"{host}:{port}"
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((scheme, netloc, parts.path or "/", query, ""))
    except Exception:
        return ""


def source_dedupe_key(item, queue_id=None):
    """Return the strongest available identity, with legacy source IDs as fallback."""
    content_key = str(item.get("content_key") or "").strip()
    if content_key:
        return content_key

    post_id = str(item.get("post_id") or "").strip()
    platform = str(item.get("platform") or item.get("source_type") or "unknown").strip().lower()
    if post_id:
        return f"source:{platform}:{post_id}"

    normalized_url = normalize_source_url(item.get("source_url"))
    if normalized_url:
        return f"url:{normalized_url}"
    if queue_id is not None:
        return f"queue:{queue_id}"
    return ""
