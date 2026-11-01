# DHB Reup YouTube Studio

[![tests](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/actions/workflows/tests.yml/badge.svg)](https://github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio/actions/workflows/tests.yml)

Bộ công cụ chạy trên máy cá nhân (Windows) để quản lý nhiều kênh YouTube và chạy quy trình **nạp nguồn → render → đăng** theo lô, dùng dashboard web cục bộ tại `http://127.0.0.1:8686`.

Tool được thiết kế theo hướng **fail-closed**: khi một bước không xác minh được (thiếu Channel ID, kết quả Checks đọc không rõ, không đọc được URL đã đăng, khoảng giãn cách chưa đủ...) thì quy trình **dừng và báo lỗi**, không tự đoán và không bỏ qua.

> Chỉ dùng tool với nội dung bạn có quyền sử dụng. Reup video của kênh khác có thể vi phạm bản quyền và quy định của YouTube; mọi rủi ro về kênh thuộc về người vận hành.

---

## Tính năng chính

| Nhóm | Nội dung |
|---|---|
| Kênh & Profile | Quản lý Profile Chrome và kênh YouTube theo nhóm; đồng bộ Profile từ bộ DHB Reup Facebook; mở trình duyệt để đăng nhập, quét và lưu `channel_id` của từng kênh con. |
| Antidetect | Đồng bộ profile từ Antidetect Browser Manager, mượn trình duyệt đang chạy qua CDP lease; kênh Antidetect chỉ đăng bằng Playwright, không dùng YouTube Data API. |
| Nạp nguồn | Dán link (YouTube, TikTok, Douyin, Facebook) hoặc quét thư mục video có sẵn; import/export CSV và Excel; sửa metadata hàng loạt. |
| AI metadata | Viết lại tiêu đề/mô tả/tags, tìm-thay hàng loạt (Gemini), dùng key gửi qua header chứ không gắn vào URL. |
| Render | Dựng video dọc 1080x1920 bằng FFmpeg: intro/outro, filter, logo, âm thanh; preset Shorts (ép thời lượng, hashtag). |
| Đăng tự động | Quy trình Studio 4 bước (Chi tiết → Thành phần → Kiểm duyệt → Khả năng hiển thị); chặn đăng khi Checks báo vi phạm; hẹn lịch giãn cách giữa các lượt đăng. |
| Monitor | Theo dõi kênh/thư mục định thời, chống trùng nguồn, hàng đợi render chạy nền độc lập với lô thủ công. |
| Vận hành | Log hệ thống trong DB, xuất CSV/log, backup, token API cục bộ cho dashboard. |

---

## Yêu cầu môi trường

- Windows 10/11, Python 3.10 (x64).
- Bộ **DHB Reup Facebook** tại `C:\DHB Reup Facebook` để dùng FFmpeg/ffprobe/kho tài nguyên Render (tool chỉ **đọc** thư mục này; nếu thiếu FFmpeg kế thừa sẽ fallback sang `ffmpeg` trong PATH).
- Playwright Chromium cho các bước thao tác YouTube Studio.
- (Tuỳ chọn) **Antidetect Browser Manager** nếu đăng bài qua profile Antidetect.

Đường dẫn mặc định khai báo trong [config.py](config.py); dữ liệu chạy máy nằm trong `data/`, `temp/`, `output/`, `logs/` và **đã được gitignore** (DB, OAuth token, profile Chrome, backup không bao giờ được commit).

---

## Cài đặt

Cách nhanh: chạy `CAI_DAT_MOI_TRUONG.bat`.

Thủ công:

```bat
pip install -r requirements.txt
python -m playwright install chromium
```

---

## Khởi động

```bat
START_DHB_YOUTUBE.bat
```

Script sẽ khởi động server (`app.py`, uvicorn bind `127.0.0.1:8686`, log ghi ra `logs\server.out.log` / `logs\server.error.log`) rồi mở dashboard. Nếu dashboard đang chạy sẵn, script chỉ mở trình duyệt.

Hoặc chạy trực tiếp:

```bat
python app.py
```

Tool **không** tự chạy hàng đợi khi khởi động — mọi lượt đăng phải bấm Start trong dashboard.

Kiểm tra nhanh trạng thái hàng đợi và log mà không cần dashboard:

```bat
python check_status.py
```

---

## Tích hợp Antidetect Browser Manager

Antidetect là chủ sở hữu duy nhất của Chrome/Profile/Proxy/Fingerprint. Tool chỉ xin quyền dùng chung trình duyệt đang chạy, mở tab riêng và đóng đúng tab do mình tạo.

Cấu hình (biến môi trường, hoặc setting trong DB):

| Biến | Ý nghĩa |
|---|---|
| `ANTIDETECT_HOME` | Thư mục cài Antidetect; mặc định `C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python`. |
| `ANTIDETECT_DATA_DIR` | Thư mục dữ liệu chứa `server.pid` và `runtime.dpapi`. |
| `ANTIDETECT_BASE_URL` | Ép địa chỉ API, ví dụ `http://127.0.0.1:8000`. Để trống thì tool tự dò từ `server.pid` rồi cổng 8000. |
| `ANTIDETECT_ACCESS_KEY` / `ANTIDETECT_API_TOKEN` | Khóa Bearer; nếu không đặt, tool giải mã `runtime.dpapi` bằng DPAPI của user hiện tại. |

Kết nối có health-check trước mỗi thao tác, tự dò lại khi máy chủ đổi cổng, kết nối CDP có retry, và lease **không** dừng profile mà nó không tự khởi chạy. Endpoint liên quan: `GET /api/antidetect/status`, `POST /api/antidetect/sync`.

Mật khẩu proxy và khóa truy cập không được đồng bộ vào DB, không hiển thị trong API/CSV/log.

---

## Cấu trúc mã nguồn

```
app.py                 Dashboard FastAPI + toàn bộ API (cổng 8686)
config.py              Đường dẫn DHB gốc / thư mục dữ liệu của tool
database.py            SQLite (WAL), migration, log, mask secret, export CSV
api_security.py        Token API cục bộ, kiểm tra Host/Origin
pipeline_manager.py    Điều phối lô, hàng đợi, monitor, quy tắc dừng
render_engine.py       Dựng video FFmpeg, preset intro/outro, quản lý tiến trình
scanner_downloader.py  Quét link/thư mục, probe video, tải nguồn
youtube_uploader.py    Thao tác YouTube Studio qua Playwright
antidetect_client.py   Client API + CDP lease cho Antidetect
ai_engine.py           Tối ưu metadata bằng Gemini
static/                Dashboard HTML/CSS/JS
tests/                 Unit test (unittest)
```

Bảng dữ liệu chính trong `data/dhb_youtube.sqlite`: `channel_groups`, `chrome_profiles`, `youtube_channels`, `upload_option_groups`, `projects`, `video_queue`, `monitors`, `published_history`, `system_logs`, `app_settings`, `render_presets`.

---

## Kiểm thử

```bat
python -m unittest discover -s tests -v
```

Toàn bộ test dùng dữ liệu tạm (temp DB, HTTP server giả, FFmpeg/Playwright giả) và không chạm dữ liệu thật. CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) chạy `python -m py_compile` các module chính rồi `python -m unittest discover -s tests -v` trên Windows + Python 3.10 cho mỗi push/PR. Khi sửa frontend, hãy chạy thêm `node --check static/app.js` tại máy của bạn.

Khi sửa frontend, nhớ tăng tham số `app.js?v=N` để trình duyệt đang mở không dùng bản cache.

---

## Tài liệu thêm

- [HUONG_DAN_SU_DUNG.txt](HUONG_DAN_SU_DUNG.txt) — hướng dẫn thao tác từng tab cho người vận hành.
- [CHANGELOG.md](CHANGELOG.md) — các thay đổi theo phiên, gồm lỗi đã sửa và số lượng test.
- [HANDOVER_PROMPT.md](HANDOVER_PROMPT.md) / [HANDOVER_PROMPT_v2.md](HANDOVER_PROMPT_v2.md) — bối cảnh bàn giao và ràng buộc khi tiếp tục phát triển.
