# PROMPT BÀN GIAO TOÀN DIỆN — 2 DỰ ÁN: ANTIDETECT BROWSER MANAGER + DHB REUP YOUTUBE STUDIO

*(v4 — cập nhật 2026-11-01 buổi tối, +0700. DHB UI **v20** — Antidetect **v1.1.0** + CI fix `313faab`.)*

**Người nhận:** kỹ sư/AI tiếp nhận bảo trì cả hai project và mối nối giữa chúng.<br>
**Cách dùng:** đọc hết mục 2, 6, 7, 8 trước khi đụng code — đó là các rule đã trả giá bằng dữ liệu thật. Sau đó kiểm tra trạng thái THẬT bằng `git log`, `git status`, DB và code hiện tại; mọi con số dưới đây là snapshot **2026-11-01**, không thay thế việc đọc code. Không hỏi lại rule đã chốt.

---

## 0. NHIỆM VỤ CỦA BẠN

Bảo trì, sửa lỗi và nâng cấp **hai** tool Windows của chủ tool, cộng với lớp tích hợp giữa chúng:

- **A. DHB Reup YouTube Studio** — dashboard FastAPI tự động reup video lên YouTube (quét nguồn → tải → render FFmpeg → đăng bằng Playwright).
- **B. Antidetect Browser Manager** — server quản lý profile Chrome cô lập (fingerprint + proxy + stealth), dashboard riêng và API HTTP.
- **Mối nối A→B** — `antidetect_client.py` của A mượn trình duyệt đang chạy của B qua CDP lease để đăng YouTube bằng profile Antidetect.

Khi nhận yêu cầu mới: đọc file thật trong project tương ứng trước khi sửa, kiểm tra cú pháp (`python -m py_compile`, `node --check`), chạy test của project đó, và chỉ báo cáo "đã xác minh" khi thực sự đã chạy.

### 0.1. Trạng thái git ngay lúc bàn giao — ĐỌC TRƯỚC KHI COMMIT

**A — `D:\DHB_Reup_YouTube_Studio` (writable):**

- Nhánh `main`, HEAD `34d1c87`, remote `origin/main` cũng ở `34d1c87`.
- Working tree **có 6 file sửa CHƯA COMMIT** (đợt P1 trong phiên bàn giao này):
  `.github/workflows/tests.yml`, `CHANGELOG.md`, `pipeline_manager.py`, `static/app.js`, `static/index.html`, `tests/test_p2_quality.py` — tổng `+246 / -2`.
- Untracked: `HANDOVER_PROMPT_v3.md` (bản bàn giao trước) và `HANDOVER_PROMPT_v4.md` (tài liệu này).
- CI gần nhất **vẫn là run cũ** `41312882433` tại `34d1c87` (SUCCESS). Workflow CI mới (11 module + `node --check`) **chưa được push** nên **chưa có run nào xác minh nó**. Không được báo "CI xanh" cho các thay đổi A-7/A-8/A-9.
- Khi được phép commit: tách commit theo workstream — CI (`tests.yml`), code (`pipeline_manager.py`, `static/app.js`, `static/index.html`, `tests/test_p2_quality.py`), docs (`CHANGELOG.md`; `HANDOVER_PROMPT_v3/v4` nếu chủ tool muốn).

**B — `C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python` (read-only trừ khi được phép):**

- Nhánh `master`, HEAD `313faab`, remote `origin/master` cũng ở `313faab`.
- Fix CI đã **commit + push**: `313faab fix(ci): install httpx2 for Starlette TestClient`.
- Working tree còn **1 file sửa CHƯA COMMIT**: `HANDOFF.md` (+17 / -10) — cập nhật baseline hash `profiles.json` và lý do drift. Không tự xóa/không commit nếu chưa được yêu cầu.
- CI mới **SUCCESS**: run `41317044530` tại `313faab`, 301 passed + 1 skipped, 0 annotation.
- Môi trường Python local đã cài `httpx2==2.14.0` (kéo theo `httpcore2==2.14.0`, `truststore==0.10.5`). Đây là thay đổi môi trường, không phải file repo.

### 0.2. Tóm tắt thay đổi so với v3

- A: UI `v19 → v20`; A-7 (CI đủ module + `node --check`), A-8 (chặn UI khi thiếu Channel ID), A-9 (ghi `published_history` cho mọi nguồn Completed) đã làm **local, chưa push**. Test `107 → 110`.
- B: CI đỏ do thiếu `httpx2` đã sửa; Actions `v4/v5 → v7`; run `41317044530` xanh. `HANDOFF.md` baseline hash đã cập nhật nhưng **chưa commit**.
- Cả hai dashboard/server đang **TẮT**; không tự bật/không tự dừng khi chưa xin phép.

---

## 1. HAI DỰ ÁN & NƠI CODE

| | A. DHB Reup YouTube Studio | B. Antidetect Browser Manager |
|---|---|---|
| Đường dẫn local | `D:\DHB_Reup_YouTube_Studio` | `C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python` |
| Remote GitHub | `github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio` (**private**) | `github.com/meolucky051101001-cyber/antidetect_python` (**private**) |
| Nhánh / HEAD | `main` / `34d1c87` — 7 commit; **6 file sửa chưa commit** | `master` / `313faab` — 25 commit; **`HANDOFF.md` sửa chưa commit** |
| Version / UI | UI `app.js?v=20` | `antidetect/__init__.py` → `__version__ = "1.1.0"` |
| Test | `110` unittest — **local OK, 110/110** (107 cũ + 3 mới) | `301 passed, 1 skipped` — **local + CI đều pass** |
| CI | `.github/workflows/tests.yml` — run `41312882433` **SUCCESS tại 34d1c87**, nhưng là workflow cũ; workflow mới **chưa push** | `.github/workflows/ci.yml` — run `41317044530` **SUCCESS tại 313faab**, 0 annotation |
| Cổng chạy | `http://127.0.0.1:8686` — **đang tắt** | `http://127.0.0.1:8766` (dashboard tay) hoặc `:8000` (server API) — **đang tắt** |
| Docs | `README.md`, `HUONG_DAN_SU_DUNG.txt`, `CHANGELOG.md`, `HANDOVER_PROMPT_v3.md`, `HANDOVER_PROMPT_v4.md` | `HANDOFF.md`, `docs/OPERATIONS.md`, `docs/LIMITATIONS.md`, `docs/VERIFICATION_2026-10-24.md`, `SECURITY.md`, `FIXES.md` |

