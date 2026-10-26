# TÀI LIỆU BÀN GIAO TOÀN DIỆN DỰ ÁN: DHB REUP YOUTUBE STUDIO
*(Hệ thống Tự Động Hóa Reup, Render Lách Bản Quyền & Tải Lên YouTube Studio Đa Kênh)*

---

## 1. TỔNG QUAN HỆ THỐNG (SYSTEM OVERVIEW)
* **Tên dự án:** `DHB Reup YouTube Studio` (Phiên bản chuyên biệt cho YouTube & Shorts).
* **Mục đích:** Kế thừa toàn bộ tài nguyên, cấu hình, profiles Chrome và công cụ của phần mềm gốc `DHB Reup Facebook` (`C:\DHB Reup Facebook`), chuyển đổi và tối ưu 100% sang nền tảng **YouTube Studio Web Automation & YouTube Data API v3**.
* **Đặc tính nổi bật:**
  1. **Anti-Checkpoint & Anti-Bot Stealth:** Đồng nhất 100% Fingerprint Desktop thực tế giữa lúc người dùng mở Chrome đăng nhập và lúc Playwright tự động chạy đăng bài. Không giả mạo user-agent chắp vá. Giữ nguyên cookie session (`SIDCC`, `__Secure-1PSIDTS`).
  2. **Bộ Chốt Chặn Vi Phạm Bản Quyền (Copyright Auto-Cancel):** Tự động phát hiện cảnh báo bản quyền tại bước Kiểm duyệt (Checks) của YouTube Studio; tự động hủy và xóa nháp ngay lập tức để bảo vệ tuyệt đối độ uy tín của kênh.
  3. **Bộ Render FFmpeg Đa Chế Độ & Lách Bản Quyền Nâng Cao:** Tự động cắt nền Gaussian Blur dọc 9:16 cho Shorts, bộ lọc màu OLED Visual Pop, lật gương khung hình (hflip), tăng tốc thông minh (1.05x), Pitch-Shift âm thanh và Equalizer 10 băng tần triệt tiêu nhận diện Content ID.
  4. **Cơ chế Dừng Tức Thì (Instant Process Kill):** Bấm "⏹️ Dừng" lập tức tiêu diệt toàn bộ tiến trình con FFmpeg và dọn dẹp file hỏng trong 0.1 giây.
  5. **Cơ Sở Dữ Liệu SQLite WAL Mode:** Chế độ ghi nhật ký trước (Write-Ahead Logging) loại bỏ hoàn toàn lỗi `database is locked` khi xử lý đa luồng song song.
  6. **Giao Diện Trực Quan (Single Page Glassmorphism UI):** Thiết kế tối ưu cho thao tác nhanh, hỗ trợ kéo thả video từ máy tính, bộ lọc tìm kiếm tức thì, thông báo Toast nổi, quản lý danh sách kênh và profiles phân cấp.

---

## 2. BẢN ĐỒ ĐƯỜNG DẪN THƯ MỤC & FILE (DIRECTORY & FILE PATHS)

### Thư mục làm việc chính:
* **Thư mục cài đặt / phân phối:** `D:\DHB_Reup_YouTube_Studio\`
* **Thư mục mã nguồn Workspace:** `C:\Users\admin\.gemini\antigravity\scratch\dhb-reup-youtube\`
* **Thư mục công cụ DHB gốc tích hợp:** `C:\DHB Reup Facebook\`

### Cấu trúc chi tiết các file trong hệ thống:
```text
D:\DHB_Reup_YouTube_Studio\
├── app.py                      # Backend máy chủ FastAPI, điều phối toàn bộ REST API
├── config.py                   # Cấu hình đường dẫn, nhận diện binary (FFmpeg, Chrome, Profiles)
├── database.py                 # Quản trị SQLite (WAL mode), đồng bộ dữ liệu từ DHB Facebook
├── pipeline_manager.py         # Điều phối hàng đợi đa luồng song song (Semaphore, Stop Event)
├── render_engine.py            # Động cơ Render FFmpeg lách bản quyền, quản lý PID & Instant Kill
├── youtube_uploader.py         # Module Upload Playwright YouTube Studio Stealth & YouTube API v3
├── scanner_downloader.py       # Quét link video mạng xã hội (TikTok, Douyin, YT) & nạp file máy tính
├── ai_engine.py                # AI tối ưu hóa tiêu đề Shorts (<55 ký tự), mô tả, tags qua Gemini/Translate
├── check_status.py             # Script kiểm tra nhanh tình trạng hàng đợi và database
├── CAI_DAT_MOI_TRUONG.bat      # Script tự động cài đặt thư viện requirements & Playwright Chromium
├── START_DHB_YOUTUBE.bat       # Script 1-click khởi chạy Web UI tại http://127.0.0.1:8686
├── requirements.txt            # Danh sách thư viện Python cần thiết
├── HUONG_DAN_SU_DUNG.txt       # Hướng dẫn thao tác nhanh cho người dùng
├── HANDOVER_PROMPT.md          # Toàn bộ tài liệu bàn giao kỹ thuật này
├── dhb_youtube.ico             # Icon nhận diện thương hiệu công cụ
├── data/
│   ├── dhb_youtube.sqlite      # Cơ sở dữ liệu SQLite chính của dự án
│   └── profiles_youtube/       # Thư mục lưu trữ Profile Chrome cục bộ dự phòng
├── static/
│   ├── index.html              # Giao diện web người dùng Single Page hiện đại
│   ├── app.js                  # Javascript điều khiển giao diện, Polling 2.5s, Toast, Event Handlers
│   └── styles.css              # Giao diện Dark-Mode Glassmorphism responsive
├── output/                     # Nơi chứa các video thành phẩm và thumbnail sau khi Render
└── temp/                       # Nơi lưu trữ video tải về và video tải lên trực tiếp từ máy tính
    └── uploads/
