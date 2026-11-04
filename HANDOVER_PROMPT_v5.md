# BÀN GIAO HIỆN TẠI — DHB REUP YOUTUBE STUDIO

**Cập nhật:** 04/11/2026 (Asia/Bangkok)<br>
**Repo:** C:/Users/admin/Projects/Tool Workspaces/DHB Reup YouTube Studio<br>
**Baseline đã nghiệm thu:** 5d0525febd2080378206d6c85b066f464aa1d0a2 — fix: reconcile monitor snapshots and preserve complete URLs<br>
**Build:** 2026.11.04-p7-fix2

> Đây là snapshot bàn giao theo bằng chứng ngày 04/11/2026. Khi tiếp nhận, hãy kiểm tra Git và code trước khi sửa. Nếu trạng thái thực tế khác tài liệu, ghi nhận khác biệt; không dùng số liệu DB/CI/live-service cũ như dữ liệu hiện tại.

## 1. Tóm tắt tình trạng

- Tool Windows chạy dashboard FastAPI cục bộ tại http://127.0.0.1:8686, quản lý kênh/profile, nạp nguồn video, xử lý metadata, render FFmpeg và điều phối upload YouTube Studio.
- Repo ở nhánh main, HEAD 5d0525f; các file được theo dõi sạch. Hai prompt cũ v3/v4 vốn chưa theo dõi; file v5 này vừa được thêm nên hiện có ba prompt bàn giao chưa theo dõi. Nhánh local ahead origin/main 7 commit; các commit này chưa được push theo trạng thái đã kiểm tra. Không push/commit nếu chủ tool chưa yêu cầu.
- Đợt sửa mới nhất R1–R3 đã nghiệm thu: đồng bộ snapshot monitor sau khi SQLite hết bận; giữ nguyên URL nguồn đầy đủ khi AI rút ngắn mô tả. Suite nguyên trạng: **212 passed, 16 subtests passed, 1 cảnh báo deprecation**; bộ kiểm tra độc lập: **18 passed, 1 cảnh báo**. compileall, node --check static/app.js, git diff --check đạt.
- Nghiệm thu này **không phải nghiệm thu phát hành toàn sản phẩm**. Chưa xác nhận cài Windows sạch, luồng ACCEPT-01 tích hợp đầy đủ, hoặc upload mới trên Chrome/Antidetect/YouTube Studio thật. Không có upload thật hay restart service trong đợt nghiệm thu 04/11.
- Cổng 8686, 8000, 8766 không có listener tại lần kiểm tra 04/11. Ứng dụng và dịch vụ Antidetect không được khởi chạy trong lần kiểm tra này.

## 2. Nơi làm việc và sự thật Git

Repo chính đã được xác định qua shortcut Open tool.lnk:

C:/Users/admin/Projects/Tool Workspaces/DHB Reup YouTube Studio

Tại lần kiểm tra 04/11:

- branch: main
- HEAD: 5d0525febd2080378206d6c85b066f464aa1d0a2
- remote: origin/main, local ahead 7 commits
- tracked working tree: clean
- untracked: HANDOVER_PROMPT_v3.md, HANDOVER_PROMPT_v4.md, HANDOVER_PROMPT_v5.md

7 commit local chưa đẩy, từ cũ đến mới:

1. bb1935f — fix: harden upload, rendering, and recovery flows
2. d2a3f59 — feat: complete hardening through phase 5
3. d850cd7 — feat: complete phase 6 and harden batch lifecycle
4. 4612c08 — test: fix local-origin integration fixtures
5. 4ad0faf — fix: redact JSON values without corrupting payloads
6. f3db822 — fix: close post-acceptance lifecycle and metadata issues
7. 5d0525f — fix: reconcile monitor snapshots and preserve complete URLs

Repo có workflow .github/workflows/tests.yml, nhưng không có bằng chứng CI đã chạy trên HEAD 5d0525f; chỉ suite local/independent nêu trên được xác nhận. Không mô tả CI là xanh cho HEAD nếu chưa kiểm tra run tương ứng.

### Quy tắc Git và dữ liệu

