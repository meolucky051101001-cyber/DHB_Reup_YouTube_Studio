import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Any, Optional
from urllib.parse import quote
import requests

from config import (
    APP_PROFILES_DIR,
    BROWSER_HOST_EXE,
)
from database import add_log, get_settings

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB giống SliceBytes của DHB jFbxQL

_PROFILE_LOCKS_GUARD = threading.Lock()
_PROFILE_LOCKS: Dict[str, threading.Lock] = {}


def _get_profile_lock(prof_path: str) -> threading.Lock:
    """Đảm bảo tại một thời điểm chỉ có 1 tiến trình Playwright mở cùng 1 thư mục Chrome Profile."""
    norm_path = os.path.normcase(os.path.abspath(prof_path))
    with _PROFILE_LOCKS_GUARD:
        if norm_path not in _PROFILE_LOCKS:
            _PROFILE_LOCKS[norm_path] = threading.Lock()
        return _PROFILE_LOCKS[norm_path]


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
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\DHB Reup Facebook\Data\Chrome\chrome.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


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
    return {
        "title": title[:100],
        "description": desc[:4900],
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
    """
    prof_name = channel.get("profile_name") or f"channel_{channel.get('id', 'default')}"
    prof_path = channel.get("profile_path") or str(APP_PROFILES_DIR / prof_name)
    Path(prof_path).mkdir(parents=True, exist_ok=True)

    chrome_exe = find_system_chrome_exe()
    if not chrome_exe:
        raise RuntimeError("Không tìm thấy Google Chrome trên hệ thống.")

    proxy_val = (channel.get("proxy") or "").strip()
    p_parts = proxy_val.split(":") if proxy_val else []

    # Nếu Proxy có xác thực User/Password (4 thành phần), mở qua Playwright để xác thực tự động không bị lỗi mạng
    if len(p_parts) >= 4:
        def _launch_playwright_login():
            from playwright.sync_api import sync_playwright
            try:
                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=prof_path,
                        executable_path=chrome_exe,
                        headless=False,
                        args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
                        ignore_default_args=["--enable-automation"],
                        proxy={
                            "server": f"http://{p_parts[0]}:{p_parts[1]}",
                            "username": p_parts[2],
                            "password": p_parts[3],
                        },
                        viewport={"width": 1366, "height": 768},
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
    if len(p_parts) == 2:
        args.insert(-1, f"--proxy-server=http://{p_parts[0]}:{p_parts[1]}")
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


def _open_studio_account_menu(page) -> bool:
    try:
        header = page.locator("ytd-active-account-header-renderer").first
        if header.count() > 0 and header.is_visible():
            return True
        if _has_visible_account_items(page):
            _close_studio_account_menus(page)
        avatar = page.locator("#avatar-btn, ytcp-button#avatar-btn, img#avatar-btn").first
        if avatar.count() == 0:
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


def probe_profile_login(prof_path: str, proxy: str = "", grab_channels: bool = True) -> Dict[str, Any]:
    """
    Mở Chrome Profile ở vị trí cửa sổ khuất để KIỂM TRA trạng thái đăng nhập Google/YouTube,
    đồng thời quét danh sách kênh YouTube thật trong Profile qua hộp chuyển tài khoản của Studio.
    Luôn chạy định danh Windows Desktop nguyên bản (không ép Mobile UA) để đồng nhất fingerprint.
    """
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
    proxy_val = (proxy or "").strip()
    if proxy_val:
        p_parts = proxy_val.split(":")
        if len(p_parts) == 2:
            ctx_kwargs["proxy"] = {"server": f"http://{p_parts[0]}:{p_parts[1]}"}
        elif len(p_parts) >= 4:
            ctx_kwargs["proxy"] = {
                "server": f"http://{p_parts[0]}:{p_parts[1]}",
                "username": p_parts[2],
                "password": p_parts[3],
            }

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

                result["logged_in"] = True
                result["message"] = "Profile đã đăng nhập Google."
                if not grab_channels:
                    return result

                # Mở hộp chuyển tài khoản (Account Switcher) đã kiểm chứng DOM thật để liệt kê chính xác từng kênh
                try:
                    active = _read_active_studio_account(page)
                    entries = []
                    if _open_studio_account_menu(page):
                        active = _read_active_studio_account(page)
                        if _open_studio_switch_account_list(page):
                            entries = _list_studio_accounts(page)

                    current_email = active.get("email", "")
                    result["channels"] = [
                        {
                            "name": entry["name"],
                            "handle": entry["handle"],
                            "channel_id": entry.get("channel_id", ""),
                            "email": current_email,
                        }
                        for entry in entries
                        if entry["name"] or entry["handle"] or entry.get("channel_id")
                    ][:50]
                    if not result["channels"] and (active.get("name") or active.get("handle")):
                        result["channels"] = [{
                            "name": active.get("name", ""),
                            "handle": active.get("handle", ""),
                            "channel_id": active.get("channel_id", ""),
                            "email": current_email,
                        }]
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


def _proxy_url_from_channel(channel: Dict[str, Any]) -> str:
    """Chuyển proxy IP:PORT[:USER:PASS] của kênh thành URL cho requests (đồng bộ với Playwright)."""
    raw = (channel.get("proxy") or "").strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) not in (2, 4):
        raise ValueError(
            f"Proxy của kênh không đúng định dạng IP:PORT hoặc IP:PORT:USER:PASS: '{raw}'."
        )
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port:
        raise ValueError(f"Proxy của kênh thiếu host hoặc port: '{raw}'.")
    auth = ""
    if len(parts) == 4:
        auth = f"{quote(parts[2], safe='')}:{quote(parts[3], safe='')}@"
    return f"http://{auth}{host}:{port}"


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

    http = session or requests.Session()
    oauth_data = _parse_oauth_payload(channel.get("oauth_token_json") or "")
    access_token = _refresh_google_access_token(oauth_data, session=http)
    file_size = os.path.getsize(video_path)

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

    video_id = ""
    with open(video_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk = f.read(CHUNK_SIZE)
            chunk_len = len(chunk)
            end_byte = offset + chunk_len - 1
            put_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Length": str(chunk_len),
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
            }

            max_retries = 5
            put_resp = None
            for attempt in range(max_retries):
                try:
                    put_resp = http.put(upload_url, headers=put_headers, data=chunk, timeout=120)
                    if put_resp.status_code in (200, 201, 308):
                        break
                    if put_resp.status_code in (500, 502, 503, 504):
                        sleep_time = min(30, 2 ** (attempt + 1))
                        time.sleep(sleep_time)
                        continue
                    break
                except (requests.ConnectionError, requests.Timeout) as net_err:
                    if attempt < max_retries - 1:
                        sleep_time = min(30, 2 ** (attempt + 1))
                        time.sleep(sleep_time)
                    else:
                        raise RuntimeError(f"Lỗi mạng khi tải lên YouTube chunk {offset}: {net_err}")

            if put_resp is None:
                raise RuntimeError(f"Không nhận được phản hồi từ YouTube tại offset {offset}")

            if put_resp.status_code in (200, 201):
                res_json = put_resp.json()
                video_id = res_json.get("id", "")
                break
            elif put_resp.status_code == 308:
                offset += chunk_len
                pct = min(95, int(offset * 100 / file_size))
                if progress_callback:
                    progress_callback(pct, f"Đang tải lên YouTube API... {pct}%")
            else:
                raise RuntimeError(f"Lỗi khi đẩy dữ liệu video ({put_resp.status_code}): {put_resp.text}")

    if not video_id:
        raise RuntimeError("Không lấy được Video ID sau khi tải lên.")

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
    return f"https://youtu.be/{video_id}"


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


def checks_report_safe(raw_text: str) -> bool:
    """True khi YouTube Studio báo kiểm tra xong và không có vấn đề (đa ngôn ngữ)."""
    text = (raw_text or "").lower()
    return any(k in text for k in _CHECKS_SAFE_KEYWORDS)


def classify_checks_text(raw_text: str) -> Dict[str, Any]:
    """Phân loại văn bản bước Checks thành: an toàn / vi phạm (chặn) / cảnh báo mềm (vẫn đăng)."""
    text = (raw_text or "").lower()
    # Cảnh báo vi phạm phải thắng câu "Checks complete"/"No issues found" đi kèm:
    # bỏ các cụm an toàn trước khi dò vi phạm để "Checks complete. Copyright-protected content found"
    # không bị phân loại nhầm là an toàn.
    stripped = text
    for safe_kw in _CHECKS_SAFE_KEYWORDS:
        stripped = stripped.replace(safe_kw, " ")
    for vk in _CHECKS_VIOLATION_KEYWORDS:
        if vk in stripped:
            return {"has_violation": True, "reason": f"Phát hiện cảnh báo: {vk.title()}", "soft_warning": ""}
    if checks_report_safe(text):
        return {"has_violation": False, "reason": "", "soft_warning": ""}
    for sk in _CHECKS_SOFT_KEYWORDS:
        if sk in text:
            return {
                "has_violation": False,
                "reason": "",
                "soft_warning": f"Cảnh báo không chặn đăng (kiếm tiền/quảng cáo): {sk}",
            }
    return {"has_violation": False, "reason": "", "soft_warning": ""}


def check_copyright_and_violations(page) -> Dict[str, Any]:
    """
    Quét và phát hiện cảnh báo tại bước Checks của YouTube Studio (đa ngôn ngữ).
    Phân biệt 2 mức:
    - has_violation (CẢNH BÁO BẢN QUYỀN / BỊ CHẶN): tự động hủy và xóa nháp để bảo vệ kênh.
    - soft_warning (KHÔNG ĐỦ ĐIỀU KIỆN KIẾM TIỀN / HẠN CHẾ QUẢNG CÁO): chỉ ghi log, vẫn đăng bình thường.
    """
    state: Dict[str, Any] = {"has_violation": False, "reason": "", "soft_warning": "", "probe_error": ""}

    try:
        checks_container = page.locator("ytcp-uploads-checks, #checks-content, div[test-id='checks-content']").first
        scope = checks_container if (checks_container.count() > 0 and checks_container.is_visible()) else page
        raw_text = (scope.inner_text() or "").lower()
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


_PUBLISHED_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/))"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def normalize_published_video_url(raw: str) -> str:
    """Chuẩn hóa URL YouTube về dạng https://youtu.be/<id>; trả rỗng nếu không phải URL video."""
    match = _PUBLISHED_VIDEO_URL_RE.search((raw or "").strip())
    if not match:
        return ""
    return f"https://youtu.be/{match.group(1)}"


def extract_published_video_url(page) -> str:
    """Đọc URL video thật từ hộp thoại chia sẻ / liên kết video của YouTube Studio."""
    selectors = [
        "ytcp-video-share-dialog input#share-url",
        "ytcp-video-share-dialog input[value*='youtu']",
        "ytcp-video-share-dialog a[href*='youtu']",
        "ytcp-video-share-dialog a[href*='youtube']",
        "a.ytcp-video-info[href*='youtu']",
        "ytcp-video-info a[href*='youtu']",
        "span.video-url-fadeable a",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
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
            for raw in candidates:
                url = normalize_published_video_url(raw)
                if url:
                    return url
        except Exception:
            continue
    return ""


def discard_and_cancel_upload(page, channel_name: str = "", video_title: str = "", reason: str = "") -> None:
    """
    Hủy tải lên và xóa hoàn toàn video nháp trên YouTube Studio khi phát hiện vi phạm bản quyền.
    """
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
        if close_btn.count() > 0 and close_btn.is_visible():
            human_move_and_click(page, close_btn, delay_after=1500)

        discard_btn = page.locator(
            "ytcp-button#delete-button, ytcp-button:has-text('Delete video'), "
            "ytcp-button:has-text('Xóa video'), ytcp-button:has-text('Discard changes'), "
            "ytcp-button:has-text('Hủy thay đổi'), ytcp-button:has-text('Delete'), "
            "ytcp-button:has-text('Xóa'), ytcp-button:has-text('Discard'), "
            "ytcp-button:has-text('Hủy')"
        ).first
        if discard_btn.count() > 0 and discard_btn.is_visible():
            human_move_and_click(page, discard_btn, delay_after=1500)

        confirm_chk = page.locator(
            "tp-yt-paper-checkbox#confirm-checkbox, tp-yt-paper-checkbox:has-text('understand'), "
            "tp-yt-paper-checkbox:has-text('hiểu'), tp-yt-paper-checkbox"
        ).first
        if confirm_chk.count() > 0 and confirm_chk.is_visible():
            human_move_and_click(page, confirm_chk, delay_after=800)

        del_forever_btn = page.locator(
            "ytcp-button#confirm-button, ytcp-button:has-text('Delete forever'), "
            "ytcp-button:has-text('Xóa vĩnh viễn'), ytcp-button:has-text('Delete')"
        ).first
        if del_forever_btn.count() > 0 and del_forever_btn.is_visible():
            human_move_and_click(page, del_forever_btn, delay_after=2000)

        page.wait_for_timeout(3000)
        add_log(
            "CopyrightGuardian",
            "✅ Đã hủy tải lên và xóa sạch bản nháp video khỏi Studio thành công!",
            "INFO",
            channel_name=channel_name,
            video_title=video_title,
        )
    except Exception as e:
        add_log(
            "CopyrightGuardian",
            f"❌ Không hủy/xóa được bản nháp sau cảnh báo bản quyền ({e}). "
            "Hãy mở YouTube Studio kiểm tra và xóa nháp thủ công.",
            "ERROR",
            channel_name=channel_name,
            video_title=video_title,
        )


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
    """
    from playwright.sync_api import sync_playwright

    settings = get_settings()
    hide_browser = bool(channel.get("use_hidden_desktop") or settings.get("hide_browser", False))
    prof_name = channel.get("profile_name") or f"channel_{channel.get('id', 'default')}"
    prof_path = channel.get("profile_path") or str(APP_PROFILES_DIR / prof_name)
    Path(prof_path).mkdir(parents=True, exist_ok=True)

    chrome_exe = find_system_chrome_exe()
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--mute-audio",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    if hide_browser:
        launch_args.append("--window-position=-3200,-3200")

    is_mobile_mode = bool(metadata.get("emulate_mobile", False))
    prof_lock = _get_profile_lock(prof_path)

    if progress_callback:
        progress_callback(8, f"Đang chờ phiên Chrome Profile '{prof_name}' sẵn sàng...")

    with prof_lock:
        if progress_callback:
            progress_callback(10, f"Khởi động trình duyệt Profile '{prof_name}' (Chống Checkpoint)...")

        with sync_playwright() as p:
            ctx_kwargs: Dict[str, Any] = {
                "user_data_dir": prof_path,
                "headless": False,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"],
            }

            if is_mobile_mode:
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

            proxy_val = (channel.get("proxy") or "").strip()
            if proxy_val:
                p_parts = proxy_val.split(":")
                if len(p_parts) == 2:
                    ctx_kwargs["proxy"] = {"server": f"http://{p_parts[0]}:{p_parts[1]}"}
                elif len(p_parts) >= 4:
                    ctx_kwargs["proxy"] = {
                        "server": f"http://{p_parts[0]}:{p_parts[1]}",
                        "username": p_parts[2],
                        "password": p_parts[3],
                    }

            if chrome_exe:
                ctx_kwargs["executable_path"] = chrome_exe

            context = _launch_persistent_context_guarded(p, ctx_kwargs, prof_name)
            draft_created = False
            publish_clicked = False
            page = None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(STEALTH_JS)
                if is_mobile_mode:
                    page.add_init_script(MOBILE_PATCH_JS)

                if progress_callback:
                    progress_callback(20, f"Đang mở YouTube Studio ({'📱 Giả lập Điện thoại' if is_mobile_mode else '💻 Máy tính'})...")

                page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                try:
                    cont_btn = page.locator(
                        "a:has-text('Continue to Studio'), button:has-text('Continue to Studio'), "
                        "a:has-text('Tiếp tục tới Studio'), button:has-text('Tiếp tục tới Studio'), "
                        "[aria-label*='Continue to Studio' i]"
                    ).first
                    if cont_btn.count() > 0 and cont_btn.is_visible():
                        cont_btn.click()
                        page.wait_for_timeout(3000)
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
                    init_scripts=[STEALTH_JS] + ([MOBILE_PATCH_JS] if is_mobile_mode else []),
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

                if progress_callback:
                    progress_callback(30, "Mở hộp thoại tải video lên...")

                # Mô phỏng người thật: Dừng ngẫu nhiên và lướt chuột nhẹ qua bảng Dashboard Studio trước khi bấm Tạo
                try:
                    import random
                    page.mouse.move(random.randint(300, 650), random.randint(180, 400), steps=random.randint(14, 22))
                    page.wait_for_timeout(random.randint(1200, 2200))
                except Exception:
                    pass

                create_btn = page.locator("#create-icon, ytcp-button#create-icon, [aria-label*='Create' i], [aria-label*='Tạo' i]").first
                if create_btn.count() > 0:
                    human_move_and_click(page, create_btn, delay_after=1000)
                    upload_menu = page.locator("#text-item-0, tp-yt-paper-item:has-text('Upload'), tp-yt-paper-item:has-text('Tải video')").first
                    if upload_menu.count() > 0:
                        human_move_and_click(page, upload_menu, delay_after=1200)

                file_input = page.locator("input[type='file']").first
                file_input.set_input_files(video_path, timeout=30000)
                draft_created = True
                page.wait_for_timeout(2500)

                if progress_callback:
                    progress_callback(45, "Đang điền Tiêu đề, Mô tả (Mô phỏng gõ phím người thật)...")

                page.wait_for_selector("#title-textarea #textbox, #textbox[aria-label]", timeout=30000)
                page.wait_for_timeout(2000)

                title_box = page.locator("#title-textarea #textbox, #textbox").first
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
                if mfk_radio.count() > 0:
                    human_move_and_click(page, mfk_radio, delay_after=600)
                    try:
                        if (mfk_radio.get_attribute("aria-checked") or "").lower() != "true":
                            mfk_radio.click(force=True)
                            page.wait_for_timeout(600)
                    except Exception as mfk_err:
                        _log_optional_step_failure("xác nhận đối tượng trẻ em", mfk_err, channel, metadata)

                # Mở rộng phần "Hiển thị thêm" (Show more) TRƯỚC để nhìn thấy mục Giới hạn độ tuổi & Nhãn AI
                human_scroll(page, delta_y=300)
                toggle_more = page.locator("#toggle-button").first
                if toggle_more.count() > 0 and toggle_more.is_visible():
                    human_move_and_click(page, toggle_more, delay_after=900)

                age_confirmed = False
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

                    if metadata.get("age_restriction"):
                        age_radio = page.locator(
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_RESTRICTED'], "
                            "tp-yt-paper-radio-button:has-text('Yes, restrict my video'), "
                            "tp-yt-paper-radio-button:has-text('Có, giới hạn video')"
                        ).first
                    else:
                        age_radio = page.locator(
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_NONE'], "
                            "tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_NOT_RESTRICTED'], "
                            "tp-yt-paper-radio-button:has-text('No, don\\'t restrict'), "
                            "tp-yt-paper-radio-button:has-text('Không, đừng giới hạn')"
                        ).first
                    if age_radio.count() > 0 and age_radio.is_visible():
                        human_move_and_click(page, age_radio, delay_after=500)
                        if (age_radio.get_attribute("aria-checked") or "").lower() != "true":
                            age_radio.click(force=True)
                            page.wait_for_timeout(600)
                        age_confirmed = (age_radio.get_attribute("aria-checked") or "").lower() == "true"
                except Exception as age_err:
                    _log_optional_step_failure("thiết lập giới hạn độ tuổi", age_err, channel, metadata)

                if metadata.get("age_restriction") and not age_confirmed:
                    age_error = (
                        "Không xác nhận được tuỳ chọn giới hạn độ tuổi 18+ trên YouTube Studio. "
                        "Đã dừng trước khi đăng để tránh video sai độ tuổi."
                    )
                    add_log(
                        "Upload",
                        f"🛑 {age_error}",
                        "ERROR",
                        channel_name=channel.get("name", ""),
                        video_title=metadata.get("title", ""),
                    )
                    raise RuntimeError(age_error)

                if thumbnail_path and os.path.exists(thumbnail_path):
                    try:
                        thumb_input = page.locator("input#file-loader, input[type='file'][accept*='image']").first
                        if thumb_input.count() > 0:
                            thumb_input.set_input_files(thumbnail_path, timeout=5000)
                            page.wait_for_timeout(1500)
                    except Exception as thumb_err:
                        _log_optional_step_failure("tải thumbnail", thumb_err, channel, metadata)

                try:
                    human_scroll(page, delta_y=220)
                    if metadata.get("altered_content"):
                        alt_radio = page.locator("tp-yt-paper-radio-button[name='VIDEO_ALTERED_CONTENT_YES'], tp-yt-paper-radio-button:has-text('Yes'), tp-yt-paper-radio-button:has-text('Có')").first
                    else:
                        alt_radio = page.locator("tp-yt-paper-radio-button[name='VIDEO_ALTERED_CONTENT_NO'], tp-yt-paper-radio-button:has-text('No'), tp-yt-paper-radio-button:has-text('Không')").first
                    if alt_radio.count() > 0 and alt_radio.is_visible():
                        human_move_and_click(page, alt_radio, delay_after=500)
                except Exception as alt_err:
                    _log_optional_step_failure("chọn nhãn nội dung AI", alt_err, channel, metadata)

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
                start_wait = time.time()
                soft_warning_note = ""
                soft_warned = False
                checks_safe_seen = False
                last_checks_text = ""
                last_probe_error = ""

                while time.time() - start_wait < max_checks_wait:
                    page.wait_for_timeout(3000)
                    elapsed = int(time.time() - start_wait)

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
                        checks_container = page.locator("ytcp-uploads-checks, #checks-content, div[test-id='checks-content']").first
                        scope = checks_container if (checks_container.count() > 0 and checks_container.is_visible()) else page
                        last_checks_text = (scope.inner_text() or "").lower()

                        if checks_report_safe(last_checks_text):
                            checks_safe_seen = True
                            break
                    except Exception as read_err:
                        last_probe_error = str(read_err)

                    cur_pct = min(84, 75 + int(elapsed * 9 / max_checks_wait))
                    if progress_callback:
                        progress_callback(cur_pct, f"Đang kiểm duyệt bản quyền Content ID... ({elapsed}s)")

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
                        checks_container = page.locator("ytcp-uploads-checks, #checks-content, div[test-id='checks-content']").first
                        scope = checks_container if (checks_container.count() > 0 and checks_container.is_visible()) else page
                        last_checks_text = (scope.inner_text() or "").lower()
                        checks_safe_seen = checks_report_safe(last_checks_text)
                    except Exception as read_err:
                        last_probe_error = str(read_err)

                if not checks_safe_seen:
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

                checks_elapsed = int(time.time() - start_wait)
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

                page.wait_for_timeout(1000)
                next_btn = page.locator("#next-button").first
                if next_btn.count() > 0 and next_btn.is_visible():
                    human_move_and_click(page, next_btn, delay_after=1800)

                privacy_status = metadata["privacy_status"]
                page.wait_for_timeout(1400)

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
                    human_move_and_click(page, sched_radio, delay_after=1500)

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
                    except RuntimeError:
                        raise
                    except Exception as sched_err:
                        raise RuntimeError(
                            f"Không điền được ngày giờ hẹn lịch ({sched_str}): {sched_err}. "
                            "Đã dừng để tránh đăng sai giờ hoặc đăng Công khai nhầm."
                        )

                    if not _is_radio_checked(sched_radio):
                        raise RuntimeError(
                            "Chưa xác nhận được tuỳ chọn Lên lịch trên Studio. Đã dừng để tránh đăng Công khai nhầm."
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
                    human_move_and_click(page, vis_radio, delay_after=1200)

                    if not _is_radio_checked(vis_radio):
                        raise RuntimeError(
                            f"Chưa xác nhận được trạng thái hiển thị {radio_name} trên Studio. "
                            "Đã dừng để tránh đăng sai trạng thái."
                        )

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
                page.wait_for_timeout(2000)
                done_btn = page.locator(
                    "#done-button, ytcp-button#done-button, ytcp-button:has-text('Publish'), "
                    "ytcp-button:has-text('Schedule'), ytcp-button:has-text('Save'), "
                    "ytcp-button:has-text('Lưu'), ytcp-button:has-text('Xuất bản')"
                ).first
                human_move_and_click(page, done_btn, delay_after=3000)
                # Từ đây video đã được gửi đăng: TUYỆT ĐỐI không tự xóa nháp nếu bước sau lỗi.
                publish_clicked = True

                # Chờ hộp thoại chia sẻ để lấy URL video THẬT làm bằng chứng đăng thành công.
                try:
                    share_dialog = page.locator("ytcp-video-share-dialog").first
                    share_dialog.wait_for(state="visible", timeout=20000)
                    page.wait_for_timeout(1500)
                except Exception as dialog_err:
                    _log_optional_step_failure("chờ hộp thoại chia sẻ sau khi đăng", dialog_err, channel, metadata)

                verified_url = extract_published_video_url(page) or video_url

                # Đóng hộp thoại xác nhận hoàn tất của Studio để Cookie phiên kịp đồng bộ
                try:
                    close_share_dialog = page.locator(
                        "ytcp-video-share-dialog #close-button, "
                        "ytcp-uploads-still-processing-dialog #close-button, "
                        "ytcp-button#close-icon-button"
                    ).first
                    close_share_dialog.wait_for(state="visible", timeout=15000)
                    page.wait_for_timeout(1500)
                    human_move_and_click(page, close_share_dialog, delay_after=1500)
                except Exception as dialog_err:
                    _log_optional_step_failure("đóng hộp thoại xác nhận đăng", dialog_err, channel, metadata)
                    page.wait_for_timeout(5000)

                # Chờ thêm 2 giây ở trang Dashboard Studio để hoàn tất các request xoay vòng Token (SIDCC)
                page.wait_for_timeout(2000)

                if not verified_url:
                    verified_url = extract_published_video_url(page)

                if not verified_url:
                    uncertain_msg = (
                        "Đã bấm Xuất bản/Lên lịch nhưng KHÔNG đọc được URL video thật từ YouTube Studio. "
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
                elif draft_created and page is not None:
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
                raise
            finally:
                try:
                    context.close()
                except Exception as close_err:
                    _log_optional_step_failure("đóng Chrome sau khi upload", close_err, channel, metadata)


def upload_video_to_youtube(
    video_item: Dict[str, Any],
    channel: Dict[str, Any],
    option_group: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Any:
    """
    Hàm điều phối trung tâm: Tự động chọn phương thức đăng (Playwright YouTube Studio hoặc YouTube Data API v3).
    """
    video_path = video_item.get("output_video_path") or video_item.get("input_video_path") or ""
    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy file video để upload: {video_path}")

    metadata = build_final_metadata(video_item, channel, option_group)
    mode = (channel.get("upload_mode") or "playwright").lower()

    if not (channel.get("channel_id") or "").strip():
        raise RuntimeError(
            f"Kênh '{channel.get('name') or channel.get('handle') or '?'}' chưa có Channel ID. "
            "Không thể xác minh chính xác kênh nhận video — hãy bấm 'Refresh Kênh' hoặc nhập Channel ID "
            "trong tab Quản Lý Kênh trước khi đăng."
        )

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