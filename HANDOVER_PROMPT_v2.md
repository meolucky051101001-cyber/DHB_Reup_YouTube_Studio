# PROMPT BÀN GIAO TOÀN DIỆN — DHB REUP YOUTUBE STUDIO (BẢN CẬP NHẬT 2026-10-24)
*(Trao cho kỹ sư tiếp nhận vai trò Senior Fullstack & Automation Engineer bảo trì tiếp dự án)*

---

## 0. NHIỆM VỤ CỦA BẠN

Tiếp nhận bảo trì và nâng cấp tool "DHB Reup YouTube Studio" trên máy Windows của tôi. Toàn bộ thay đổi code CHỈ thực hiện trong `D:\DHB_Reup_YouTube_Studio\`. Tuyệt đối không sửa/xóa file ngoài thư mục này (đặc biệt `C:\DHB Reup Facebook\` chỉ được ĐỌC để tham khảo — không ghi gì vào đó). Khi nhận yêu cầu mới: đọc file thật trong thư mục dự án trước khi sửa, luôn kiểm tra cú pháp (`python -m py_compile` + `node --check`) và chạy thử server sau khi sửa.

---

## 1. THÔNG TIN HỆ THỐNG

- **Thư mục chính:** `D:\DHB_Reup_YouTube_Studio\` — mọi chỉnh sửa ở đây.
- **Khởi chạy:** `START_DHB_YOUTUBE.bat` → web UI tại `http://127.0.0.1:8686` (server chạy bằng lệnh thô `python app.py`, KHÔNG dùng venv). Sau khi sửa frontend, nâng version `app.js?v=N` trong `index.html` (hiện tại **v18**) và yêu cầu tôi Ctrl+F5.
- **Stack:** Python 3.10 + FastAPI + Uvicorn + SQLite WAL (`database.py:get_conn` — timeout 30s, journal_mode=WAL, synchronous=NORMAL); frontend SPA vanilla JS (polling 2.5s); Playwright điều khiển Chrome thật theo Profile; FFmpeg/ffprobe/youtube-dl kế thừa từ `C:\DHB Reup Facebook\`.
- **Cấu trúc file:** `app.py` (REST API ~35 endpoints), `config.py` (đường dẫn + kho tài nguyên DHB), `database.py` (schema + migration + backup + housekeeping), `pipeline_manager.py` (đa luồng Semaphore + stop_event + Auto Monitor 24/7), `render_engine.py` (7 chế độ render FFmpeg + PID trap + branding overlays), `youtube_uploader.py` (Playwright Studio 4 bước + YouTube Data API v3 + probe đăng nhập + quét kênh), `scanner_downloader.py` (yt-dlp quét/tải + quét thư mục đệ quy), `ai_engine.py` (Gemini/OpenAI/DeepSeek/Google Translate), `static/` (index.html + app.js + styles.css), `data/dhb_youtube.sqlite` (DB chính), `data/backups/` (backup tự động, giữ 5 bản).

---

## 2. TÌNH TRẠNG SAU 6 ĐỢT NÂNG CẤP + 4 ĐỢT P0/P1/P2 (đã làm gì)

**Tổng quan: 73 bài test tự động PASS ở lần chạy cuối (2026-10-26).** Bộ test nằm NGAY TRONG REPO tại `tests/` và chạy bằng `python -m unittest discover -s tests -v` (không còn file tạm ở `%TEMP%`). CI: `.github/workflows/tests.yml`.

### Đợt 7–10 (2026-10-25/26) — Sửa lỗi P0/P1/P2 có kiểm thử:
- **P0 fail-closed (mới nhất):** xác minh Channel ID tuyệt đối trước upload (`_verify_studio_channel_id` bắt buộc cả ID đích lẫn ID thực tế); Publish chỉ Completed khi đọc được **URL video thật** từ hộp thoại chia sẻ (`published_uncertain=True` nếu không đọc được, pipeline báo Error và không move/xóa file nguồn); Checks phải thấy chữ "No issues found"/tương đương mới được bấm Next (timeout → dừng, không đăng); giới hạn 18+ fail-closed và chặn ở chế độ YouTube API; nút Dừng chờ upload hiện tại hoàn tất thay vì giết giữa chừng.
- **P0 trước đó:** giãn cách 30 phút fail-closed (`last_upload_started_epoch`/`last_upload_finished_epoch`), chống đăng lại Completed + claim nguyên tử, chặn schedule thiếu giờ, bỏ fallback PUBLIC, sửa render 16:9/audio.
- **P1:** chặn SQL injection metadata hàng loạt; che secret khỏi API/log/CSV; bearer token cục bộ + chặn Host/Origin (`api_security.py`); bỏ endpoint purge; bỏ `shell=True`; proxy cho YouTube API; tên file render không trùng.
- **P2:** dedupe monitor theo kênh; bổ sung field 18+/Kids/AI/mobile cho upload kéo-thả; bỏ `innerHTML` khỏi toast; TLS verification; siết lỗi bị nuốt; tạm dừng polling khi tab ẩn.
- **P1/P2 còn lại (2026-10-26):** nhóm kênh chia vòng theo **ID video** (không theo thứ tự batch) và bỏ fallback ngầm `Default → Mặc định` (DB tự chuẩn hóa dữ liệu cũ); frontend tự lấy lại API token khi gặp 401 sau restart (không cần Ctrl+F5); lỗi đọc trạng thái Checks được đưa vào thông báo dừng thay vì nuốt im lặng; các bước Playwright không bắt buộc (đọc kênh, mở menu, thumbnail/bình luận API, đóng Chrome) đều ghi log WARNING.
- **Quy trình:** `requirements.txt` pin phiên bản; `.gitignore` chặn DB/OAuth khỏi Git; test đặt trong `tests/`.

### Đợt 1 — Quản lý Profile/Kênh & tùy chọn đăng:
- Bảng **👤 Quản Lý Profile Chrome** (tab 4) với nút: 🌐 Mở Đăng Nhập, 🔍 Kiểm Tra (mở Chrome khuất `--window-position=-3200,-3200`, tự xác nhận phiên Google còn sống, lưu `chrome_profiles.login_status`/`last_login_check`), 🔄 Quét Kênh (mở hộp chuyển tài khoản Studio, parse tên kênh/handle/email, upsert vào `youtube_channels`), ⚙️ Sửa Proxy (đồng bộ proxy xuống mọi kênh con), 🗑️ Xóa (chỉ xóa bản ghi DB, giữ nguyên thư mục đĩa).
- Endpoints mới: `PUT/DELETE /api/profiles/{name}`, `POST /api/profiles/{name}/open-browser|check-login|refresh-channels`, `PUT /api/channels/{id}`.
- Sửa lỗi **thứ tự bấm "Hiển thị thêm" (Show more)**: trước đây radio Giới hạn độ tuổi bị bấm khi mục còn ẩn → 18+/nhãn AI bị bỏ qua. Giờ mở rộng trước, bấm sau.
- Bước Checks tách 2 mức: bản quyền/chặn khu vực → vẫn tự Hủy + Xóa nháp (giữ nguyên bộ chốt); cảnh báo kiếm tiền/quảng cáo (soft) → chỉ log, vẫn đăng.
- Validate `schedule_time` trong `build_final_metadata` (quá khứ/sai định dạng → ValueError sớm); chế độ YouTube API hỗ trợ `publishAt` (private đến giờ hẹn).

### Đợt 2 — Review tổng thể + gia cố:
- **Sửa lỗi an toàn nghiêm trọng:** default DB `emulate_mobile` là 1 (giả lập mobile trên Profile Windows → nguy cơ Google checkpoint). Đã đổi default 0 + migration mỗi khởi động ép các dòng TỰ SINH (kênh placeholder, video CSV Pending) về Desktop.
- Backup DB tự động mỗi khởi động (SQLite Backup API, an toàn WAL, giữ 5 bản tại `data/backups/`); cắt `system_logs` còn 5000 dòng mỗi khởi động; `requirements.txt` thêm `yt-dlp` (trước đây thiếu → cài mới là sập); `check_status.py` trỏ đúng DB (trước sai path); render dọn file dở dang khi lỗi/Dừng (`_cleanup_partial_output` + `partial_output=` cho cả 2 ffmpeg runner); Monitor giãn cách đăng `monitor_upload_gap_minutes` (mặc định 30'); sửa bug precedence import CSV + thêm cột cấu hình; `/api/upload-video-files` nhận thêm made_for_kids/age_restriction/altered_content/emulate_mobile.

### Đợt 3 — Cứng hóa vận hành:
- `/api/upload-video-files` đổi `async def` → `def` (FastAPI đẩy threadpool) — trước đây chặn event loop, UI treo khi nạp file lớn.
- CSV xuất/nhập khép kín 100% (Visibility, Schedule_Time, Made_For_Kids, Age_Restriction, Altered_Content).
- **Video câm render được:** `probe_video_info` trả `has_audio`; render tự chèn `anullsrc` (lưu ý index input: nhánh không nhạc nền map im lặng ở index 1, nhánh pip index 2, nhánh có nhạc chỉ dùng nhạc).
- Housekeeping daemon mỗi 6h (`app.py:_housekeeping_worker` → `database.run_housekeeping()`): backup + prune log + prune `published_history` >90 ngày + dọn file `temp/wmtext_*.txt` quá 1 ngày.
- Nút Dừng giết FFmpeg **có chọn lọc**: kill PID đã theo dõi + PowerShell `Get-CimInstance Win32_Process` chỉ kill ffmpeg có command line nằm trong temp/output CỦA TOOL (đã bỏ `taskkill /F /IM ffmpeg.exe` toàn cục).
- Upload Playwright gãy giữa chừng **tự Hủy + Xóa bản nháp** trong Studio (cờ `draft_created` set sau `set_input_files`, except gọi `discard_and_cancel_upload` rồi re-raise).
- `_launch_persistent_context_guarded()` — báo lỗi thân thiện "hãy đóng cửa sổ Chrome của Profile này" khi profile bị chiếm.
- Polling UI ngưng re-render bảng khi modal sửa/xem video đang mở.

### Đợt 4 — Port tính năng từ tool gốc DHB Reup Facebook (đọc `C:\DHB Reup Facebook`):
- **Preset Render có tên:** bảng `render_presets` + `GET/POST/DELETE /api/render-presets` (POST = upsert theo tên) + card UI "💾 Preset Render Có Tên" (tab Render).
- **Lớp phủ branding** (`_build_branding` trong render_engine): watermark ảnh PNG/JPG (kho `_Image`, cỡ/vị trí/độ mờ), **logo video chroma-key** (kho `_Video`, `chromakey=0x{hex}:{sim}:{blend}`, `-stream_loop -1`), watermark chữ + **chữ chạy ngang** (drawtext qua textfile tạm), áp dụng cho mọi chế độ render.
- **Intro/Outro tự động** (`_apply_intro_outro`): ghép concat từ kho `_Intro`/`_Outro`, hỗ trợ chọn ngẫu nhiên (`intro_random`/`outro_random`), chuẩn hóa fps/sar/pix_fmt/timebase + aformat audio cho từng đoạn.
- Video nền ngẫu nhiên (`bg_video_random`) cho pip; trộn âm **% Nhạc Gốc / % Nhạc Nền** (`audio_original_percent`, `bg_music_percent` — học OriginalAudioPercent của DHB).
- `max_uploads_per_run` (0 = tắt) — pipeline đếm số lần đăng, chạm mức thì video còn lại giữ `Pending` + log (chống spam); `thumbnail_source` (reup|generate); độ phân giải tải riêng TikTok/Douyin (`download_resolution_tiktok`, mặc định 720p, hàm `_height_cap`); lọc nguồn theo `min_comments` (chỉ khi yt-dlp trả comment_count).

### Đợt 5 — Lên lịch số lượng lớn (50+ video):
- `/api/queue/auto-schedule` làm cứng: **ép giãn cách ≥30 phút**; giờ bắt đầu bắt buộc TƯƠNG LAI (quá khứ → HTTP 400); không nhập giờ → mặc định **now+60 phút** (tránh video cuối lô bị "lịch đã qua" vì upload tốn thời gian); response trả `first_time/last_time/interval_minutes`.
- Modal `modal-auto-schedule`: chọn ngày/giờ đầu + khoảng cách (min 30) + Công chiếu + **tóm tắt trực tiếp** ("50 video từ X đến Y, ~Z ngày").

### Đợt 6 — Kết nối thư mục máy tính:
- `GET /api/browse-disk?path=&recursive_count=`: liệt kê ổ đĩa/thư mục con/video (ẩn entry `.`/`$`, path sai tự fallback an toàn, `recursive_count=1` đếm đệ quy os.walk).
- `/api/add-local-paths` + `/api/queue/scan` nhận `recursive` (os.walk toàn cây); checkbox "📂 Quét cả THƯ MỤC CON" ở panel quét.
- Modal `modal-browse-folder` (nút **🗂️ Kết Nối Thư Mục**): chọn ổ đĩa/gõ path/điều hướng cây, nút ➕ thêm riêng từng thư mục con, 2 nút chính "Thêm Video Trong Thư Mục Này" / "🌳 Thêm Cả Thư Mục Con (Đệ Quy)" kèm xác nhận số video.

---

## 3. NGUYÊN TẮC BẤT DI BẤT DỊCH (ANTI-CHECKPOINT) — KHÔNG ĐƯỢC PHÁ

1. **Native Desktop:** mặc định `emulate_mobile = 0`. Không bao giờ ép User-Agent di động vào Profile Chrome tạo trên Windows (lệch WebGL → Google khóa phiên). Code hiện đã ép default ở DB + migration; đừng đảo ngược.
2. **Không đóng Chrome đột ngột sau khi bấm Đăng:** giữ nguyên đoạn chờ dialog chia sẻ + 2s ở Dashboard sau `#done-button` (cookie SIDCC/__Secure-1PSIDTS cần 6-8s đồng bộ).
3. **Chống spam:** lịch giãn cách tối thiểu 30 phút/video (đã ép trong auto-schedule + `monitor_upload_gap_minutes`); có `max_uploads_per_run` nếu cần giới hạn mỗi phiên.
4. **Proxy đồng nhất:** profile và mọi kênh con thuộc profile phải đi cùng proxy (đã đồng bộ khi sửa proxy profile — giữ cơ chế này).

