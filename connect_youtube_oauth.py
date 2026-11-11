"""Connect a Google Desktop OAuth client to an existing DHB channel."""

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
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
DHB_URL = "http://127.0.0.1:8686"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)


def read_desktop_client(path: Path) -> tuple[str, str]:
    try:
        installed = json.loads(path.read_text(encoding="utf-8-sig"))["installed"]
        client_id = installed["client_id"]
        client_secret = installed["client_secret"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Không đọc được file JSON OAuth Client loại Desktop app.") from None
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
        raise RuntimeError("Client ID trong file Google OAuth không đúng định dạng.")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise RuntimeError("File Desktop app thiếu client secret.")
    return client_id, client_secret


def authorize(client_id: str) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, _format, *_args):
            # The request URL contains the single-use authorization code.
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
            else:
                result["error"] = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write("Đã nhận phản hồi Google. Quay lại cửa sổ DHB để xem kết quả.".encode("utf-8"))

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "select_account consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        if not webbrowser.open(f"{AUTH_URL}?{query}"):
            raise RuntimeError("Không mở được trình duyệt mặc định để xin quyền Google.")
        server.timeout = 0.5
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise RuntimeError("Google chưa cấp quyền hoặc phiên kết nối đã hết thời gian.")
    return result["code"], redirect, verifier


def google_json(response, action):
    if not response.ok:
        # Do not echo provider responses or URLs that may contain credentials.
        raise RuntimeError(f"{action} thất bại (HTTP {response.status_code}).")
    return response.json()


def authorized_credentials(http, client_id, client_secret, expected_channel):
    code, redirect, verifier = authorize(client_id)
    client = {"client_id": client_id, "client_secret": client_secret}
    granted = google_json(http.post(TOKEN_URL, data={
        **client,
        "code": code,
        "redirect_uri": redirect,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
    }, timeout=20), "Đổi mã OAuth")
    scopes = granted.get("scope")
    if scopes is not None and not set(SCOPES) <= set(scopes.split()):
        raise RuntimeError("Google chưa cấp đủ quyền đăng và đọc thông tin kênh.")
    refresh_token = granted.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google không trả refresh token cho kết nối dài hạn.")
    refreshed = google_json(http.post(TOKEN_URL, data={
        **client,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=20), "Thử làm mới OAuth")
    if not refreshed.get("access_token"):
        raise RuntimeError("Google không trả access token sau khi làm mới.")
    identity = google_json(http.get(
        CHANNELS_URL,
        params={"part": "id", "mine": "true", "maxResults": 2},
        headers={"Authorization": f"Bearer {refreshed['access_token']}"},
        timeout=20,
    ), "Kiểm tra Channel ID")
    if [item.get("id") for item in identity.get("items", [])] != [expected_channel]:
        raise RuntimeError("OAuth chưa trỏ duy nhất đến đúng Channel ID của DHB. Hãy chọn lại kênh trên Google.")
    return {**client, "refresh_token": refresh_token}


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
        print(f"Kênh DHB #{channel_row_id}: {target['name']} ({target['channel_id']}).", flush=True)
        print("Chọn đúng kênh và tự cấp quyền trong cửa sổ Google. Thời gian chờ 5 phút.", flush=True)
        credentials = authorized_credentials(google, client_id, client_secret, target["channel_id"])

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
