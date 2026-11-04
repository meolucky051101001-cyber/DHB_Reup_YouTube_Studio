import json
import ipaddress
import os
import re
import subprocess
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Any, Optional
from urllib.parse import quote, urlsplit, parse_qs
import requests

from config import (
    APP_PROFILES_DIR,
    BROWSER_HOST_EXE,
)
from database import add_log, get_settings
from antidetect_client import (
    AntidetectBrowserLease,
    AntidetectClient,
    resolve_antidetect_profile_id,
)

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB giống SliceBytes của DHB jFbxQL

_PROFILE_LOCKS_GUARD = threading.Lock()
_PROFILE_LOCKS: Dict[str, threading.Lock] = {}


def _get_named_lock(key: str) -> threading.Lock:
    with _PROFILE_LOCKS_GUARD:
        if key not in _PROFILE_LOCKS:
            _PROFILE_LOCKS[key] = threading.Lock()
        return _PROFILE_LOCKS[key]


def _get_profile_lock(prof_path: str) -> threading.Lock:
    """Đảm bảo tại một thời điểm chỉ có 1 tiến trình Playwright mở cùng 1 thư mục Chrome Profile."""
    norm_path = os.path.normcase(os.path.abspath(prof_path))
    return _get_named_lock(f"path:{norm_path}")


def _get_antidetect_lock(profile_id: str) -> threading.Lock:
    return _get_named_lock(f"antidetect:{str(profile_id).strip()}")


def _channel_antidetect_id(channel: Dict[str, Any]) -> str:
    declared_id = str(channel.get("antidetect_profile_id") or "").strip()
    profile_path = str(channel.get("profile_path") or "").strip()
    profile_source = str(channel.get("profile_source") or channel.get("source") or "").strip().lower()
    declared_antidetect = bool(
        declared_id or profile_path.lower().startswith("antidetect://") or profile_source == "antidetect"
    )
    profile_name = str(channel.get("profile_name") or "").strip()
    profile_path_id = (
        profile_path[len("antidetect://"):].split("/", 1)[0].strip()
        if profile_path.lower().startswith("antidetect://")
        else ""
    )
    if (
        profile_name
        and not declared_id
        and not profile_path_id
        and not profile_path.lower().startswith("antidetect://")
    ):
        try:
            from database import get_profile_binding
            binding = get_profile_binding(profile_name) or {}
            declared_antidetect = declared_antidetect or (
                str(binding.get("source") or "").strip().lower() == "antidetect"
                or bool(str(binding.get("antidetect_profile_id") or "").strip())
            )
        except Exception:
            if declared_antidetect:
                raise
    try:
        resolved = str(resolve_antidetect_profile_id(channel) or "").strip()
    except Exception as exc:
        if declared_antidetect:
            raise RuntimeError(f"Không xác định được ID Profile Antidetect; đã dừng để không chuyển sang Chrome local: {exc}") from exc
        return ""
    if declared_antidetect and not resolved:
        raise RuntimeError("Profile Antidetect chưa có ID liên kết; đã dừng để không chuyển sang Chrome local.")
    return resolved


def _log_optional_step_failure(step: str, err: Exception, channel: Dict[str, Any], metadata: Dict[str, Any]):
    """Ghi log các bước DOM không bắt buộc thất bại, thay vì nuốt lỗi hoàn toàn."""
    try:
        add_log(
            "Upload",
            f"Bước không bắt buộc '{step}' thất bại: {err}",
            "WARNING",
            channel_name=channel.get("name", ""),
            video_title=metadata.get("title", ""),
        )
    except Exception:
        pass