**Phạm vi hợp tác đã thống nhất với chủ tool:** làm mọi việc kỹ thuật (bug, UI, DB, pipeline, downloader, hạ tầng uploader, lịch, branding...). KHÔNG phát triển/cải tiến các tính năng có mục đích né tránh phát hiện bản quyền (chuỗi pitch-shift/EQ "lách Content ID" trong render_engine — giữ nguyên hiện trạng, không tối ưu thêm, không tạo filter mới cùng mục đích). Nếu được yêu cầu, từ chối phần đó và đề xuất hướng hợp pháp.

---

## 4. BẪY KỸ THUẬT ĐÃ TRẢ GIÁ (đừng mắc lại)

1. **drawtext trên Windows qua argv:** đường dẫn phải escape 2 cấp — dấu `:` ổ đĩa thành `C\:/...` NGAY CẢ trong nháy đơn; dấu phẩy trong biểu thức filter phải `\,` (vd `x='w-mod(t*10\,w+tw)'`). Xem `_ff_path` trong render_engine.
2. **concat filter:** input phải XEN KẼ theo segment `[v0][a0][v1][a1]`, không phải video hết rồi audio; các đoạn phải đồng bộ `settb=AVTB`/`aformat`/`format=yuv420p`/`fps` else "Error linking filters".
3. **render_engine có cờ `rendered`:** nhánh 16:9 mặc định bọc trong `if not rendered:` — thêm chế độ render mới phải set cờ + `out_w/out_h`, không thì render đè video.
4. **Race pipeline cho test automation:** pipeline set trạng thái cuối TRƯỚC khi `_batch_worker` nhả `_is_running`; khi viết test phải (a) đợi `/api/overview.pipeline_running == false`, (b) đợi trạng thái/output THAY ĐỔI so với trước start rồi mới đợi terminal status.
5. **DB import:** `database.py` cần đủ import (`APP_DATA_DIR`, `APP_TEMP_DIR` từng bị thiếu → NameError bị nuốt âm thầm).
6. **FFmpeg stderr đọc theo dòng** với `errors="ignore"`; tiến trình bọc trong `_ACTIVE_FFMPEG_PROCS` để kill tức thì.