1. Đọc git status --short --branch, git log -5 --oneline, diff và nội dung file trước khi làm.
2. Không reset, checkout làm mất thay đổi, dọn file, commit hoặc push nếu chưa được yêu cầu.
3. Không đưa DB, backup, log, cookie/profile, OAuth, proxy credential, API key hoặc bearer token vào Git, prompt, log hay API response.
4. data/, logs/, temp/, output/ là dữ liệu chạy máy; chỉ đọc khi cần và không xóa/di chuyển nếu chưa được yêu cầu.
5. Chỉ đọc repo DHB gốc ở C:/DHB Reup Facebook để dùng tài nguyên được cấu hình; không ghi sửa vào đó.
6. Chỉ dùng nội dung người vận hành có quyền sử dụng. Khi YouTube yêu cầu xác minh hoặc quy trình không xác định được trạng thái an toàn, dừng và báo rõ; không cố vượt qua biện pháp bảo vệ của dịch vụ.

## 3. Kiến trúc và luồng nghiệp vụ

Stack: Windows 10/11 x64, Python 3.10, FastAPI/Uvicorn, SQLite WAL, giao diện HTML/CSS/JavaScript thuần, Playwright, FFmpeg/ffprobe và yt-dlp. Dashboard được bind cục bộ; API có lớp token/Host/Origin trong api_security.py.

Luồng chính:

Nạp URL/tệp/CSV/Excel/thư mục<br>
→ Quét và tải nguồn → tạo/nhận dạng queue item → chống trùng/claim<br>
→ Chuẩn hóa metadata / AI (nếu bật) → render FFmpeg (nếu bật)<br>
→ Giải quyết kênh đích và điều kiện an toàn<br>
→ Playwright YouTube Studio hoặc YouTube Data API theo cấu hình kênh<br>
→ Ghi trạng thái, URL đã đăng, lịch sử, log và giải phóng claim

PipelineManager điều phối batch và monitor; worker render/download có thể dừng theo các hợp đồng tương ứng. Database là nguồn lưu trạng thái queue, channel/profile, monitors, settings và logs. Các đường upload được thiết kế fail-closed: thiếu Channel ID hoặc không xác nhận được bước bắt buộc thì không được suy đoán là upload thành công.

Antidetect là tích hợp ngoài: antidetect_client.py gọi API và tạo CDP lease. Antidetect sở hữu profile/browser/proxy; DHB chỉ dùng phiên được cấp lease, giữ nguyên browser/context dùng chung và đóng tab do chính lease tạo. Đối với profile Antidetect, dùng luồng Playwright; không chuyển sang API upload làm sai ngữ cảnh phiên.

## 4. Bản đồ codebase tại HEAD 5d0525f

### Backend và nghiệp vụ

| File | Vai trò |
|---|---|
| app.py | FastAPI app, dashboard/static, API quản lý hàng đợi, monitor, profile/kênh, import/export, diagnostics và điều phối thao tác. |
| config.py | Đường dẫn dữ liệu/tài nguyên và cấu hình executable; hỗ trợ override bằng biến môi trường. |
| database.py | SQLite connection, schema/migration, transaction, truy vấn queue/channel, log và lưu trữ. |
| api_security.py | Bảo vệ API cục bộ bằng token và kiểm tra Host/Origin. |
| app_instance_lock.py | Ngăn khởi chạy nhiều instance ứng dụng. |
| pipeline_manager.py | Batch workers, monitor, lifecycle, dedupe/claim, dừng và phục hồi trạng thái. |
| download_worker.py | Worker tải nguồn. |
| scanner_downloader.py | Phân tích URL/thư mục, probe media và tải video. |
| queue_import.py | Nạp queue từ các định dạng/import flow được hỗ trợ. |
| content_identity.py | Khóa nhận dạng nội dung và quy tắc chống trùng nguồn. |
| group_distribution.py | Chọn/phân phối video tới nhóm kênh. |
| phase4_rules.py | Quy tắc nghiệp vụ của luồng phase 4. |
| render_engine.py | Render/transcode bằng FFmpeg, tiến trình con, báo tiến độ và dọn output lỗi. |
| storage_manager.py | Quản lý file, dung lượng và thao tác lưu trữ. |
| youtube_uploader.py | Thao tác YouTube Studio qua Playwright, các probe và đường YouTube Data API được hỗ trợ. |
| antidetect_client.py | Kết nối Antidetect API, health-check, CDP lease và vòng đời tab. |
| ai_engine.py | Provider metadata, fallback/translate và bảo toàn URL nguồn khi giới hạn độ dài. |
| check_status.py | Chẩn đoán nhanh trạng thái queue/app từ command line. |

### Giao diện, kiểm thử, tài liệu và vận hành

