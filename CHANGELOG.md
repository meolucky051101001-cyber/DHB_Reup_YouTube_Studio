# CHANGELOG — DHB Reup YouTube Studio

## 2026-11-01 — Gia cố kết nối Antidetect

- Kết nối CDP trong `AntidetectBrowserLease` có retry 3 lần, backoff tăng dần (0.8s → 1.6s); Chrome vừa được Antidetect khởi chạy nhưng CDP chưa mở kịp không còn làm hỏng lượt upload ngay lần đầu. Khi cạn lượt, lỗi báo rõ cổng CDP đã thử.
- Lease xác định quyền sở hữu phiên bằng dữ liệu cấu trúc (`already_running`, `pid`, `ws_endpoint`) thay vì so khớp câu chữ `"Profile đang chạy"`; API cũ không trả các trường này thì fallback về trạng thái profile đọc trước khi start. Thiếu căn cứ thì KHÔNG tự dừng profile (ưu tiên không đóng phiên người dùng đang mở).
- `resolve_base_url()` health-check lại URL đã cache trước mỗi thao tác; cache chết hoặc máy chủ đổi cổng được xóa và dò lại từ `server.pid` rồi cổng 8000. Lỗi kết nối trong `_request` cũng xóa cache để thao tác sau tự dò lại.
- Thêm 11 test trong `tests/test_antidetect_integration.py` (fresh launch/already-running, retry CDP, health-check + rediscovery, xóa cache khi mất kết nối). Tổng bộ test: **107/107 PASS**; `py_compile` và `node --check static/app.js` sạch. Không chạy upload thật.

## 2026-11-01 — Sửa 3 lỗi P0 render/pipeline

- Ghép Intro/Outro: xác định đúng vị trí video chính thay vì giả định `i == 1`; trước đây khi chỉ bật Outro, segment Outro không được scale/pad về cùng khung nên concat có thể lỗi "Error linking filters" hoặc ra video sai kích thước.
- `_run_ffmpeg_with_progress`: mọi nhánh lỗi/dừng (callback progress lỗi, DB lỗi, người dùng bấm Dừng, FFmpeg exit khác 0) đều kill + reap tiến trình trước khi xóa file dở dang — không còn FFmpeg mồ côi giữ/ghi đè file output. stdout chuyển DEVNULL để tránh pipe đầy làm treo render.
- Phân làn FFmpeg theo owner (`batch` / `monitor`); `stop_all()` chỉ hủy render của làn batch, render Monitor đang chạy không bị giết và chỉ dừng theo stop-event riêng của monitor. Dừng toàn cục (owner=None) vẫn giữ hành vi cũ.
- Thêm `tests/test_render_pipeline_p0.py` (9 test). Tổng bộ test: **96/96 PASS**; `py_compile` toàn bộ module và `node --check static/app.js` sạch. Không chạy upload thật.

## 2026-10-29 — Antidetect (working tree): v19

- Tích hợp Antidetect Browser Manager qua CDP lease (`antidetect_client.py`): đồng bộ profile không kéo mật khẩu proxy; kênh Antidetect chỉ đăng bằng Playwright, chặn YouTube Data API; không ép mobile/UA/proxy/stealth lên trình duyệt Antidetect.
- Thêm cột `chrome_profiles.antidetect_profile_id` + index unique partial; migration đã áp trên DB thật ngày 2026-10-29.
- Sửa: `app.js?v=19`; fail-closed khi mở Profile `antidetect://` thiếu ID liên kết thay vì rơi xuống nhánh Chrome cục bộ.
- Xác minh `/api/antidetect/status`: 401 khi thiếu token, 200 khi có token. Chưa chạy upload thật.

## 2026-10-26 — v18: fail-closed upload + gia cố P1/P2

### Upload an toàn (P0)
- Bắt buộc `channel_id` khi upload bằng Playwright và YouTube API; xác minh chính xác kênh con trước khi đăng, nút "Quét Kênh" lưu ID đọc được. Kênh thiếu ID bị chặn (fail-closed), không tự đoán kênh.
- Nút Dừng chờ lượt upload đang chạy kết thúc rồi mới dừng lô; không giết tiến trình giữa chừng.
- Giới hạn 18+ fail-closed trên Studio; chế độ YouTube API từ chối video 18+ vì không xác nhận được UI. Kids + 18+ bị từ chối ngay từ metadata.
- Bước Checks chỉ cho bấm Next khi thấy kết quả an toàn được nhận diện; timeout/không đọc được trạng thái thì hủy nháp và báo lỗi.
- Chỉ đánh dấu Completed khi đọc được URL `youtu.be`/`watch` thật từ hộp thoại chia sẻ. Trường hợp không xác minh được ghi `published_uncertain`, chuyển Error và giữ file nguồn.
- Giãn cách tối thiểu 30 phút giữa các lượt đăng được ép ở backend, kể cả khi settings bị sửa/xóa.

### Nhóm kênh (P1)
- Chia kênh theo nhóm dùng ID video làm khóa xoay vòng, ổn định khi thứ tự lô thay đổi; log rõ kênh được chọn cho từng video.
- Bỏ fallback ngầm `Default → Mặc định`; nhóm không có kênh sẽ báo lỗi và dừng thay vì đăng nhầm.
- Khi khởi động, DB tự gộp dữ liệu cũ `Default` vào nhóm chuẩn `Mặc định` ở `channel_groups`, `youtube_channels`, `video_queue`, `monitors`.

### Frontend / vận hành (P1)
- Tab đang mở tự lấy lại bearer token khi gặp 401 sau khi restart server (không cần Ctrl+F5); áp dụng cho API, tải file, upload trực tiếp và import Excel.
- Bỏ endpoint/UI purge dữ liệu toàn cục; token cục bộ theo tiến trình kèm kiểm tra Host/Origin cho request thay đổi trạng thái.
- Tạm dừng polling khi tab ẩn; toast không render HTML; bổ sung đủ trường 18+/Kids/nội dung AI/mobile cho upload kéo-thả.

### Uploader / chất lượng (P2)
- Các lỗi Playwright không bắt buộc (đọc danh sách kênh, mở menu, thumbnail/bình luận API, đóng Chrome) được ghi log thay vì nuốt im lặng.
- Lỗi đọc trạng thái Checks được đưa vào thông báo dừng fail-closed kèm lý do.
- Hủy nháp thất bại được ghi ERROR và hướng dẫn xóa thủ công trong Studio.
- Monitor có heartbeat/restart, chống import trùng, dedupe theo kênh; probe video khi import thư mục được song song hóa.

### Kiểm thử và quy trình
- Bộ test trong repo: `tests/test_p0_fail_closed.py`, `test_p1_p2_remaining.py`, `test_p1_security.py`, `test_p2_quality.py`, `test_upload_gap_p0_7.py`.
- Lần chạy cuối: **73/73 PASS**; `py_compile` và `node --check static/app.js` sạch.
- `requirements.txt` pin phiên bản; `.gitignore` loại DB/OAuth/profile/log khỏi Git; CI Windows tại `.github/workflows/tests.yml`.
- Test xoay vòng kênh được mock `add_log` để không ghi dữ liệu giả vào `system_logs` thật.

### Cổng vận hành còn mở (chưa xác minh thực tế)
- 7/7 kênh vẫn trống `channel_id`: cần "🔄 Quét Kênh" hoặc nhập tay trước khi đăng.
- Cần xác minh phiên đăng nhập của từng Chrome Profile.
- Chưa chạy lại selector giới hạn 18+ trên Studio thật; cần một lượt đăng có giám sát.
- Chưa đo hiệu năng render branding trên video dài (>5 phút) và 4K.
