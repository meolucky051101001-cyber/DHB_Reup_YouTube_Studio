# CHANGELOG — DHB Reup YouTube Studio

## 2026-11-03 — Khóa một phiên server và phục hồi job sau khi tiến trình dừng

- Thêm khóa singleton trên file trong thư mục data; server lấy khóa trước migration và phục hồi DB. Tiến trình thứ hai bị từ chối, không chạy dọn claim lên dữ liệu của server còn sống.
- Khi khởi động lại hợp lệ, job bị ngắt trước upload chuyển về Pending; job đang upload/xác minh hoặc có trạng thái upload chưa phân loại được chuyển sang NeedsReview. Completed có URL giữ nguyên; claim cũ được xóa sau khi đã phân loại.
- Thêm kiểm thử khóa giữa hai tiến trình và phục hồi các trạng thái upload/render, gồm thông điệp tiến độ cũ.
- Kiểm tra working copy: 3 test vòng đời pass; py_compile và node --check pass. Chưa chạy server đang hoạt động hay DB thật.

## 2026-11-03 — Chặn retry khi chưa rõ kết quả đăng; gia cố CSV và quyền sở hữu worker

- Kết quả đã gửi yêu cầu đăng nhưng chưa xác minh được URL chuyển sang `NeedsReview`; cả API chạy lô lẫn worker đều chặn tự chạy lại trạng thái này, kể cả khi có cờ ép đăng lại. UI hiển thị riêng để người vận hành kiểm tra Studio.
- Nếu chưa xác minh được URL, pipeline giữ nguyên file nguồn và không tự move/xóa file tạm.
- Cập nhật queue chỉ áp dụng khi dòng vẫn mang đúng `claim_token` của worker; callback tiến độ chỉ ghi phần trăm, không ghi đè trạng thái vòng đời.
- Import CSV xử lý BOM, chuẩn hóa URL trong phạm vi cùng dự án, bỏ qua URL thiếu/sai và dòng không có tiêu đề; trả số lượng đã thêm/bỏ qua. Toàn lô nhập dùng transaction SQLite.
- Bổ sung hồi quy cho trạng thái cần đối soát, CSV BOM/trùng/thiếu dữ liệu và chặn retry.
- Kiểm tra trên working copy cô lập: 87 test mục tiêu pass; `py_compile`/AST, `node --check` và `git diff --check` pass. Bộ đầy đủ chưa kết thúc do các bài smoke/TestClient và Antidetect bị treo/chờ; chưa xác minh upload Studio thật.

## 2026-11-02 (đợt 3) — Đồng bộ Antidetect với YouTube Studio (nút "🧲 Quét Kênh Studio")

- **Luồng mới:** kéo Profile từ Antidetect về tool chỉ là bước một — giờ có thể quét tiếp **YouTube Studio bên trong từng Profile Antidetect**: mở profile qua lease CDP (Antidetect vẫn là chủ sở hữu duy nhất của trình duyệt/proxy/fingerprint), đọc trạng thái đăng nhập Google + danh sách kênh thật (tên, handle, email, **Channel ID**) rồi đồng bộ về `youtube_channels`, sẵn sàng để đăng với Channel ID đầy đủ (không còn bị chặn fail-closed "thiếu Channel ID").
- Endpoint `POST /api/antidetect/sync` nhận thêm `{"scan_channels": true}`; không truyền thì giữ nguyên hành vi cũ (chỉ đồng bộ danh sách profile). Hàm lõi `sync_antidetect_studio_channels` chạy tuần tự, ghi log từng profile vào tab Nhật Ký để theo dõi tiến độ (mỗi profile ~15-30s).
- Tách hàm dùng chung `_upsert_profile_channels` (khớp theo channel_id → handle → tên, upsert không nhân đôi) — `refresh-channels` của Profile Chrome local và luồng Antidetect nay dùng chung một logic.
- `_probe_antidetect_login` giữ thêm khóa per-profile `_get_antidetect_lock`: đang upload trên profile Antidetect thì quét phải chờ (và ngược lại) — không còn 2 lease CDP tranh cùng Chrome.
- UI: nút **"🧲 Quét Kênh Studio"** cạnh "🔗 Đồng Bộ Antidetect" (xác nhận trước khi chạy, tóm tắt kết quả từng profile sau khi xong); `app.js?v=21`.
- Thêm `tests/test_antidetect_studio_sync.py` (6 test, mock hoàn toàn — không mở trình duyệt): tạo kênh với Channel ID, chạy lại không nhân đôi, ghi nhận chưa đăng nhập, lỗi 1 profile không chặn profile sau, endpoint mặc định không quét, endpoint có flag quét đúng. Tổng: **137/137 PASS**; `py_compile` và `node --check static/app.js` sạch.
- Đã restart server live; xác minh: nút mới + v=21 được phục vụ, endpoint phản hồi fail-closed đúng khi Antidetect chưa mở. ⚠️ Chưa chạy được lượt quét thật (Antidetect chưa mở trên máy lúc verify) — lần chạy thật đầu tiên cần mở Antidetect trước; nếu số kênh đọc sai thì tinh chỉnh selector ở `_collect_studio_channels` theo log.
- **Xác minh thực tế (cùng ngày, Antidetect chạy thật, 9 profile):** quét 3 lượt full qua API live. Lượt cuối: 9/9 profile quét thành công, 8/9 đăng nhập Google, **9/9 kênh đều có Channel ID đầy đủ** (điều kiện bắt buộc để đăng fail-closed). Quá trình xác minh phát hiện và đã vá 3 lỗi thật: (1) lỗi cấu trúc sau khi thêm khóa antidetect — các nhánh kiểm tra URL chạy NGOÀI lease làm page bị đóng trước khi đọc; (2) tài khoản Google chưa có kênh (Studio redirect về youtube.com + form "Tạo kênh") bị báo chung chung — nay báo đúng nguyên nhân, dò qua body.innerText (form nằm trong shadow DOM); (3) kênh KHÔNG active trong hộp chuyển tài khoản không trả browseId (Studio build mới) — nay tra ID qua canonical link của youtube.com/@handle (ytcfg.CHANNEL_ID đã xác minh rỗng); kèm retry mở hộp chuyển tài khoản 2 lần (P6 + meosieubeo: 0 kênh → 2+1 kênh nhờ retry). Kết quả: 9 kênh mới từ Antidetect nằm trong tab Quản Lý Kênh, sẵn sàng gán đăng.