---

## 5. CÁCH KIỂM THỬ (pattern đã dùng)

- Chạy server nền: `cd /d/DHB_Reup_YouTube_Studio && python app.py > /tmp/dhb.log 2>&1 &` rồi `sleep 7`.
- Tắt: tìm PID `netstat -ano | grep :8686 | grep LISTENING` → `taskkill //F //PID <pid>` (Git Bash cần `//`).
- Test bằng HTTP trực tiếp (requests/curl) tới 127.0.0.1:8686; tạo video mẫu bằng `ffmpeg -f lavfi testsrc + sine`; render thật qua `/api/pipeline/start` rồi ffprobe kiểm tra kích thước/thời luợng/audio stream.
- Bộ test hiện tại nằm trong repo: `tests/test_p1_security.py`, `tests/test_p2_quality.py`, `tests/test_upload_gap_p0_7.py`, `tests/test_p0_fail_closed.py`, `tests/test_p1_p2_remaining.py`. Chạy toàn bộ: `PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -v` (73 test).
- Sau mọi thay đổi: chạy lại toàn bộ bộ test + `python -m py_compile app.py pipeline_manager.py youtube_uploader.py database.py render_engine.py scanner_downloader.py ai_engine.py config.py`; kiểm tra cú pháp JS bằng `node --check static/app.js`; dọn sạch project/file test (`_TestZCode*`) sau khi xong.
- Kiểm thử Playwright không cần tài khoản thật: dùng trang HTML cục bộ + `page.route()` để giả lập DOM Studio (đã dùng cho P2). TUYỆT ĐỐI không upload thật khi chỉ đang test code.

