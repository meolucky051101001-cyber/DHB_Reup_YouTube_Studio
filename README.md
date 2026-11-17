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
| Antidetect | Đồng bộ profile từ Antidetect Browser Manager và dùng Chrome của profile khi đăng qua Studio. Chế độ YouTube Data API dùng quyền OAuth riêng của kênh. |
| Nạp nguồn | Dán link (YouTube, TikTok, Douyin, Facebook) hoặc quét thư mục video có sẵn; import/export CSV và Excel; sửa metadata hàng loạt. |
| AI metadata | Viết lại tiêu đề/mô tả/tags, tìm-thay hàng loạt (Gemini), dùng key gửi qua header chứ không gắn vào URL. |
| Render | Dựng video dọc 1080x1920 bằng FFmpeg: intro/outro, filter, logo, âm thanh; preset Shorts (ép thời lượng, hashtag). |
| Đăng tự động | Lô file có sẵn hoặc toàn trình tải → render → đăng qua Studio/API; xem trước và xác nhận đích/metadata, kiểm tra kết quả, dùng chung cooldown upload tối thiểu 30 phút. Lịch phát hành YouTube là thiết lập riêng. |
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

Script khởi động server (`app.py`, cổng `127.0.0.1:8686`) và ghi log vào `logs\server.out.log` / `logs\server.error.log`. Nếu DHB đã chạy từ đúng thư mục, script mở dashboard hiện tại. Nếu cổng thuộc bản DHB khác hoặc chương trình khác, script báo xung đột. Xem revision, thư mục nguồn và cảnh báo đổi mã ở **Hệ thống → Chẩn đoán**.

Hoặc chạy trực tiếp:

```bat
python app.py
```

Mở Antidetect trước nếu dùng profile Antidetect. Lô thủ công chỉ bắt đầu khi người vận hành bấm chạy trong dashboard.

## Đăng video theo lô

1. Trong **Kênh & profile**, xác nhận Channel ID và quyền nhận video: phiên profile nếu dùng Studio, OAuth nếu dùng API. Kiểm tra Bộ tùy chọn/Khả năng đăng; lưu metadata chưa có nghĩa yêu cầu đó đã được hỗ trợ.
2. Trong **Video & dự án**, chọn dự án rồi **Thêm video**. Tool tự chọn các video mới thêm. Dùng **Sửa nhanh**, **Gán cấu hình** hoặc **Chi tiết** để chỉnh metadata, đích và hiển thị; đọc cả số video đang chọn nhưng bị bộ lọc ẩn.
3. Mở **Quy trình đăng tự động**, chọn file có sẵn hoặc toàn trình rồi **Tiếp tục xem trước lô**. Đọc bản xem trước máy chủ về file sẽ gửi, kênh/profile/phương thức, metadata, hiển thị và lịch rồi xác nhận chạy. Mở hướng dẫn hoặc tiếp tục xem trước chưa gửi video.
4. Theo dõi hàng đợi và **Xử lý → Kiểm tra lượt đăng**. Cooldown tối thiểu 30 phút dùng chung cho mọi profile, tính từ khi lượt trước kết thúc. Lịch là giờ YouTube phát hành, không phải giờ tự bật batch. Completed có thể là Riêng tư, Không công khai hoặc Đã lên lịch; mở link đối chiếu kết quả. Với **Cần đối soát** (`NeedsReview`), kiểm tra đúng kênh trước khi quyết định cho phép đăng lại.
5. Bấm **Dừng lô hiện tại** để ngừng nhận video batch kế tiếp; upload đã bắt đầu được chờ hoàn tất. Dừng Monitor riêng trong **Theo dõi nguồn**. Đợi lifecycle Stopped và Chẩn đoán xác nhận worker liên quan đã dừng trước khi tắt DHB hoặc máy.

Với nguồn theo dõi tự động: **Theo dõi nguồn → Tạo luồng → lưu → Bật nguồn → Xem trước nguồn tự động → Xác nhận và bật nguồn**. Lưu hoặc hủy bản xem trước không bật luồng. Monitor đã được bật có thể được supervisor khôi phục sau sự cố; dừng đúng luồng và đợi worker thoát nếu muốn ngừng tự động.

Xem [quy trình đăng tự động](docs/quy-trinh-dang-tu-dong.md) để đi từng bước cho batch/Monitor, lịch và đối soát. [Hướng dẫn vận hành](docs/van-hanh-local.md) bổ sung xử lý sự cố, sao lưu, khôi phục và điều kiện tăng quy mô.

Để kết nối YouTube Data API qua OAuth 2.0, xem [hướng dẫn OAuth cho DHB](docs/youtube-oauth.md).

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
| `ANTIDETECT_HOME` | Thư mục cài Antidetect. Mặc định là `C:\Projects\Tools\Antidetect Python`. |
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

Toàn bộ test dùng dữ liệu tạm (temp DB, HTTP server giả, FFmpeg/Playwright giả) và không chạm dữ liệu thật. CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) kiểm tra cú pháp Python và JavaScript rồi chạy `unittest` trên Windows với Python 3.10 cho mỗi push và PR.

Khi sửa frontend, nhớ tăng tham số `app.js?v=N` để trình duyệt đang mở không dùng bản cache.

---

## Tài liệu thêm

- [Quy trình đăng tự động](docs/quy-trinh-dang-tu-dong.md) — chuẩn bị, phạm vi, xem trước/xác nhận, batch/Monitor, lịch và kết quả.
- [Hướng dẫn vận hành local](docs/van-hanh-local.md) — xử lý sự cố, đối soát, phục hồi và tăng quy mô.
- [CHANGELOG.md](CHANGELOG.md) — các thay đổi theo phiên, gồm lỗi đã sửa và số lượng test.
- [Tài liệu handover lưu trữ](docs/archive/handovers/) — các snapshot lịch sử; README và mã nguồn hiện tại mới là nguồn tham chiếu chính.

## Đường dẫn công cụ và chẩn đoán

Mặc định tool tìm bộ DHB tại `C:\DHB Reup Facebook`; có thể đổi thư mục gốc bằng `DHB_ROOT`. Có thể chỉ định riêng executable khi cài đặt khác chuẩn: `DHB_FFMPEG`, `DHB_FFPROBE`, `DHB_FFPLAY`, `DHB_YTDL`, `DHB_BROWSER_HOST`, `DHB_MAGICK` và `DHB_STHP`. Nếu không đặt override, tool dùng file nằm trong thư mục DHB mặc định hoặc executable tương ứng trong `PATH` khi được hỗ trợ.

Mục **Hệ thống → Chẩn đoán** và API `GET /api/diagnostics` hiển thị revision, schema, trạng thái worker và slot, số video cần đối soát, dung lượng, heartbeat và tình trạng các công cụ media.