```

---

## 3. KIẾN TRÚC MÃ NGUỒN VÀ VAI TRÒ TỪNG MODULE (MODULE BREAKDOWN)

### 1. `app.py` (FastAPI Server - Cổng Điều Phối)
* Cung cấp hơn 25 API endpoints phục vụ giao diện:
  * Quản lý hàng đợi: `/api/queue`, `/api/queue/edit-metadata`, `/api/queue/delete`, `/api/queue/auto-schedule`, `/api/queue/shorts-boost`.
  * Nạp file: `/api/upload-video-files` (nhận nhiều file cùng lúc qua multipart Form), `/api/browse-native-folder`.
  * Điều khiển tiến trình: `/api/pipeline/start`, `/api/pipeline/stop`.
  * Quản lý kênh/profile: `/api/profiles`, `/api/profiles/{name}/channels`, `/api/channels`.
  * Đồng bộ & tài nguyên: `/api/sync-from-dhb`, `/api/render-resources`, `/api/overview`, `/api/logs`.

### 2. `config.py` (Central Configuration)
* Tự động phát hiện và ánh xạ các file thực thi trong hệ điều hành:
  * `FFMPEG_EXE`: Ưu tiên `C:\DHB Reup Facebook\ffmpeg.exe`, fallback về `ffmpeg` hệ thống.
  * `DHB_PROFILE_CHROME_DIR`: Ánh xạ tới `C:\DHB Reup Facebook\Data\Profile Chrome`.
  * Tự động tạo sẵn cấu trúc thư mục `data/`, `output/`, `temp/`, `profiles_youtube/`.

### 3. `database.py` (SQLite Database Layer)
* Sử dụng chuẩn kết nối chống khóa đa luồng:
  ```python
  conn = sqlite3.connect(str(APP_DB_PATH), timeout=30.0, check_same_thread=False)
  conn.execute("PRAGMA journal_mode=WAL;")
  conn.execute("PRAGMA synchronous=NORMAL;")
  ```
* Bảng chính:
  * `video_queue`: Lưu trữ trạng thái (`Pending`, `Rendering...`, `Rendered`, `Uploading...`, `Completed`, `Stopped`, `Error`), tiến độ (`download_progress`, `render_progress`, `upload_progress`), thông tin video, cấu hình render, kênh đích.
  * `youtube_channels`: Kênh YouTube, liên kết với Profile Chrome và Nhóm kênh.
  * `youtube_profiles`: Lưu tên profile, proxy (`ip:port:user:pass`), user agent tùy chỉnh.
  * `system_logs`: Nhật ký hoạt động toàn thời gian thực.
  * `upload_option_groups`: Cấu hình nâng cao (bình luận mẫu, gắn thẻ, thẻ kết thúc).

### 4. `render_engine.py` (FFmpeg Transcoding Engine)
* Quản lý tiến trình cấp PID (`_ACTIVE_FFMPEG_PROCS`), hỗ trợ `kill_all_render_processes()`:
  * Khi người dùng bấm Dừng, tiến trình `proc.kill()` và `taskkill /F /IM ffmpeg.exe` được kích hoạt ngay lập tức, đồng thời xóa file dở dang để bảo vệ ổ đĩa.
* Các chế độ Render cốt lõi:
  * `shorts_9_16`: Tỷ lệ khung hình dọc 1080x1920 (hoặc 2K/4K) với nền mờ Gaussian Blur (`boxblur=20:10`), lớp phủ trung tâm, bộ lọc màu `OLED Visual Pop` (`contrast=1.04:saturation=1.12, unsharp=3:3:0.35:3:3:0.0`).
  * `pip_thu_nho`: Video thu nhỏ trên nền động từ kho `_Backgroud Video`.
  * `square_1_1`: Khung vuông 1080x1080.
  * `full_16_9`: Tỷ lệ chuẩn ngang Full HD/4K.
  * `md5_cut`: Cắt đầu/cuối vài giây + xóa toàn bộ ID3 Metadata (thực thi siêu tốc dạng `copy stream`).
  * `dhb_audio_master`: Lách âm thanh bằng chuỗi Equalizer 10 băng tần + Compand nén dải động + Phaser + Chorus + Dịch tần số mẫu `asetrate=44100*0.97, atempo=1/0.97`.

### 5. `youtube_uploader.py` (YouTube Automation & Stealth Engine)
* **Chế độ Upload 1: Playwright YouTube Studio (Mặc định - Khuyên dùng):**
  * Tự động khởi chạy Chrome với thư mục Profile thật (`--user-data-dir`).
  * Nhúng `STEALTH_JS` xóa cờ tự động hóa `navigator.webdriver = false`, sửa `chrome.runtime`, hỗ trợ chuẩn WebGL/AudioContext của phần cứng thật.
  * Quy trình 4 bước YouTube Studio (`#next-button`):
    * Bước 1: Điền tiêu đề, mô tả, gắn thẻ tags, chọn danh sách phát, đối tượng người xem (Made for kids).
    * Bước 2: Bỏ qua thành phần video.
    * Bước 3 (Checks): Quét DOM tìm cảnh báo vi phạm bản quyền (`[id*="copyright"]`, `"Bản quyền"`, `"Checks"`). Nếu có cảnh báo $\rightarrow$ Lập tức nhấn Hủy, xóa bản nháp vĩnh viễn và ghi log đỏ.
    * Bước 4: Đăng ngay (Công khai/Riêng tư/Không công khai) hoặc Hẹn giờ phát hành (Schedule / Premiere).
    * Chờ đợi request mạng ổn định trước khi đóng trình duyệt để bảo toàn Cookie phiên đăng nhập.