def find_system_chrome_exe() -> Optional[str]:
    """Tìm đường dẫn Chrome / Chromium trên máy để chạy Persistent Profile."""
    configured = os.environ.get("DHB_CHROME_EXE", "").strip()
    candidates = ([str(Path(configured).expanduser())] if configured else []) + [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\DHB Reup Facebook\Data\Chrome\chrome.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("chrome") or shutil.which("chrome.exe") or shutil.which("chromium")


def _launch_persistent_context_guarded(p, ctx_kwargs: Dict[str, Any], prof_name: str):
    """
    Khởi chạy Chrome Persistent Context với thông báo lỗi thân thiện khi Profile
    đang bị một cửa sổ Chrome khác chiếm dụng (ProcessSingleton) hoặc Chrome sập lúc mở.
    """
    try:
        return p.chromium.launch_persistent_context(**ctx_kwargs)
    except Exception as launch_err:
        msg = str(launch_err).lower()
        if any(
            k in msg
            for k in (
                "singleton",
                "already running",
                "user data directory",
                "failed to launch",
                "target closed",
                "browser has been closed",
                "browser closed",
            )
        ):
            raise RuntimeError(
                f"Không mở được Chrome cho Profile '{prof_name}' — Profile có thể đang bị một cửa sổ Chrome khác chiếm dụng. "
                f"Hãy ĐÓNG hết các cửa sổ Chrome của Profile này rồi thử lại. Chi tiết kỹ thuật: {launch_err}"
            ) from launch_err
        raise


def build_final_metadata(
    video_item: Dict[str, Any],
    channel: Dict[str, Any],
    option_group: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Ghép nối Title, Description, Tags và Link Shopee Affiliate từ Option Group.
    """
    title = (video_item.get("title") or "Video Shorts").strip()
    desc = (video_item.get("description") or "").strip()
    tags_raw = (video_item.get("tags") or "").strip()
    upload_to = video_item.get("upload_to") or "Shorts"

    sched_time = (video_item.get("schedule_time") or "").strip()
    if sched_time:
        from datetime import datetime
        try:
            sched_dt = datetime.fromisoformat(sched_time.replace(" ", "T"))
        except Exception:
            raise ValueError(
                f"Thời gian hẹn lịch không đúng định dạng: '{sched_time}' (định dạng đúng: 2026-10-25 19:30)"
            )
        if sched_dt <= datetime.now():
            raise ValueError(
                f"Thời gian hẹn lịch '{sched_time}' đã qua. YouTube Studio chỉ nhận lịch phát hành trong tương lai — hãy chọn lại giờ đăng."
            )

    privacy_status = (video_item.get("visibility") or channel.get("default_privacy") or "public").strip().lower()
    if sched_time:
        privacy_status = "schedule"
    elif privacy_status in ("schedule", "scheduled"):
        raise ValueError(
            f"Video '{title}' đang đặt chế độ Lên lịch nhưng chưa có thời gian đăng. "
            "Hãy nhập giờ đăng cụ thể trong tương lai — hệ thống không tự chuyển sang Công khai."
        )
    elif privacy_status not in ("public", "unlisted", "private"):
        raise ValueError(
            f"Chế độ hiển thị '{privacy_status}' không hợp lệ (chỉ nhận public / unlisted / private / schedule)."
        )

    if option_group:
        hashtags = (option_group.get("append_hashtags") or "").strip()
        if hashtags and hashtags not in title and len(title) + len(hashtags) + 1 <= 100:
            title = f"{title} {hashtags}".strip()

        if option_group.get("affiliate_enabled") and option_group.get("affiliate_url"):
            cta = option_group.get("affiliate_CTA") or "👉 Mua sản phẩm chính hãng tại đây:"
            aff_link = option_group.get("affiliate_url")
            aff_block = f"\n\n━━━━━━━━━━━━━━━━━━━━\n{cta}\n🔗 {aff_link}\n━━━━━━━━━━━━━━━━━━━━\n"
            if aff_link not in desc:
                desc = (desc + aff_block).strip()

        if option_group.get("default_tags"):
            tags_raw = f"{tags_raw}, {option_group['default_tags']}".strip(", ")

    if upload_to == "Shorts" and "#shorts" not in title.lower() and "#shorts" not in desc.lower():
        if len(title) <= 91:
            title = f"{title} #shorts"
        else:
            desc = f"{desc}\n#shorts".strip()

    tags_list = [t.strip().lstrip("#") for t in re.split(r"[,;\n]+", tags_raw) if t.strip()]
    made_for_kids = bool(
        video_item.get("made_for_kids")
        if ("made_for_kids" in video_item and video_item.get("made_for_kids") is not None)
        else (option_group.get("made_for_kids") if option_group is not None else channel.get("made_for_kids", 0))
    )
    age_restriction = bool(video_item.get("age_restriction", 0))
    if made_for_kids and age_restriction:
        raise ValueError(
            f"Video '{title}' không thể vừa 'Dành cho trẻ em' vừa giới hạn độ tuổi 18+. Hãy chọn lại một trong hai."
        )
    if len(desc) > 5000:
        raise ValueError(
            f"Mô tả video sau khi ghép hashtag/affiliate dài {len(desc)} ký tự, vượt giới hạn 5.000 ký tự "
            "của YouTube. Hãy rút gọn mô tả hoặc nội dung affiliate; không cắt tự động để tránh mất URL."
        )
    return {
        "title": title[:100],
        "description": desc,
        "upload_to": upload_to,
        "tags": tags_list[:25],
        "category_id": (option_group.get("category_id") if option_group else None)
        or channel.get("default_category_id")
        or "22",
        "made_for_kids": made_for_kids,
        "age_restriction": age_restriction,
        "altered_content": bool(
            video_item.get("altered_content")
            if ("altered_content" in video_item and video_item.get("altered_content") is not None)
            else (option_group.get("ai_label", 0) if option_group is not None else channel.get("altered_content", 0))
        ),
        "privacy_status": privacy_status,
        "schedule_time": sched_time,
        "is_premiere": bool(video_item.get("is_premiere", 0)),
        "pin_comment": (
            option_group.get("pin_comment_text")
            if (option_group and option_group.get("pin_comment_enabled"))
            else ""
        ),
        "emulate_mobile": bool(
            video_item.get("emulate_mobile", 0)
            if ("emulate_mobile" in video_item and video_item.get("emulate_mobile") is not None)
            else channel.get("emulate_mobile", 0)
        ),
        "resolution": str(video_item.get("resolution") or "1080p"),
        "channel_name": channel.get("name", ""),
    }


def open_profile_for_login(channel: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mở trình duyệt Chrome với Profile của kênh để người dùng đăng nhập YouTube Studio / Google.
    Profile Antidetect được mở qua API Antidetect và dùng chung phiên với tool đăng bài.
    """
    prof_name = channel.get("profile_name") or f"channel_{channel.get('id', 'default')}"
    ad_id = _channel_antidetect_id(channel)
    if ad_id:
        try:
            with _get_antidetect_lock(ad_id):
                res = AntidetectClient().open_login(ad_id)
        except Exception as e:
            add_log(
                "Browser",
                f"Không mở được Profile Antidetect '{prof_name}': {e}",
                "ERROR",
                channel_name=channel.get("name", ""),
            )
            raise RuntimeError(f"Không mở được Profile Antidetect: {e}") from e
        add_log(
            "Browser",
            f"Đã mở Profile Antidetect '{prof_name}' tại studio.youtube.com (dùng chung phiên với tool).",
            "INFO",
            channel_name=channel.get("name", ""),
        )
        return {
            "ok": True,
            "antidetect": True,
            "profile_path": f"antidetect://{ad_id}",
            "port": res.get("port"),
            "message": (
                f"Đã mở Profile Antidetect: {prof_name}. Hãy đăng nhập Google/YouTube trên cửa sổ vừa mở. "
                "Không cần đóng trình duyệt — tool sẽ dùng chung đúng phiên này khi kiểm tra và đăng bài."
            ),
        }

    if str(channel.get("profile_path") or "").lower().startswith("antidetect://"):
        raise RuntimeError(
            f"Profile '{prof_name}' được đánh dấu thuộc Antidetect nhưng chưa có ID liên kết. "
            "Hãy bấm 'Đồng Bộ Antidetect' trong tab Quản Lý Kênh rồi thử lại."
        )

    prof_path = channel.get("profile_path") or str(APP_PROFILES_DIR / prof_name)
    Path(prof_path).mkdir(parents=True, exist_ok=True)

    chrome_exe = find_system_chrome_exe()
    if not chrome_exe:
        raise RuntimeError("Không tìm thấy Google Chrome trên hệ thống.")

    proxy_config = _parse_channel_proxy(channel)

    # Nếu Proxy có xác thực User/Password (4 thành phần), mở qua Playwright để xác thực tự động không bị lỗi mạng
    if proxy_config and proxy_config["username"]:
        def _launch_playwright_login():
            from playwright.sync_api import sync_playwright
            try:
                with _get_profile_lock(prof_path):
                    with sync_playwright() as p:
                        ctx = _launch_persistent_context_guarded(
                            p,
                            {
                                "user_data_dir": prof_path,
                                "executable_path": chrome_exe,
                                "headless": False,
                                "args": ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
                                "ignore_default_args": ["--enable-automation"],
                                "proxy": {
                                    "server": f"http://{proxy_config['host']}:{proxy_config['port']}",
                                    "username": proxy_config["username"],
                                    "password": proxy_config["password"],
                                },
                                "viewport": {"width": 1366, "height": 768},
                            },
                            prof_name,
                        )
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        page.add_init_script(STEALTH_JS)
                        page.goto("https://studio.youtube.com")
                        try:
                            page.wait_for_event("close", timeout=0)
                        except Exception:
                            pass
                        try:
                            ctx.close()
                        except Exception as close_err:
                            _log_optional_step_failure("đóng Chrome sau khi mở đăng nhập", close_err, channel, {})
            except Exception as e:
                add_log(
                    "Browser",
                    f"Lỗi mở Chrome Profile '{prof_name}' để đăng nhập (qua Proxy): {e}. Nếu Profile đang mở ở Chrome khác, hãy đóng lại rồi thử.",
                    "ERROR",
                    channel_name=channel.get("name", ""),
                )

        import threading
        t = threading.Thread(target=_launch_playwright_login, daemon=True)
        t.start()
        add_log("Browser", f"Đã mở Chrome Profile '{prof_name}' kèm Proxy Auth tại studio.youtube.com", "INFO", channel.get("name", ""))
        return {"ok": True, "profile_path": prof_path, "message": f"Đã mở Chrome Profile kèm Proxy: {prof_name}"}

    args = [
        chrome_exe,
        f"--user-data-dir={prof_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "https://studio.youtube.com",
    ]
    if proxy_config:
        args.insert(-1, f"--proxy-server=http://{proxy_config['host']}:{proxy_config['port']}")
    with _get_profile_lock(prof_path):
        subprocess.Popen(args)
    add_log("Browser", f"Đã mở Chrome Profile '{prof_name}' tại studio.youtube.com", "INFO", channel.get("name", ""))
    return {"ok": True, "profile_path": prof_path, "message": f"Đã mở Chrome Profile: {prof_name}"}


_CHANNEL_ID_PATTERN = r"UC[A-Za-z0-9_-]{22}"
_STUDIO_CHANNEL_ID_RE = re.compile(rf"/channel/({_CHANNEL_ID_PATTERN})", re.IGNORECASE)
_CHANNEL_ID_SEARCH_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])({_CHANNEL_ID_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_ACCOUNT_HANDLE_RE = re.compile(r"@[\w.\-]{2,}")
_ACCOUNT_LABEL_SKIP = {
    "tài khoản", "tài khoản của bạn", "your account", "account",
    "kênh", "channel", "kênh youtube", "youtube channel",
}


def normalize_channel_key(value: Any) -> str:
    """Chuẩn hóa tên/handle để đối chiếu tên kênh giữa DB và YouTube Studio."""
    text = str(value or "").strip().casefold().lstrip("@")
    return re.sub(r"\s+", " ", text).strip()


def _channel_identity_matches(target: Dict[str, Any], name: str, handle: str) -> bool:
    target_keys = {
        key for key in (
            normalize_channel_key(target.get("name")),
            normalize_channel_key(target.get("handle")),
        ) if key
    }
    entry_keys = {
        key for key in (
            normalize_channel_key(name),
            normalize_channel_key(handle),
        ) if key
    }
    return bool(target_keys & entry_keys)


def _extract_channel_id(raw: str) -> str:
    match = _CHANNEL_ID_SEARCH_RE.search(raw or "")
    return match.group(1) if match else ""


def _no_channel_or_redirect_message(page) -> str:
    """Studio không mở (redirect khỏi studio.youtube.com) sau khi đã đăng nhập Google:
    phân biệt rõ 'chưa có kênh' (form Tạo kênh) với lỗi khác để người dùng biết phải làm gì.
    Dò qua body.innerText vì form Tạo kênh nằm trong shadow DOM (locator text= không thấy)."""
    cur_url = (page.url or "").strip()
    body_text = ""
    try:
        # Form "Tạo kênh" render chậm hơn redirect — chờ thêm trước khi dò.
        page.wait_for_timeout(2000)
        body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        body_text = ""
    lowered = body_text.lower()
    if "tạo kênh" in lowered or "create a channel" in lowered:
        return (
            "Tài khoản Google này CHƯA CÓ kênh YouTube nào (Studio chuyển về form 'Tạo kênh'). "
            "Hãy tạo kênh trong profile rồi quét lại."
        )
    return (
        f"Đã đăng nhập Google nhưng Studio không mở được (đang ở: {cur_url[:90]}). "
        "Hãy mở profile, vào Studio thủ công một lần rồi quét lại."
    )


def _resolve_channel_id_by_handle(page, handle: str) -> str:
    """Đọc ID kênh thật qua trang youtube.com/@handle (tab do tool tạo, đọc thụ động qua
    ytcfg.CHANNEL_ID của trang kênh). Dùng cho kênh KHÔNG active mà hộp chuyển tài khoản
    không trả browseId (Studio build mới). Navigate đi rồi quay về Studio, không đổi state."""
    h = (handle or "").strip()
    if not h:
        return ""
    if not h.startswith("@"):
        h = "@" + h
    original_url = page.url or "https://studio.youtube.com"
    try:
        page.goto(f"https://www.youtube.com/{quote(h)}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        # Thứ tự tin cậy: canonical link → og:url → meta identifier → ytcfg.
        # (ytcfg.CHANNEL_ID đã xác minh rỗng trên youtube.com build 2026-11.)
        blob = page.evaluate(
            """() => {
                try {
                    const parts = [];
                    const canonical = document.querySelector('link[rel="canonical"]');
                    if (canonical && canonical.href) parts.push(canonical.href);
                    const og = document.querySelector('meta[property="og:url"]');
                    if (og && og.content) parts.push(og.content);
                    const ident = document.querySelector('meta[itemprop="identifier"]');
                    if (ident && ident.content) parts.push(ident.content);
                    try {
                        const cfg = window.ytcfg;
                        if (cfg && cfg.get) parts.push(String(cfg.get('CHANNEL_ID') || ''));
                    } catch (e) {}
                    return parts.join('\\n');
                } catch (e) { return ''; }
            }"""
        ) or ""
        cid = _extract_channel_id(blob)
        if not cid:
            cid = _extract_channel_id(page.url or "")
        return cid
    except Exception:
        return ""
    finally:
        try:
            page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def _read_studio_channel_id(page) -> str:
    match = _STUDIO_CHANNEL_ID_RE.search(page.url or "")
    if match:
        return match.group(1)
    try:
        cfg_blob = page.evaluate(
            """() => {
                try {
                    const cfg = window.ytcfg;
                    if (!cfg || !cfg.get) return '';
                    return [cfg.get('CHANNEL_ID'), cfg.get('DATASYNC_ID')].filter(Boolean).join(' ');
                } catch (e) { return ''; }
            }"""
        )
    except Exception:
        cfg_blob = ""
    return _extract_channel_id(cfg_blob or "")


def _read_active_studio_account(page) -> Dict[str, str]:
    """Đọc kênh đang hoạt động từ header menu tài khoản Studio (đã kiểm chứng DOM thật)."""
    result = {"name": "", "handle": "", "email": "", "channel_id": _read_studio_channel_id(page)}
    try:
        header = page.locator("ytd-active-account-header-renderer").first
        raw = header.inner_text() if (header.count() > 0 and header.is_visible()) else ""
    except Exception:
        raw = ""
    if not raw:
        return result

    handles = _ACCOUNT_HANDLE_RE.findall(raw)
    result["handle"] = handles[0] if handles else ""
    email_match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw)
    if email_match:
        result["email"] = email_match.group(0).strip()

    for line in raw.splitlines():
        cleaned = _ACCOUNT_HANDLE_RE.sub("", line).strip(" -–—|•")
        if not cleaned or "@" in cleaned:
            continue
        if result["email"] and cleaned.lower() == result["email"].lower():
            continue
        if cleaned.casefold() in _ACCOUNT_LABEL_SKIP:
            continue
        result["name"] = cleaned
        break
    return result


def _has_visible_account_items(page) -> bool:
    try:
        items = page.locator("ytd-account-item-renderer")
        for idx in range(min(items.count(), 5)):
            if items.nth(idx).is_visible():
                return True
    except Exception:
        pass
    return False


def _close_studio_account_menus(page):
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            break


# Nút "Tạo" trong Studio: phải khớp nhãn CHÍNH XÁC. Dạng tìm chuỗi con
# [aria-label*='Tạo' i] từng bấm nhầm "Trò chuyện với Bộ phận hỗ trợ nhà sáng tạo".
STUDIO_CREATE_BUTTON_SELECTOR = (
    "[aria-label='Tạo'], [aria-label='Create'], "
    "#create-icon, ytcp-button#create-icon, ytcp-button.ytcpAppHeaderCreateIcon"
)

# Con trỏ tạm trên window để CDP lấy đúng node input mà Playwright đã phân giải.
_UPLOAD_INPUT_MARKER = "__dhbUploadFileInput"

# Video dài/4K có thể còn xử lý sau khi bấm Xuất bản; hộp thoại chia sẻ chỉ hiện
# khi Studio hoàn tất nên cần chờ lâu hơn thay vì kết luận thất bại sau 20 giây.
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 300


def attach_upload_file(page, file_input, file_path) -> None:
    """Nạp file video vào <input type=file> của Studio.

    Playwright 1.62 từ chối set_input_files với file >50MB khi trình duyệt được
    gắn qua CDP (browser không được đánh dấu đồng vị trí với driver). Chrome đọc
    thẳng từ ổ đĩa nên DOM.setFileInputFiles không có giới hạn đó; vì vậy ưu tiên
    CDP và chỉ dùng set_input_files cho trường hợp không mở được CDP session.
    """
    resolved_path = str(Path(str(file_path)).resolve())
    try:
        file_input.evaluate(
            "(el, marker) => { window[marker] = el; }", _UPLOAD_INPUT_MARKER
        )
        session = page.context.new_cdp_session(page)
        try:
            session.send("DOM.enable")
            session.send("Runtime.enable")
            evaluated = session.send(
                "Runtime.evaluate",
                {
                    "expression": f"window[{_UPLOAD_INPUT_MARKER!r}]",
                    "objectGroup": "DHBUpload",
                },
            )
            object_id = (evaluated.get("result") or {}).get("objectId")
            if not object_id:
                raise RuntimeError("Không lấy được node input tải file qua CDP.")
            session.send(
                "DOM.setFileInputFiles", {"objectId": object_id, "files": [resolved_path]}
            )
        finally:
            try:
                file_input.evaluate(
                    "(el, marker) => { delete window[marker]; }", _UPLOAD_INPUT_MARKER
                )
            except Exception:
                pass
            try:
                session.detach()
            except Exception:
                pass
        return
    except Exception as cdp_error:
        try:
            file_input.set_input_files(resolved_path, timeout=30000)
        except Exception as fallback_error:
            raise RuntimeError(
                f"Không nạp được file tải lên: CDP lỗi ({cdp_error}); "
                f"set_input_files dự phòng cũng lỗi ({fallback_error})"
            ) from fallback_error


def _open_studio_account_menu(page) -> bool:
    try:
        header = page.locator("ytd-active-account-header-renderer").first
        if header.count() > 0 and header.is_visible():
            return True
        if _has_visible_account_items(page):
            _close_studio_account_menus(page)
        avatar = page.locator("#avatar-btn, ytcp-button#avatar-btn, img#avatar-btn").first
        # Studio là SPA: nút avatar hiện sau domcontentloaded, càng chậm khi cửa sổ bị dời
        # ra ngoài màn hình. Chờ nút hiện rồi mới bấm để không bỏ cuộc ở lần đọc đầu tiên.
        try:
            page.wait_for_selector("#avatar-btn, ytcp-button#avatar-btn, img#avatar-btn", timeout=20000)
        except Exception:
            return False
        human_move_and_click(page, avatar, delay_after=1400)
        page.wait_for_timeout(1200)
        page.wait_for_selector("ytd-active-account-header-renderer", timeout=8000)
    except Exception:
        pass
    try:
        header = page.locator("ytd-active-account-header-renderer").first
        return header.count() > 0 and header.is_visible()
    except Exception:
        return False


def _open_studio_switch_account_list(page) -> bool:
    switch_item = page.locator(
        "ytd-compact-link-renderer:has-text('Chuyển đổi tài khoản'), "
        "ytd-compact-link-renderer:has-text('Switch account'), "
        "tp-yt-paper-item:has-text('Chuyển đổi tài khoản'), "
        "tp-yt-paper-item:has-text('Switch account')"
    ).first
    try:
        if switch_item.count() == 0 or not switch_item.is_visible():
            return False
        human_move_and_click(page, switch_item, delay_after=1600)
        page.wait_for_timeout(1400)
        page.wait_for_selector("ytd-account-item-renderer", timeout=8000)
    except Exception as switch_err:
        _log_optional_step_failure("mở danh sách chuyển tài khoản Studio", switch_err, {}, {})
    try:
        return page.locator("ytd-account-item-renderer").count() > 0
    except Exception:
        return False


def _list_studio_accounts(page) -> list:
    entries = []
    items = page.locator("ytd-account-item-renderer")
    try:
        item_count = min(items.count(), 50)
    except Exception:
        return entries

    for idx in range(item_count):
        item = items.nth(idx)
        try:
            if not item.is_visible():
                continue
            title_el = item.locator("#channel-title").first
            name = (title_el.inner_text() or "").strip() if title_el.count() > 0 else ""
            raw = item.inner_text() or ""
        except Exception as item_err:
            _log_optional_step_failure(
                f"đọc kênh #{idx} trong hộp chuyển tài khoản", item_err, {}, {}
            )
            continue

        handles = _ACCOUNT_HANDLE_RE.findall(raw)
        handle = handles[0] if handles else ""
        if not name:
            for line in raw.splitlines():
                cleaned = _ACCOUNT_HANDLE_RE.sub("", line).strip(" -–—|•")
                if cleaned and "@" not in cleaned and cleaned.casefold() not in _ACCOUNT_LABEL_SKIP:
                    name = cleaned
                    break

        is_active = False
        try:
            is_active = "enable-ring-for-active-account" in (item.get_attribute("class") or "")
            if not is_active:
                selected = item.locator("yt-icon#selected").first
                is_active = selected.count() > 0 and selected.is_visible()
        except Exception:
            pass

        channel_id = ""
        try:
            channel_id = item.evaluate(
                """el => {
                    try {
                        const d = el.data || el.__data || {};
                        const be = d.navigationEndpoint && d.navigationEndpoint.browseEndpoint;
                        return (be && be.browseId) || '';
                    } catch (e) { return ''; }
                }"""
            ) or ""
        except Exception:
            channel_id = ""
        if not channel_id:
            try:
                blobs = [
                    item.get_attribute("data-channel-id") or "",
                    item.get_attribute("data-account-id") or "",
                    item.inner_html() or "",
                ]
                try:
                    blobs.extend(item.evaluate(
                        "el => Array.from(el.attributes).map(a => a.value)"
                    ) or [])
                except Exception:
                    pass
                link_el = item.locator("a[href*='/channel/']").first
                if link_el.count() > 0:
                    blobs.append(link_el.get_attribute("href") or "")
                channel_id = _extract_channel_id("\n".join(blobs))
            except Exception:
                channel_id = ""
        if channel_id:
            channel_id = _extract_channel_id(channel_id)

        entries.append({
            "name": name,
            "handle": handle,
            "channel_id": channel_id,
            "is_active": is_active,
            "index": idx,
        })
    return entries


def _is_radio_checked(radio) -> bool:
    try:
        if (radio.get_attribute("aria-checked") or "").lower() == "true":
            return True
    except Exception:
        pass
    try:
        return bool(radio.evaluate("el => !!(el.checked || el.hasAttribute('checked'))"))
    except Exception:
        return False


def _set_radio_and_verify(page, radio, expected: bool, label: str):
    """Click a metadata radio when needed and wait for its selected DOM state."""
    if radio.count() == 0 or not radio.is_visible():
        raise RuntimeError(f"Không tìm thấy lựa chọn '{label}' trên YouTube Studio.")
    try:
        current = radio.get_attribute("aria-checked")
        current_state = current.lower() == "true" if current is not None else None
    except Exception:
        current_state = None
    if current_state is not bool(expected):
        human_move_and_click(page, radio, delay_after=0)
    try:
        handle = radio.element_handle()
        if handle is None:
            raise RuntimeError("DOM control không còn tồn tại sau thao tác.")
        page.wait_for_function(
            """({el, expected}) => {
                if (!el || !el.isConnected) return false;
                const raw = el.getAttribute('aria-checked');
                if (raw !== null) return (raw.toLowerCase() === 'true') === expected;
                if ('checked' in el) return !!el.checked === expected;
                return false;
            }""",
            arg={"el": handle, "expected": bool(expected)},
            timeout=5000,
            polling="raf",
        )
    except Exception as exc:
        state = _is_radio_checked(radio)
        if state != bool(expected):
            raise RuntimeError(f"Chưa xác minh được lựa chọn '{label}' trên YouTube Studio.") from exc


def _schedule_values_match(date_value: str, time_value: str, expected) -> bool:
    """Compare Studio's read-back values with the requested local schedule."""
    date_value = str(date_value or "").strip()
    time_value = str(time_value or "").strip()
    parsed_date = None
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            parsed_date = datetime.strptime(date_value, fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        try:
            parsed_date = datetime.fromisoformat(date_value).date()
        except ValueError:
            return False
    parsed_time = None
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            parsed_time = datetime.strptime(time_value, fmt).time()
            break
        except ValueError:
            continue
    return bool(parsed_time and parsed_date == expected.date() and parsed_time.hour == expected.hour and parsed_time.minute == expected.minute)


def _verify_studio_channel_id(active: Dict[str, str], channel: Dict[str, Any]):
    expected = (channel.get("channel_id") or "").strip()
    actual = (active.get("channel_id") or "").strip()
    if not expected:
        raise RuntimeError(
            f"Kênh đích '{channel.get('name') or channel.get('handle') or '?'}' chưa có Channel ID. "
            "Không thể xác minh chính xác kênh nhận video — hãy bấm 'Refresh Kênh' hoặc nhập Channel ID "
            "trong tab Quản Lý Kênh trước khi đăng."
        )
    if not actual:
        raise RuntimeError(
            f"Không đọc được Channel ID của kênh đang hoạt động trên Studio (kênh đích: {expected}). "
            "Đã dừng để tránh đăng nhầm kênh."
        )
    if expected != actual:
        raise RuntimeError(
            f"Channel ID không khớp: Studio đang mở kênh '{actual}' nhưng kênh đích cấu hình là '{expected}'. "
            "Đã dừng để tránh đăng nhầm kênh — hãy kiểm tra lại Channel ID trong tab Quản Lý Kênh."
        )


def _open_fresh_studio_page(context, init_scripts: Optional[list] = None):
    new_page = context.new_page()
    for script in init_scripts or []:
        try:
            new_page.add_init_script(script)
        except Exception as script_err:
            _log_optional_step_failure("nạp script chống nhận diện vào tab Studio mới", script_err, {}, {})
    new_page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)
    new_page.wait_for_timeout(4000)
    try:
        cont_btn = new_page.locator(
            "a:has-text('Continue to Studio'), button:has-text('Continue to Studio'), "
            "a:has-text('Tiếp tục tới Studio'), button:has-text('Tiếp tục tới Studio'), "
            "[aria-label*='Continue to Studio' i]"
        ).first
        if cont_btn.count() > 0 and cont_btn.is_visible():
            cont_btn.click()
            new_page.wait_for_timeout(3000)
    except Exception as cont_err:
        _log_optional_step_failure("bấm Continue to Studio ở tab mới", cont_err, {}, {})
    if "accounts.google.com" in new_page.url or "signin" in new_page.url.lower():
        raise RuntimeError("Mất phiên đăng nhập Google khi chuyển kênh. Hãy đăng nhập lại Profile rồi thử lại.")
    return new_page


def select_studio_channel(
    page,
    channel: Dict[str, Any],
    context=None,
    init_scripts: Optional[list] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> tuple:
    """
    Chọn đúng kênh con trên YouTube Studio trước khi upload và xác minh lại kênh đang hoạt động.
    Sau khi chuyển kênh, mở tab Studio mới (tab cũ giữ trạng thái tài khoản cũ) và trả về tab đó.
    Fail-closed: không tìm thấy / không xác minh được kênh đích thì dừng, tuyệt đối không đăng bừa.
    Trả về (page, thông tin kênh đang hoạt động).
    """
    target_name = (channel.get("name") or "").strip()
    target_handle = (channel.get("handle") or "").strip()
    target_id = (channel.get("channel_id") or "").strip()
    label = target_name or target_handle or target_id or "không rõ"

    if not target_name and not target_handle:
        raise RuntimeError(
            f"Kênh đích '{label}' chưa có Tên kênh hoặc Handle để đối chiếu với YouTube Studio. "
            "Hãy cập nhật đúng tên kênh con trong tab Quản Lý Kênh trước khi đăng."
        )

    if not target_id:
        raise RuntimeError(
            f"Kênh đích '{label}' chưa có Channel ID. Không thể xác minh chính xác kênh nhận video — "
            "hãy bấm 'Refresh Kênh' hoặc nhập Channel ID trong tab Quản Lý Kênh trước khi đăng."
        )

    if progress_callback:
        progress_callback(24, f"Xác minh kênh YouTube đích: {label} ({target_id})...")

    if not _open_studio_account_menu(page):
        raise RuntimeError(
            "Không mở được menu tài khoản YouTube Studio để xác minh kênh đích. "
            "Đã dừng để tránh đăng nhầm kênh."
        )

    active = _read_active_studio_account(page)
    if active.get("channel_id") == target_id:
        _close_studio_account_menus(page)
        return page, active
    if _channel_identity_matches(channel, active.get("name", ""), active.get("handle", "")):
        _verify_studio_channel_id(active, channel)
        _close_studio_account_menus(page)
        return page, active

    if not _open_studio_switch_account_list(page):
        _close_studio_account_menus(page)
        raise RuntimeError(
            f"Kênh đang hoạt động là '{active.get('name') or 'không xác định'}' và không mở được danh sách kênh "
            f"để chuyển sang '{label}'. Đã dừng để tránh đăng nhầm kênh."
        )

    entries = _list_studio_accounts(page)
    matched = next(
        (entry for entry in entries if entry.get("channel_id") == target_id),
        None,
    )
    if matched is None:
        # Chỉ nhận entry khớp tên/handle khi CHƯA đọc được ID; entry có ID khác đã bị loại.
        matched = next(
            (
                entry for entry in entries
                if not entry.get("channel_id")
                and _channel_identity_matches(channel, entry["name"], entry["handle"])
            ),
            None,
        )
    if not matched:
        available = ", ".join(
            (entry["name"] or entry["handle"] or "?")
            + (f" ({entry['handle']})" if entry["name"] and entry["handle"] else "")
            for entry in entries
        ) or "không đọc được kênh nào"
        _close_studio_account_menus(page)
        raise RuntimeError(
            f"Không tìm thấy kênh đích '{label}' trong Profile Chrome này. Các kênh đang có: {available}. "
            "Đã dừng để tránh đăng nhầm kênh — hãy sửa Tên kênh/Handle trong tab Quản Lý Kênh cho khớp."
        )

    if not matched["is_active"]:
        if progress_callback:
            progress_callback(26, f"Chuyển tài khoản sang kênh: {matched['name'] or label}...")
        human_move_and_click(
            page,
            page.locator("ytd-account-item-renderer").nth(matched["index"]),
            delay_after=2500,
        )
        page.wait_for_timeout(1500)
        if context is not None:
            page = _open_fresh_studio_page(context, init_scripts)

    verified: Dict[str, str] = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        if _open_studio_account_menu(page):
            candidate = _read_active_studio_account(page)
            if candidate.get("channel_id") == target_id:
                verified = candidate
                break
            if candidate.get("name") or candidate.get("handle"):
                verified = candidate
                break
        page.wait_for_timeout(1500)

    _close_studio_account_menus(page)

    if not (verified.get("channel_id") or verified.get("name") or verified.get("handle")):
        raise RuntimeError(
            f"Đã chọn kênh '{label}' nhưng không đọc lại được kênh đang hoạt động để xác minh. "
            "Đã dừng để bảo đảm an toàn."
        )

    _verify_studio_channel_id(verified, channel)
    return page, verified


def _collect_studio_channels(page) -> tuple:
    """Quét kênh trong hộp chuyển tài khoản Studio; trả về (active, danh sách kênh)."""
    active = _read_active_studio_account(page)
    entries = []
    # Xác minh thật 2026-11-02: mở hộp chuyển tài khoản thỉnh thoảng hụt do timing
    # (Studio vừa load, overlay chưa sẵn sàng) — thử tối đa 2 lần trước khi bỏ cuộc.
    for attempt in (1, 2):
        if _open_studio_account_menu(page):
            active = _read_active_studio_account(page)
            if _open_studio_switch_account_list(page):
                entries = _list_studio_accounts(page)
                if entries:
                    break
        if attempt == 1:
            _close_studio_account_menus(page)
            try:
                page.wait_for_timeout(2000)
            except Exception:
                pass

    current_email = active.get("email", "")
    channels = [
        {
            "name": entry["name"],
            "handle": entry["handle"],
            "channel_id": entry.get("channel_id", ""),
            "email": current_email,
        }
        for entry in entries
        if entry["name"] or entry["handle"] or entry.get("channel_id")
    ][:50]

    # Xác minh thật 2026-11-02 (Studio build mới): item trong hộp chuyển tài khoản không
    # trả browseId qua el.data — nhưng ID kênh ĐANG HOẠT ĐỘNG đọc được từ URL/ytcfg.
    # Gắn cho đúng item: khớp handle/tên, hoặc khi hộp chỉ có đúng 1 kênh.
    active_id = (active.get("channel_id") or "").strip()
    if active_id and channels:
        active_name = (active.get("name") or "").strip().casefold()
        active_handle = (active.get("handle") or "").strip()
        for ch in channels:
            if ch.get("channel_id"):
                continue
            matches_active = (
                (active_handle and (ch.get("handle") or "").strip() == active_handle)
                or (active_name and (ch.get("name") or "").strip().casefold() == active_name)
            )
            if matches_active or len(channels) == 1:
                ch["channel_id"] = active_id

    # Kênh KHÔNG active không có ID từ hộp chuyển tài khoản — tra qua youtube.com/@handle
    # (tối đa 5 kênh/lượt để không kéo dài lượt quét).
    unresolved = [ch for ch in channels if not (ch.get("channel_id") or "").strip() and (ch.get("handle") or "").strip()]
    for ch in unresolved[:5]:
        cid = _resolve_channel_id_by_handle(page, ch["handle"])
        if cid:
            ch["channel_id"] = cid

    if not channels and (active.get("name") or active.get("handle")):
        channels = [{
            "name": active.get("name", ""),
            "handle": active.get("handle", ""),
            "channel_id": active.get("channel_id", ""),
            "email": current_email,
        }]
    return active, channels


def _probe_antidetect_login(profile_id: str, grab_channels: bool = True) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "logged_in": False,
        "email": "",
        "channel_name": "",
        "channels": [],
        "message": "",
        "antidetect": True,
    }
    try:
        # Cùng khóa với luồng upload: đang upload trên profile này thì quét Studio phải chờ,
        # và ngược lại — tránh 2 lease CDP tranh cùng một Chrome của Antidetect.
        with _get_antidetect_lock(profile_id):
            with AntidetectBrowserLease(profile_id, stop_if_started=True, hide_window=True) as lease:
                page = lease.page
                page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)

                try:
                    cont_btn = page.locator(
                        "a:has-text('Continue to Studio'), button:has-text('Continue to Studio'), "
                        "a:has-text('Tiếp tục tới Studio'), button:has-text('Tiếp tục tới Studio'), "
                        "[aria-label*='Continue to Studio' i]"
                    ).first
                    if cont_btn.count() > 0 and cont_btn.is_visible():
                        cont_btn.click()
                        page.wait_for_timeout(2500)
                except Exception as cont_err:
                    _log_optional_step_failure("bấm Continue to Studio khi kiểm tra đăng nhập Antidetect", cont_err, {}, {})

                if "accounts.google.com" in page.url or "signin" in page.url.lower():
                    result["message"] = "Profile Antidetect này chưa đăng nhập tài khoản Google/YouTube."
                    return result
                if "studio.youtube.com" not in (page.url or ""):
                    # Xác minh thật 2026-11-02: tài khoản Google chưa có kênh → Studio redirect
                    # về youtube.com kèm form "Tạo kênh". Báo đúng nguyên nhân thay vì báo lỗi DOM.
                    result["message"] = _no_channel_or_redirect_message(page)
                    # Google session vẫn còn (không bị đá về trang đăng nhập) — chỉ là chưa có kênh.
                    result["logged_in"] = True
                    return result

                result["logged_in"] = True
                result["message"] = "Profile Antidetect đã đăng nhập Google."
                if not grab_channels:
                    return result

                try:
                    active, channels = _collect_studio_channels(page)
                    result["channels"] = channels
                    result["email"] = active.get("email", "")
                    result["channel_id"] = active.get("channel_id", "")
                    if channels:
                        result["message"] = (
                            "Profile Antidetect đã đăng nhập Google"
                            + (f" ({result['email']})" if result["email"] else "")
                            + f", phát hiện {len(channels)} kênh YouTube."
                        )
                    else:
                        result["message"] = (
                            "Profile Antidetect đã đăng nhập Google nhưng không đọc được "
                            "danh sách kênh trong hộp chuyển tài khoản."
                        )
                except Exception as parse_err:
                    result["message"] = (
                        f"Profile Antidetect đã đăng nhập Google nhưng không quét được danh sách kênh: {parse_err}"
                    )
    except Exception as e:
        raise RuntimeError(f"Không kiểm tra được Profile Antidetect {profile_id}: {e}") from e
    return result


def probe_profile_login(
    prof_path: str,
    proxy: str = "",
    grab_channels: bool = True,
    antidetect_profile_id: str = "",
) -> Dict[str, Any]:
    """
    Mở Chrome Profile ở vị trí cửa sổ khuất để KIỂM TRA trạng thái đăng nhập Google/YouTube,
    đồng thời quét danh sách kênh YouTube thật trong Profile qua hộp chuyển tài khoản của Studio.
    Luôn chạy định danh Windows Desktop nguyên bản (không ép Mobile UA) để đồng nhất fingerprint.
    Profile Antidetect được kiểm tra qua CDP trên chính trình duyệt do Antidetect quản lý.
    """
    if not antidetect_profile_id and str(prof_path or "").lower().startswith("antidetect://"):
        antidetect_profile_id = str(prof_path).split("antidetect://", 1)[1].split("/", 1)[0].strip()
    if antidetect_profile_id:
        return _probe_antidetect_login(antidetect_profile_id, grab_channels=grab_channels)

    from playwright.sync_api import sync_playwright

    chrome_exe = find_system_chrome_exe()
    if not chrome_exe:
        raise RuntimeError("Không tìm thấy Google Chrome trên hệ thống.")
    Path(prof_path).mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {
        "logged_in": False,
        "email": "",
        "channel_name": "",
        "channels": [],
        "message": "",
    }

    ctx_kwargs: Dict[str, Any] = {
        "user_data_dir": prof_path,
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-position=-3200,-3200",
        ],
        "ignore_default_args": ["--enable-automation"],
        "viewport": {"width": 1366, "height": 768},
        "executable_path": chrome_exe,
    }
    proxy_config = _parse_channel_proxy({"proxy": proxy})
    if proxy_config:
        ctx_kwargs["proxy"] = {
            "server": f"http://{proxy_config['host']}:{proxy_config['port']}"
        }
        if proxy_config["username"]:
            ctx_kwargs["proxy"].update({
                "username": proxy_config["username"],
                "password": proxy_config["password"],
            })

    prof_name = Path(prof_path).name
    prof_lock = _get_profile_lock(prof_path)
    with prof_lock:
        with sync_playwright() as p:
            context = _launch_persistent_context_guarded(p, ctx_kwargs, prof_name)
            page = None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(STEALTH_JS)
                page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3500)

                try:
                    cont_btn = page.locator(
                        "a:has-text('Continue to Studio'), button:has-text('Continue to Studio'), "
                        "a:has-text('Tiếp tục tới Studio'), [aria-label*='Continue to Studio' i]"
                    ).first
                    if cont_btn.count() > 0 and cont_btn.is_visible():
                        cont_btn.click()
                        page.wait_for_timeout(2500)
                except Exception as cont_err:
                    _log_optional_step_failure("bấm Continue to Studio khi kiểm tra đăng nhập", cont_err, {}, {})

                if "accounts.google.com" in page.url or "signin" in page.url.lower():
                    result["message"] = "Profile này chưa đăng nhập tài khoản Google/YouTube."
                    return result
                if "studio.youtube.com" not in (page.url or ""):
                    # Cùng phân biệt với Antidetect: đăng nhập rồi nhưng chưa có kênh → form Tạo kênh.
                    result["message"] = _no_channel_or_redirect_message(page)
                    result["logged_in"] = True
                    return result

                result["logged_in"] = True
                result["message"] = "Profile đã đăng nhập Google."
                if not grab_channels:
                    return result

                # Mở hộp chuyển tài khoản (Account Switcher) đã kiểm chứng DOM thật để liệt kê chính xác từng kênh
                try:
                    active, channels = _collect_studio_channels(page)
                    current_email = active.get("email", "")
                    result["channels"] = channels
                    result["email"] = current_email
                    result["channel_id"] = active.get("channel_id", "")
                    if result["channels"]:
                        result["message"] = (
                            f"Profile đã đăng nhập Google"
                            + (f" ({current_email})" if current_email else "")
                            + f", phát hiện {len(result['channels'])} kênh YouTube."
                        )
                    else:
                        result["message"] = "Profile đã đăng nhập Google nhưng không đọc được danh sách kênh trong hộp chuyển tài khoản."
                except Exception as parse_err:
                    result["message"] = f"Profile đã đăng nhập Google nhưng không quét được danh sách kênh: {parse_err}"
                return result
            finally:
                if page is not None:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                try:
                    context.close()
                except Exception as close_err:
                    _log_optional_step_failure("đóng Chrome sau khi kiểm tra đăng nhập", close_err, {}, {})