---

## 6. VẤN ĐỀ CẦN GIẢI QUYẾT (danh sách bàn giao — theo thứ tự ưu tiên)

**P0 — chặn vận hành, cần chủ tool thao tác/cần chạy thật để xác minh:**
1. **Channel ID đang TRỐNG ở cả 7 kênh trong DB** → mọi lượt upload fail-closed (đúng thiết kế). Bấm "🔄 Quét Kênh" cho từng profile để tool tự điền ID (đọc từ `page.url`, `ytcfg.CHANNEL_ID`/`DATASYNC_ID`, và `el.data.navigationEndpoint.browseEndpoint.browseId` của account item), hoặc nhập tay từ URL Studio. Phần đọc `el.data` CHƯA được kiểm chứng trên profile thật — nếu quét vẫn trống ID, nhập tay.
2. **Xác minh phiên đăng nhập từng profile:** 6/7 profile chưa được kiểm tra trong phiên gần nhất; `chrome_profiles.login_status` có thể cũ. Mở "🌐 Đăng Nhập" rồi "🔍 Kiểm Tra" cho từng profile.
3. **1 lần đăng thật có giám sát** (chỉ sau khi (1) và (2) xong): xác nhận chọn đúng kênh con, đọc URL từ share dialog, Checks "No issues found", và đặc biệt **giới hạn 18+** trên Studio thật (radio `VIDEO_AGE_RESTRICTION_RESTRICTED` — selector chưa được chạy thật; nếu không xác nhận được, tool sẽ hủy nháp và báo lỗi, không đăng sai tuổi).
4. Render branding (overlay/intro/outro) trên video DÀI (>5 phút) và 4K: mới test video ngắn 3s — chưa đo hiệu năng.