* **Chế độ Upload 2: YouTube Data API v3:**
  * Dành cho các kênh đã cấp quyền OAuth2 Client ID/Secret.

### 6. `pipeline_manager.py` (Multi-thread Queue Orchestrator)
* Kiểm soát số luồng song song qua `threading.Semaphore(max_workers)`.
* Đọc cài đặt độ trễ ngẫu nhiên giữa các video (`actual_delay = delay_sec + random.randint(2, 6)`).
* Nhận diện và ngắt sạch sẽ toàn bộ hàng đợi khi `self._stop_event.is_set()`.

### 7. `ai_engine.py` (Metadata & SEO Optimizer)
* Tự động viết lại Tiêu đề, Mô tả, Tags tối ưu theo thuật toán YouTube Shorts:
  * Tiêu đề ngắn gọn < 55 ký tự (tránh bị giao diện mobile che khuất).
  * Chứa từ khóa gây tò mò (Hook).
  * Tự động gắn kèm `#shorts` và thẻ hot trend.
  * Hỗ trợ API Google Gemini hoặc Google Translate Engine miễn phí có cơ chế dự phòng.

---

## 4. BÍ QUYẾT CHỐNG CHECKPOINT & AN TOÀN TÀI KHOẢN (ANTI-CHECKPOINT RULES)
Nếu bàn giao cho model tiếp theo phát triển tiếp các tính năng, **tuyệt đối tuân thủ 4 nguyên tắc sau**:
1. **Desktop Consistency (Tính nhất quán máy tính):** Mặc định luôn đặt `emulate_mobile = 0`. Người dùng đăng nhập tài khoản Google bằng Chrome Windows thì lúc đăng video tự động cũng phải chạy đúng Chrome Windows đó. Tuyệt đối không ép Header di động (Android Pixel) vào thư mục Profile tạo trên Windows vì sẽ bị Google quét lệch phần cứng (WebGL báo GPU máy tính nhưng User-Agent báo điện thoại $\rightarrow$ Checkpoint ngay lập tức).
2. **Không đóng Chrome đột ngột sau khi bấm Đăng:** Phải chờ tối thiểu 6-8 giây hoặc đợi request cập nhật trạng thái kết thúc để Google hoàn tất việc cập nhật cookie bảo mật `SIDCC` và `__Secure-1PSIDTS`.
3. **Giãn cách thời gian đăng video:** Luôn sử dụng chức năng `📅 Lên Lịch Giãn Cách` (tối thiểu cách nhau 30 - 60 phút) để tránh bị hệ thống YouTube phạt vì hành vi spam tần suất cao.
4. **Proxy đồng nhất:** Nếu kênh dùng Proxy, cả lúc mở Chrome đăng nhập thủ công lẫn lúc tự động đăng video đều phải đi qua cùng một địa chỉ IP Proxy đó.

---

## 5. HƯỚNG DẪN KHỞI CHẠY NHANH (QUICK START)
1. Mở thư mục `D:\DHB_Reup_YouTube_Studio\` (hoặc thư mục đã giải nén).
2. (Nếu lần đầu sử dụng): Chạy `CAI_DAT_MOI_TRUONG.bat` để cài đặt thư viện.
3. Chạy `START_DHB_YOUTUBE.bat`. Hệ thống sẽ tự động khởi động server uvicorn tại cổng 8686 và mở trình duyệt tại địa chỉ: `http://127.0.0.1:8686`.