## 2026-11-02 (đợt 2) — Chống đăng trùng Monitor, chặn xóa đang chạy, dedupe kéo-thả, smoke test CI

- **Race dedupe Monitor (P1):** trước đây 2 monitor cùng đích đăng (cùng kênh hoặc cùng nhóm — nhóm cùng thì vòng xoay kênh cũng ra cùng kênh) có thể chèn cùng một nguồn rồi đăng trùng, vì khoảng cách giữa lúc quét và lúc insert có bước AI có thể kéo dài hàng chục giây. Giờ: (a) bước lọc khi quét nhìn thấy cả hàng đợi của monitor KHÁC cùng đích (`_monitor_source_queued`), không chỉ project của mình; (b) insert chuyển sang `insert_monitor_queue_item` nguyên tử (`INSERT ... SELECT ... WHERE NOT EXISTS`) — chặn ngay tại DB, monitor chen sau nhận `None` và bị bỏ qua có log.
- **Chặn xóa video đang xử lý:** `POST /api/queue/delete` từ chối (HTTP 409 kèm danh sách) các video đang `Uploading/Rendering/Downloading/Chờ giãn cách` hoặc đang bị worker giữ (`claim_token` khác rỗng) — trước đây xóa được cả hàng đang đăng, worker đăng xong ghi vào dòng đã mất. Chỉ xóa được sau khi bấm Dừng và luồng kết thúc.
- **Dedupe kéo-thả theo nội dung:** post_id của file upload trực tiếp giờ hash theo nội dung (kích thước + 1MB đầu) thay vì md5(tên file ngẫu nhiên) — kéo cùng một video 2 lần chỉ tạo 1 hàng, bản trùng bị bỏ qua và file tạm được dọn.
- **Không nuốt lỗi im lặng:** `release_item_claim`, `clear_stale_claims`, `stop_all` (phần UPDATE trạng thái) ghi log WARNING/ERROR khi thất bại thay vì `except: pass` — claim rò rỉ và sai lệch trạng thái trước đây không thể nhận diện.
- **Smoke test CI:** thêm `tests/test_guard_round2_smoke.py` (12 test): 3 test dedupe monitor (chặn cùng đích, cùng nhóm, không chặn video thêm tay), 3 test guard xóa, 1 test dedupe upload, 5 test smoke qua `fastapi.testclient` (index 200, token, 401 khi thiếu token, overview 200 với token) — bắt cả lớp lỗi import-time mà `py_compile` không thấy. Thêm `httpx==0.28.1` vào `requirements.txt` cho TestClient.
- **Đợt 1 cùng ngày** (đã ghi ở entry dưới): P0 render treo vô hạn, watchdog FFmpeg, Dừng khi tải, import Excel threadpool.
- Tổng bộ test: **131/131 PASS**; `py_compile` và `node --check static/app.js` sạch. Server live đã restart lên code mới, xác minh: overview/export-excel/import-excel/delete hoạt động, log boot sạch. Không chạy upload thật.

