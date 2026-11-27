"""Read the displayed channel-level 48-hour counter in YouTube Studio."""

import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from antidetect_client import AntidetectBrowserLease
from database import get_profile_binding
from youtube_uploader import (
    _channel_antidetect_id, _get_antidetect_lock, _get_profile_lock, find_system_chrome_exe,
)


_CARDS = ":is(ytcp-realtime-metrics, yta-realtime-card, ytcp-realtime-card):visible"
_LABEL = re.compile(
    r"(?:views\s*[·•-]?\s*(?:last\s+)?48[\s-]+hours|"
    r"48[\s-]+hour\s+views|(?:số\s+)?lượt xem\s*[·•-]?\s*48\s+giờ(?:\s+qua)?)",
    re.IGNORECASE,
)
_NUMBER = r"(?:[0-9]{1,3}(?:[., ][0-9]{3})+|[0-9]{1,18})"


def _count_from_card(text):
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    candidates = set()
    for index, line in enumerate(lines):
        number = None
        inline = re.fullmatch(rf"(?:({_NUMBER})\s+{_LABEL.pattern}|{_LABEL.pattern}\s+({_NUMBER}))", line, re.I)
        if inline:
            number = inline[1] or inline[2]
        elif _LABEL.fullmatch(line):
            if index and re.fullmatch(_NUMBER, lines[index - 1]):
                number = lines[index - 1]
            elif index + 1 < len(lines) and re.fullmatch(_NUMBER, lines[index + 1]):
                number = lines[index + 1]
        if number is not None:
            digits = re.sub(r"[., ]", "", number)
            if len(digits) <= 18:
                candidates.add(int(digits))
    return candidates


def _matches_channel(url, channel_id):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "studio.youtube.com"
            and parsed.path.split("/")[1:5] == ["channel", channel_id, "analytics", "tab-overview"])


def _read_page(page, channel_id):
    url = f"https://studio.youtube.com/channel/{channel_id}/analytics/tab-overview/period-default"
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    if not _matches_channel(page.url, channel_id):
        raise ValueError("Studio không mở trang tổng quan của đúng kênh.")
    cards = page.locator(_CARDS).filter(has_text=_LABEL)
    cards.first.wait_for(state="visible", timeout=10000)
    matching = set()
    for card in cards.all():
        if card.is_visible():
            matching.update(_count_from_card(card.inner_text(timeout=2000)))
    if not _matches_channel(page.url, channel_id) or len(matching) != 1:
        raise ValueError("Studio chưa hiển thị một số lượt xem 48 giờ có thể xác minh.")
    return matching.pop()


@contextmanager
def _read_only_page(channel):
    profile_id = _channel_antidetect_id(channel)
    if profile_id:
        lock = _get_antidetect_lock(profile_id)
        if not lock.acquire(blocking=False):
            raise ValueError("Profile đang được DHB sử dụng. Thử lại sau khi lượt hiện tại hoàn tất.")
        try:
            with AntidetectBrowserLease(profile_id, stop_if_started=True) as lease:
                yield lease.page
        finally:
            lock.release()
        return

    binding = get_profile_binding(channel.get("profile_name") or "") or {}
    configured_path = channel.get("profile_path") or binding.get("path")
    executable = find_system_chrome_exe()
    if not configured_path or not Path(configured_path).is_dir() or not executable:
        raise ValueError("Cần liên kết một Chrome profile đã đăng nhập Studio.")
    from playwright.sync_api import sync_playwright

    lock = _get_profile_lock(configured_path)
    if not lock.acquire(blocking=False):
        raise ValueError("Profile đang được DHB sử dụng. Thử lại sau khi lượt hiện tại hoàn tất.")
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                configured_path, executable_path=executable, headless=False, timeout=15000,
            )
            try:
                yield context.new_page()
            finally:
                context.close()
    finally:
        lock.release()


def read_realtime_views(channel):
    from playwright.sync_api import Error

    channel_id = channel.get("channel_id") or ""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("Kênh chưa có YouTube Channel ID hợp lệ.")
    try:
        with _read_only_page(channel) as page:
            return _read_page(page, channel_id)
    except (Error, OSError, RuntimeError):
        raise ValueError("Không đọc được báo cáo trực tiếp. Kiểm tra phiên đăng nhập Studio và thử lại.") from None