- static/index.html, static/app.js, static/styles.css: dashboard SPA. JS được nạp với cache tag app.js?v=20261103-p6 tại baseline này.
- tests/: 19 file test gồm lifecycle, monitor, claims/dedupe, fail-closed uploader, bảo mật, render, storage, AI fallback, Antidetect, Studio sync và smoke HTTP.
- .github/workflows/tests.yml: workflow kiểm tra CI trên Windows.
- README.md: cài đặt, khởi động, môi trường và tổng quan kiến trúc.
- HUONG_DAN_SU_DUNG.txt: hướng dẫn người vận hành.
- CHANGELOG.md: nhật ký theo đợt và số liệu test lịch sử.
- CAI_DAT_MOI_TRUONG.bat, START_DHB_YOUTUBE.bat: cài dependency/Playwright và khởi động dashboard.
- requirements.txt: dependency runtime đã pin.
- dhb_youtube.ico: icon ứng dụng.
- HANDOVER_PROMPT.md, HANDOVER_PROMPT_v2.md: prompt cũ; xem mục 9.

### Các bảng SQLite chính

channel_groups, chrome_profiles, youtube_channels, upload_option_groups, projects, video_queue, monitors, published_history, system_logs, app_settings, render_presets và sqlite_sequence.

## 5. Hệ thống ngoài và cài chạy

- Tool cần Python 3.10 x64 và Windows; các dependency Python nằm trong requirements.txt.
- Chạy CAI_DAT_MOI_TRUONG.bat để cài thư viện và Chromium của Playwright, hoặc làm theo hướng dẫn thủ công trong README.md.
- Chạy START_DHB_YOUTUBE.bat; ứng dụng phục vụ tại http://127.0.0.1:8686. Có thể chạy python app.py khi cần theo dõi log foreground. check_status.py hỗ trợ kiểm tra nhanh.
- Đường dẫn executable/media mặc định và biến override được mô tả trong README.md/config.py; không hard-code giả định về máy khác.
- Antidetect tùy chọn phải chạy riêng trước khi dùng profile Antidetect. Service API thường dùng cổng 8000; dashboard Antidetect có thể dùng 8766. Trạng thái kiểm tra 04/11: cả hai cổng không listen.
- Build nhận diện trong API: 2026.11.04-p7-fix2. Không khẳng định phiên bản executable hoặc dịch vụ ngoài đang cài trên máy khác nếu chưa chạy diagnostics tại máy đó.

## 6. Kết quả mới nhất và giới hạn nghiệm thu

Commit 5d0525f gồm các sửa R1–R3:

- **R1 — monitor/SQLite:** snapshot lifecycle được đọc dưới cùng khóa quản lý; sau khi DB hết bận, GET phản ánh trạng thái đã dừng nhất quán (is_running, status, lifecycle state, khả năng chạy lại/xóa). Probe giữ khóa SQLite thật, dùng heartbeat và HTTP.
- **R2 — URL chung tiền tố:** việc nhận diện URL trong mô tả không cắt nhầm phần đầu của URL khác có cùng prefix/path/query/fragment.
- **R3 — biên 5.000 ký tự:** giữ URL nguồn đầy đủ đến giới hạn; rút gọn phần văn bản khi đủ chỗ và báo lỗi rõ khi URL/block bắt buộc tự nó vượt giới hạn.

Bằng chứng chốt:

- python -X utf8 -m pytest -q tests --tb=short — 212 passed, 16 subtests, 1 warning
- Bộ probe audit-only `acceptance_checks.py` trong thư mục bằng chứng `_acceptance_5d0525f`: 18 passed, 1 warning (không phải file thuộc repo sản phẩm).
- python -m compileall -q . — PASS
- node --check static/app.js — PASS
- git diff --check f3db822 5d0525f — PASS

Cảnh báo là deprecation anyio.abc.BlockingPortal từ Starlette/AnyIO TestClient; không phải test failure.

### Chưa được xác nhận

- ACCEPT-01 end-to-end qua pipeline thật với hàng loạt queue items và vòng phục hồi process hoàn chỉnh.
- Toàn bộ ma trận monitor start/stop/delete, worker overlap, claim release và process restart rộng hơn R1.
- Fresh install trên Windows sạch; migration/backup/restore trên môi trường phát hành được chỉ định.
- Thao tác Chrome local/Antidetect và upload thật có giám sát trên YouTube Studio. Không chạy upload chỉ để kiểm thử code.
- Hiệu năng render branding với video dài (>5 phút) và 4K.
- CI trên HEAD 5d0525f; nhánh hiện đang ahead remote 7 commit.

Không biến các mục chưa xác nhận này thành kết luận “đã nghiệm thu sản phẩm”. Mỗi cổng cần bằng chứng riêng và chỉ thử với tài khoản/video theo chỉ đạo rõ của chủ tool.