**P1 — hạn chế đã biết, đáng làm tiếp:**
5. Chưa chặn ở UI khi bấm Start lô có kênh thiếu Channel ID (backend đã fail-closed; bảng kênh có badge "⚠️ Thiếu Channel ID").
6. Chế độ YouTube API: OAuth JSON nhập tay, chưa có hướng dẫn lấy refresh token; API mode chủ động từ chối video 18+ (đúng thiết kế vì API không xác nhận được UI).
7. Từ khóa Checks đã phủ nhiều ngôn ngữ phổ biến; ngôn ngữ Studio lạ sẽ fail-closed (an toàn nhưng không đăng) — có thể bổ sung thêm khi gặp thực tế.
8. `bulk-group` được tham chiếu trong app.js nhưng không có trong HTML (null-guard, vô hại).
9. Import đệ quy/ffprobe đã song song hóa — cần theo dõi hiệu năng thực tế với 100+ file.
10. Monitor heartbeat/restart + dedupe đã có — cần theo dõi thực tế 24/7.

**P2 — ý tưởng nâng cấp tiếp theo (chưa làm):**
11. Tách hàng đợi render sang tiến trình pool riêng (hiện chạy trong thread pipeline).
12. Export CSV định kỳ toàn bộ DB ra thư mục ngoài nếu chủ tool yêu cầu (backup SQLite 5 bản đã có).
13. Git init/commit + branch workflow (hiện repo chưa có `.git`; `.gitignore` và CI đã chuẩn bị sẵn).