Stack chung: Python **3.10.11** (bản hệ thống `C:\Users\admin\AppData\Local\Programs\Python\Python310`, **không dùng venv**), FastAPI + Uvicorn, SQLite WAL, Playwright điều khiển Chrome thật, Windows 10/11 x64, Git Bash.

---

## 2. PHẠM VI GHI FILE — BẮT BUỘC, KHÔNG MỞ RỘNG

1. **A (`D:\DHB_Reup_YouTube_Studio`) writable mặc định.** Chỉ sửa/tạo file trong thư mục này.
2. **B (Antidetect) là READ-ONLY reference.** Muốn sửa code/tài liệu B phải xin phép chủ tool **từng lần một**, nêu rõ file + lý do. Đọc để đối chiếu field name/behavior thì thoải mái.
   - Ngoại lệ đã được cấp phép trong phiên này: `requirements-dev.txt`, `constraints.txt`, `.github/workflows/ci.yml` (fix CI) và `HANDOFF.md` (baseline hash). Đã làm xong; **không sửa thêm** nếu chưa được phép lần mới.
3. **`C:\DHB Reup Facebook\` là READ-ONLY tuyệt đối** — A kế thừa FFmpeg/ffprobe/youtube-dl và thư mục profile Chrome từ đó. Không ghi, không xóa, không "dọn".
4. `C:\tool v1` là project khác, không liên quan.
5. Trong B, `data/`, `browsers/`, `exports/`, `backups/`, `*.dpapi` **không bao giờ** là đối tượng sửa — kể cả khi được phép sửa code B.
6. Trong A, `data/`, `*.sqlite`, `data.db`, `logs/`, `temp/`, `output/`, `_backup_p*_20261026/` là dữ liệu/artifact — **không tự xóa**. A-12 liệt kê các artifact cần chủ tool duyệt trước khi dọn.
7. `HANDOVER_PROMPT_v3.md` và `HANDOVER_PROMPT_v4.md` là tài liệu bàn giao; không xóa, không ghi đè lẫn nhau.

---

## 3. DỰ ÁN A — DHB REUP YOUTUBE STUDIO: HIỆN TRẠNG

### 3.1. Kiến trúc file

`app.py` (REST API, ~37 endpoints) · `config.py` (đường dẫn DHB gốc + kho tài nguyên) · `database.py` (schema + migration + backup + housekeeping) · `pipeline_manager.py` (đa luồng Semaphore + stop_event + Auto Monitor 24/7) · `render_engine.py` (7 chế độ render FFmpeg + PID trap + branding overlays) · `youtube_uploader.py` (Playwright Studio 4 bước + YouTube Data API v3 + probe đăng nhập + quét kênh) · `antidetect_client.py` (API client + CDP lease) · `api_security.py` (bearer token cục bộ, chặn Host/Origin) · `scanner_downloader.py` (yt-dlp + quét thư mục đệ quy) · `ai_engine.py` (Gemini/OpenAI/DeepSeek/Translate) · `static/` (SPA vanilla JS, polling 2.5s) · `tests/` · `data/dhb_youtube.sqlite`.

### 3.2. Thay đổi trong phiên bàn giao này (CHƯA COMMIT)

**A-7 — CI phủ đủ module + frontend syntax**

- `.github/workflows/tests.yml`: `py_compile` đủ **11 module** (thêm `api_security.py`, `antidetect_client.py`, `check_status.py`) và thêm bước `node --check static/app.js`.
- Chưa push; CI mới chưa chạy. Run xanh `41312882433` vẫn là workflow cũ (8 module).

**A-8 — chặn sớm ở UI khi kênh thiếu Channel ID**

- `static/app.js` có `findChannelIdBlockers(ids, forceReupload)`; gọi trong `startPipeline()` trước khi xác nhận chạy lô có upload.
- Thuật toán bám backend `_resolve_target_channel`: kênh cụ thể → profile đơn → xoay vòng nhóm; sắp kênh theo `id ASC` vì `/api/channels` trả `id DESC`; bỏ qua video đã `Completed`/có `published_url`/đang `Uploading` khi không ép đăng lại.
- Kênh thiếu ID bị chặn kèm danh sách video/kênh; backend vẫn fail-closed là nguồn chân lý cuối cùng.
- `static/index.html`: `app.js?v=19 → v20`. Phải Ctrl+F5.
- **Chưa test bằng browser**; mới kiểm tra cú pháp + harness Node cho logic xoay vòng. Nếu `gChannels` rỗng/cũ, UI có thể không chặn — backend vẫn chặn.

**A-9 — `published_history` cho mọi nguồn Completed**

- `pipeline_manager.py` thêm `PipelineManager._record_published_history_for_item(queue_id, channel)` (static): đọc `video_queue`, chỉ ghi khi `status == "Completed"` và `published_url` có thật; `INSERT OR IGNORE` khóa `(source_post_id, channel_id)` với `source_post_id = post_id`; `post_id` rỗng dùng `queue:<id>`; lưu cả `published_url`.
- Gọi ngay sau khi `_update_row(status="Completed", ...)` trong nhánh upload thành công — áp dụng cho link, kéo-thả, local path, không chỉ Auto Monitor.
- `tests/test_p2_quality.py` thêm 3 test: chạy `process_single_item` với upload mock để chứng minh history được ghi; bỏ qua item chưa Completed/thiếu URL; idempotent + khóa theo hàng đợi.
- **DB hiện tại vẫn `published_history = 0`**; row `#197` Completed từ 2026-10-25 **chưa được backfill**. Code chỉ ghi cho các lượt Completed **mới**. Không tự backfill khi chưa có phê duyệt (phải chọn `channel_id` và xác minh `post_id`).

