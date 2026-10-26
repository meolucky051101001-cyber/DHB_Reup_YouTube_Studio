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


def generate_api_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)