def _parse_oauth_payload(raw: str) -> Dict[str, Any]:
    """Đọc OAuth token JSON (hoặc Access Token thô) do người dùng dán vào kênh."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception as e:
            raise ValueError(
                f"OAuth token JSON không hợp lệ ({e}). Cần dạng "
                '{"client_id":"...","client_secret":"...","refresh_token":"..."}.'
            )
        return data if isinstance(data, dict) else {}
    return {"access_token": raw}


def get_oauth_token_local_status(channel: Dict[str, Any]) -> Dict[str, Any]:
    """Trạng thái OAuth token đọc từ DB (không gọi mạng)."""
    raw = (channel.get("oauth_token_json") or "").strip()
    status: Dict[str, Any] = {
        "configured": bool(raw),
        "has_access_token": False,
        "has_refresh_token": False,
        "has_client_credentials": False,
        "expires_at": "",
        "expires_in_seconds": None,
        "expired": None,
        "error": "",
    }
    if not raw:
        status["error"] = "Chưa cấu hình OAuth token JSON cho kênh này."
        return status
    try:
        data = _parse_oauth_payload(raw)
    except ValueError as e:
        status["error"] = str(e)
        return status

    status["has_access_token"] = bool(data.get("access_token"))
    status["has_refresh_token"] = bool(data.get("refresh_token"))
    status["has_client_credentials"] = bool(data.get("client_id") and data.get("client_secret"))

    created = data.get("created") or data.get("issued_at") or 0
    expires_in = data.get("expires_in") or 3600
    if status["has_access_token"] and created:
        try:
            expires_epoch = float(created) + float(expires_in)
        except (TypeError, ValueError):
            expires_epoch = 0
        if expires_epoch:
            status["expires_at"] = datetime.fromtimestamp(expires_epoch).strftime("%Y-%m-%d %H:%M:%S")
            status["expires_in_seconds"] = int(expires_epoch - time.time())
            status["expired"] = expires_epoch <= time.time()

    if status["has_refresh_token"] and not status["has_client_credentials"]:
        status["error"] = "Có refresh_token nhưng thiếu client_id/client_secret — không thể tự làm mới token."
    elif not status["has_refresh_token"] and status["expired"]:
        status["error"] = (
            "Access token đã hết hạn và không có refresh_token. Hãy dán JSON có refresh_token "
            "(client_id, client_secret, refresh_token) để tool tự gia hạn."
        )
    elif not status["has_access_token"] and not status["has_refresh_token"]:
        status["error"] = "OAuth token JSON thiếu cả access_token lẫn refresh_token."
    return status


def _parse_channel_proxy(channel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse the documented HOST:PORT[:USER:PASS] form or fail closed."""
    raw = str(channel.get("proxy") or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 4):
        raise ValueError("Proxy không đúng định dạng HOST:PORT hoặc HOST:PORT:USER:PASS.")
    host, raw_port = parts[0].strip(), parts[1].strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Host proxy không hợp lệ; proxy IPv6 hoặc ký tự lạ chưa được hỗ trợ.")
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port proxy phải là số từ 1 đến 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Port proxy phải là số từ 1 đến 65535.")
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise ValueError("Host proxy không hợp lệ.") from exc
        if (
            not host
            or len(host) > 253
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split("."))
        ):
            raise ValueError("Host proxy không hợp lệ.")
    username = password = ""
    if len(parts) == 4:
        username, password = parts[2].strip(), parts[3].strip()
        if not username or not password:
            raise ValueError("Proxy có xác thực phải có đủ username và password.")
    return {"host": host, "port": port, "username": username, "password": password}


def _proxy_url_from_channel(channel: Dict[str, Any]) -> str:
    """Chuyển proxy kênh thành URL requests; cấu hình sai không được bỏ qua."""
    parsed = _parse_channel_proxy(channel)
    if not parsed:
        return ""
    auth = ""
    if parsed["username"]:
        auth = f"{quote(parsed['username'], safe='')}:{quote(parsed['password'], safe='')}@"
    return f"http://{auth}{parsed['host']}:{parsed['port']}"


def _http_session_for_channel(channel: Dict[str, Any]) -> requests.Session:
    """Session HTTP dùng đúng proxy của kênh cho toàn bộ upload qua YouTube Data API."""
    session = requests.Session()
    proxy_url = _proxy_url_from_channel(channel)
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def check_oauth_token_status(channel: Dict[str, Any]) -> Dict[str, Any]:
    """Làm mới token thật + xác minh kênh mà token đại diện. Trả dict thay vì raise để UI hiển thị rõ."""
    local = get_oauth_token_local_status(channel)
    result: Dict[str, Any] = {"ok": False, "local": local, "error": "", "identity": None, "matched": False}
    if not local["configured"]:
        result["error"] = local["error"]
        return result
    session = None
    try:
        session = _http_session_for_channel(channel)
        oauth_data = _parse_oauth_payload(channel.get("oauth_token_json") or "")
        if not oauth_data:
            result["error"] = "OAuth token JSON rỗng."
            return result
        access_token = _refresh_google_access_token(oauth_data, session=session)
        identity = _fetch_oauth_channel_identity(access_token, session=session)
    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        if session is not None:
            session.close()

    result["ok"] = True
    result["identity"] = identity
    target_id = (channel.get("channel_id") or "").strip()
    if target_id:
        result["matched"] = identity["channel_id"] == target_id
    else:
        result["matched"] = _channel_identity_matches(channel, identity["name"], "")
    return result