**Đã xác minh local (2026-11-01):**

- `python -m unittest discover -s tests`: **110/110 OK**, exit 0.
- `python -m py_compile` 11 module: OK.
- `node --check static/app.js`: OK.
- Harness Node cho `findChannelIdBlockers`: OK (lô thường chặn đúng 3 video; khi ép đăng lại chặn thêm video Completed).
- **Không** chạy upload thật, **không** mở dashboard, **không** chạm DB/profile thật.

### 3.3. DB thật `data/dhb_youtube.sqlite` — đọc readonly ngày 2026-11-01

Kích thước `167,936` bytes. 11 bảng nghiệp vụ + `sqlite_sequence`:

| Bảng | Số dòng | Ghi chú |
|---|---:|---|
| `channel_groups` | 3 | `Mặc định`, `Kênh Shorts`, `Kênh Dài (16:9)` |
| `chrome_profiles` | 7 | **Không có cột `id`**; khóa là `name` (TEXT PK) |
| `youtube_channels` | 7 | Tất cả `channel_id` **rỗng**; `upload_mode='playwright'` |
| `upload_option_groups` | 2 | |
| `projects` | 2 | `Dự án YouTube #1`, `Kiểm tra video thật` |
| `video_queue` | 5 | 1 `Completed`, 4 `Error` |
| `monitors` | 0 | |
| `published_history` | **0** | Dù đã có video Completed #197 |
| `system_logs` | 344 | Log cuối `2026-10-29 12:58:14` |
| `app_settings` | 23 | |
| `render_presets` | 0 | |

**Kênh YouTube (7/7 thiếu Channel ID):**

| ID | Tên | Channel ID | Nhóm | Profile | `status` |
|---:|---|---|---|---|---|
| 1 | YT - Nguyễn Thành Công (Công chính) | EMPTY | Mặc định | Công chính | Ready |
| 2 | YT - Bóng Ma Đề Cương | EMPTY | Mặc định | Bóng Ma Đề Cương | Ready |
| 3 | YT - Nguyễn Thành Công (Công phụ) | EMPTY | Mặc định | Công phụ | **Đã đăng nhập** |
| 4 | YT - Thi Đại Học | EMPTY | Mặc định | Thi Đại Học | Ready |
| 5 | YT - Cong Nguyen (congnguyen) | EMPTY | Mặc định | congnguyen | Ready |
| 6 | YT - Lê Thị Quyên | EMPTY | Mặc định | Lê Thị Quyên | Ready |
| 7 | YT - Lê Thị Quyên (lethiquyen2) | EMPTY | Mặc định | lethiquyen2 | Ready |

**Chrome Profile (bảng `chrome_profiles`):**

- 3 path **tồn tại**: `Bóng Ma Đề Cương`, `Công phụ`, `lethiquyen2`.
- 4 path **MISSING**: `Công chính` (trỏ nhầm ổ `D:` thay vì `C:`), `Thi Đại Học`, `congnguyen`, `Lê Thị Quyên`.
- `antidetect_profile_id`: **0/7 binding** — chưa nối profile nào sang B.
- Chỉ `Công phụ` có `login_status='Đã đăng nhập'`, `last_login_check='2026-10-25 14:04:10'` (đã cũ). 6 profile còn lại để trống.
- Lưu ý: `chrome_profiles` dùng `name` làm khóa, **không có `id`**. Đừng viết SQL kiểu `WHERE id = ?` cho bảng này.

**Video queue:**

- `#197`: `Completed`, `published_url=SET`, `post_id=SET`, `source_type='File'`, `platform='LocalFile'`, tạo `2026-10-25 06:59:36`.
- `#193–#196`: `Error`, cùng nguồn LocalFile, tạo `2026-10-25 06:57:55`.
- Không có monitor nào; log cuối 2026-10-29.

### 3.4. P0 — phải có chủ tool thao tác, không làm hộ được

- **A-1. Channel ID trống 7/7** → mọi lượt upload bị fail-closed **đúng thiết kế**. Chủ tool bấm "🔄 Quét Kênh" từng profile hoặc nhập tay từ URL Studio. Phần đọc `el.data.navigationEndpoint` **chưa từng được kiểm chứng trên profile thật**; quét vẫn trống thì nhập tay.
- **A-2. 4/7 thư mục Chrome Profile không tồn tại** (như mục 3.3). `Công chính` trỏ sai ổ `D:` — nguồn lỗi `[WinError 2]`. Cần chủ tool xác nhận kênh nào còn dùng trước khi sửa path trong DB; backup DB + sửa từng dòng, **không xóa thư mục**.
- **A-3. Phiên đăng nhập gần như chưa biết:** chỉ `Công phụ` = "Đã đăng nhập", check cuối **2026-10-25 14:04** (đã 7 ngày, coi như cũ). 6 profile còn lại trống. Cần "🌐 Mở Đăng Nhập" rồi "🔍 Kiểm Tra" từng profile.
- **A-4. MỘT lần đăng thật có giám sát** (chỉ sau A-1→A-3): xác nhận chọn đúng kênh con, đọc URL từ share dialog, Checks ra "No issues found", đặc biệt **giới hạn 18+** trên Studio thật (radio `VIDEO_AGE_RESTRICTION_RESTRICTED` — **selector chưa từng chạy thật**). Không xác nhận được thì hủy nháp + báo lỗi, không đăng sai tuổi.
- **A-5. Upload thật qua Antidetect: 0 lần.** `antidetect_profile_id` = **0 binding**, server B đang **TẮT** (pid file stale). Toàn bộ đường Antidetect mới chỉ được chứng minh bằng HTTP server giả lập + fake lease. Việc: bật B → `POST /api/antidetect/sync` → `GET /api/antidetect/status` → probe đăng nhập → **một** upload có giám sát.
- **A-6. Render branding (overlay/intro/outro) trên video DÀI >5 phút và 4K:** mới test video 3s, chưa đo hiệu năng/nhiệt độ RAM của concat + chroma-key.