## 2026-11-02 — Fix P0 render treo vô hạn + watchdog FFmpeg + Dừng có hiệu lực khi tải

- **P0 render (đã reproduce thực nghiệm):** video nguồn câm + nhạc nền (`-stream_loop -1`) khiến FFmpeg encode âm thanh vô hạn — `-t` đặt trước `-i` chỉ giới hạn input 0, còn nhạc nền lặp không bao giờ kết thúc. Hai nhánh `shorts_9_16` và `full_16_9` (render_engine.py) có nhạc nền nay thêm `-shortest` để output dừng theo video. Repro trước vá: video 2s phình 7.8MB và không thoát (kill sau 25s); sau vá: thoát ngay, output đúng ~2s.
- **Watchdog FFmpeg:** `_run_ffmpeg_with_progress` có luồng watchdog — FFmpeg không ghi gì ra stderr trong 120s (`FFMPEG_SILENCE_TIMEOUT_SEC`, có thể ép qua tham số `silence_timeout` khi test) thì tự kill + báo lỗi rõ ràng thay vì kẹt vĩnh viễn một slot của render queue (pool chỉ 2 worker). Nhờ watchdog, nút **Dừng** giờ có hiệu lực cả khi FFmpeg im lặng (trước đây `readline()` block vô hạn nên Dừng không ăn).
- **Dừng khi tải video (nhánh S5 cũ):** `download_video` nhận `stop_event` — raise trong progress hook của yt-dlp, và KHÔNG rơi vào nhánh fallback youtube-dl khi người dùng bấm Dừng; fallback youtube-dl.exe có thêm timeout cứng 30 phút. `scan_social_urls` nhận `stop_event` (kiểm tra giữa các URL); làn Monitor truyền stop-event riêng vào cả hai.
- **Import Excel (nhánh S4 cũ):** phần nặng (openpyxl parse + ghép dòng + ghi DB) tách thành `_excel_import_worker` chạy qua `run_in_threadpool` — event loop không còn bị giữ khi import file lớn, polling 2.5s của dashboard mượt trở lại.
- Thêm `tests/test_p0_render_hang_and_stop.py` (9 test): render video câm + nhạc nền ở 2 chế độ (chống treo 90s + kiểm tra thời lượng output), watchdog kill FFmpeg câm, Dừng ăn khi readline đang block, download dừng trước/khi đang tải qua HTTP server giả chậm cục bộ, `_excel_import_worker` ghép đúng theo cột ID + từ chối file rác. Tổng: **119/119 PASS**; `py_compile` và `node --check static/app.js` sạch. Không chạy upload thật, không chạy dashboard.

## 2026-11-01 — P1: chặn sớm thiếu Channel ID, lịch sử đăng đủ nguồn, CI phủ đủ module

- UI `app.js?v=20`: trước khi chạy lô có upload, kiểm tra kênh đích thực tế của từng video (kênh cụ thể → profile đơn → xoay vòng nhóm với thứ tự `id ASC` như backend). Kênh thiếu Channel ID bị chặn ngay tại UI kèm danh sách video/kênh; backend vẫn fail-closed như cũ. Các video đã `Completed`/có link được bỏ qua khi người dùng chọn "chỉ chạy video còn lại", nên không bị chặn oan.
- `published_history` được ghi cho mọi lượt đăng `Completed` có URL thật (link, kéo-thả, local path), không chỉ Auto Monitor; khóa `(post_id, channel_id)`, `post_id` rỗng dùng `queue:<id>`. Dedupe của Monitor không còn phụ thuộc riêng `video_queue`.
- CI `py_compile` phủ đủ 11 module (thêm `api_security.py`, `antidetect_client.py`, `check_status.py`) và thêm bước `node --check static/app.js`.
- Thêm 3 test trong `tests/test_p2_quality.py`: ghi history từ `process_single_item` (upload mock), bỏ qua item chưa Completed/thiếu URL, idempotent + khóa theo hàng đợi. Tổng: **110/110 PASS**; `py_compile` và `node --check` sạch. Không chạy upload thật, không chạy dashboard.

## 2026-11-01 — Tài liệu: thêm README

- `README.md` mô tả tổng quan tool, yêu cầu môi trường, cài đặt/khởi động, biến cấu hình Antidetect, cấu trúc module + bảng DB, cách chạy test và liên kết tới các tài liệu vận hành sẵn có.

## 2026-11-01 — CI: nâng GitHub Actions

- `actions/checkout` v4 → v7 và `actions/setup-python` v5 → v7 (dùng Node 24), loại bỏ cảnh báo Node.js 20 của runner. Cấu hình job giữ nguyên, `cache: pip` vẫn hoạt động.

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
