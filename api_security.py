import secrets
from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_host(host_header: str) -> str:
    value = (host_header or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def is_allowed_host(host_header: str) -> bool:
    return normalize_host(host_header) in LOCAL_HOSTS


def is_allowed_origin(origin: str) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and normalize_host(parsed.netloc) in LOCAL_HOSTS


def is_same_local_origin(origin: str, host_header: str) -> bool:
    """Require a real Origin header matching the request Host including its port."""
    if not origin or not host_header or not is_allowed_origin(origin) or not is_allowed_host(host_header):
        return False
    try:
        parsed = urlsplit(origin)
        request_host = urlsplit("//" + host_header)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = request_host.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return normalize_host(parsed.netloc) == normalize_host(host_header) and origin_port == request_port


def generate_api_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)