def _refresh_google_access_token(oauth_data: Dict[str, Any], session: Optional[requests.Session] = None) -> str:
    """
    Làm mới OAuth2 Access Token từ Refresh Token cho YouTube Data API v3.
    """
    http = session or requests
    now = time.time()
    created = oauth_data.get("created") or oauth_data.get("issued_at") or 0
    expires_in = oauth_data.get("expires_in") or 3600

    if oauth_data.get("access_token") and created and (created + expires_in > now + 300):
        return oauth_data["access_token"]

    if oauth_data.get("refresh_token") and oauth_data.get("client_id") and oauth_data.get("client_secret"):
        try:
            resp = http.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": oauth_data["client_id"],
                    "client_secret": oauth_data["client_secret"],
                    "refresh_token": oauth_data["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
        except requests.RequestException as net_err:
            raise RuntimeError(f"Không kết nối được Google OAuth để làm mới token: {net_err}")
        if resp.status_code != 200:
            err_code = ""
            err_desc = ""
            try:
                err_json = resp.json()
                err_code = err_json.get("error") or ""
                err_desc = err_json.get("error_description") or ""
            except Exception:
                err_desc = resp.text[:200]
            if err_code == "invalid_grant":
                raise RuntimeError(
                    "Refresh token đã bị thu hồi hoặc hết hạn (invalid_grant). "
                    "Hãy lấy lại refresh_token mới cho đúng kênh rồi dán lại JSON vào kênh."
                )
            raise RuntimeError(
                f"Google từ chối làm mới OAuth token ({resp.status_code} {err_code}): {err_desc or 'không rõ lý do'}."
            )
        token_res = resp.json()
        new_token = token_res.get("access_token")
        if not new_token:
            raise RuntimeError("Google không trả về access_token khi làm mới token.")
        oauth_data["access_token"] = new_token
        oauth_data["created"] = int(time.time())
        oauth_data["expires_in"] = token_res.get("expires_in", 3600)
        return new_token

    if oauth_data.get("access_token"):
        if created and (created + expires_in <= now):
            raise RuntimeError(
                "Access token đã hết hạn và thiếu refresh_token/client_id/client_secret để tự gia hạn. "
                "Hãy dán OAuth JSON có refresh_token."
            )
        return oauth_data["access_token"]
    raise ValueError("Thiếu thông tin OAuth2 Access Token hoặc Refresh Token cho kênh YouTube.")


def _fetch_oauth_channel_identity(access_token: str, session: Optional[requests.Session] = None) -> Dict[str, str]:
    """Lấy kênh thực tế mà OAuth token đang đại diện (YouTube Data API không cho chọn kênh con khi upload)."""
    http = session or requests
    try:
        resp = http.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except requests.RequestException as net_err:
        raise RuntimeError(f"Không kiểm tra được kênh của token YouTube API do lỗi mạng: {net_err}")

    if resp.status_code == 401:
        raise RuntimeError("Token YouTube API không hợp lệ hoặc đã hết hạn. Hãy cập nhật lại OAuth token cho kênh.")
    if resp.status_code == 403:
        raise RuntimeError(
            "Token YouTube API thiếu quyền đọc thông tin kênh (cần scope youtube.readonly hoặc youtube). "
            "Không thể xác minh kênh đích nên đã dừng để tránh đăng nhầm kênh."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Không kiểm tra được kênh của token YouTube API ({resp.status_code}): {resp.text[:300]}"
        )

    items = (resp.json() or {}).get("items") or []
    if not items:
        raise RuntimeError(
            "Token YouTube API không gắn với kênh YouTube nào. Hãy cấp lại quyền cho đúng tài khoản/kênh đích."
        )
    item = items[0]
    return {
        "channel_id": item.get("id") or "",
        "name": ((item.get("snippet") or {}).get("title") or "").strip(),
    }


def _read_resumable_offset(response, file_size: int) -> int:
    """Return the first byte not confirmed stored by a 308 response."""
    value = str(response.headers.get("Range") or "").strip()
    if not value:
        return 0
    match = re.fullmatch(r"bytes\s*=\s*(\d+)-(\d+)", value, flags=re.IGNORECASE)
    if not match:
        raise RuntimeError(f"YouTube trả Range không hợp lệ cho phiên resumable: {value!r}")
    first, last = (int(match.group(1)), int(match.group(2)))
    if first != 0 or last < first or last >= file_size:
        raise RuntimeError(f"YouTube trả Range ngoài kích thước file: {value!r}")
    return last + 1


def _query_resumable_upload_status(http, upload_url: str, access_token: str, file_size: int):
    """Ask YouTube which bytes it stored; this is required after an ambiguous PUT."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Length": "0",
        "Content-Range": f"bytes */{file_size}",
    }
    last_error = None
    for attempt in range(3):
        try:
            response = http.put(upload_url, headers=headers, data=b"", timeout=30)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(min(8, 2 ** (attempt + 1)))
                continue
            raise RuntimeError(f"Không truy vấn được trạng thái phiên resumable: {exc}") from exc
        if response.status_code in (200, 201):
            video_id = str((response.json() or {}).get("id") or "").strip()
            if not video_id:
                raise RuntimeError("YouTube xác nhận phiên đã hoàn tất nhưng không trả Video ID.")
            return {"offset": file_size, "video_id": video_id}
        if response.status_code == 308:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(min(60, max(0, int(retry_after))))
                except (TypeError, ValueError):
                    pass
            return {"offset": _read_resumable_offset(response, file_size), "video_id": ""}
        if response.status_code in (500, 502, 503, 504) and attempt < 2:
            time.sleep(min(8, 2 ** (attempt + 1)))
            continue
        if response.status_code in (404, 410):
            raise RuntimeError(
                "Phiên resumable đã hết hạn hoặc không còn tồn tại. Không tự tạo phiên upload mới "
                "vì chưa thể loại trừ khả năng video đã được nhận; hãy đối soát YouTube Studio."
            )
        raise RuntimeError(
            f"Không xác định được trạng thái phiên resumable ({response.status_code}): {response.text}"
        )
    raise RuntimeError(f"Không xác định được trạng thái phiên resumable: {last_error or 'lỗi máy chủ'}")


def _verify_api_video(http, video_id: str, access_token: str, metadata: Dict[str, Any], progress_callback=None):
    """Wait for API processing and read back the audience/visibility/schedule fields."""
    try:
        timeout_seconds = int(get_settings().get("youtube_processing_timeout_seconds", 900) or 900)
    except (TypeError, ValueError):
        timeout_seconds = 900
    timeout_seconds = max(30, min(3600, timeout_seconds))
    deadline = time.monotonic() + timeout_seconds
    delay = 2
    while True:
        try:
            response = http.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "status,processingDetails", "id": video_id},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except (requests.ConnectionError, requests.Timeout):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Hết thời gian xác minh xử lý video qua YouTube API sau lỗi mạng.")
            time.sleep(min(delay, max(1, int(deadline - time.monotonic()))))
            delay = min(30, delay * 2)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Hết thời gian xác minh xử lý video qua YouTube API ({response.status_code}).")
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = min(60, max(1, int(retry_after))) if retry_after else delay
            except (TypeError, ValueError):
                wait_seconds = delay
            time.sleep(wait_seconds)
            delay = min(30, delay * 2)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"Không đọc lại được video sau upload ({response.status_code}): {response.text}")
        items = (response.json() or {}).get("items") or []
        item = next((candidate for candidate in items if str(candidate.get("id") or "") == video_id), None)
        if not item:
            raise RuntimeError("YouTube chưa trả video ID vừa tạo khi đọc lại; cần đối soát kết quả upload.")
        status = item.get("status") or {}
        upload_status = str(status.get("uploadStatus") or "").lower()
        processing_status = str((item.get("processingDetails") or {}).get("processingStatus") or "").lower()
        if upload_status in {"failed", "rejected", "deleted"} or processing_status in {"failed", "terminated"}:
            raise RuntimeError(
                f"YouTube không xử lý xong video (uploadStatus={upload_status or 'unknown'}, "
                f"processingStatus={processing_status or 'unknown'})."
            )
        if (
            (processing_status == "processing" or upload_status == "uploaded")
            and processing_status != "succeeded"
            and upload_status != "processed"
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"YouTube vẫn đang xử lý video sau {timeout_seconds}s.")
            if progress_callback:
                progress_callback(96, "YouTube đang xử lý video sau khi chuyển file hoàn tất...")
            time.sleep(min(delay, max(1, int(deadline - time.monotonic()))))
            delay = min(30, delay * 2)
            continue
        if upload_status != "processed" and processing_status != "succeeded":
            raise RuntimeError(
                f"Chưa xác định được trạng thái xử lý cuối của video "
                f"(uploadStatus={upload_status or 'missing'}, processingStatus={processing_status or 'missing'})."
            )

        expected_privacy = "private" if metadata.get("privacy_status") == "schedule" else metadata.get("privacy_status")
        if str(status.get("privacyStatus") or "").lower() != expected_privacy:
            raise RuntimeError("Visibility đọc lại từ YouTube không khớp giá trị đã yêu cầu.")
        if "selfDeclaredMadeForKids" not in status:
            raise RuntimeError("YouTube không trả lại trạng thái Dành cho trẻ em để xác minh.")
        if "containsSyntheticMedia" not in status:
            raise RuntimeError("YouTube không trả lại nhãn nội dung AI để xác minh.")
        if bool(status.get("selfDeclaredMadeForKids")) != bool(metadata.get("made_for_kids")):
            raise RuntimeError("Thiết lập Dành cho trẻ em đọc lại từ YouTube không khớp giá trị đã yêu cầu.")
        if bool(status.get("containsSyntheticMedia")) != bool(metadata.get("altered_content")):
            raise RuntimeError("Nhãn nội dung AI đọc lại từ YouTube không khớp giá trị đã yêu cầu.")
        if metadata.get("privacy_status") == "schedule":
            raw_publish_at = str(status.get("publishAt") or "").strip()
            if not raw_publish_at:
                raise RuntimeError("YouTube không trả lại thời gian công bố đã hẹn.")
            try:
                actual_publish_at = datetime.fromisoformat(raw_publish_at.replace("Z", "+00:00"))
                expected_publish_at = datetime.fromisoformat(
                    str(metadata.get("schedule_time") or "").replace(" ", "T")
                ).astimezone()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Không đọc/so sánh được thời gian hẹn từ YouTube.") from exc
            if abs(actual_publish_at.timestamp() - expected_publish_at.timestamp()) > 60:
                raise RuntimeError("Thời gian công bố đọc lại từ YouTube không khớp lịch đã yêu cầu.")
        return item


def _upload_via_youtube_api_with_session(
    video_path: str,
    metadata: Dict[str, Any],
    channel: Dict[str, Any],
    thumbnail_path: str = "",
    progress_callback: Optional[Callable[[int, str], None]] = None,
    session: Optional[requests.Session] = None,
) -> str:
    """
    Đăng video lên YouTube qua YouTube Data API v3 Resumable Upload (cắt mảnh 8MB).
    Trả về link YouTube sau khi đăng thành công.
    """
    if metadata.get("age_restriction"):
        raise RuntimeError(
            "Chế độ YouTube API không xác nhận được giới hạn độ tuổi 18+ trên YouTube Studio. "
            "Hãy dùng phương thức Playwright Studio cho video này."
        )

    file_size = os.path.getsize(video_path)
    if file_size <= 0:
        raise RuntimeError("File video rỗng, không thể khởi tạo YouTube resumable upload.")
    http = session or requests.Session()
    oauth_data = _parse_oauth_payload(channel.get("oauth_token_json") or "")
    access_token = _refresh_google_access_token(oauth_data, session=http)

    # XÁC MINH KÊNH: YouTube Data API không cho chọn kênh con khi upload — token chính là kênh nhận video.
    identity = _fetch_oauth_channel_identity(access_token, session=http)
    target_id = (channel.get("channel_id") or "").strip()
    target_label = (channel.get("name") or "").strip() or (channel.get("handle") or "").strip()
    if not target_id:
        raise RuntimeError(
            f"Kênh '{target_label}' chưa có Channel ID nên không thể xác minh chính xác kênh nhận video qua YouTube API. "
            "Hãy bấm 'Refresh Kênh' hoặc nhập Channel ID trong tab Quản Lý Kênh trước khi đăng."
        )
    if identity["channel_id"] != target_id:
        raise RuntimeError(
            f"Token YouTube API đang đại diện kênh '{identity['name']}' (ID {identity['channel_id']}), "
            f"không khớp Channel ID đích {target_id}. Đã dừng để tránh đăng nhầm kênh."
        )
    if progress_callback:
        progress_callback(6, f"Đã xác minh kênh YouTube API: {identity['name']} ({identity['channel_id']})")

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": str(metadata["category_id"]),
        },
        "status": {
            "selfDeclaredMadeForKids": metadata["made_for_kids"],
            "containsSyntheticMedia": metadata["altered_content"],
        },
    }

    # Lên lịch phát hành (publishAt) cho YouTube Data API: video ở trạng thái private tới giờ hẹn.
    # Tuyệt đối không fallback về PUBLIC khi thiếu/sai giờ hẹn.
    privacy_status = metadata["privacy_status"]
    sched_time = (metadata.get("schedule_time") or "").strip()
    if privacy_status == "schedule":
        if not sched_time:
            raise ValueError(
                "Video đang ở chế độ Lên lịch nhưng thiếu thời gian đăng — không đăng Công khai thay thế."
            )
        from datetime import datetime as _dt
        try:
            sched_dt = _dt.fromisoformat(sched_time.replace(" ", "T"))
        except Exception:
            raise ValueError(f"Thời gian hẹn lịch không hợp lệ: '{sched_time}'.")
        if sched_dt <= _dt.now():
            raise ValueError(f"Thời gian hẹn lịch '{sched_time}' đã qua — hãy chọn giờ trong tương lai.")
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = sched_dt.astimezone().isoformat(timespec="seconds")
    elif privacy_status in ("public", "unlisted", "private"):
        body["status"]["privacyStatus"] = privacy_status
    else:
        raise ValueError(
            f"Chế độ hiển thị '{privacy_status}' không hợp lệ — không tự fallback về Công khai."
        )

    init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }
    if progress_callback:
        progress_callback(5, "Khởi tạo phiên tải lên YouTube API...")

    init_resp = http.post(init_url, headers=headers, json=body, timeout=30)
    if init_resp.status_code not in (200, 201):
        raise RuntimeError(f"YouTube API từ chối khởi tạo upload ({init_resp.status_code}): {init_resp.text}")

    upload_url = init_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Không nhận được Location URL từ YouTube Resumable Upload.")
    upload_location = urlsplit(upload_url)
    upload_host = (upload_location.hostname or "").lower().rstrip(".")
    try:
        upload_port = upload_location.port
    except ValueError as exc:
        raise RuntimeError("Location của phiên resumable có port không hợp lệ.") from exc
    if (
        upload_location.scheme.lower() != "https"
        or upload_location.username
        or upload_location.password
        or upload_port not in (None, 443)
        or not (upload_host == "googleapis.com" or upload_host.endswith(".googleapis.com"))
    ):
        raise RuntimeError("Location của phiên resumable không thuộc HTTPS Google APIs; đã dừng để bảo vệ OAuth token.")

    video_id = ""
    with open(video_path, "rb") as f:
        offset = 0
        while offset < file_size:
            f.seek(offset)
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                raise RuntimeError(f"Không đọc được dữ liệu tại offset {offset} trong file upload.")
            request_resolved = False
            for attempt in range(5):
                request_error = None
                end_byte = offset + len(chunk) - 1
                put_headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                }
                try:
                    put_resp = http.put(upload_url, headers=put_headers, data=chunk, timeout=120)
                except (requests.ConnectionError, requests.Timeout) as net_err:
                    request_error = net_err
                    put_resp = None
                if put_resp is None or put_resp.status_code in (500, 502, 503, 504):
                    # A PUT can be accepted even when its response is lost or 5xx.
                    # Query the server before deciding whether any bytes may be resent.
                    state = _query_resumable_upload_status(http, upload_url, access_token, file_size)
                    if state["video_id"]:
                        video_id = state["video_id"]
                        request_resolved = True
                        break
                    if state["offset"] < offset:
                        raise RuntimeError(
                            f"Offset YouTube bị lùi từ {offset} về {state['offset']}; dừng để tránh gửi sai dữ liệu."
                        )
                    if state["offset"] > offset:
                        offset = state["offset"]
                        request_resolved = True
                        break
                    if attempt < 4:
                        time.sleep(min(30, 2 ** (attempt + 1)))
                    elif request_error:
                        raise RuntimeError(
                            f"Lỗi mạng khi tải lên YouTube chunk {offset}; server xác nhận chưa nhận thêm byte: {request_error}"
                        ) from request_error
                    else:
                        raise RuntimeError(
                            f"YouTube tiếp tục lỗi máy chủ khi gửi chunk {offset} "
                            f"({put_resp.status_code if put_resp is not None else 'không rõ'}); server xác nhận chưa nhận thêm byte."
                        )
                    continue

                if put_resp.status_code in (200, 201):
                    video_id = str((put_resp.json() or {}).get("id") or "").strip()
                    if not video_id:
                        raise RuntimeError("YouTube hoàn tất PUT nhưng không trả Video ID.")
                    request_resolved = True
                    break
                if put_resp.status_code == 308:
                    accepted_offset = _read_resumable_offset(put_resp, file_size)
                    if accepted_offset < offset:
                        raise RuntimeError(
                            f"Offset YouTube bị lùi từ {offset} về {accepted_offset}; dừng để tránh gửi sai dữ liệu."
                        )
                    if accepted_offset > offset:
                        offset = accepted_offset
                        request_resolved = True
                        break
                    if attempt < 4:
                        time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
                raise RuntimeError(
                    f"Lỗi khi đẩy dữ liệu video ({put_resp.status_code}): {put_resp.text}"
                )
            if video_id:
                break
            if not request_resolved:
                raise RuntimeError(f"Phiên resumable không tiến triển sau 5 lần thử tại offset {offset}.")
            pct = min(95, int(offset * 100 / file_size))
            if progress_callback:
                progress_callback(pct, f"Đang chuyển file lên YouTube API... {pct}%")
            if offset == file_size and not video_id:
                state = _query_resumable_upload_status(http, upload_url, access_token, file_size)
                video_id = state["video_id"]
                if not video_id:
                    raise RuntimeError("YouTube đã nhận toàn bộ file nhưng chưa xác nhận hoàn tất phiên upload.")

    if not video_id:
        raise RuntimeError("Không lấy được Video ID sau khi tải lên.")
    verified_url = normalize_published_video_url(f"https://youtu.be/{video_id}")
    if not verified_url:
        raise RuntimeError("YouTube trả Video ID không đúng định dạng video 11 ký tự.")

    if progress_callback:
        progress_callback(96, "Đã chuyển file xong; đang chờ YouTube xử lý và đọc lại metadata...")
    _verify_api_video(http, video_id, access_token, metadata, progress_callback=progress_callback)

    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            with open(thumbnail_path, "rb") as tf:
                http.post(
                    f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}",
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"},
                    data=tf.read(),
                    timeout=30,
                )
        except Exception as thumb_err:
            _log_optional_step_failure("tải thumbnail qua YouTube API", thumb_err, channel, metadata)

    if metadata.get("pin_comment"):
        try:
            http.post(
                "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "snippet": {"textOriginal": metadata["pin_comment"]}
                        },
                    }
                },
                timeout=20,
            )
        except Exception as pin_err:
            _log_optional_step_failure("ghim bình luận qua YouTube API", pin_err, channel, metadata)

    if progress_callback:
        progress_callback(100, "Đăng thành công qua YouTube API")
    return verified_url


def upload_via_youtube_api(
    video_path: str,
    metadata: Dict[str, Any],
    channel: Dict[str, Any],
    thumbnail_path: str = "",
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> str:
    with _http_session_for_channel(channel) as session:
        return _upload_via_youtube_api_with_session(
            video_path=video_path,
            metadata=metadata,
            channel=channel,
            thumbnail_path=thumbnail_path,
            progress_callback=progress_callback,
            session=session,
        )


STEALTH_JS = """
// 1. Gỡ bỏ thuộc tính nhận diện tự động hóa Playwright / Puppeteer
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
});