---

## 7. HIỆN TRẠNG PHIÊN BẢN

- `app.js?v=18`, DB có các bảng: video_queue, youtube_channels, chrome_profiles (có login_status/last_login_check), channel_groups, upload_option_groups, projects, monitors, published_history, system_logs, app_settings, **render_presets**.
- Settings toàn cục đáng chú ý: `checks_timeout_seconds` (120), `monitor_upload_gap_minutes` (30), `max_uploads_per_run` (0), `thumbnail_source` (reup), `download_resolution_tiktok` (720p), `max_concurrent_threads` (2), `upload_delay_seconds` (10).
- Bảo mật cục bộ: mọi `/api/*` (trừ `/api/auth/token`) yêu cầu header `X-DHB-Token`; Host phải là localhost; request thay đổi trạng thái phải có Origin localhost (`api_security.py`). Frontend tự lấy token bằng `ensureApiToken()` và gửi kèm trong `apiFetch()`.
- Upload fail-closed: thiếu Channel ID, không đọc được ID thực tế, không thấy Checks an toàn, không xác nhận 18+, hoặc không lấy được URL video sau khi bấm Đăng → coi như lỗi/không chắc chắn, không báo Completed, không move/xóa file nguồn.
- Lần cuối kiểm chứng: 2026-10-26 — **61/61 test PASS** (`python -m unittest discover -s tests`), `python -m py_compile` sạch, chưa chạy upload thật trong phiên này.

*(Hết prompt bàn giao — đọc kỹ mục 3, 4, 6 trước khi bắt tay vào việc.)*
