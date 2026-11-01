"""Cầu nối tới Antidetect Browser Manager: API, xác thực DPAPI và CDP lease.

Antidetect là chủ sở hữu duy nhất của Chrome/Profile/Proxy/Fingerprint. Module này
chỉ xin quyền dùng chung trình duyệt đang chạy, mở tab riêng và KHÔNG bao giờ mở
Chrome bằng user_data_dir của Antidetect.
"""
import ctypes
import json
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

DEFAULT_ANTIDETECT_HOME = Path(
    os.environ.get("ANTIDETECT_HOME")
    or r"C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python"
)
HEALTH_APPLICATION = "antidetect-python"
_HTTP_TIMEOUT = 15.0
_START_TIMEOUT = 180.0
_CDP_CONNECT_TIMEOUT_MS = 30000
_CDP_CONNECT_ATTEMPTS = 3
_CDP_RETRY_BASE_DELAY_SEC = 0.8


class AntidetectError(RuntimeError):
    """Lỗi có thông điệp an toàn để hiển thị cho người vận hành (không chứa secret)."""


def _read_antidetect_runtime(data_dir: Path) -> Dict[str, Any]:
    """Đọc runtime.dpapi của Antidetect bằng DPAPI của user Windows hiện tại."""
    if os.name != "nt":
        return {}
    runtime_file = Path(data_dir) / "runtime.dpapi"
    if not runtime_file.is_file():
        return {}

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def crypt(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel32.LocalFree(output.data)

    try:
        return json.loads(crypt(runtime_file.read_bytes()))
    except Exception:
        return {}


def _read_server_pid(data_dir: Path) -> Optional[Dict[str, Any]]:
    pid_file = Path(data_dir) / "server.pid"
    try:
        content = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
        if isinstance(data, int) and not isinstance(data, bool) and data > 0:
            return {"pid": data, "port": 8000, "legacy": True}
    except ValueError:
        if content.isdigit():
            return {"pid": int(content), "port": 8000, "legacy": True}
    return None


def _session() -> requests.Session:
    s = requests.Session()
    # Không cho proxy từ biến môi trường chen vào kết nối nội bộ tới Antidetect.
    s.trust_env = False
    return s


def _normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise AntidetectError(
            "Địa chỉ Antidetect phải bắt đầu bằng http:// hoặc https:// "
            f"(hiện tại: {value})."
        )
    return value


def _probe_health(base_url: str, timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    try:
        with _session() as s:
            resp = s.get(f"{base_url}/health", timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict) and data.get("application") == HEALTH_APPLICATION:
            return data
    except Exception:
        return None
    return None


@dataclass(frozen=True)
class AntidetectConfig:
    home: Path
    data_dir: Path
    base_url: str = ""
    access_key: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)

    @classmethod
    def load(cls) -> "AntidetectConfig":
        settings: Dict[str, Any] = {}
        try:
            from database import get_settings
            settings = get_settings()
        except Exception:
            settings = {}

        home = Path(
            str(settings.get("antidetect_home") or os.environ.get("ANTIDETECT_HOME") or DEFAULT_ANTIDETECT_HOME)
        ).expanduser()
        data_dir = Path(
            str(
                settings.get("antidetect_data_dir")
                or os.environ.get("ANTIDETECT_DATA_DIR")
                or (home / "data")
            )
        ).expanduser()
        base_url = _normalize_base_url(
            str(settings.get("antidetect_base_url") or os.environ.get("ANTIDETECT_BASE_URL") or "")
        )

        access_key = (os.environ.get("ANTIDETECT_ACCESS_KEY") or "").strip()
        api_token = (os.environ.get("ANTIDETECT_API_TOKEN") or "").strip()
        if not access_key and not api_token:
            runtime = _read_antidetect_runtime(data_dir)
            access_key = str(runtime.get("access_key") or "").strip()
        return cls(
            home=home,
            data_dir=data_dir,
            base_url=base_url,
            access_key=access_key,
            api_token=api_token,
        )


class AntidetectClient:
    def __init__(self, config: Optional[AntidetectConfig] = None):
        self.config = config or AntidetectConfig.load()
        self.base_url = ""

    def _auth_key(self) -> str:
        if self.config.access_key:
            return self.config.access_key
        if self.config.api_token:
            return self.config.api_token
        raise AntidetectError(
            "Chưa đọc được khóa truy cập Antidetect. Hãy chắc chắn Antidetect đã chạy ít nhất một lần "
            f"(runtime.dpapi trong: {self.config.data_dir}), hoặc đặt biến môi trường "
            "ANTIDETECT_ACCESS_KEY / ANTIDETECT_API_TOKEN."
        )

    def resolve_base_url(self) -> str:
        if self.base_url and _probe_health(self.base_url):
            return self.base_url
        # Cache cũ đã chết hoặc máy chủ đổi cổng: bỏ và dò lại trước khi dùng.
        self.base_url = ""

        explicit = self.config.base_url
        if explicit:
            if not _probe_health(explicit):
                raise AntidetectError(
                    f"Máy chủ Antidetect không phản hồi đúng tại {explicit}. "
                    "Hãy mở Antidetect trước rồi thử lại."
                )
            self.base_url = explicit
            return explicit

        candidates: List[str] = []
        info = _read_server_pid(self.config.data_dir)
        if info and info.get("port"):
            candidates.append(f"http://127.0.0.1:{int(info['port'])}")
        candidates.append("http://127.0.0.1:8000")

        seen = set()
        for base in candidates:
            if base in seen:
                continue
            seen.add(base)
            if _probe_health(base):
                self.base_url = base
                return base

        raise AntidetectError(
            "Không kết nối được máy chủ Antidetect. Hãy mở Antidetect Browser Manager "
            f"(dữ liệu: {self.config.data_dir}) rồi thử lại."
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = _HTTP_TIMEOUT,
    ) -> Any:
        base = self.resolve_base_url()
        headers = {
            "Authorization": f"Bearer {self._auth_key()}",
            "Accept": "application/json",
        }
        try:
            with _session() as s:
                resp = s.request(method, f"{base}{path}", params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            self.base_url = ""
            raise AntidetectError(
                f"Không kết nối được máy chủ Antidetect tại {base}: {exc}"
            ) from exc

        if resp.status_code == 401:
            raise AntidetectError(
                "Khóa truy cập Antidetect không hợp lệ hoặc đã thay đổi. "
                "Hãy khởi động lại Antidetect rồi thử lại."
            )
        if resp.status_code >= 400:
            detail = ""
            try:
                payload = resp.json()
                detail = str(payload.get("detail") or payload.get("error") or "")
            except Exception:
                detail = (resp.text or "").strip()
            detail = detail[:500]
            raise AntidetectError(
                f"Antidetect trả lỗi HTTP {resp.status_code}"
                + (f": {detail}" if detail else ".")
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise AntidetectError("Antidetect trả dữ liệu không phải JSON.") from exc

    def status(self) -> Dict[str, Any]:
        result = {
            "connected": False,
            "base_url": self.config.base_url or "",
            "data_dir": str(self.config.data_dir),
            "profile_count": 0,
            "error": "",
        }
        try:
            result["base_url"] = self.resolve_base_url()
            profiles = self.list_profiles()
            result["connected"] = True
            result["profile_count"] = len(profiles)
        except AntidetectError as exc:
            result["error"] = str(exc)
        return result

    def list_profiles(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/profiles")
        return data if isinstance(data, list) else []

    def get_profile_state(self, profile_id: str) -> Optional[Dict[str, Any]]:
        for item in self.list_profiles():
            if str(item.get("id") or "") == str(profile_id):
                return item
        return None

    def start_profile(self, profile_id: str, url: Optional[str] = None) -> Dict[str, Any]:
        params = {"url": url} if url else None
        data = self._request(
            "POST",
            f"/api/profiles/{quote(str(profile_id), safe='')}/start",
            params=params,
            timeout=_START_TIMEOUT,
        )
        if not isinstance(data, dict) or not data.get("success"):
            detail = (data or {}).get("error") if isinstance(data, dict) else ""
            raise AntidetectError(detail or "Antidetect không khởi chạy được profile.")
        if not data.get("port"):
            state = self.get_profile_state(profile_id) or {}
            data = {**data, "port": state.get("port")}
        if not data.get("port"):
            raise AntidetectError("Antidetect không trả về cổng CDP của profile.")
        return data

    def stop_profile(self, profile_id: str) -> Dict[str, Any]:
        data = self._request(
            "POST",
            f"/api/profiles/{quote(str(profile_id), safe='')}/stop",
            timeout=45.0,
        )
        if isinstance(data, dict) and data.get("success") is False:
            raise AntidetectError(data.get("error") or "Antidetect không dừng được profile.")
        return data if isinstance(data, dict) else {}

    def open_login(self, profile_id: str, url: str = "https://studio.youtube.com") -> Dict[str, Any]:
        res = self.start_profile(profile_id, url=url)
        return {
            "ok": True,
            "antidetect": True,
            "profile_id": str(profile_id),
            "port": res.get("port"),
            "message": "Đã mở Profile bằng Antidetect tại studio.youtube.com",
        }


class _TrackedContext:
    """Bọc context CDP để chỉ đóng những tab do tool này tạo ra."""

    def __init__(self, context, pages: List[Any]):
        self._context = context
        self._pages = pages

    def new_page(self):
        page = self._context.new_page()
        self._pages.append(page)
        return page

    def __getattr__(self, name):
        return getattr(self._context, name)


def _lease_started_by_us(
    start_result: Dict[str, Any], state_known: bool, was_running: bool
) -> bool:
    """Lease có tự khởi chạy profile không, chỉ dựa vào dữ liệu cấu trúc.

    API mới trả ``pid``/``ws_endpoint`` khi khởi chạy mới và ``already_running``
    khi dùng lại phiên có sẵn; không phụ thuộc câu chữ ``message``. Với API cũ
    không có các trường này, dùng trạng thái profile đọc trước khi start.
    """
    if start_result.get("already_running") is True:
        return False
    if start_result.get("pid") or start_result.get("ws_endpoint"):
        return True
    return bool(state_known and not was_running)


class AntidetectBrowserLease:
    """Mượn trình duyệt của một profile Antidetect qua CDP.

    Chỉ mở/đóng tab do lease tạo. Không đóng context/browser của Antidetect.
    Nếu lease tự khởi chạy profile (trước đó đang tắt) thì mặc định dừng lại
    sau khi xong để không bỏ lại Chrome mồ côi.
    """

    def __init__(
        self,
        profile_id: str,
        *,
        client: Optional[AntidetectClient] = None,
        stop_if_started: bool = True,
    ):
        self.profile_id = str(profile_id)
        self.client = client or AntidetectClient()
        self.stop_if_started = stop_if_started
        self._playwright = None
        self._browser = None
        self._pages: List[Any] = []
        self._started_by_us = False
        self._closed = False
        self.context = None
        self.tracked_context = None
        self.page = None
        self.port = None

    def __enter__(self):
        state_known = False
        was_running = False
        try:
            state = self.client.get_profile_state(self.profile_id)
            state_known = state is not None
            was_running = bool(state and state.get("status") == "running")
        except AntidetectError:
            state_known = False

        try:
            res = self.client.start_profile(self.profile_id)
            self.port = int(res["port"])
            self._started_by_us = _lease_started_by_us(res, state_known, was_running)

            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._connect_cdp()
            contexts = self._browser.contexts
            if not contexts:
                raise AntidetectError("Không tìm thấy context trình duyệt của profile Antidetect.")
            self.context = contexts[0]
            self.tracked_context = _TrackedContext(self.context, self._pages)
            self.page = self.tracked_context.new_page()
            return self
        except Exception:
            self._cleanup()
            raise

    def _connect_cdp(self):
        last_error: Optional[Exception] = None
        for attempt in range(1, _CDP_CONNECT_ATTEMPTS + 1):
            try:
                return self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.port}",
                    timeout=_CDP_CONNECT_TIMEOUT_MS,
                )
            except Exception as exc:
                last_error = exc
                if attempt < _CDP_CONNECT_ATTEMPTS:
                    time.sleep(_CDP_RETRY_BASE_DELAY_SEC * attempt)
        raise AntidetectError(
            f"Không kết nối được CDP tới profile Antidetect (cổng {self.port}) "
            f"sau {_CDP_CONNECT_ATTEMPTS} lần thử: {last_error}"
        )

    def new_page(self):
        if self.tracked_context is None:
            raise AntidetectError("Lease Antidetect chưa sẵn sàng.")
        return self.tracked_context.new_page()

    def __exit__(self, exc_type, exc, tb):
        self._cleanup()

    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        for page in reversed(self._pages):
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
        self._pages.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._browser = None
        if self.stop_if_started and self._started_by_us:
            try:
                self.client.stop_profile(self.profile_id)
            except Exception:
                pass
            self._started_by_us = False


def resolve_antidetect_profile_id(channel: Dict[str, Any]) -> str:
    explicit = str(channel.get("antidetect_profile_id") or "").strip()
    if explicit:
        return explicit

    profile_path = str(channel.get("profile_path") or "")
    if profile_path.startswith("antidetect://"):
        return profile_path.split("antidetect://", 1)[1].split("/", 1)[0].strip()

    profile_name = str(channel.get("profile_name") or "").strip()
    if not profile_name:
        return ""
    try:
        from database import get_antidetect_profile_id
        return get_antidetect_profile_id(profile_name)
    except Exception:
        return ""