### 3.5. Hợp đồng tích hợp Antidetect (KHÔNG được đảo ngược)

- Antidetect **sở hữu** Chrome/Profile/Proxy/Fingerprint. A chỉ: đồng bộ metadata profile, bật profile qua API của B, mượn trình duyệt đang chạy bằng CDP lease.
- **Không** mở Chrome local bằng `user_data_dir` của Antidetect. **Không** ép mobile/UA/viewport/proxy/stealth script lên trình duyệt Antidetect. **Không** dùng YouTube Data API cho kênh Antidetect (sai proxy/fingerprint) — upload qua Antidetect bắt buộc Playwright.
- Chỉ đóng **tab do lease tạo**; không đóng browser/context dùng chung.
- Profile Antidetect trong DB A dùng pseudo-path `antidetect://<id>` + cột `antidetect_profile_id`. Sync **không** ghi đè profile local trùng tên, **không** kéo mật khẩu proxy về DB.
- Endpoint liên quan: `GET /api/antidetect/status`, `POST /api/antidetect/sync`.

### 3.6. Artifact A-12 còn tồn tại (chưa dọn)

Đã kiểm tra ngày 2026-11-01, tất cả vẫn còn:

- 4 thư mục backup ở gốc repo: `_backup_p0_20261026`, `_backup_p0_final_20261026`, `_backup_p1_20261026`, `_backup_p2_20261026`.
- `data.db` (0 byte, file rác — **không phải** DB thật `data/dhb_youtube.sqlite`).
- `temp/test_zcode3`, `temp/test_zcode4`, `temp/wmtext_1792926669033.txt`, `temp/wmtext_1792926669034.txt`.
- Chỉ xóa sau khi chủ tool duyệt; không đụng `temp/uploads/`.

---

## 4. DỰ ÁN B — ANTIDETECT BROWSER MANAGER: HIỆN TRẠNG

### 4.1. Package

`antidetect/`: applog, browser_env, browser_runtime, bulk_jobs, cookies, facebook, fingerprint, human, launch_gate, launcher, metrics, models, process_identity, profile_audit, proxy, runtime_auth, scheduler, secrets, server_control, stealth, storage, updater · `web/` (dashboard + API) · `cli.py` / `run.py` · `tools/`: detector_audit, e2e_smoke, install_browser, leak_audit, probe_fingerprint, release, update, verify_live, audit_common · `tests/` (pytest) · stealth payload là asset thật: `antidetect/assets/stealth_page.js`, `stealth_worker.js`.

`pytest.ini`: `norecursedirs = backups exports data .git .system_generated` — test không được động vào data thật.

### 4.2. Dữ liệu thật `data/` — đọc readonly ngày 2026-11-01

- `profiles.json`: **7 profile**, 10,958 bytes, mtime `2026-10-29 13:40:12`, SHA256
  `23E81C476B9D53B98F29942BA3C77596A50F647DE929DC3D72449D43F2712F35`.
- Tên 7 profile: `Profile 7 - Meo Meo`, `Profile 1 - Nguyễn Thành Công(phụ)`, `Profile 2 - Bóng Ma Đề Cương`, `Profile 3 - Thi Đại Học`, `Profile 4 - Lê Thị Quyên`, `Profile 5 - Lê Thị Quyên 2`, `Profile 6 - Cong Nguyen`.
- Chỉ **1 profile có proxy host**: `Profile 1 - Nguyễn Thành Công(phụ)`; 6 profile còn lại `proxy.host` rỗng. Không chép credential proxy vào tài liệu.
- **10 backup profiles**, 5 backup secrets; `data/profiles/` có 7 thư mục; `secrets.dpapi`, `runtime.dpapi` còn nguyên.
- Backup mới nhất `data/backups/profiles_20261101_214043_bd0a3c.json` có **hash trùng hệt** `profiles.json`; các backup `profiles_20261101_213923_*`, `profiles_20261101_213944_*` cũng trùng nội dung.
- Ba backup mới được tạo trong phiên `2026-11-01 21:39–21:40`; `profiles.json` **không đổi hash/mtime**. Không còn process Python liên quan; `data/app.log` không có entry mới (mtime `2026-10-27`). **Nguồn tạo chưa xác định** (có thể dashboard/CLI của chủ tool hoặc một test chạy song song). Không xóa backup; nếu điều tra, so hash trước khi kết luận.
- `data/server.pid` còn đó nhưng **PID đã chết**, port ghi `8000`; **port 8000 và 8766 đều không listening** → server B đang TẮT. A phải xử lý được tình huống này (đã xử lý: health probe fail → báo "hãy mở Antidetect trước rồi thử lại").
- Hash cũ `8CFC56C4…ABCA5` trong `HANDOFF.md` đã **stale**; drift hợp lệ vì chủ tool dùng dashboard 2026-10-26 → 2026-10-29 và tool tạo backup trước mỗi lần lưu. `HANDOFF.md` mục 3 đã được cập nhật hash mới + lý do drift; **thay đổi này chưa commit**. Không dùng hash cũ để kiểm tra toàn vẹn.

### 4.3. Fix CI đã commit + push (`313faab`)

**Nguyên nhân:** `requirements-dev.txt` + `constraints.txt` không khai báo `httpx`/`httpx2`; starlette 1.6.0 đòi `httpx2` cho `TestClient`. Máy local pass vì có sẵn `httpx 0.28.1`; runner sạch fail với:

```text
RuntimeError: The starlette.testclient module requires the httpx2 package to be installed.
```

**Đã sửa (commit `313faab`, đã push `master`):**

- `requirements-dev.txt`: thêm `httpx2>=2.14.0` (kèm comment vì sao).
- `constraints.txt`: pin `httpx2==2.14.0`.
- `.github/workflows/ci.yml`: `actions/checkout@v4 → v7`, `actions/setup-python@v5 → v7`; `cache-dependency-path` giữ nguyên 3 file hiện có (không tách file requirements mới).

**Kết quả CI thật — run [41317044530](https://github.com/meolucky051101001-cyber/antidetect_python/actions/runs/41317044530) tại `313faab`:**

- **SUCCESS**, cả 7 bước chính `success`: Install dependencies → Syntax check → Ruff → Tests + coverage.
- **301 passed, 1 skipped**, 23.49s; coverage **71.10%** (gate 58%).
- **0 annotation** — không còn cảnh báo Node.js 20.
- Log cài đặt xác nhận runner sạch pull `httpx2-2.14.0` + `httpcore2-2.14.0`.
- Warning duy nhất còn lại: `Duplicate name: 'release.json'` trong `test_updater.py::test_duplicate_manifest_entry_rejected` — test **cố ý** tạo zip trùng manifest, không phải lỗi.

**Khác biệt local/CI cần biết:** CI sạch cài `starlette 1.7.0`; local đang `starlette 1.6.0`. Cả hai đều pass 301 test. Local còn warning `anyio.abc.BlockingPortal` do starlette 1.6.0; CI không còn. `constraints.txt` **không pin starlette** — nếu muốn local/CI khớp tuyệt đối, cân nhắc pin thêm (ngoài phạm vi fix này).

### 4.4. Test & warning hiện tại

- Local `python -m pytest -q`: **301 passed, 1 skipped**, 20.02s (skip = Playwright UI smoke, chỉ bật khi `ANTIDETECT_RUN_UI_SMOKE=1`).
- Local `pytest --cov=antidetect --cov=web --cov-fail-under=58`: **71.10%**, pass.
- `python -m ruff check .`: **All checks passed**.
- `python -m compileall -q antidetect web cli.py run.py tools`: sạch.
- Warning local: (1) `anyio.abc.BlockingPortal` alias (starlette 1.6.0); (2) `Duplicate name: 'release.json'` (cố ý). CI chỉ còn warning (2).
- `pip check`: không có dependency hỏng.

### 4.5. Những điều đã chứng minh ở B — ĐỪNG đi ngược lại

- CreepJS baseline desktop = **25% like-headless** (heuristic noContentIndex/noContactsManager/noDownlinkMax thiếu trên **mọi** Chrome sạch). Tool bằng baseline. **Không coi 0% là mục tiêu.**
- Pixelscan "masking detected" ở chế độ **Unique** là đúng thiết kế; Authentic → not detected.
- Battery/connection hooks **mặc định TẮT** (`_DEFAULT_OFF_GROUPS`) — mỗi hook là thêm bề mặt detect.
- JA3 xoay GREASE giữa các connection; TLS rewriting MITM đã **LOẠI** (phá cert validation).
- **Không** dùng Proxy cho `Function.prototype.toString` — dùng WeakMap + concise method.
- Stealth payload là f-string: mọi sửa JS phải qua `test_stealth_payload_is_valid_javascript` (`node --check`) và `test_stealth_assets.py`.
- Scheduler dùng **leased claims xuyên process + heartbeat gia hạn lease** (`renew_claim`); run history `data/schedule_runs.json` ring buffer 200, atomic. Đừng đơn giản hóa về check in-memory (từng bị duplicate execution).
- Bulk job xuyên instance qua **một slot** ở `data/bulk_jobs.json` (lease 90s, heartbeat 2s, stale takeover); file hỏng phải **fail-closed 503**, không tự xóa.
- Updater: chữ ký **Ed25519 bắt buộc**, không auto-download/auto-update, apply chỉ ghi `payload/`, không bao giờ ghi `data/`/`browsers/`/secret, backup trước khi ghi + auto-rollback; rollback kiểm hash toàn bộ backup; file `added` đã bị sửa thì **giữ lại**.
- UI Edit gửi `expected_revision`; server trả **409** khi profile bị sửa ở phiên khác → UI phải đóng modal + refresh, **không** tự ghi đè.
- `start_all`/`stop_all`: `profile_ids=None` = tất cả; `profile_ids=[]` = **không làm gì** (đừng "fix" thành mở hết).
- CSP `script-src 'self'` chặn inline `<script>` — JS unlock phải ở `/static/js/unlock.js`. `script-src-attr 'unsafe-inline'` còn vì `index.html` còn ~68 handler `onclick`/`onchange`.
- Leak audit proxy thật trên 1.1.0 (2026-10-26): **PASS** (WebRTC 0 candidate, DNS CLEAN, JA3/JA4 đo được). **Không** suy diễn PASS này cho SOCKS5/proxy khác/soak 24h.
- **Chưa verify:** deep sync Fanpage với page FB thật; soak 24h / >20 profile đồng thời / SOCKS5 thật / DataDome+Turnstile; Playwright UI smoke không chạy trong CI nhưng bắt buộc khi đổi UI; launcher `launch/stop/CDP` ~1.9k dòng coverage 29%; `web/app.py` ~1.4k dòng; bulk job single-slot cục bộ (không distributed lock đa máy); updater không auto-download, không Authenticode.

---

## 5. QUY TẮC AN TOÀN DỮ LIỆU (áp dụng cả hai project)

1. **Không chạy live test với `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật** của B. Chỉ dùng thư mục tạm (`%TEMP%\opencode\ad-*`). Test của A phải mock `get_conn`/`add_log`, DB `:memory:`/temp, DOM Studio giả lập.
2. **Không sửa** `data/profiles.json`, `data/profiles/*`, `secrets.dpapi`, `runtime.dpapi`, `api_token.dpapi`, `data/backups/*`.
3. Sau live test: dừng browser, dọn profile tạm, kiểm tra hash + 0 process còn sống.
4. **Không bao giờ** in/log/commit: OAuth JSON, access/refresh token, cookie Chrome, proxy user/pass, Gemini key, access key Antidetect, `server.pid` nguyên văn. Không đưa secret vào URL, API response, CSV export hay prompt bàn giao.
5. DB/profile/backup của A (`data/`, `*.sqlite`, `data.db`, `logs/`, `temp/`, `output/`) luôn nằm ngoài Git — đã có `.gitignore`; kiểm tra `git status` trước khi commit.
6. Đọc DB A production bằng **bản copy** + `file:...?mode=ro`; không mở WAL thật để ghi. Đọc B `profiles.json` chỉ để hash/count/field name, không chép nội dung.
7. Backup mới của B trong `data/backups/` (kể cả 3 bản ngày 2026-11-01) **không phải** rác — không xóa, không "dọn" dù nội dung trùng.

---

## 6. NGUYÊN TẮC BẤT DI BẤT DỊCH — ANTI-CHECKPOINT (project A)

1. **Native Desktop:** mặc định `emulate_mobile = 0`. Không ép User-Agent di động vào Chrome Profile tạo trên Windows (lệch WebGL → Google khóa phiên). DB + migration đã ép; đừng đảo ngược.
2. **Không đóng Chrome đột ngột sau khi bấm Đăng:** giữ đoạn chờ dialog chia sẻ + 2s ở Dashboard sau `#done-button` (SIDCC / `__Secure-1PSIDTS` cần 6-8s đồng bộ).
3. **Chống spam:** giãn cách tối thiểu **30 phút/video** (auto-schedule + `monitor_upload_gap_minutes`), `max_uploads_per_run` nếu cần giới hạn phiên.
4. **Proxy đồng nhất:** profile và mọi kênh con phải đi cùng proxy.
5. **Không làm yếu fail-closed:** thiếu/sai Channel ID, Checks không rõ, 18+ không xác nhận, không đọc được URL publish, không kiểm chứng được 30 phút → **dừng + báo lỗi**, không fallback "cho chạy xong".
6. **Dừng là dừng lịch sự:** nút Dừng chờ upload đang chạy hoàn tất; **không** kill FFmpeg toàn hệ thống (đã giới hạn theo command line thuộc temp/output của tool).
7. **Phạm vi hợp tác:** làm mọi việc kỹ thuật. **KHÔNG** phát triển/tối ưu tính năng né phát hiện bản quyền (chuỗi pitch-shift/EQ "lách Content ID" trong `render_engine` — giữ nguyên hiện trạng, không thêm filter cùng mục đích). Nếu được yêu cầu, từ chối phần đó và đề xuất hướng hợp pháp.

---

## 7. RULE LÀM VIỆC BẮT BUỘC CHO KỸ SƯ/AI TIẾP NHẬN

1. Chỉ ghi trong phạm vi mục 2.
2. **Không upload thật** khi đang audit/test/sửa code. Chỉ upload khi chủ tool yêu cầu rõ, sau khi Channel ID + phiên đăng nhập đã xác minh, và **phải có giám sát**. Test dùng video synthetic + DOM giả lập.
3. Test không chạm dữ liệu production: sau khi chạy, kiểm tra `system_logs` không sinh dòng giả, không còn `_TestZCode*`/project test.
4. **Sau mọi thay đổi code:** chạy **toàn bộ** test của project đó; `python -m py_compile` các module đã đụng; frontend thì `node --check static/app.js` **và** tăng `app.js?v=N` (hiện tại **20**) rồi yêu cầu Ctrl+F5; cập nhật `CHANGELOG.md` khi hành vi đổi.
5. **Git:** không commit/push/đổi git config/thêm remote/đổi visibility khi chưa được yêu cầu. Khi được yêu cầu: review `git status` + staged diff, loại secret, rồi mới commit. Không force-push, không reset/checkout làm mất việc. **Commit độc lập theo từng workstream** (code / CI / docs thành commit riêng).
6. **Không có `gh` CLI.** Dùng credential Git Credential Manager đã lưu qua REST (`git credential fill` → token, **không bao giờ in token**). Khi đọc log Actions: endpoint log trả **302** sang signed URL Azure → phải fetch URL đó **KHÔNG** kèm header `Authorization`. Một số API mới **từ chối** `X-GitHub-Api-Version: 2022-03-08` (chấp nhận `2026-03-10` / `2022-11-28`) — bỏ header nếu không cần.
7. **Không có git identity** trong repo. Commit bằng env vars từng lệnh: `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` = `DHB Studio <dhb-studio@localhost>`; **không** `git config`.
8. **Không tự ý dừng/khởi động lại** dashboard, bot, server, job đang chạy của chủ tool — phải xin phép, nêu rõ phạm vi.
9. **Báo cáo trung thực:** phân biệt "đã test bằng code/DOM giả lập" với "đã chạy thật trên YouTube / trên Chrome thật". Không tuyên bố selector 18+ hay upload Antidetect đã xác minh nếu chưa chạy thật.
10. Rule Antidetect (mục 3.5) là luật: CDP only, chỉ tab mình tạo, không local-launch profile của B, không API mode cho kênh Antidetect.
11. CI **xanh không có nghĩa môi trường đúng**: run cũ của B đỏ vì thiếu dependency mà local che giấu; run A xanh nhưng workflow cũ chỉ `py_compile` 8/11 module. Luôn đọc log chi tiết job, phân biệt warning của runner với warning của repo.
12. **Trạng thái treo:** A có 6 file sửa chưa commit; B có `HANDOFF.md` sửa chưa commit. Không tự "dọn" bằng reset/checkout. Nếu cần commit, tách đúng workstream và hỏi chủ tool nếu chưa rõ.

---

## 8. BẪY KỸ THUẬT ĐÃ TRẢ GIÁ

### A — DHB

1. `drawtext` trên Windows qua argv: escape 2 cấp — `:` ổ đĩa thành `C\:/...` **ngay trong nháy đơn**; dấu phẩy trong biểu thức filter phải `\,` (vd `x='w-mod(t*10\,w+tw)'`). Xem `_ff_path`.
2. `concat` filter: input phải **xen kẽ** `[v0][a0][v1][a1]`; các đoạn phải đồng bộ `settb=AVTB`/`aformat`/`format=yuv420p`/`fps`, nếu không "Error linking filters".
3. `render_engine` có cờ `rendered`: nhánh 16:9 mặc định bọc `if not rendered:` — thêm chế độ render mới phải set cờ + `out_w/out_h`, không thì video bị render đè.
4. Race pipeline khi test tự động: pipeline set trạng thái cuối **trước khi** `_batch_worker` nhả `_is_running`; test phải (a) đợi `overview.pipeline_running == false`, (b) đợi trạng thái/output **đổi** so với trước start, rồi mới đợi terminal status.
5. `database.py` thiếu import (`APP_DATA_DIR`, `APP_TEMP_DIR`) từng gây NameError **bị nuốt âm thầm** — import phải đủ.
6. FFmpeg stderr đọc theo dòng với `errors="ignore"`; mọi tiến trình bọc trong `_ACTIVE_FFMPEG_PROCS` để kill tức thì.
7. Video câm phải chèn `anullsrc`; **chú ý index input**: nhánh không nhạc map im lặng ở index 1, nhánh pip index 2, nhánh có nhạc chỉ dùng nhạc.
8. `/api/upload-video-files` phải là `def` (không phải `async def`) để FastAPI đẩy threadpool — `async` từng chặn event loop, UI treo khi nạp file lớn.
9. Thứ tự bấm **"Hiển thị thêm"** trong Studio: phải mở rộng trước rồi mới bấm radio 18+/AI; bấm khi mục còn ẩn thì bị bỏ qua.
10. Git Bash trên Windows: `taskkill //F //PID <pid>` (hai dấu `/`); in tiếng Việt cần `PYTHONIOENCODING=utf-8`.
11. **Schema DB A dễ viết sai:** `chrome_profiles` **không có `id`** — khóa là `name`; `youtube_channels` dùng cột `status` (không phải `login_status`); `chrome_profiles` mới có `login_status`/`last_login_check`. Đừng query `WHERE id = ?` cho `chrome_profiles`.
12. **A-9 chỉ ghi history cho lượt Completed mới.** `#197` đã Completed từ 2026-10-25 nhưng `published_history` vẫn 0. Muốn backfill phải chọn `channel_id` đúng và xác minh `post_id`; không tự chạy migration trên DB thật.
13. **A-8 là lớp UX, không thay backend.** `findChannelIdBlockers` dựa trên `gChannels`/`gQueueItems` đã tải; nếu channels rỗng/cũ, UI có thể không chặn. Backend `_resolve_target_channel` vẫn fail-closed. Chưa test browser thật.
14. **CI A:** workflow mới local chưa push; `py_compile` phải đủ 11 module; `node --check` cần Node trên runner (có sẵn). Đừng báo CI xanh cho thay đổi chưa push.

### B — Antidetect

1. **`httpx2` là dependency bắt buộc của Starlette `TestClient`** (starlette 1.6+). Không thêm `httpx` làm giải pháp chính — nó chỉ là fallback deprecated. Pin trong `requirements-dev.txt` + `constraints.txt`; đừng để local có sẵn package che lỗi runner.
2. **Local 1.6.0 / CI 1.7.0** của starlette: cả hai pass; `constraints.txt` không pin starlette. Nếu cần tái lập tuyệt đối, pin có chủ đích và chạy lại cả hai môi trường.
3. **Backup B mới** trong `data/backups/` (3 bản 2026-11-01) trùng nội dung `profiles.json`; không xóa/không sửa. Nguồn tạo chưa xác định; `data/app.log` không có entry mới.
4. `data/server.pid` stale: **không tin pid file**, phải health probe; A đã làm đúng (`resolve_base_url`).
5. `pytest` local có warning `Duplicate name: 'release.json'` là **cố ý**; đừng "sửa" bằng cách bỏ test hoặc ẩn warning nếu chưa hiểu.
6. Actions `checkout@v7`/`setup-python@v7` đã dùng; `cache-dependency-path` vẫn 3 file. Nếu tách requirements mới, phải thêm vào cache path.
7. Các invariant khác ở mục 4.5 — đọc trước khi sửa scheduler/bulk/updater/CSP.

---

## 9. CÁCH KIỂM THỬ ĐÃ DÙNG

### A

```bash
cd /d/DHB_Reup_YouTube_Studio
python -m py_compile app.py api_security.py antidetect_client.py pipeline_manager.py \
    youtube_uploader.py database.py render_engine.py scanner_downloader.py \
    ai_engine.py config.py check_status.py
node --check static/app.js
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -v   # 110 test
```

- Test HTTP thật: `python app.py` → `127.0.0.1:8686`; video mẫu `ffmpeg -f lavfi testsrc + sine`; render qua `/api/pipeline/start` rồi `ffprobe`. Không chạy upload thật.
- Playwright: trang HTML cục bộ + `page.route()` giả DOM Studio; không cần tài khoản thật.
- DB production: copy file + `file:...?mode=ro`; không mở WAL thật để ghi.

### B

```powershell
cd C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python
python -m pytest -q                     # 301 passed + 1 skipped
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing --cov-fail-under=58
python -m ruff check .
python -m compileall -q antidetect web cli.py run.py tools
$env:ANTIDETECT_RUN_UI_SMOKE = "1"; python -m pytest test_ui_smoke.py -q   # Chrome thật, data tạm
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"; python tools\e2e_smoke.py
python run.py --port 8766
```

- Cài dependency đúng: `python -m pip install -r requirements-dev.txt -c constraints.txt`.
- CI: Windows, Python 3.10, `compileall → ruff → pytest + coverage gate 58%`.
- Đọc log Actions: REST + token từ `git credential fill`; log endpoint 302 → fetch signed URL **không** kèm Authorization.

---

## 10. VIỆC CẦN GIẢI QUYẾT (theo thứ tự ưu tiên)

### Project A — P0, phải có chủ tool thao tác

- [ ] **A-1. Channel ID trống 7/7** → quét kênh hoặc nhập tay; fail-closed hiện tại là đúng.
- [ ] **A-2. 4/7 path Chrome Profile thiếu** → chủ tool xác nhận kênh còn dùng; backup DB + sửa từng dòng, không xóa thư mục.
- [ ] **A-3. Phiên đăng nhập:** chỉ `Công phụ` có status "Đã đăng nhập" (check cuối 2026-10-25 14:04); 6 profile còn lại trống.
- [ ] **A-4. Một lần đăng thật có giám sát**, đặc biệt selector 18+ chưa từng chạy thật.
- [ ] **A-5. Upload thật qua Antidetect: 0 lần**; B đang tắt, 0 binding.
- [ ] **A-6. Render branding trên video >5 phút / 4K** chưa đo hiệu năng.

### Project A — P1 (code được, không cần tài khoản)

- [x] **A-7. CI đủ module + `node --check`** — đã sửa local; **chưa push/CI chưa chạy**.
- [x] **A-8. Chặn UI khi thiếu Channel ID** — đã sửa local; **chưa test browser**.
- [x] **A-9. `published_history` cho mọi nguồn** — code + 3 test local; **DB hiện tại vẫn 0**, #197 chưa backfill.
- [ ] **A-10. YouTube API mode:** OAuth JSON nhập tay; cần kiểm tra OAuth thật theo từng kênh; API mode chủ động từ chối 18+ (đúng thiết kế).
- [ ] **A-11. Từ khóa Checks** phủ nhiều ngôn ngữ; Studio ngôn ngữ lạ → fail-closed; bổ sung theo phản hồi thật.
- [ ] **A-12. Dọn artifact cũ** (4 `_backup_p*_20261026`, `data.db`, `temp/test_zcode3/4`, `temp/wmtext_*.txt`) — chỉ sau khi chủ tool duyệt.

### Project B

- [x] **B-1. CI đỏ do thiếu `httpx2`** — đã sửa, commit `313faab`, run `41317044530` SUCCESS.
- [x] **B-2. Actions v7** — đã sửa, 0 annotation.
- [x] **B-3. Baseline hash `profiles.json`** — `HANDOFF.md` đã cập nhật `23E81C47…2F35` + lý do drift; **chưa commit**.
- [~] **B-4. Warning** — CI còn 1 warning cố ý (`release.json`); local còn thêm anyio do starlette 1.6.0. Không phải lỗi.
- [ ] **B-5. Chưa verify:** deep sync Fanpage thật; soak 24h / >20 profile / SOCKS5 thật / DataDome+Turnstile; Playwright UI smoke trong CI; launcher coverage 29%; `web/app.py` ~1.4k dòng; ~68 inline handler CSP; bulk job single-slot; updater không auto-download/Authenticode.

### Việc chéo

- [x] **X-1 (B).** Repo B đã push `313faab`; CI xanh; 0 secret trong lịch sử (audit trước đó).
- [ ] **X-1 (A).** A có 6 file sửa chưa commit; CI mới chưa push. Chỉ commit/push khi chủ tool yêu cầu.
- [ ] **X-2.** Hai project cùng FastAPI/Playwright nhưng pin khác nhau (A pin cứng trong `requirements.txt`; B dùng `>=` + `constraints.txt`). Khi sửa API chung (`/api/profiles/{id}/start`), phải bump cả hai phía và chạy test cả hai; `pid`/`ws_endpoint`/`already_running` là hợp đồng của `_lease_started_by_us()`.

---

## 11. HIỆN TRẠNG PHIÊN BẢN (snapshot 2026-11-01)

```text
A  DHB Reup YouTube Studio   main 34d1c87 (remote same)   UI v20   110/110 test local
    Working tree: 6 file sửa CHƯA COMMIT (A-7/A-8/A-9, +246/-2)
    CI: 41312882433 SUCCESS tại 34d1c87 — workflow CŨ; workflow mới chưa push
    Dashboard :8686 đang TẮT

B  Antidetect Browser Mgr    master 313faab (remote same)  v1.1.0   301 passed +1 skipped
    CI: 41317044530 SUCCESS tại 313faab, 0 annotation, 1 warning cố ý
    Working tree: HANDOFF.md sửa CHƯA COMMIT (baseline hash)
    Server :8000 / dashboard :8766 đang TẮT; data/server.pid stale
    profiles.json SHA256 23E81C47…2F35; 7 profile; 10 backup; 1 profile có proxy

Chung: private repos; không có secret trong git; Windows + Python 3.10.11; không venv.
```

---

## 12. SECRET KHÔNG NẰM TRONG PROMPT NÀY

OAuth JSON/token, cookie Chrome, mật khẩu proxy (kể cả IP:port proxy test), API key Gemini, access key Antidetect, nội dung DB/profile, nội dung `secrets.dpapi`/`runtime.dpapi`, `server.pid` nguyên văn. Tất cả chỉ tồn tại local; không sao chép khi bàn giao, không in ra log/API/CSV.

---

*Hết bàn giao v4. Nếu có mâu thuẫn giữa tài liệu này và code/DB hiện tại, code/DB thắng — nhưng phải kiểm tra bằng chứng (git log, DB readonly, CI log) trước khi kết luận.*