## 7. Snapshot DB và dịch vụ trên máy tại 04/11

DB local: data/dhb_youtube.sqlite, **mtime 02/11/2026 11:14:57**. Thống kê chỉ đọc; đây là trạng thái dữ liệu tại lần DB ghi cuối, không phải xác nhận dữ liệu live mới nhất:

| Nhóm | Số liệu |
|---|---:|
| Profile | 16; có 9 profile gắn antidetect_profile_id |
| Kênh | 16; 7 kênh đang thiếu channel_id |
| Monitor | 0 |
| Queue | 5: 1 Completed, 4 Error |
| published_history | 0 |
| system_logs | 412 |
| Nhóm kênh / dự án / tuỳ chọn đăng / settings | 3 / 2 / 3 / 23 |

DB chưa được sửa trong lần audit. Không in tên kênh, profile, URLs, log text hay bí mật vào tài liệu này. Trước thao tác vận hành, mở DB readonly hoặc dashboard để cập nhật thống kê; không chạy migration chỉ để làm mới prompt.

## 8. Fix gần nhất trong lịch sử và các nguyên tắc kỹ thuật

- bb1935f: gia cố upload, render và recovery.
- d2a3f59, d850cd7: hardening tiếp theo, phase 5/6 và lifecycle batch.
- 4612c08: sửa fixture origin cho test tích hợp.
- 4ad0faf: che dữ liệu JSON nhạy cảm mà không làm hỏng payload.
- f3db822: đóng các vấn đề lifecycle/metadata hậu nghiệm thu.
- 5d0525f: sửa snapshot monitor và bảo toàn URL nguồn.

Quy tắc duy trì:

1. Giữ fail-closed khi thiếu Channel ID, không đọc được kết quả bắt buộc, không chắc upload đã hoàn tất hoặc khoảng giãn cách chưa đủ.
2. Không đánh dấu Completed nếu không có URL/video result đủ căn cứ theo code hiện hành.
3. Giữ biệt lập test khỏi DB, profile và account thật; dùng fixture/DB tạm.
4. Chỉ thay đổi contract Antidetect sau khi kiểm tra cả hai phía và có test lease/quyền sở hữu browser.
5. Sau sửa code, chạy suite phù hợp/toàn bộ, compile Python và node --check static/app.js nếu đụng frontend; cập nhật CHANGELOG.md khi hành vi thay đổi.
6. Không dùng thử nghiệm upload thật để thay thế unit/integration tests.

## 9. Rà soát các prompt bàn giao cũ

- HANDOVER_PROMPT.md là tài liệu marketing cũ: ghi đường dẫn D:/DHB_Reup_YouTube_Studio và scratch path không đúng repo hiện tại, nêu các tính năng như thể đã xác minh live, và dùng thuật ngữ dễ gây hiểu nhầm về né kiểm duyệt/bản quyền. Không dùng làm nguồn sự thật.
- HANDOVER_PROMPT_v2.md mang snapshot 26/10, UI v18, 86 test và tình trạng Antidetect chưa commit. Các con số và việc cần làm ở đó đã lỗi thời.
- HANDOVER_PROMPT_v3.md / HANDOVER_PROMPT_v4.md cập nhật hơn nhưng v4 vẫn chốt snapshot 01/11: HEAD 34d1c87, UI v20, 110 test, 6 file code chưa commit. Trạng thái đó mâu thuẫn với HEAD/test/working tree đã kiểm tra ngày 04/11.
- Tạo file này thành v5 để bảo toàn lịch sử; không xóa/ghi đè v3/v4. Khi bàn giao tiếp theo, cập nhật v5 sau khi xác minh Git, DB và dịch vụ một cách tách biệt.

## 10. Bằng chứng nghiệm thu

Nguồn được nghiệm thu là archive đúng commit 5d0525f, SHA-256:

89c9cd1dadf66c1ff2923647c7ab4cb27c89c9e06434a9d14bf9b1ec2066424c

Biên bản chi tiết: NGHIEM_THU_5d0525f_2026-11-04.md. Gói kiểm tra độc lập, log suite, snapshot monitor và diagnostics nằm trong thư mục bằng chứng _acceptance_5d0525f cạnh biên bản nghiệm thu. Archive/bằng chứng là snapshot audit; không thay thế checkout Git chính.

**Bắt đầu task tiếp theo:** xác nhận git status, HEAD và remote gap; đọc README.md, CHANGELOG.md và module liên quan; sau đó mới sửa. Không commit/push, không restart service và không upload thật nếu chưa được chủ tool yêu cầu.
