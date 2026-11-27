"""Connect a Google Desktop OAuth client to an existing DHB channel with Antidetect isolation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
GCLOUD_YOUTUBE_API_URL = "https://console.cloud.google.com/apis/library/youtube.googleapis.com"
DHB_URL = "http://127.0.0.1:8686"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
CHANNEL_WORKSPACE_SCOPES = (*SCOPES,
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)


def parse_desktop_client_json(raw_text: str) -> tuple[str, str]:
    try:
        parsed = json.loads(str(raw_text or "").lstrip("\ufeff"))
        installed = parsed["installed"]
        client_id = installed["client_id"]
        client_secret = installed["client_secret"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(
            "File JSON không đúng chuẩn OAuth Client loại 'Desktop app' (thiếu mục 'installed'). "
            "Khi tạo OAuth Client ID trên Google Cloud, hãy chọn Application type = Desktop app."
        ) from None
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.apps\.googleusercontent\.com", client_id.strip()):
        raise RuntimeError("Client ID trong file Google OAuth không đúng định dạng.")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise RuntimeError("File Desktop app thiếu client_secret.")
    return client_id.strip(), client_secret.strip()


def read_desktop_client(path: Path) -> tuple[str, str]:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        raise RuntimeError("Không đọc được file JSON OAuth Client loại Desktop app.") from None
    return parse_desktop_client_json(raw)


def extract_antidetect_profile_id(channel: Dict[str, Any], profile_binding: Optional[Dict[str, Any]] = None) -> str:
    if profile_binding:
        ad_id = str(profile_binding.get("antidetect_profile_id") or "").strip()
        if ad_id:
            return ad_id
        p_path = str(profile_binding.get("path") or "").strip()
        if p_path.startswith("antidetect://"):
            return p_path[len("antidetect://"):].strip()
    ch_ad = str(channel.get("antidetect_profile_id") or "").strip()
    if ch_ad:
        return ch_ad
    ch_path = str(channel.get("profile_path") or "").strip()
    if ch_path.startswith("antidetect://"):
        return ch_path[len("antidetect://"):].strip()
    return ""


def open_url_in_channel_browser(
    channel: Dict[str, Any],
    url: str,
    profile_binding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Open URL strictly inside the channel's Antidetect profile when bound to Antidetect."""
    ad_id = extract_antidetect_profile_id(channel, profile_binding)
    is_antidetect = bool(ad_id) or (
        profile_binding is not None and str(profile_binding.get("source") or "").casefold() == "antidetect"
    )
    if is_antidetect:
        if not ad_id:
            raise RuntimeError("Profile Antidetect của kênh thiếu ID liên kết; không mở trình duyệt ngoài để tránh lộ tài khoản.")
        from antidetect_client import AntidetectClient

        client = AntidetectClient()
        res = client.open_url(ad_id, url)
        return {"browser": "antidetect", "antidetect_profile_id": ad_id, "port": res.get("port")}

    if not webbrowser.open(url):
        raise RuntimeError("Không mở được trình duyệt để xin quyền Google.")
    return {"browser": "system_default"}


def authorize(
    client_id: str,
    *,
    open_url: Optional[Callable[[str], Any]] = None,
    timeout_seconds: int = 240,
    requested_scopes=SCOPES,
) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result: Dict[str, Any] = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, _format, *_args):
            # Never log single-use authorization codes.
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            states = query.get("state", [])
            if (
                parsed.path != "/oauth/callback"
                or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                or len(states) != 1
                or not secrets.compare_digest(states[0].encode("utf-8"), state.encode("ascii"))
            ):
                self.send_error(400, "Invalid OAuth callback")
                return

            codes = query.get("code", [])
            if len(codes) == 1 and "error" not in query:
                result["code"] = codes[0]
                msg = "✅ Đã nhận phản hồi từ Google! DHB Studio đang xác minh Channel ID — bạn có thể quay lại tab DHB Studio."
            else:
                result["error"] = True
                msg = "⚠️ Google chưa cấp quyền hoặc bạn đã hủy thao tác. Hãy quay lại DHB Studio để thử lại."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            html = (
                "<!doctype html><html lang='vi'><head><meta charset='utf-8'><title>DHB YouTube OAuth</title>"
                "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#f8fafc;display:grid;place-items:center;height:90vh;margin:0}"
                ".card{background:#1e293b;padding:28px 36px;border-radius:14px;border:1px solid #334155;max-width:520px;text-align:center;line-height:1.5}</style>"
                f"</head><body><div class='card'><h2>{msg}</h2></div></body></html>"
            )
            self.wfile.write(html.encode("utf-8"))

    opener = open_url or webbrowser.open
    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(requested_scopes),
            "access_type": "offline",
            "prompt": "select_account consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        opened = opener(f"{AUTH_URL}?{query}")
        if opened is False:
            raise RuntimeError("Không mở được trình duyệt để xin quyền Google.")
        server.timeout = 0.5
        deadline = time.monotonic() + max(15, int(timeout_seconds))
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise RuntimeError("Google chưa cấp quyền hoặc phiên chờ cấp quyền đã hết thời gian (4 phút).")
    return result["code"], redirect, verifier


def google_json(response, action):
    if not response.ok:
        raise RuntimeError(f"{action} thất bại (HTTP {response.status_code}).")
    return response.json()