// 2. Giả lập chrome runtime
if (!window.chrome) {
    window.chrome = {};
}
if (!window.chrome.runtime) {
    window.chrome.runtime = {};
}

// 3. Giả lập ngôn ngữ và plugins người dùng
Object.defineProperty(navigator, 'languages', {
    get: () => ['vi-VN', 'vi', 'en-US', 'en'],
});

// 4. Giả lập window.navigator.permissions
const origPerm = window.navigator.permissions && window.navigator.permissions.query;
if (origPerm) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            origPerm(parameters)
    );
}
"""

MOBILE_PATCH_JS = """
// Đồng bộ hóa nền tảng phần cứng khi bật chế độ Giả lập Mobile để tránh lệch Fingerprint
Object.defineProperty(navigator, 'platform', {
    get: () => 'Linux armv81',
});
Object.defineProperty(navigator, 'maxTouchPoints', {
    get: () => 5,
});
"""


def human_scroll(page, delta_y: int = 250, steps: int = 5):
    """
    Cuộn trang nhẹ nhàng từng bước nhỏ mô phỏng người dùng lướt chuột xem biểu mẫu.
    """
    import random
    sub = delta_y / max(1, steps)
    for _ in range(steps):
        page.mouse.wheel(0, sub)
        page.wait_for_timeout(random.randint(35, 75))
    page.wait_for_timeout(random.randint(250, 500))


def human_move_and_click(page, locator, delay_after: Optional[int] = None):
    """
    Mô phỏng thao tác di chuyển chuột và bấm nút chuẩn người thật.
    """
    import random
    try:
        locator.scroll_into_view_if_needed(timeout=7000)
        box = locator.bounding_box()
        if box and box["width"] > 0 and box["height"] > 0:
            target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            page.mouse.move(target_x, target_y, steps=random.randint(12, 22))
            page.wait_for_timeout(random.randint(120, 260))
            page.mouse.down()
            page.wait_for_timeout(random.randint(60, 130))
            page.mouse.up()
            after_wait = delay_after if delay_after is not None else random.randint(350, 750)
            page.wait_for_timeout(after_wait)
            return
    except Exception:
        pass
    locator.click()
    page.wait_for_timeout(random.randint(300, 600))


def simulate_human_type(page, locator, text: str, is_long_text: bool = False):
    """
    Mô phỏng nhịp độ gõ phím và tương tác tự nhiên của người dùng.
    """
    import random
    human_move_and_click(page, locator, delay_after=random.randint(150, 300))
    page.keyboard.press("Control+A")
    page.wait_for_timeout(random.randint(80, 150))
    page.keyboard.press("Backspace")
    page.wait_for_timeout(random.randint(180, 350))

    if not is_long_text:
        for ch in text:
            if random.random() < 0.02 and ch.isalpha():
                typo_char = random.choice("abcdefghijklmnopqrstuvwxyz")
                page.keyboard.type(typo_char)
                page.wait_for_timeout(random.randint(180, 320))
                page.keyboard.press("Backspace")
                page.wait_for_timeout(random.randint(120, 220))

            page.keyboard.type(ch)
            delay = random.randint(35, 75)
            if ch in (" ", ",", ".", "-", "_", "\n") or ch.isupper():
                delay += random.randint(100, 220)
            page.wait_for_timeout(delay)
    else:
        idx = 0
        while idx < len(text):
            chunk_size = random.randint(8, 22)
            chunk = text[idx:idx + chunk_size]
            page.keyboard.insert_text(chunk)
            idx += chunk_size
            page.wait_for_timeout(random.randint(45, 110))


_CHECKS_SAFE_KEYWORDS = [
    # English
    "no issues found", "checks complete", "no copyright issues were found", "no issues detected",
    # Tiếng Việt
    "không phát hiện vấn đề nào", "kiểm tra hoàn tất",
    "không phát hiện thấy vấn đề nào về bản quyền", "không có vấn đề",
    # Español
    "no se encontraron problemas", "no se ha encontrado ningún problema",
    "comprobaciones completadas", "verificaciones completadas", "sin problemas",
    # Português
    "nenhum problema encontrado", "nenhum problema foi encontrado",
    "verificações concluídas", "sem problemas",
    # Bahasa Indonesia
    "tidak ada masalah", "tidak ditemukan masalah", "pemeriksaan selesai",
    # ไทย
    "ไม่พบปัญหา", "การตรวจสอบเสร็จสิ้น",
    # हिन्दी
    "कोई समस्या नहीं मिली", "जाँच पूरी हुई",
    # 日本語
    "問題は見つかりませんでした", "チェックが完了しました",
    # 한국어
    "문제가 발견되지 않았습니다", "검사가 완료되었습니다",
    # 中文（简体 / 繁體）
    "未发现问题", "未找到任何问题", "检查已完成",
    "未發現問題", "找不到問題", "檢查完成",
    # العربية
    "لم يتم العثور على مشكلات", "اكتملت عمليات الفحص",
    # Français
    "aucun problème détecté", "vérifications terminées",
    # Deutsch
    "keine probleme gefunden", "prüfungen abgeschlossen",
    # Русский
    "проблем не обнаружено", "проверка завершена",
    # Türkçe
    "sorun bulunamadı", "kontroller tamamlandı",
]

# Cảnh báo CHẶN ĐĂNG (bản quyền, chặn khu vực) -> Hủy + Xóa nháp
_CHECKS_VIOLATION_KEYWORDS = [
    # English
    "issue found", "issues found", "copyright-protected content", "copyright claim",
    "copyright issue", "includes copyrighted content", "blocked in some territories",
    "blocked in some countries", "copyrighted material", "content id claim",
    # Tiếng Việt
    "vấn đề đã tìm thấy", "phát hiện vấn đề", "nội dung được bảo vệ bản quyền",
    "khiếu nại bản quyền", "vấn đề bản quyền", "bị chặn ở một số quốc gia",
    "bị chặn ở một số khu vực",
    # Español
    "se encontraron problemas", "problema encontrado", "contenido protegido por derechos de autor",
    "reclamación de derechos de autor", "problema de derechos de autor",
    "bloqueado en algunos países", "bloqueado en algunos territorios",
    "incluye contenido con derechos de autor",
    # Português
    "problemas encontrados", "problema encontrado", "conteúdo protegido por direitos autorais",
    "reivindicação de direitos autorais", "bloqueado em alguns países",
    "bloqueado em alguns territórios", "inclui conteúdo protegido por direitos autorais",
    # Bahasa Indonesia
    "masalah ditemukan", "konten dilindungi hak cipta", "klaim hak cipta",
    "masalah hak cipta", "diblokir di beberapa negara", "diblokir di beberapa wilayah",
    # ไทย
    "พบปัญหา", "เนื้อหาที่ได้รับการคุ้มครองลิขสิทธิ์", "การอ้างสิทธิ์ลิขสิทธิ์",
    "ปัญหาลิขสิทธิ์", "ถูกบล็อกในบางประเทศ",
    # हिन्दी
    "समस्या मिली", "कॉपीराइट-सुरक्षित सामग्री", "कॉपीराइट दावा",
    "कॉपीराइट समस्या", "कुछ देशों में ब्लॉक",
    # 日本語
    "問題が見つかりました", "著作権で保護されたコンテンツ", "著作権の申し立て",
    "著作権の問題", "一部の国でブロック",
    # 한국어
    "문제가 발견되었습니다", "저작권으로 보호되는 콘텐츠", "저작권 소유권 주장",
    "저작권 문제", "일부 국가에서 차단",
    # 中文（简体 / 繁體）
    "发现了问题", "受版权保护的内容", "版权声明", "版权问题",
    "在某些国家/地区被屏蔽", "在某些国家/地区被封锁",
    "發現問題", "受著作權保護的內容", "著作權聲明", "著作權問題",
    "在某些國家/地區遭到封鎖",
    # العربية
    "تم العثور على مشكلات", "محتوى محمي بحقوق الطبع والنشر",
    "مطالبة بحقوق الطبع والنشر", "مشكلة في حقوق الطبع والنشر", "محظور في بعض البلدان",
    # Français
    "problèmes détectés", "contenu protégé par des droits d'auteur",
    "réclamation pour atteinte aux droits d'auteur", "problème de droits d'auteur",
    "bloqué dans certains pays",
    # Deutsch
    "probleme gefunden", "urheberrechtlich geschütztes material",
    "urheberrechtsanspruch", "urheberrechtsproblem", "in einigen ländern blockiert",
    # Русский
    "обнаружены проблемы", "контент, защищенный авторским правом",
    "претензия о нарушении авторских прав", "проблема с авторскими правами",
    "заблокировано в некоторых странах",
    # Türkçe
    "sorun bulundu", "telif hakkıyla korunan içerik", "telif hakkı talebi",
    "telif hakkı sorunu", "bazı ülkelerde engellendi",
]

# Cảnh báo KHÔNG CHẶN (kiếm tiền / quảng cáo) -> Chỉ ghi nhận, vẫn đăng
_CHECKS_SOFT_KEYWORDS = [
    # English
    "video cannot be monetized", "limited ads", "not eligible", "ineligible",
    "not suitable for all advertisers", "ad suitability", "limited or no ads",
    # Tiếng Việt
    "không thể kiếm tiền", "quảng cáo bị hạn chế", "không đủ điều kiện",
    "không phù hợp với tất cả nhà quảng cáo",
    # Español
    "no se puede monetizar", "anuncios limitados", "no apto para todos los anunciantes",
    "idoneidad para anunciantes",
    # Português
    "não pode ser monetizado", "anúncios limitados",
    "não é adequado para todos os anunciantes", "adequação do anúncio",
    # Bahasa Indonesia
    "tidak dapat dimonetisasi", "iklan terbatas", "tidak cocok untuk semua pengiklan",
    "kesesuaian iklan",
    # ไทย
    "ไม่สามารถสร้างรายได้", "โฆษณาถูกจำกัด", "ไม่เหมาะสำหรับผู้ลงโฆษณาทั้งหมด",
    # हिन्दी
    "मुद्रीकृत नहीं किया जा सकता", "सीमित विज्ञापन",
    "सभी विज्ञापनदाताओं के लिए उपयुक्त नहीं",
    # 日本語
    "収益化できません", "広告が制限されています",
    "すべての広告主に適しているわけではありません",
    # 한국어
    "수익을 창출할 수 없음", "광고 제한됨", "모든 광고주에게 적합하지 않음",
    # 中文（简体 / 繁體）
    "无法创收", "广告受限", "并非适合所有广告客户",
    "無法營利", "廣告受到限制", "不適合所有廣告客戶",
    # العربية
    "لا يمكن تحقيق الدخل", "إعلانات محدودة", "غير مناسب لجميع المعلنين",
    # Français
    "ne peut pas être monétisé", "annonces limitées",
    "ne convient pas à tous les annonceurs",
    # Deutsch
    "kann nicht monetarisiert werden", "eingeschränkte anzeigen",
    "nicht für alle werbetreibenden geeignet",
    # Русский
    "невозможно монетизировать", "ограниченная реклама",
    "не подходит для всех рекламодателей",
    # Türkçe
    "para kazanılamıyor", "sınırlı reklamlar", "tüm reklamverenler için uygun değil",
]

_CHECKS_PROCESSING_KEYWORDS = [
    "still processing", "currently processing", "processing your video", "processing will begin",
    "processing video", "checks are in progress", "checking your video", "checking for issues",
    "video is uploading", "uploading video", "đang xử lý", "vẫn đang xử lý", "đang kiểm tra",
    "đang tải lên", "đang quét",
]


def checks_report_safe(raw_text: str) -> bool:
    """True khi YouTube Studio báo kiểm tra xong và không có vấn đề (đa ngôn ngữ)."""
    text = (raw_text or "").lower()
    if any(k in text for k in _CHECKS_PROCESSING_KEYWORDS):
        return False
    return any(k in text for k in _CHECKS_SAFE_KEYWORDS)


def _read_checks_text(page) -> str:
    """Read only the visible Checks panel; never infer its result from page-wide text."""
    checks_container = page.locator("ytcp-uploads-checks, #checks-content, div[test-id='checks-content']").first
    if checks_container.count() == 0 or not checks_container.is_visible():
        raise RuntimeError("Không tìm thấy vùng Checks đang hiển thị.")
    return (checks_container.inner_text() or "").lower()


def classify_checks_text(raw_text: str) -> Dict[str, Any]:
    """Phân loại Checks thành an toàn, vi phạm rõ hoặc chưa xác định."""
    text = (raw_text or "").lower()
    # Cảnh báo vi phạm phải thắng câu "Checks complete"/"No issues found" đi kèm:
    # bỏ các cụm an toàn trước khi dò vi phạm để "Checks complete. Copyright-protected content found"
    # không bị phân loại nhầm là an toàn.
    stripped = text
    for safe_kw in _CHECKS_SAFE_KEYWORDS:
        stripped = stripped.replace(safe_kw, " ")
    for vk in _CHECKS_VIOLATION_KEYWORDS:
        if vk in stripped:
            return {
                "result": "violation",
                "has_violation": True,
                "reason": f"Phát hiện cảnh báo: {vk.title()}",
                "soft_warning": "",
            }
    if any(k in text for k in _CHECKS_PROCESSING_KEYWORDS):
        return {"result": "unknown", "has_violation": False, "reason": "", "soft_warning": ""}
    if checks_report_safe(text):
        return {"result": "safe", "has_violation": False, "reason": "", "soft_warning": ""}
    for sk in _CHECKS_SOFT_KEYWORDS:
        if sk in text:
            return {
                "result": "unknown",
                "has_violation": False,
                "reason": "",
                "soft_warning": f"Cảnh báo không chặn đăng (kiếm tiền/quảng cáo): {sk}",
            }
    return {"result": "unknown", "has_violation": False, "reason": "", "soft_warning": ""}


def check_copyright_and_violations(page) -> Dict[str, Any]:
    """
    Quét và phát hiện cảnh báo tại bước Checks của YouTube Studio (đa ngôn ngữ).
    Phân biệt 2 mức:
    - has_violation (CẢNH BÁO BẢN QUYỀN / BỊ CHẶN): tự động hủy và xóa nháp để bảo vệ kênh.
    - soft_warning (KHÔNG ĐỦ ĐIỀU KIỆN KIẾM TIỀN / HẠN CHẾ QUẢNG CÁO): chỉ ghi log, vẫn đăng bình thường.
    """
    state: Dict[str, Any] = {
        "result": "unknown", "has_violation": False, "reason": "", "soft_warning": "", "probe_error": ""
    }

    try:
        raw_text = _read_checks_text(page)
        state = classify_checks_text(raw_text)
        state["probe_error"] = ""
        if state["has_violation"]:
            return state
        if checks_report_safe(raw_text):
            return state
    except Exception as read_err:
        state["probe_error"] = str(read_err)

    alert_selectors = [
        "ytcp-badge[type='BADGE_STYLE_ALERT']",
        ".warning-container",
        ".error-container",
        "[type='alert']",
        "ytcp-uploads-checks iron-icon[icon*='error']",
        "ytcp-uploads-checks iron-icon[icon*='warning']",
    ]
    for sel in alert_selectors:
        try:
            elem = page.locator(sel).first
            if elem.count() > 0 and elem.is_visible():
                txt = (elem.inner_text() or elem.text_content() or "").strip()
                if not txt:
                    continue
                alert_state = classify_checks_text(txt)
                if alert_state["has_violation"]:
                    return alert_state
                if alert_state["soft_warning"] and not state["soft_warning"]:
                    state = alert_state
        except Exception as alert_err:
            if not state.get("probe_error"):
                state["probe_error"] = str(alert_err)

    return state


_PUBLISHED_URL_IN_TEXT_RE = re.compile(
    r"https?://(?:www\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/|embed/))"
    r"[A-Za-z0-9_-]{11}"
)

_STUDIO_VIDEO_EDIT_PATH_RE = re.compile(
    r"/video/([A-Za-z0-9_-]{11})/edit/?", re.IGNORECASE
)


def normalize_published_video_url(raw: str) -> str:
    """Validate a real YouTube video URL by parsed host, path, and 11-char ID."""
    try:
        parts = urlsplit(str(raw or "").strip())
        if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password:
            return ""
        port = parts.port
        if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
            return ""
        host = (parts.hostname or "").lower().rstrip(".")
        if host not in {"youtu.be", "www.youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"}:
            return ""
        video_id = ""
        path_parts = [part for part in parts.path.split("/") if part]
        if host.endswith("youtu.be"):
            if len(path_parts) == 1:
                video_id = path_parts[0]
        elif path_parts == ["watch"]:
            values = parse_qs(parts.query, keep_blank_values=True).get("v", [])
            if len(values) == 1:
                video_id = values[0]
        elif len(path_parts) == 2 and path_parts[0].lower() in {"shorts", "live", "embed"}:
            video_id = path_parts[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
            return ""
    except (TypeError, ValueError):
        return ""
    return f"https://youtu.be/{video_id}"


def extract_published_video_url(page, previous_url: str = "") -> str:
    """Đọc URL video thật từ hộp thoại chia sẻ / liên kết video của YouTube Studio."""
    selectors = [
        "input#share-url",
        "input[value*='youtu']",
        "a[href*='youtu']",
        "a[href*='youtube']",
    ]
    try:
        dialog = page.locator("ytcp-video-share-dialog").first
        if dialog.count() > 0 and dialog.is_visible():
            for sel in selectors:
                try:
                    loc = dialog.locator(sel).first
                    if loc.count() == 0 or not loc.is_visible():
                        continue
                    candidates = []
                    try:
                        candidates.append(loc.input_value())
                    except Exception:
                        pass
                    for attr in ("value", "href"):
                        try:
                            candidates.append(loc.get_attribute(attr) or "")
                        except Exception:
                            pass
                    try:
                        candidates.append(loc.inner_text() or "")
                    except Exception:
                        pass
                    for candidate in candidates:
                        url = normalize_published_video_url(candidate)
                        if url:
                            return url
                except Exception:
                    continue
            # Studio hiện URL dạng text ("Đường liên kết của video https://youtu.be/...")
            # chứ không còn nằm trong input/href, nên phải quét cả nội dung hộp thoại.
            match = _PUBLISHED_URL_IN_TEXT_RE.search(dialog.inner_text() or "")
            if match:
                url = normalize_published_video_url(match.group(0))
                if url:
                    return url
    except Exception:
        pass

    # After a completed publish, Studio may route to the edit page instead of
    # showing its share dialog. Accept only an ID in this current Studio URL.
    try:
        parts = urlsplit(str(page.url or ""))
        host = (parts.hostname or "").lower().rstrip(".")
        if (
            host in {"studio.youtube.com", "youtube.com", "www.youtube.com"}
            and str(page.url or "") != str(previous_url or "")
        ):
            query = parse_qs(parts.query)
            for key in ("videoId", "video_id", "v"):
                values = query.get(key, [])
                if len(values) == 1:
                    url = normalize_published_video_url(f"https://youtu.be/{values[0]}")
                    if url:
                        return url
            path_parts = [part for part in parts.path.split("/") if part]
            for index, part in enumerate(path_parts[:-1]):
                if part.lower() in {"video", "videos"}:
                    url = normalize_published_video_url(f"https://youtu.be/{path_parts[index + 1]}")
                    if url:
                        return url
    except Exception:
        pass
    return ""


def _wait_for_publish_completion(page, previous_url: str, timeout: int = PUBLISH_CONFIRM_TIMEOUT_SECONDS) -> str:
    """Chờ Studio hoàn tất đăng và trả URL thật; video còn xử lý có thể trả kết quả muộn."""
    deadline = time.monotonic() + max(30, int(timeout))
    processing_logged = False
    while time.monotonic() < deadline:
        url = extract_published_video_url(page, previous_url)
        if url:
            return url
        try:
            processing_dialog = page.locator("ytcp-uploads-still-processing-dialog").first
            if (
                not processing_logged
                and processing_dialog.count() > 0
                and processing_dialog.is_visible()
            ):
                processing_logged = True
                add_log(
                    "Upload",
                    "YouTube Studio vẫn đang xử lý video sau khi bấm đăng — tiếp tục chờ hộp thoại chia sẻ.",
                    "INFO",
                )
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return ""


def _normalize_studio_title(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _studio_row_matches_title(row, expected_title: str) -> bool:
    """Match a content row by its full title, avoiding substring collisions."""
    expected = _normalize_studio_title(expected_title)
    if not expected:
        return False

    for selector in ("#video-title", "a#video-title", "ytcp-video-title"):
        try:
            title = row.locator(selector).first
            if title.count() > 0 and _normalize_studio_title(title.inner_text()) == expected:
                return True
        except Exception:
            continue

    try:
        lines = [_normalize_studio_title(line) for line in (row.inner_text() or "").splitlines()]
        return expected in lines
    except Exception:
        return False


_STUDIO_DRAFT_MARKERS = (
    "bản nháp", "draft", "đã tải được", "đang tải lên", "uploading",
    "đang chờ xử lý", "chờ xử lý", "đang xử lý", "processing",
)
_STUDIO_SCHEDULE_MARKERS = ("đã lên lịch", "scheduled")
_STUDIO_UNLISTED_MARKERS = ("không công khai", "unlisted")
_STUDIO_PRIVATE_MARKERS = ("riêng tư", "private")
_STUDIO_PUBLIC_MARKERS = ("công khai", "public")


def _studio_content_sections(upload_to: str) -> tuple:
    # Studio dùng tab Shorts ở đường dẫn /videos/short (số ít); /videos/shorts trả danh sách rỗng.
    if str(upload_to or "").strip().casefold() == "shorts":
        return ("short", "upload")
    return ("upload", "short")


def _studio_row_visibility_state(row, title: str = "") -> str:
    """Trạng thái hiển thị của hàng Studio; rỗng nếu hàng còn nháp/đang tải."""
    try:
        lines = (row.inner_text() or "").splitlines()
    except Exception:
        return ""
    # Bỏ dòng tiêu đề: tiêu đề có thể chứa "public"/"private"/"scheduled" và làm sai phân loại.
    expected_title = _normalize_studio_title(title)
    text = _normalize_studio_title(
        "\n".join(line for line in lines if _normalize_studio_title(line) != expected_title)
    )
    if not text or any(marker in text for marker in _STUDIO_DRAFT_MARKERS):
        return ""
    if any(marker in text for marker in _STUDIO_SCHEDULE_MARKERS):
        return "schedule"
    if any(marker in text for marker in _STUDIO_UNLISTED_MARKERS):
        return "unlisted"
    if any(marker in text for marker in _STUDIO_PRIVATE_MARKERS):
        return "private"
    if any(marker in text for marker in _STUDIO_PUBLIC_MARKERS):
        return "public"
    return ""


def _studio_row_video_id(row) -> str:
    try:
        edit_links = row.locator("a[href*='/video/'][href*='/edit']")
        for link_index in range(min(edit_links.count(), 10)):
            href = edit_links.nth(link_index).get_attribute("href") or ""
            parts = urlsplit(href)
            host = (parts.hostname or "").lower().rstrip(".")
            if host and host != "studio.youtube.com":
                continue
            match = _STUDIO_VIDEO_EDIT_PATH_RE.fullmatch(parts.path)
            if match:
                return match.group(1)
    except Exception:
        return ""
    return ""


def _collect_studio_title_candidates(
    page, channel_id: str, title: str, upload_to: str = "list", require_readable: bool = False
) -> list:
    """Đọc ID + trạng thái của các hàng trùng chính xác tiêu đề trên tab Video/Shorts."""
    candidates = []
    unreadable = []
    for section in _studio_content_sections(upload_to):
        content_url = f"https://studio.youtube.com/channel/{channel_id}/videos/{section}"
        page.goto(content_url, wait_until="domcontentloaded", timeout=30000)
        rows_ready = True
        try:
            page.wait_for_selector("ytcp-video-row", timeout=15000)
        except Exception:
            # Tab trống hoặc danh sách chậm vẫn phải được đọc một lần.
            rows_ready = False
            page.wait_for_timeout(1500)
        rows = page.locator("ytcp-video-row")
        raw_count = rows.count()
        if require_readable and ((not rows_ready and raw_count == 0) or raw_count >= 100):
            # Không phân biệt được tab trống với tab chưa tải xong, hoặc chưa đọc hết phân trang.
            unreadable.append(section)
            continue
        for index in range(min(raw_count, 100)):
            row = rows.nth(index)
            if not _studio_row_matches_title(row, title):
                continue
            video_id = _studio_row_video_id(row)
            if require_readable and not video_id:
                # Hàng trùng tiêu đề nhưng không đọc được ID -> không thể loại trừ về sau.
                unreadable.append(section)
                break
            candidates.append(
                {"id": video_id, "visibility": _studio_row_visibility_state(row, title)}
            )
    if unreadable:
        raise RuntimeError(
            "Không đọc đầy đủ danh sách nội dung tab " + ", ".join(sorted(set(unreadable))) + "."
        )
    return candidates


def _snapshot_studio_exact_title_ids(
    page, channel_id: str, title: str, upload_to: str = ""
) -> Optional[set]:
    """Chụp ID các video trùng tiêu đề TRƯỚC khi upload để loại trừ video cũ khi xác minh."""
    channel_id = str(channel_id or "").strip()
    if not re.fullmatch(_CHANNEL_ID_PATTERN, channel_id, re.IGNORECASE):
        return None
    original_url = str(page.url or "https://studio.youtube.com")
    try:
        candidates = _collect_studio_title_candidates(
            page, channel_id, title, upload_to, require_readable=True
        )
        return {candidate["id"] for candidate in candidates if candidate["id"]}
    except Exception:
        return None
    finally:
        try:
            page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def _find_published_video_in_studio_content(
    page,
    channel_id: str,
    title: str,
    upload_to: str = "",
    expected_visibility: str = "",
    known_video_ids: Optional[set] = None,
) -> str:
    """Bằng chứng dự phòng: đúng 1 ID không nằm trong snapshot đã quan sát được chấp nhận."""
    channel_id = str(channel_id or "").strip()
    if not re.fullmatch(_CHANNEL_ID_PATTERN, channel_id, re.IGNORECASE):
        return ""
    if known_video_ids is None:
        # Không có snapshot trước upload thì không thể loại trừ video cũ -> fail closed.
        return ""
    expected = str(expected_visibility or "").strip().casefold()
    if expected in ("scheduled", "schedule"):
        expected = "schedule"
    if expected not in ("public", "unlisted", "private", "schedule"):
        return ""
    try:
        candidates = _collect_studio_title_candidates(
            page, channel_id, title, upload_to, require_readable=True
        )
    except Exception:
        return ""
    new_entries = [
        candidate
        for candidate in candidates
        if candidate["id"] and candidate["id"] not in known_video_ids
    ]
    new_ids = {candidate["id"] for candidate in new_entries}
    # Đếm TRƯỚC khi lọc visibility: Public + Draft cùng tiêu đề vẫn là 2 ứng viên mới.
    if len(new_ids) != 1:
        return ""
    video_id = next(iter(new_ids))
    if any(
        candidate["visibility"] != expected
        for candidate in new_entries
        if candidate["id"] == video_id
    ):
        return ""
    return f"https://youtu.be/{video_id}"


DISCARD_DIALOG_WAIT_TIMEOUT_MS = 8000


def discard_and_cancel_upload(
    page, channel_name: str = "", video_title: str = "", reason: str = ""
) -> Dict[str, Any]:
    """Hủy tải lên/xóa nháp; chỉ báo xóa khi đã thấy hộp thoại xác nhận và nó đóng lại."""
    result: Dict[str, Any] = {
        "status": "not_found",
        "clicked": [],
        "dialog_open": False,
        "dialog_observed": False,
    }
    read_state = {"error": False}

    def _visible_destructive_dialog():
        for selector in ("tp-yt-paper-dialog:visible", "ytcp-confirmation-dialog:visible"):
            try:
                dialogs = page.locator(selector)
                for index in range(min(dialogs.count(), 5)):
                    dialog = dialogs.nth(index)
                    text = (dialog.inner_text() or "").casefold()
                    if any(
                        marker in text
                        for marker in (
                            "xóa vĩnh viễn", "delete forever", "xóa video nháp",
                            "hủy việc tải lên", "cancel upload", "discard",
                        )
                    ):
                        return dialog
            except Exception:
                read_state["error"] = True
                continue
        return None

    def _wait_destructive_dialog():
        deadline = time.monotonic() + DISCARD_DIALOG_WAIT_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            dialog = _visible_destructive_dialog()
            if dialog is not None:
                return dialog
            page.wait_for_timeout(500)
        return _visible_destructive_dialog()

    def _click_visible(locator, label: str) -> bool:
        try:
            if locator.count() > 0 and locator.is_visible():
                human_move_and_click(page, locator, delay_after=1200)
                result["clicked"].append(label)
                return True
        except Exception:
            pass
        return False

    def _dialog_has_confirm(dialog) -> bool:
        try:
            button = dialog.locator("#confirm-button").first
            return button.count() > 0 and button.is_visible()
        except Exception:
            read_state["error"] = True
            return False

    def _finish(status: str, message: str, level: str) -> Dict[str, Any]:
        result["status"] = status
        add_log(
            "CopyrightGuardian",
            message,
            level,
            channel_name=channel_name,
            video_title=video_title,
        )
        return result

    try:
        add_log(
            "CopyrightGuardian",
            f"🛑 Đang tiến hành HỦY TẢI LÊN và XÓA BẢN NHÁP do phát hiện vi phạm: {reason}",
            "WARNING",
            channel_name=channel_name,
            video_title=video_title,
        )
        page.wait_for_timeout(1000)

        close_btn = page.locator(
            "ytcp-icon-button#close-button, #close-button, ytcp-button#close-button, "
            "[aria-label*='Close' i], [aria-label*='Đóng' i], [aria-label*='Cancel' i]"
        ).first
        _click_visible(close_btn, "close")

        dialog = _wait_destructive_dialog()
        if dialog is None and not read_state["error"]:
            open_delete_btn = page.locator(
                "ytcp-button#delete-button, ytcp-button:has-text('Delete video'), "
                "ytcp-button:has-text('Xóa video')"
            ).first
            if _click_visible(open_delete_btn, "open-delete"):
                dialog = _wait_destructive_dialog()

        if dialog is None:
            result["dialog_open"] = bool(_visible_destructive_dialog())
            if read_state["error"]:
                return _finish(
                    "uncertain",
                    "⚠️ Không đọc được hộp thoại xác nhận hủy/xóa trên Studio. "
                    "Hãy mở YouTube Studio kiểm tra và xóa thủ công nếu còn.",
                    "WARNING",
                )
            return _finish(
                "not_found",
                "⚠️ Không quan sát thấy hộp thoại xác nhận hủy/xóa bản nháp trên Studio. "
                "Đã dừng thao tác để tránh báo xóa sai; hãy mở Studio kiểm tra thủ công.",
                "WARNING",
            )

        result["dialog_observed"] = True

        if not _dialog_has_confirm(dialog):
            discard_btn = dialog.locator(
                "ytcp-button#delete-button, ytcp-button:has-text('Delete forever'), "
                "ytcp-button:has-text('Xóa vĩnh viễn'), ytcp-button:has-text('Discard changes'), "
                "ytcp-button:has-text('Hủy thay đổi'), ytcp-button:has-text('Discard')"
            ).first
            _click_visible(discard_btn, "delete")
            dialog = _visible_destructive_dialog() or dialog

        confirm_chk = dialog.locator(
            "tp-yt-paper-checkbox#confirm-checkbox, tp-yt-paper-checkbox:has-text('understand'), "
            "tp-yt-paper-checkbox:has-text('hiểu'), tp-yt-paper-checkbox"
        ).first
        _click_visible(confirm_chk, "confirm-checkbox")

        del_forever_btn = dialog.locator(
            "#confirm-button, ytcp-button:has-text('Delete forever'), "
            "ytcp-button:has-text('Xóa vĩnh viễn')"
        ).first
        confirm_clicked = _click_visible(del_forever_btn, "confirm-delete")

        page.wait_for_timeout(3000)
        read_state["error"] = False
        result["dialog_open"] = _visible_destructive_dialog() is not None
        final_read_error = read_state["error"]

        if not confirm_clicked:
            return _finish(
                "uncertain" if result["clicked"] else "not_found",
                "⚠️ Đã thấy hộp thoại hủy/xóa nhưng không bấm được nút xác nhận. "
                "Hãy mở YouTube Studio kiểm tra và xóa thủ công nếu còn.",
                "WARNING",
            )
        if final_read_error or result["dialog_open"]:
            return _finish(
                "uncertain",
                "⚠️ Đã bấm xác nhận hủy/xóa nhưng CHƯA xác minh được hộp thoại đã đóng. "
                "Hãy mở YouTube Studio kiểm tra và xóa thủ công nếu còn.",
                "WARNING",
            )
        return _finish(
            "deleted",
            "✅ Đã hủy tải lên và xóa bản nháp video khỏi Studio "
            "(đã thấy hộp thoại xác nhận và hộp thoại đã đóng).",
            "INFO",
        )
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        add_log(
            "CopyrightGuardian",
            f"❌ Không hủy/xóa được bản nháp sau cảnh báo bản quyền ({e}). "
            "Hãy mở YouTube Studio kiểm tra và xóa nháp thủ công.",
            "ERROR",
            channel_name=channel_name,
            video_title=video_title,
        )
    return result


class _StudioBrowserSession:
    """Mở trình duyệt Studio theo 2 chế độ: Chrome cục bộ hoặc profile Antidetect qua CDP.

    Chế độ Antidetect không đổi UA/viewport/mobile/proxy và không nạp stealth script;
    Antidetect đã làm việc đó cho toàn bộ tab mới. Chỉ các tab do session tạo mới bị đóng.
    """

    def __init__(
        self,
        *,
        channel: Dict[str, Any],
        prof_name: str,
        prof_path: str,
        chrome_exe: Optional[str],
        is_mobile_mode: bool,
        hide_browser: bool,
        antidetect_profile_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.channel = channel
        self.prof_name = prof_name
        self.prof_path = prof_path
        self.chrome_exe = chrome_exe
        self.is_mobile_mode = is_mobile_mode
        self.hide_browser = hide_browser
        self.antidetect_profile_id = antidetect_profile_id
        self.metadata = metadata or {}
        self.context = None
        self.page = None
        self.init_scripts: list = []
        self._pw = None
        self._lease = None
        self._closed = False

    def __enter__(self):
        if self.antidetect_profile_id:
            if self.is_mobile_mode:
                add_log(
                    "Upload",
                    "Đã bỏ qua chế độ giả lập Mobile: Profile Antidetect tự quản lý fingerprint/viewport.",
                    "WARNING",
                    channel_name=self.channel.get("name", ""),
                    video_title=self.metadata.get("title", ""),
                )
            self.is_mobile_mode = False
            self._lease = AntidetectBrowserLease(
                self.antidetect_profile_id,
                stop_if_started=True,
                hide_window=self.hide_browser,
            )
            self._lease.__enter__()
            self.context = self._lease.tracked_context
            self.page = self._lease.page
            self.init_scripts = []
            return self

        from playwright.sync_api import sync_playwright
        Path(self.prof_path).mkdir(parents=True, exist_ok=True)

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--mute-audio",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if self.hide_browser:
            launch_args.append("--window-position=-3200,-3200")

        ctx_kwargs: Dict[str, Any] = {
            "user_data_dir": self.prof_path,
            "headless": False,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"],
        }
        if self.is_mobile_mode:
            mobile_ua = (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Mobile Safari/537.36"
            )
            ctx_kwargs.update({
                "user_agent": mobile_ua,
                "viewport": {"width": 412, "height": 915},
                "is_mobile": True,
                "has_touch": True,
                "device_scale_factor": 2.625,
                "extra_http_headers": {
                    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                    "sec-ch-ua-mobile": "?1",
                    "sec-ch-ua-platform": '"Android"',
                },
            })
        else:
            # Chế độ Desktop nguyên bản (Khuyên dùng): Giữ nguyên 100% Fingerprint thật của máy tính
            ctx_kwargs.update({
                "viewport": {"width": 1366, "height": 768},
                "is_mobile": False,
                "has_touch": False,
                "device_scale_factor": 1.0,
            })

        proxy_config = _parse_channel_proxy(self.channel)
        if proxy_config:
            ctx_kwargs["proxy"] = {
                "server": f"http://{proxy_config['host']}:{proxy_config['port']}"
            }
            if proxy_config["username"]:
                ctx_kwargs["proxy"].update({
                    "username": proxy_config["username"],
                    "password": proxy_config["password"],
                })

        if self.chrome_exe:
            ctx_kwargs["executable_path"] = self.chrome_exe

        self._pw = sync_playwright().start()
        try:
            self.context = _launch_persistent_context_guarded(self._pw, ctx_kwargs, self.prof_name)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.init_scripts = [STEALTH_JS] + ([MOBILE_PATCH_JS] if self.is_mobile_mode else [])
            for script in self.init_scripts:
                self.page.add_init_script(script)
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None
            return
        if self.context is not None:
            try:
                self.context.close()
            except Exception as close_err:
                _log_optional_step_failure("đóng Chrome sau khi upload", close_err, self.channel, self.metadata)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None


def upload_via_playwright_studio(
    video_path: str,
    metadata: Dict[str, Any],
    channel: Dict[str, Any],
    thumbnail_path: str = "",
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Tự động điều khiển trình duyệt (Playwright) đăng bài lên YouTube Studio (https://studio.youtube.com).
    Tích hợp khóa đồng bộ Profile Chrome, Anti-Checkpoint Stealth Shield & Copyright Check Guardian.
    Với Profile Antidetect: dùng chung trình duyệt qua CDP, không tự mở Chrome/user_data_dir.
    """
    settings = get_settings()
    hide_browser = bool(channel.get("use_hidden_desktop") or settings.get("hide_browser", False))
    prof_name = channel.get("profile_name") or f"channel_{channel.get('id', 'default')}"
    prof_path = channel.get("profile_path") or str(APP_PROFILES_DIR / prof_name)
    ad_id = _channel_antidetect_id(channel)
    if not ad_id and str(prof_path).lower().startswith("antidetect://"):
        raise RuntimeError(
            f"Profile '{prof_name}' được đánh dấu thuộc Antidetect nhưng chưa có ID liên kết. "
            "Hãy bấm 'Đồng Bộ Antidetect' trong tab Quản Lý Kênh rồi thử lại."
        )
    if not ad_id:
        Path(prof_path).mkdir(parents=True, exist_ok=True)

    is_mobile_mode = bool(metadata.get("emulate_mobile", False)) and not ad_id
    prof_lock = _get_antidetect_lock(ad_id) if ad_id else _get_profile_lock(prof_path)
    chrome_exe = None if ad_id else find_system_chrome_exe()

    if progress_callback:
        progress_callback(8, f"Đang chờ phiên Chrome Profile '{prof_name}' sẵn sàng...")

    with prof_lock:
        if progress_callback:
            if ad_id:
                progress_callback(10, f"Đang mở/kết nối Profile Antidetect '{prof_name}'...")
            else:
                progress_callback(10, f"Khởi động trình duyệt Profile '{prof_name}' (Chống Checkpoint)...")

        with _StudioBrowserSession(
            channel=channel,
            prof_name=prof_name,
            prof_path=prof_path,
            chrome_exe=chrome_exe,
            is_mobile_mode=is_mobile_mode,
            hide_browser=hide_browser,
            antidetect_profile_id=ad_id,
            metadata=metadata,
        ) as browser_session:
            context = browser_session.context
            page = browser_session.page
            init_scripts = browser_session.init_scripts
            draft_created = False
            publish_clicked = False
            draft_preserve_for_review = False
            checks_safe_seen = False
            try:
                if progress_callback:
                    progress_callback(20, f"Đang mở YouTube Studio ({'📱 Giả lập Điện thoại' if is_mobile_mode else '💻 Máy tính'})...")

                page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)

                try:
                    cont_btn = page.locator(
                        "a:has-text('Continue to Studio'), button:has-text('Continue to Studio'), "
                        "a:has-text('Tiếp tục tới Studio'), button:has-text('Tiếp tục tới Studio'), "
                        "[aria-label*='Continue to Studio' i]"
                    ).first
                    if cont_btn.count() > 0 and cont_btn.is_visible():
                        cont_btn.click()
                except Exception as cont_err:
                    _log_optional_step_failure("bấm Continue to Studio", cont_err, channel, metadata)

                if "accounts.google.com" in page.url or "signin" in page.url.lower():
                    raise RuntimeError(
                        f"Profile '{prof_name}' chưa đăng nhập tài khoản Google/YouTube. Hãy bấm nút 'Mở Profile Đăng Nhập' trong tab Quản Lý Kênh trước!"
                    )

                # BẮT BUỘC: chọn đúng kênh con và xác minh lại trước khi nạp file lên Studio.
                # Nếu không khớp kênh đích -> dừng ngay, không đăng nhầm kênh.
                page, active_channel = select_studio_channel(
                    page,
                    channel,
                    context=context,
                    init_scripts=init_scripts,
                    progress_callback=progress_callback,
                )
                add_log(
                    "Upload",
                    f"Đã xác minh kênh đích trên Studio: {active_channel.get('name') or channel.get('name')}"
                    + (f" ({active_channel['handle']})" if active_channel.get("handle") else "")
                    + (f" [channel_id={active_channel['channel_id']}]" if active_channel.get("channel_id") else ""),
                    "INFO",
                    channel_name=channel.get("name", ""),
                    video_title=metadata.get("title", ""),
                )

                known_video_ids = _snapshot_studio_exact_title_ids(
                    page,
                    active_channel.get("channel_id", ""),
                    metadata.get("title", ""),
                    metadata.get("upload_to", ""),
                )
                if known_video_ids is None:
                    add_log(
                        "Upload",
                        "Không đọc được danh sách video hiện có để đối chiếu — chỉ chấp nhận bằng chứng "
                        "từ hộp thoại chia sẻ, không dùng fallback danh sách nội dung.",
                        "WARNING",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )

                if progress_callback:
                    progress_callback(30, "Mở hộp thoại tải video lên...")

                create_btn = page.locator(STUDIO_CREATE_BUTTON_SELECTOR).first
                create_btn.wait_for(state="visible", timeout=30000)
                human_move_and_click(page, create_btn, delay_after=0)
                upload_menu = page.locator(
                    "#text-item-0, tp-yt-paper-item:has-text('Upload'), tp-yt-paper-item:has-text('Tải video')"
                ).first
                upload_menu.wait_for(state="visible", timeout=10000)
                human_move_and_click(page, upload_menu, delay_after=0)

                file_input = page.locator("input[type='file']").first
                file_input.wait_for(state="attached", timeout=15000)
                attach_upload_file(page, file_input, video_path)
                draft_created = True
                if progress_callback:
                    progress_callback(40, "Đang chuyển file lên YouTube Studio...")

                if progress_callback:
                    progress_callback(45, "Đang điền Tiêu đề, Mô tả (Mô phỏng gõ phím người thật)...")

                title_box = page.locator("#title-textarea #textbox, #textbox").first
                title_box.wait_for(state="visible", timeout=120000)
                simulate_human_type(page, title_box, metadata["title"], is_long_text=False)

                desc_box = page.locator("#description-textarea #textbox").first
                if desc_box.count() > 0 and metadata["description"]:
                    simulate_human_type(page, desc_box, metadata["description"], is_long_text=True)

                if progress_callback:
                    progress_callback(60, "Thiết lập Đối tượng người xem & Giới hạn độ tuổi...")

                human_scroll(page, delta_y=280)
                if metadata.get("made_for_kids"):
                    mfk_radio = page.locator(
                        "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_MFK'], "
                        "tp-yt-paper-radio-button:has-text('Yes, it\\'s made for kids'), "
                        "tp-yt-paper-radio-button:has-text('Có, nội dung này dành cho trẻ em')"
                    ).first
                else:
                    mfk_radio = page.locator(
                        "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], "
                        "tp-yt-paper-radio-button[name='NOT_MADE_FOR_KIDS'], "
                        "tp-yt-paper-radio-button:has-text('No, it\\'s not made for kids'), "
                        "tp-yt-paper-radio-button:has-text('Không, nội dung này không dành cho trẻ em')"
                    ).first
                # Radio đã được chọn theo đúng giá trị mong muốn, vì vậy điều kiện
                # xác minh là chính radio đó ĐƯỢC tích (expected=True), không phải
                # giá trị tùy chọn — passes bool(made_for_kids) từng làm bước này
                # "pass" mà không thực sự tích chọn.
                _set_radio_and_verify(page, mfk_radio, True, "Dành cho trẻ em")

                # Mở rộng phần "Hiển thị thêm" (Show more) TRƯỚC để nhìn thấy mục Giới hạn độ tuổi & Nhãn AI
                human_scroll(page, delta_y=300)
                toggle_more = page.locator("#toggle-button").first
                if toggle_more.count() > 0 and toggle_more.is_visible():
                    human_move_and_click(page, toggle_more, delay_after=900)

                try:
                    human_scroll(page, delta_y=220)
                    age_section = page.locator(
                        "button.expand-button:has-text('Giới hạn độ tuổi'), "
                        "button.expand-button:has-text('Age restriction'), "
                        "#age-restriction-advanced, ytcp-button[aria-label*='Age restriction' i], "
                        "ytcp-button:has-text('Age restriction'), ytcp-button:has-text('Giới hạn độ tuổi')"
                    ).first
                    age_group = page.locator("ytcp-age-restriction-select#age-restriction").first
                    age_visible = page.locator(
                        "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_RESTRICTED']:visible, "
                        "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_NONE']:visible"
                    ).count() > 0
                    if age_section.count() > 0 and (not age_visible or age_group.get_attribute("hidden") is not None):
                        human_move_and_click(page, age_section, delay_after=700)

                    age_group.wait_for(state="visible", timeout=10000)

                    if metadata.get("age_restriction"):
                        age_radio = age_group.locator(
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_RESTRICTED']"
                        ).first
                    else:
                        age_radio = age_group.locator(
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_NONE'], "
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_NOT_RESTRICTED']"
                        ).first
                    _set_radio_and_verify(page, age_radio, True, "Giới hạn độ tuổi")
                except Exception as age_err:
                    raise RuntimeError(
                        f"Không xác nhận được lựa chọn Giới hạn độ tuổi trên Studio: {age_err}. "
                        "Đã dừng trước khi gửi đăng."
                    ) from age_err

                if thumbnail_path and os.path.exists(thumbnail_path):
                    try:
                        thumb_input = page.locator("input#file-loader, input[type='file'][accept*='image']").first
                        if thumb_input.count() > 0:
                            thumb_input.set_input_files(thumbnail_path, timeout=5000)
                            page.wait_for_timeout(1500)
                    except Exception as thumb_err:
                        _log_optional_step_failure("tải thumbnail", thumb_err, channel, metadata)

                human_scroll(page, delta_y=220)
                # Studio đặt tên radio là VIDEO_HAS_ALTERED_CONTENT_*; dạng không có "HAS_"
                # từng khiến bước này báo "Không tìm thấy lựa chọn 'Nhãn nội dung AI'".
                if metadata.get("altered_content"):
                    alt_radio = page.locator(
                        "tp-yt-paper-radio-button[name='VIDEO_HAS_ALTERED_CONTENT_YES'], "
                        "tp-yt-paper-radio-button[name='VIDEO_ALTERED_CONTENT_YES']"
                    ).first
                else:
                    alt_radio = page.locator(
                        "tp-yt-paper-radio-button[name='VIDEO_HAS_ALTERED_CONTENT_NO'], "
                        "tp-yt-paper-radio-button[name='VIDEO_ALTERED_CONTENT_NO']"
                    ).first
                _set_radio_and_verify(page, alt_radio, True, "Nhãn nội dung AI")

                if metadata["tags"]:
                    tags_input = page.locator("input[aria-label*='Tags' i], input[aria-label*='Thẻ' i], #text-input").first
                    if tags_input.count() > 0 and tags_input.is_visible():
                        tags_input.fill(", ".join(metadata["tags"]))
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(600)

                if progress_callback:
                    progress_callback(68, "Hoàn tất Tiêu Đề & Đối Tượng Người Xem -> Sang Thành phần video...")

                page.wait_for_timeout(1500)
                next_btn = page.locator("#next-button").first
                if next_btn.count() > 0 and next_btn.is_visible():
                    human_move_and_click(page, next_btn, delay_after=1500)

                if progress_callback:
                    progress_callback(72, "Bỏ qua màn hình kết thúc -> Chuyển sang bước KIỂM DUYỆT (Checks)...")

                page.wait_for_timeout(1500)
                next_btn = page.locator("#next-button").first
                if next_btn.count() > 0 and next_btn.is_visible():
                    human_move_and_click(page, next_btn, delay_after=2000)

                if progress_callback:
                    progress_callback(75, "Đang ở Bước Kiểm Duyệt (Checks) - Theo dõi quét bản quyền Content ID...")

                max_checks_wait = max(30, int(settings.get("checks_timeout_seconds", 120) or 120))
                start_wait = time.monotonic()
                checks_deadline = start_wait + max_checks_wait
                soft_warning_note = ""
                soft_warned = False
                last_checks_text = ""
                last_probe_error = ""

                while time.monotonic() < checks_deadline:
                    elapsed = int(time.monotonic() - start_wait)
                    wait_slice_ms = max(1, min(5000, int((checks_deadline - time.monotonic()) * 1000)))
                    try:
                        page.wait_for_function(
                            """({safe, violation, processing}) => {
                                const el = document.querySelector(
                                    'ytcp-uploads-checks, #checks-content, div[test-id="checks-content"]'
                                );
                                if (!el || !el.getClientRects().length) return false;
                                const text = (el.innerText || '').toLowerCase();
                                return (safe.some(value => text.includes(value))
                                    && !processing.some(value => text.includes(value)))
                                    || violation.some(value => text.includes(value));
                            }""",
                            arg={
                                "safe": _CHECKS_SAFE_KEYWORDS,
                                "violation": _CHECKS_VIOLATION_KEYWORDS,
                                "processing": _CHECKS_PROCESSING_KEYWORDS,
                            },
                            timeout=wait_slice_ms,
                            polling="mutation",
                        )
                    except Exception as wait_err:
                        if time.monotonic() >= checks_deadline:
                            last_probe_error = str(wait_err)
                            break
                        cur_pct = min(84, 75 + int(elapsed * 9 / max_checks_wait))
                        if progress_callback:
                            progress_callback(cur_pct, f"YouTube đang chạy Checks... ({elapsed}s)")
                        continue

                    chk = check_copyright_and_violations(page)
                    if chk.get("probe_error"):
                        last_probe_error = chk["probe_error"]
                    if chk["has_violation"]:
                        reason = chk["reason"]
                        if progress_callback:
                            progress_callback(80, f"🛑 PHÁT HIỆN VI PHẠM BẢN QUYỀN: {reason}! Đang HỦY VIDEO...")

                        discard_and_cancel_upload(
                            page=page,
                            channel_name=channel.get("name", ""),
                            video_title=metadata.get("title", ""),
                            reason=reason,
                        )

                        return {
                            "cancelled": True,
                            "url": "",
                            "flagged_copyright": True,
                            "warning_message": f"Phát hiện cảnh báo vi phạm bản quyền trên YouTube Studio ({reason}). Đã tự động HỦY BỎ và XÓA video để bảo vệ kênh an toàn!",
                        }

                    if chk.get("soft_warning") and not soft_warned:
                        soft_warned = True
                        soft_warning_note = chk["soft_warning"]
                        add_log(
                            "Checks",
                            f"⚠️ {soft_warning_note}. Video vẫn tiếp tục được đăng (không ảnh hưởng tính toàn vẹn kênh).",
                            "WARNING",
                            channel_name=channel.get("name", ""),
                            video_title=metadata.get("title", ""),
                        )
                        if progress_callback:
                            progress_callback(80, f"⚠️ {soft_warning_note} — tiếp tục đăng...")

                    try:
                        last_checks_text = _read_checks_text(page)
                        if checks_report_safe(last_checks_text):
                            checks_safe_seen = True
                            break
                    except Exception as read_err:
                        last_probe_error = str(read_err)

                final_chk = check_copyright_and_violations(page)
                if final_chk["has_violation"]:
                    reason = final_chk["reason"]
                    discard_and_cancel_upload(
                        page=page,
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                        reason=reason,
                    )
                    return {
                        "cancelled": True,
                        "url": "",
                        "flagged_copyright": True,
                        "warning_message": f"Phát hiện cảnh báo vi phạm bản quyền ({reason}). Đã tự động HỦY BỎ và XÓA video để bảo vệ kênh an toàn!",
                    }

                if not checks_safe_seen:
                    # Đọc lại lần cuối: YouTube có thể vừa hiện "No issues found" ngay sát hạn chờ.
                    try:
                        last_checks_text = _read_checks_text(page)
                        checks_safe_seen = checks_report_safe(last_checks_text)
                    except Exception as read_err:
                        last_probe_error = str(read_err)

                if not checks_safe_seen:
                    draft_preserve_for_review = True
                    checks_error = (
                        f"Không xác nhận được kết quả kiểm duyệt an toàn sau {max_checks_wait}s "
                        "(YouTube chưa báo hoàn tất hoặc nội dung trạng thái không xác định). "
                        "Đã dừng để tránh đăng video chưa kiểm tra xong."
                    )
                    if last_probe_error:
                        checks_error += f" Lỗi đọc trạng thái gần nhất: {last_probe_error}."
                    add_log(
                        "Checks",
                        f"🛑 {checks_error}",
                        "ERROR",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )
                    raise RuntimeError(checks_error)

                checks_elapsed = int(time.monotonic() - start_wait)
                add_log(
                    "Checks",
                    f"✅ Kiểm duyệt YouTube hoàn tất sau ~{checks_elapsed}s: KHÔNG có cảnh báo chặn đăng."
                    + (f" ({soft_warning_note})" if soft_warning_note else ""),
                    "SUCCESS",
                    channel_name=channel.get("name", ""),
                    video_title=metadata.get("title", ""),
                )

                if progress_callback:
                    progress_callback(85, "Kiểm duyệt an toàn (No issues found) ✔️. Chuyển sang bước Khả năng hiển thị...")

                next_btn = page.locator("#next-button").first
                if next_btn.count() > 0 and next_btn.is_visible():
                    human_move_and_click(page, next_btn, delay_after=0)

                privacy_status = metadata["privacy_status"]

                if privacy_status == "schedule":
                    sched_str = (metadata.get("schedule_time") or "").strip()
                    if not sched_str:
                        raise RuntimeError(
                            "Video đang ở chế độ Lên lịch nhưng thiếu thời gian đăng. "
                            "Đã dừng trước khi bấm Xuất bản để tránh đăng Công khai nhầm."
                        )
                    if progress_callback:
                        progress_callback(88, f"Thiết lập Lịch đăng: {sched_str}...")

                    sched_radio = page.locator(
                        "tp-yt-paper-radio-button[name='SCHEDULE'], #schedule-radio-button, "
                        "tp-yt-paper-radio-button:has-text('Schedule'), tp-yt-paper-radio-button:has-text('Lên lịch')"
                    ).first
                    if sched_radio.count() == 0:
                        raise RuntimeError(
                            "Không tìm thấy tuỳ chọn Lên lịch trên YouTube Studio. Đã dừng để tránh đăng Công khai nhầm."
                        )
                    _set_radio_and_verify(page, sched_radio, True, "Lên lịch")

                    try:
                        from datetime import datetime
                        dt_obj = datetime.fromisoformat(sched_str.replace(" ", "T"))
                        html_lang = (page.locator("html").get_attribute("lang") or "vi").lower()

                        date_input = page.locator(
                            "#datepicker-trigger input, input[aria-label*='Date' i], input[aria-label*='Ngày' i]"
                        ).first
                        if date_input.count() == 0:
                            raise RuntimeError("Không tìm thấy ô chọn ngày đăng trên Studio.")
                        human_move_and_click(page, date_input, delay_after=300)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                        date_str = dt_obj.strftime("%d/%m/%Y") if "vi" in html_lang else dt_obj.strftime("%b %d, %Y")
                        page.keyboard.type(date_str, delay=40)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(600)

                        time_input = page.locator(
                            "#time-of-day-trigger input, input[aria-label*='Time' i], input[aria-label*='Giờ' i]"
                        ).first
                        if time_input.count() == 0:
                            raise RuntimeError("Không tìm thấy ô chọn giờ đăng trên Studio.")
                        human_move_and_click(page, time_input, delay_after=300)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                        page.keyboard.type(dt_obj.strftime("%H:%M"), delay=40)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(600)

                        if not (date_input.input_value() or "").strip():
                            raise RuntimeError("Ô ngày đăng bị trống sau khi điền.")
                        if not (time_input.input_value() or "").strip():
                            raise RuntimeError("Ô giờ đăng bị trống sau khi điền.")
                        if not _schedule_values_match(
                            date_input.input_value(), time_input.input_value(), dt_obj
                        ):
                            raise RuntimeError(
                                "Giờ/ngày đọc lại từ YouTube Studio không khớp lịch đã yêu cầu."
                            )
                    except RuntimeError:
                        raise
                    except Exception as sched_err:
                        raise RuntimeError(
                            f"Không điền được ngày giờ hẹn lịch ({sched_str}): {sched_err}. "
                            "Đã dừng để tránh đăng sai giờ hoặc đăng Công khai nhầm."
                        )

                    if metadata.get("is_premiere"):
                        prem_cb = page.locator(
                            "tp-yt-paper-checkbox#schedule-premiere-checkbox, "
                            "tp-yt-paper-checkbox:has-text('Set as Premiere'), tp-yt-paper-checkbox:has-text('Công chiếu')"
                        ).first
                        if prem_cb.count() > 0 and not prem_cb.get_attribute("checked"):
                            human_move_and_click(page, prem_cb, delay_after=500)

                else:
                    priv_map = {
                        "public": "PUBLIC",
                        "unlisted": "UNLISTED",
                        "private": "PRIVATE",
                    }
                    radio_name = priv_map.get(privacy_status)
                    if not radio_name:
                        raise RuntimeError(
                            f"Chế độ hiển thị '{privacy_status}' không hợp lệ. Đã dừng để tránh đăng sai trạng thái."
                        )
                    if progress_callback:
                        progress_callback(88, f"Thiết lập Chế độ hiển thị: {radio_name}...")

                    vis_radio = page.locator(f"tp-yt-paper-radio-button[name='{radio_name}']").first
                    if vis_radio.count() == 0:
                        raise RuntimeError(
                            f"Không tìm thấy tuỳ chọn hiển thị {radio_name} trên Studio. "
                            "Đã dừng để tránh đăng sai trạng thái."
                        )
                    _set_radio_and_verify(page, vis_radio, True, f"Hiển thị {radio_name}")

                    if radio_name == "PUBLIC" and metadata.get("is_premiere"):
                        prem_inst = page.locator(
                            "tp-yt-paper-checkbox#set-as-premiere-checkbox, "
                            "tp-yt-paper-checkbox:has-text('Premiere'), tp-yt-paper-checkbox:has-text('công chiếu')"
                        ).first
                        if prem_inst.count() > 0 and not prem_inst.get_attribute("checked"):
                            human_move_and_click(page, prem_inst, delay_after=500)

                # Không lấy URL trước khi đăng: chỉ URL trong hộp thoại chia sẻ sau khi bấm mới là bằng chứng.
                video_url = ""

                # Bấm nút Xuất bản / Lên lịch / Lưu
                done_btn = page.locator(
                    "#done-button, ytcp-button#done-button, ytcp-button:has-text('Publish'), "
                    "ytcp-button:has-text('Schedule'), ytcp-button:has-text('Save'), "
                    "ytcp-button:has-text('Lưu'), ytcp-button:has-text('Xuất bản')"
                ).first
                done_btn.wait_for(state="visible", timeout=15000)
                if not done_btn.is_enabled():
                    raise RuntimeError("Nút Xuất bản/Lên lịch chưa sẵn sàng trên YouTube Studio.")
                studio_url_before_publish = str(page.url or "")
                if progress_callback:
                    progress_callback(92, "Đang gửi yêu cầu Xuất bản/Lên lịch lên YouTube Studio...")
                # Mark intent before dispatch: Playwright can lose the connection
                # after the click was sent but before it reports success.
                publish_clicked = True
                human_move_and_click(page, done_btn, delay_after=3000)
                # Từ đây video có thể đã được gửi đăng: TUYỆT ĐỐI không tự xóa nháp.

                if progress_callback:
                    progress_callback(96, "Đã gửi yêu cầu; đang xác minh URL video từ Studio...")

                verified_url = _wait_for_publish_completion(
                    page, studio_url_before_publish, PUBLISH_CONFIRM_TIMEOUT_SECONDS
                ) or video_url
                if not verified_url:
                    verified_url = _find_published_video_in_studio_content(
                        page,
                        active_channel.get("channel_id", ""),
                        metadata.get("title", ""),
                        metadata.get("upload_to", ""),
                        metadata.get("privacy_status", ""),
                        known_video_ids,
                    )

                # Đóng hộp thoại xác nhận hoàn tất của Studio để Cookie phiên kịp đồng bộ
                try:
                    close_share_dialog = page.locator(
                        "ytcp-video-share-dialog #close-button, "
                        "ytcp-uploads-still-processing-dialog #close-button, "
                        "ytcp-button#close-icon-button"
                    ).first
                    if close_share_dialog.count() > 0 and close_share_dialog.is_visible():
                        human_move_and_click(page, close_share_dialog, delay_after=0)
                except Exception as dialog_err:
                    _log_optional_step_failure("đóng hộp thoại xác nhận đăng", dialog_err, channel, metadata)

                if not verified_url:
                    uncertain_msg = (
                        "Đã bấm Xuất bản/Lên lịch nhưng KHÔNG đọc được URL video thật từ hộp thoại hoặc "
                        "danh sách Nội dung (Video/Shorts) của YouTube Studio. "
                        "Không thể xác minh video đã đăng thành công — hãy mở Studio kiểm tra trực tiếp "
                        "trước khi chạy lại để tránh đăng trùng."
                    )
                    add_log(
                        "Upload",
                        f"❌ {uncertain_msg}",
                        "ERROR",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )
                    if progress_callback:
                        progress_callback(100, "Đã bấm đăng nhưng CHƯA xác minh được URL video!")
                    return {
                        "cancelled": False,
                        "url": "",
                        "flagged_copyright": False,
                        "warning_message": uncertain_msg,
                        "published_uncertain": True,
                    }

                add_log(
                    "Upload",
                    f"✅ Đã xác minh video đăng thành công: {verified_url}",
                    "SUCCESS",
                    channel_name=channel.get("name", ""),
                    video_title=metadata.get("title", ""),
                )
                if progress_callback:
                    progress_callback(100, "Đăng YouTube Studio thành công!")

                return {
                    "cancelled": False,
                    "url": verified_url,
                    "flagged_copyright": False,
                    "warning_message": "",
                    "published_uncertain": False,
                }
            except Exception:
                # Upload gãy giữa chừng SAU KHI đã nạp file: dọn BẢN NHÁP trong Studio
                # để kênh không tồn video rác (rủi ro bị đánh giá spam).
                # Nếu đã bấm Xuất bản/Lên lịch thì KHÔNG xóa — tránh xóa nhầm video đã đăng.
                if publish_clicked:
                    add_log(
                        "Upload",
                        "⚠️ Lỗi xảy ra SAU khi đã bấm đăng — KHÔNG tự xóa bản nháp để tránh xóa nhầm "
                        "video đã xuất bản. Hãy mở YouTube Studio kiểm tra trực tiếp.",
                        "WARNING",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )
                elif draft_created and page is not None and not draft_preserve_for_review:
                    try:
                        add_log(
                            "Upload",
                            "⚠️ Upload thất bại giữa chừng — đang tự động HỦY và XÓA bản nháp trong Studio để bảo vệ kênh...",
                            "WARNING",
                            channel_name=channel.get("name", ""),
                            video_title=metadata.get("title", ""),
                        )
                        discard_and_cancel_upload(
                            page=page,
                            channel_name=channel.get("name", ""),
                            video_title=metadata.get("title", ""),
                            reason="Upload thất bại giữa chừng (lỗi kỹ thuật / mạng / DOM đổi)",
                        )
                    except Exception as cleanup_err:
                        add_log(
                            "Upload",
                            f"Không thể tự hủy bản nháp sau lỗi: {cleanup_err}",
                            "ERROR",
                            channel_name=channel.get("name", ""),
                            video_title=metadata.get("title", ""),
                        )
                elif draft_created and draft_preserve_for_review:
                    add_log(
                        "Upload",
                        "Giữ bản nháp để đối soát vì Checks chưa xác nhận trạng thái an toàn; không tự xóa video.",
                        "WARNING",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )
                raise


