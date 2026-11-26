"""Read the CSV export of a public Google Sheet and parse supported Drive links."""

import csv
import io
import re
from time import monotonic
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests


MAX_SHEET_BYTES = 2 * 1024 * 1024
MAX_SHEET_ROWS = 1000
_GOOGLE_ID = re.compile(r"[A-Za-z0-9_-]{12,128}")


def _https_host(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port is not None:
        raise ValueError("Dùng liên kết HTTPS của Google.")
    return parts


def drive_url(value):
    parts = _https_host(value.strip())
    if parts.hostname != "drive.google.com":
        raise ValueError("Cần liên kết chia sẻ tệp Google Drive.")
    query = parse_qs(parts.query)
    match = re.fullmatch(r"/file/d/([^/]+)(?:/(?:view|preview))?/?", parts.path)
    if match:
        file_id = match[1]
    elif parts.path in ("/open", "/uc", "/download"):
        file_id = query.get("id", [""])[0]
    else:
        raise ValueError("Cần liên kết đến một tệp Google Drive.")
    if len(file_id) < 28 or not _GOOGLE_ID.fullmatch(file_id):
        raise ValueError("ID tệp Google Drive không hợp lệ.")
    key = query.get("resourcekey", [""])[0]
    if key and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", key):
        raise ValueError("Khóa tài nguyên Google Drive không hợp lệ.")
    suffix = "?" + urlencode({"resourcekey": key}) if key else ""
    return file_id, f"https://drive.google.com/file/d/{file_id}/view{suffix}"


def sheet_csv_url(value):
    parts = _https_host(value.strip())
    match = re.fullmatch(r"/spreadsheets/d/([^/]+)(?:/(?:edit|view|export))?/?", parts.path)
    if parts.hostname != "docs.google.com" or not match or not _GOOGLE_ID.fullmatch(match[1]):
        raise ValueError("Cần liên kết Google Sheets có quyền xem CSV.")
    gids = parse_qs(parts.fragment).get("gid") or parse_qs(parts.query).get("gid") or ["0"]
    if len(gids) != 1 or not re.fullmatch(r"[0-9]{1,12}", gids[0]):
        raise ValueError("Mã tab Google Sheets không hợp lệ.")
    return f"https://docs.google.com/spreadsheets/d/{match[1]}/export?format=csv&gid={gids[0]}"


def read_sheet(value):
    url = sheet_csv_url(value)
    deadline = monotonic() + 30
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(5):
            parts = _https_host(url)
            if parts.hostname != "docs.google.com" and not parts.hostname.endswith("-sheets.googleusercontent.com"):
                raise ValueError("Google Sheets chuyển sang trang khác. Kiểm tra quyền xem của bảng.")
            with session.get(url, stream=True, allow_redirects=False, timeout=(5, 10)) as response:
                target = response.headers.get("Location")
                if 300 <= response.status_code < 400 and target:
                    url = urljoin(url, target)
                    continue
                response.raise_for_status()
                if response.status_code != 200 or "text/csv" not in response.headers.get("Content-Type", "").lower():
                    raise ValueError("Google Sheets không trả về CSV. Kiểm tra quyền chia sẻ và tab đã chọn.")
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > MAX_SHEET_BYTES or monotonic() > deadline:
                        raise ValueError("CSV Google Sheets quá lớn hoặc thời gian tải quá lâu.")
                break
        else:
            raise ValueError("Google Sheets chuyển hướng quá nhiều lần.")
    rows = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
    header = next(rows, None)
    if not header or len(header) > 100:
        raise ValueError("CSV cần có hàng tiêu đề và tối đa 100 cột.")
    data = []
    for index, row in enumerate(rows, 2):
        if index > MAX_SHEET_ROWS + 1:
            raise ValueError("Mỗi lần nhập tối đa 1.000 dòng.")
        if len(row) > len(header):
            raise ValueError(f"Dòng {index} có nhiều cột hơn tiêu đề.")
        if any(cell.strip() for cell in row):
            data.append((index, row + [""] * (len(header) - len(row))))
    return header, data