def authorized_credentials(
    http,
    client_id: str,
    client_secret: str,
    expected_channel: str,
    *,
    open_url: Optional[Callable[[str], Any]] = None,
    timeout_seconds: int = 240,
    return_identity: bool = False,
    requested_scopes=SCOPES,
):
    code, redirect, verifier = authorize(
        client_id, open_url=open_url, timeout_seconds=timeout_seconds,
        **({"requested_scopes": requested_scopes} if requested_scopes != SCOPES else {}),
    )
    client = {"client_id": client_id, "client_secret": client_secret}
    granted = google_json(http.post(TOKEN_URL, data={
        **client,
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
    }, timeout=20), "Đổi mã OAuth")
    scopes = granted.get("scope")
    if scopes is not None and not set(requested_scopes) <= set(scopes.split()):
        raise RuntimeError("Google chưa cấp đủ quyền YouTube đã chọn. Hãy kiểm tra các quyền trên màn hình Google.")
    refresh_token = granted.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google không trả refresh_token cho kết nối dài hạn.")
    refreshed = google_json(http.post(TOKEN_URL, data={
        **client,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=20), "Thử làm mới OAuth")
    if not refreshed.get("access_token"):
        raise RuntimeError("Google không trả access_token sau khi làm mới.")
    identity = google_json(http.get(
        CHANNELS_URL,
        params={"part": "id,snippet", "mine": "true", "maxResults": 2},
        headers={"Authorization": f"Bearer {refreshed['access_token']}"},
        timeout=20,
    ), "Kiểm tra Channel ID")
    items = identity.get("items") or []
    found_ids = [item.get("id") for item in items]
    if found_ids != [expected_channel]:
        picked = ""
        if items:
            first_title = ((items[0].get("snippet") or {}).get("title")) or ""
            picked = f" (bạn vừa chọn '{first_title}' - {found_ids[0]})"
        raise RuntimeError(
            f"OAuth chưa trỏ đến đúng Channel ID {expected_channel}{picked}. "
            "Hãy bấm kết nối lại và chọn đúng kênh YouTube tương ứng trên màn hình Google!"
        )
    creds = {**client, "refresh_token": refresh_token}
    if return_identity:
        title = ((items[0].get("snippet") or {}).get("title")) or ""
        return creds, {"channel_id": expected_channel, "name": title}
    return creds


def find_channel(http, channel_row_id):
    response = http.get(f"{DHB_URL}/api/channels", timeout=15)
    if not response.ok:
        raise RuntimeError("Không đọc được danh sách kênh từ DHB local.")
    channels = response.json()
    target = next((row for row in channels if row["id"] == channel_row_id), None)
    if not target or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", target.get("channel_id") or ""):
        raise RuntimeError("Kênh DHB chưa có YouTube Channel ID hợp lệ.")
    if sum(row.get("channel_id") == target["channel_id"] for row in channels) != 1:
        raise RuntimeError("Channel ID đã có nhiều dòng trong DHB. Hãy giữ một dòng kênh trước khi kết nối.")
    return target


def connect(client_file, channel_row_id, activate):
    client_id, client_secret = read_desktop_client(client_file)
    with requests.Session() as local, requests.Session() as google:
        local.trust_env = False
        google.trust_env = False
        token_response = local.get(f"{DHB_URL}/api/auth/token", timeout=10)
        if not token_response.ok:
            raise RuntimeError("Không kết nối được API của DHB local.")
        local.headers.update({"X-DHB-Token": token_response.json()["token"], "Origin": DHB_URL})
        target = find_channel(local, channel_row_id)
        print(f"Kênh DHB #{channel_row_id}: {target['name']} ({target['channel_id']}) — Profile: {target.get('profile_name')}.", flush=True)
        print("Mở cửa sổ trình duyệt của đúng Profile để chọn kênh và cấp quyền (thời gian chờ 4 phút)...", flush=True)
        credentials = authorized_credentials(
            google,
            client_id,
            client_secret,
            target["channel_id"],
            open_url=lambda url: open_url_in_channel_browser(target, url),
        )

        if find_channel(local, channel_row_id)["channel_id"] != target["channel_id"]:
            raise RuntimeError("Channel ID đã thay đổi trong khi cấp quyền.")
        payload = {"oauth_token_json": json.dumps(credentials)}
        if activate:
            payload["upload_mode"] = "youtube_api"
        saved = local.put(f"{DHB_URL}/api/channels/{channel_row_id}", json=payload, timeout=15)
        if not saved.ok or not saved.json().get("ok"):
            if activate:
                raise RuntimeError("DHB chưa chuyển sang API. Kiểm tra bản chạy hoặc dùng lệnh không có --activate để lưu OAuth.")
            raise RuntimeError("DHB chưa lưu được OAuth. Kiểm tra kênh có đang được worker sử dụng không.")
        updated = find_channel(local, channel_row_id)
        status = local.get(f"{DHB_URL}/api/channels/{channel_row_id}/oauth-status", timeout=15)
        if (
            not status.ok
            or not status.json().get("configured")
            or (activate and updated["upload_mode"] != "youtube_api")
        ):
            raise RuntimeError("DHB chưa xác nhận trạng thái cấu hình sau khi lưu.")
        print("OAuth đã lưu cho đúng kênh. " + ("DHB đang dùng YouTube Data API." if activate else "Đã giữ chế độ đăng hiện tại."))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True, help="File JSON của Google OAuth Desktop app")
    parser.add_argument("--channel", type=int, required=True, help="ID dòng kênh trong DHB")
    parser.add_argument("--activate", action="store_true", help="Chuyển kênh sang YouTube Data API sau khi xác minh")
    args = parser.parse_args()
    try:
        connect(args.client, args.channel, args.activate)
    except requests.RequestException:
        parser.exit(1, "Kết nối mạng với Google hoặc DHB thất bại.\n")
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
