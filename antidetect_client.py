"""Cầu nối tới Antidetect Browser Manager: API, xác thực DPAPI và CDP lease.

Antidetect là chủ sở hữu duy nhất của Chrome/Profile/Proxy/Fingerprint. Module này
chỉ xin quyền dùng chung trình duyệt đang chạy, mở tab riêng và KHÔNG bao giờ mở
Chrome bằng user_data_dir của Antidetect.
"""
import ctypes
import json
import logging
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
    or r"C:\Projects\Tools\Antidetect Python"
)
HEALTH_APPLICATION = "antidetect-python"
_HTTP_TIMEOUT = 15.0
_START_TIMEOUT = 180.0
_CDP_CONNECT_TIMEOUT_MS = 30000
_CDP_CONNECT_ATTEMPTS = 3
_CDP_RETRY_BASE_DELAY_SEC = 0.8


class AntidetectError(RuntimeError):
    """Lỗi có thông điệp an toàn để hiển thị cho người vận hành (không chứa secret)."""


class AntidetectNotReady(AntidetectError):
    def __init__(self, message, start_result=None):
        super().__init__(message)
        self.start_result = start_result or {}


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
        try:
            data = self._request(
                "POST",
                f"/api/profiles/{quote(str(profile_id), safe='')}/start",
                params=params,
                timeout=_START_TIMEOUT,
            )
        except AntidetectError as exc:
            raise AntidetectNotReady(str(exc)) from exc
        if not isinstance(data, dict) or not data.get("success"):
            detail = (data or {}).get("error") if isinstance(data, dict) else ""
            raise AntidetectNotReady(detail or "Antidetect không khởi chạy được profile.", data if isinstance(data, dict) else None)
        health = data.get("cdp_health") or {}
        if (data.get("profile_id", profile_id) != profile_id or data.get("degraded")
                or data.get("cdp_ready") is False or data.get("configuration_verified") is False
                or (data.get("launched_now") is True and
                    (data.get("cdp_ready") is not True or data.get("configuration_verified") is not True))
                or not isinstance(data.get("port"), int) or not 0 < data["port"] < 65536
                or health.get("state") != "healthy" or not health.get("launch_id")):
            raise AntidetectNotReady("Profile Antidetect chưa xác minh được cấu hình hoặc CDP.", data)
        return data

    def require_ready(self, profile_id: str, launch_id: str, port: int):
        try:
            state = self.get_profile_state(profile_id) or {}
        except AntidetectError as exc:
            raise AntidetectNotReady(str(exc)) from exc
        health = state.get("cdp_health") or {}
        if (not launch_id or state.get("status") != "running" or state.get("port") != port
                or health.get("launch_id") != launch_id or health.get("state") != "healthy"):
            raise AntidetectNotReady("Phiên Antidetect đã thay đổi hoặc CDP không còn sẵn sàng.")

    def stop_profile(self, profile_id: str, *, expected_launch_id: str = "") -> Dict[str, Any]:
        data = self._request(
            "POST",
            f"/api/profiles/{quote(str(profile_id), safe='')}/stop" + ("/owned" if expected_launch_id else ""),
            params={"expected_launch_id": expected_launch_id} if expected_launch_id else None,
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

    def __init__(self, context, pages: List[Any], require_ready):
        self._context = context
        self._pages = pages
        self._require_ready = require_ready

    def new_page(self):
        self._require_ready()
        page = self._context.new_page()
        self._pages.append(page)
        return page

    def __getattr__(self, name):
        return getattr(self._context, name)



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
        hide_window: bool = False,
    ):
        self.profile_id = str(profile_id)
        self.client = client or AntidetectClient()
        self.stop_if_started = stop_if_started
        self.hide_window = hide_window
        self._playwright = None
        self._browser = None
        self._pages: List[Any] = []
        self._started_by_us = False
        self._closed = False
        self.context = None
        self.tracked_context = None
        self.page = None
        self.port = None
        self.launch_id = ""
        self.cleanup_unconfirmed = False

    def __enter__(self):
        try:
            try:
                res = self.client.start_profile(self.profile_id)
            except AntidetectNotReady as exc:
                self._set_owner(exc.start_result)
                raise
            self._set_owner(res)
            self.port = int(res["port"])
            self.require_ready()

            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._connect_cdp()
            contexts = self._browser.contexts
            if not contexts:
                raise AntidetectNotReady("Không tìm thấy context trình duyệt của profile Antidetect.")
            self.context = contexts[0]
            self.tracked_context = _TrackedContext(self.context, self._pages, self.require_ready)
            self.page = self.tracked_context.new_page()
            # Chỉ dời cửa sổ khi chính lease khởi chạy profile; nếu người dùng
            # đang mở sẵn trình duyệt thì không đụng vào cửa sổ của họ.
            if self.hide_window and self._started_by_us:
                self._move_windows_offscreen()
            return self
        except Exception:
            self._cleanup()
            raise

    def _set_owner(self, result):
        self.launch_id = str((result.get("cdp_health") or {}).get("launch_id") or "")
        self._started_by_us = bool(self.launch_id and result.get("launched_now") is True)

    def require_ready(self):
        self.client.require_ready(self.profile_id, self.launch_id, self.port)

    def _move_windows_offscreen(self):
        """Dời cửa sổ Chrome ra ngoài mọi màn hình; không đổi fingerprint/trạng thái tab."""
        moved = set()
        for page in list(self.context.pages):
            try:
                session = self.context.new_cdp_session(page)
            except Exception:
                continue
            try:
                window_id = (session.send("Browser.getWindowForTarget") or {}).get("windowId")
                if window_id is None or window_id in moved:
                    continue
                moved.add(window_id)
                session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"left": -3200, "top": -3200}},
                )
            except Exception:
                continue
            finally:
                try:
                    session.detach()
                except Exception:
                    pass

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
        raise AntidetectNotReady(
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
                self.cleanup_unconfirmed = True
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
                self.client.stop_profile(self.profile_id, expected_launch_id=self.launch_id)
                self.cleanup_unconfirmed = False
            except Exception:
                self.cleanup_unconfirmed = True
                logging.getLogger(__name__).warning("Không xác nhận được cleanup phiên Antidetect do lease mở.")
            self._started_by_us = False


def resolve_antidetect_profile_id(channel: Dict[str, Any]) -> str:
    explicit = str(channel.get("antidetect_profile_id") or "").strip()
    if explicit:
        return explicit

    profile_path = str(channel.get("profile_path") or "").strip()
    antidetect_prefix = "antidetect://"
    if profile_path.lower().startswith(antidetect_prefix):
        return profile_path[len(antidetect_prefix):].split("/", 1)[0].strip()

    profile_name = str(channel.get("profile_name") or "").strip()
    if not profile_name:
        return ""
    try:
        from database import get_antidetect_profile_id
        return get_antidetect_profile_id(profile_name)
    except Exception:
        return ""