def upload_video_to_youtube(
    video_item: Dict[str, Any],
    channel: Dict[str, Any],
    option_group: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Any:
    """
    Hàm điều phối trung tâm: Tự động chọn phương thức đăng (Playwright YouTube Studio hoặc YouTube Data API v3).
    """
    metadata = validate_upload_preflight(video_item, channel, option_group)
    mode = (channel.get("upload_mode") or "playwright").strip().lower()
    video_path = video_item.get("output_video_path") or video_item.get("input_video_path") or ""
    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy file video để upload: {video_path}")

    if mode == "youtube_api":
        return upload_via_youtube_api(
            video_path=video_path,
            metadata=metadata,
            channel=channel,
            thumbnail_path=video_item.get("thumbnail_path") or "",
            progress_callback=progress_callback,
        )
    else:
        return upload_via_playwright_studio(
            video_path=video_path,
            metadata=metadata,
            channel=channel,
            thumbnail_path=video_item.get("thumbnail_path") or "",
            progress_callback=progress_callback,
        )


def validate_upload_preflight(
    video_item: Dict[str, Any],
    channel: Dict[str, Any],
    option_group: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate channel ownership and final metadata before download/render work starts."""
    if not (channel.get("channel_id") or "").strip():
        raise RuntimeError(
            f"Kênh '{channel.get('name') or channel.get('handle') or '?'}' chưa có Channel ID. "
            "Hãy đồng bộ hoặc nhập Channel ID trước khi chạy video."
        )
    metadata = build_final_metadata(video_item, channel, option_group)
    mode = (channel.get("upload_mode") or "playwright").strip().lower()
    ad_id = _channel_antidetect_id(channel)
    if mode == "youtube_api":
        if ad_id:
            raise RuntimeError(
                f"Kênh '{channel.get('name') or channel.get('handle') or '?'}' dùng Profile Antidetect nên "
                "không thể đăng bằng YouTube Data API. Hãy dùng YouTube Studio với đúng Profile Antidetect."
            )
        unsupported = []
        if metadata.get("age_restriction"):
            unsupported.append("giới hạn độ tuổi 18+")
        if metadata.get("is_premiere"):
            unsupported.append("Công chiếu")
        if unsupported:
            raise RuntimeError(
                "YouTube Data API hiện không thể xác minh/thiết lập " + ", ".join(unsupported)
                + "; hãy chuyển sang YouTube Studio trước khi tải hoặc render video."
            )
    if not ad_id:
        _parse_channel_proxy(channel)
    return metadata
