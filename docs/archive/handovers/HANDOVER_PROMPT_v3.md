# PROMPT BÀN GIAO TOÀN DIỆN — 2 DỰ ÁN: ANTIDETECT BROWSER MANAGER + DHB REUP YOUTUBE STUDIO
*(Cập nhật 2026-11-01. DHB UI **v19** — Antidetect **v1.1.0**. Trao cho kỹ sư/AI tiếp nhận bảo trì cả hai project và mối nối giữa chúng.)*

**Cách dùng:** đọc hết mục 2, 6, 7, 8 trước khi đụng code — đó là các rule đã trả giá bằng dữ liệu thật. Sau đó kiểm tra trạng thái THẬT bằng `git log`, `git status`, DB và code hiện tại; mọi con số dưới đây là snapshot 2026-11-01, không thay thế việc đọc code. Không hỏi lại rule đã chốt.

---

## 0. NHIỆM VỤ CỦA BẠN

Bảo trì, sửa lỗi và nâng cấp **hai** tool Windows của chủ tool, cộng với lớp tích hợp giữa chúng:

- **A. DHB Reup YouTube Studio** — dashboard FastAPI tự động reup video lên YouTube (quét nguồn → tải → render FFmpeg → đăng bằng Playwright).
- **B. Antidetect Browser Manager** — server quản lý profile Chrome cô lập (fingerprint + proxy + stealth), dashboard riêng và API HTTP.
- **Mối nối A→B** — `antidetect_client.py` của A mượn trình duyệt đang chạy của B qua CDP lease để đăng YouTube bằng profile Antidetect.

Khi nhận yêu cầu mới: đọc file thật trong project tương ứng trước khi sửa, kiểm tra cú pháp (`python -m py_compile`, `node --check`), chạy test của project đó, và chỉ báo cáo "đã xác minh" khi thực sự đã chạy.

---

## 1. HAI DỰ ÁN & NƠI CODE (đã kiểm tra 2026-11-01)

| | A. DHB Reup YouTube Studio | B. Antidetect Browser Manager |
|---|---|---|
| Đường dẫn local | `D:\DHB_Reup_YouTube_Studio` | `C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python` |
| Remote GitHub | `github.com/meolucky051101001-cyber/DHB_Reup_YouTube_Studio` (**private**) | `github.com/meolucky051101001-cyber/antidetect_python` (**private**, vừa push 2026-11-01) |
| Nhánh / HEAD | `main` / `34d1c87` — 7 commit, working tree **sạch** | `master` / `51bb963` — 24 commit, working tree **sạch** |
| Version / UI | UI `app.js?v=19` | `antidetect/__init__.py` → `__version__ = "1.1.0"` |
| Test | `107` unittest — **CI pass, `Ran 107 tests ... OK`** | `301 passed, 1 skipped` pytest, 22.7s — **local pass** |
| CI | `.github/workflows/tests.yml` — run `41312882433` **SUCCESS, 0 `##[warning]`** | `.github/workflows/ci.yml` — run `41313775931` **FAILURE** (xem mục 4.3) |
| Cổng chạy | `http://127.0.0.1:8686` | `http://127.0.0.1:8766` (dashboard tay) hoặc `:8000` (server API) |
| Docs | `README.md`, `HUONG_DAN_SU_DUNG.txt`, `CHANGELOG.md`, `HANDOVER_PROMPT_v2.md` | `HANDOFF.md`, `docs/OPERATIONS.md`, `docs/LIMITATIONS.md`, `docs/VERIFICATION_2026-10-24.md`, `SECURITY.md`, `FIXES.md` |

Stack chung: Python **3.10.11** (bản hệ thống `C:\Users\admin\AppData\Local\Programs\Python\Python310`, **không dùng venv**), FastAPI + Uvicorn, SQLite WAL, Playwright điều khiển Chrome thật, Windows 10 x64, Git Bash.

## 2. PHẠM VI GHI FILE — BẮT BUỘC, KHÔNG MỞ RỘNG

1. **A (`D:\DHB_Reup_YouTube_Studio`) writable mặc định.** Chỉ sửa/tạo file trong thư mục này.
2. **B (Antidetect) là READ-ONLY reference.** Muốn sửa code B phải xin phép chủ tool **từng lần một**, nêu rõ file + lý do. Đọc để đối chiếu field name/behavior thì thoải mái.
3. **`C:\DHB Reup Facebook\` là READ-ONLY tuyệt đối** — A kế thừa FFmpeg/ffprobe/youtube-dl và thư mục profile Chrome từ đó. Không ghi, không xóa, không "dọn".
4. `C:\tool v1` là project khác, không liên quan.
5. Trong B, `data/`, `browsers/`, `exports/`, `backups/`, `*.dpapi` **không bao giờ** là đối tượng sửa — kể cả khi được phép sửa code B.

---

## 3. DỰ ÁN A — DHB REUP YOUTUBE STUDIO: HIỆN TRẠNG

### 3.1 Kiến trúc file
`app.py` (REST API, ~37 endpoints) · `config.py` (đường dẫn DHB gốc + kho tài nguyên) · `database.py` (schema + migration + backup + housekeeping) · `pipeline_manager.py` (đa luồng Semaphore + stop_event + Auto Monitor 24/7) · `render_engine.py` (7 chế độ render FFmpeg + PID trap + branding overlays) · `youtube_uploader.py` (Playwright Studio 4 bước + YouTube Data API v3 + probe đăng nhập + quét kênh) · `antidetect_client.py` (API client + CDP lease) · `api_security.py` (bearer token cục bộ, chặn Host/Origin) · `scanner_downloader.py` (yt-dlp + quét thư mục đệ quy) · `ai_engine.py` (Gemini/OpenAI/DeepSeek/Translate) · `static/` (SPA vanilla JS, polling 2.5s) · `tests/` · `data/dhb_youtube.sqlite`.

DB 12 bảng: `channel_groups`(3) · `chrome_profiles`(7) · `youtube_channels`(7) · `upload_option_groups`(2) · `projects`(2) · `video_queue`(5: 1 Completed, 4 Error) · `monitors`(0) · `published_history`(**0**) · `system_logs`(344) · `app_settings` · `render_presets`(0) + `sqlite_sequence`.

### 3.2 Đã làm (tóm tắt các đợt — chi tiết trong `HANDOVER_PROMPT_v2.md` và `CHANGELOG.md`)
- 6 đợt tính năng + 4 đợt P0/P1/P2: fail-closed Channel ID, fail-closed publish URL, fail-closed Checks, fail-closed 18+, giãn cách 30 phút, chống đăng lại Completed, anti-SQLi metadata, che secret, bearer token cục bộ, housekeeping 6h, kill FFmpeg có chọn lọc, preset render có tên, branding overlay/chroma-key/chữ chạy, intro/outro tự động, auto-schedule ≥30', browse-disk, monitor dedupe.
- **v19 (b42c30e):** tích hợp Antidetect qua CDP.
- **2026-11-01 (c5838f5) gia cố kết nối Antidetect — 3 điểm dễ vỡ:**
  - **Health check trước mọi thao tác:** `resolve_base_url()` ([antidetect_client.py:185](file:///D:/DHB_Reup_YouTube_Studio/antidetect_client.py#L185)) probe `/health` (bắt buộc `application == "antidetect-python"`) trước khi dùng cache; cache chết → bỏ và dò lại (`server.pid` → fallback `:8000`). `_request` xóa cache khi `RequestException` ([antidetect_client.py:238](file:///D:/DHB_Reup_YouTube_Studio/antidetect_client.py#L238)).
  - **Bỏ phụ thuộc câu chữ:** quyền sở hữu lease do hàm thuần `_lease_started_by_us()` ([antidetect_client.py:349](file:///D:/DHB_Reup_YouTube_Studio/antidetect_client.py#L349)) quyết định bằng **dữ liệu có cấu trúc** (`already_running`, `pid`, `ws_endpoint`, state trước khi start) — không parse message tiếng Việt/tiếng Anh. Mặc định an toàn: **không dừng profile mà không chứng minh được mình đã bật nó**.
  - **Retry CDP:** `_connect_cdp()` thử 3 lần, backoff `0.8s × lần thử`, hết thì báo lỗi rõ cổng + nguyên nhân.
- **2026-11-01 (52351c6):** `actions/checkout@v7` + `actions/setup-python@v7` → **hết cảnh báo Node.js 20**, log CI 0 warning.
- **2026-11-01 (34d1c87):** `README.md` mới (7.3 KB) làm trang chủ repo; topics `youtube automation ffmpeg playwright antidetect`.

### 3.3 Hợp đồng tích hợp Antidetect (KHÔNG được đảo ngược)
- Antidetect **sở hữu** Chrome/Profile/Proxy/Fingerprint. A chỉ: đồng bộ metadata profile, bật profile qua API của B, mượn trình duyệt đang chạy bằng CDP lease.
- **Không** mở Chrome local bằng `user_data_dir` của Antidetect. **Không** ép mobile/UA/viewport/proxy/stealth script lên trình duyệt Antidetect. **Không** dùng YouTube Data API cho kênh Antidetect (sai proxy/fingerprint) — upload qua Antidetect bắt buộc Playwright.
- Chỉ đóng **tab do lease tạo**; không đóng browser/context dùng chung.
- Profile Antidetect trong DB A dùng pseudo-path `antidetect://<id>` + cột `antidetect_profile_id`. Sync **không** ghi đè profile local trùng tên, **không** kéo mật khẩu proxy về DB.
- Endpoint liên quan: `GET /api/antidetect/status`, `POST /api/antidetect/sync`.

---

## 4. DỰ ÁN B — ANTIDETECT BROWSER MANAGER: HIỆN TRẠNG

### 4.1 Package
`antidetect/`: applog, browser_env, browser_runtime, bulk_jobs, cookies, facebook, fingerprint, human, launch_gate, launcher, metrics, models, process_identity, profile_audit, proxy, runtime_auth, scheduler, secrets, server_control, stealth, storage, updater · `web/` (dashboard + API) · `cli.py` / `run.py` · `tools/`: detector_audit, e2e_smoke, install_browser, leak_audit, probe_fingerprint, release, update, verify_live, audit_common · `tests/` (pytest) · stealth payload là asset thật: `antidetect/assets/stealth_page.js`, `stealth_worker.js`.

`pytest.ini`: `norecursedirs = backups exports data .git .system_generated` — test không được động vào data thật.

### 4.2 Trạng thái dữ liệu thật `data/` (đọc 2026-11-01, KHÔNG ghi)
- **7 profile còn đủ**, id + tên **khớp 100%** với backup mới nhất `data/backups/profiles_20261029_134012_513075.json`. Chỉ "Profile 1 - Nguyễn Thành Công(phụ)" có proxy; 6 profile còn lại `proxy.host` rỗng.
- ⚠️ **Hash `profiles.json` đã đổi so với baseline ghi trong `HANDOFF.md`**: `8CFC56C4…ABCA5` → hiện tại `23E81C476B9D53B98F29942BA3C77596A50F647DE929DC3D72449D43F2712F35`. Chủ tool đã thật sự dùng dashboard từ 2026-10-26 đến 2026-10-29 (lưu lại làm backup pre-save), nên drifted là **hợp lệ về ngữ nghĩa** — nhưng con số trong `HANDOFF.md` mục 3 và 5 đã **stale**. **Việc cần làm:** chốt baseline hash mới = giá trị hiện tại và ghi chú lý do, chỉ sửa `HANDOFF.md` khi được phép.
- `data/server.pid` còn đó (`pid 20340`, `port 8000`) nhưng **PID 20340 không tồn tại** → pid file stale, server Antidetect đang **TẮT**. A phải xử lý được tình huống này (đã xử lý: health probe fail → báo lỗi "hãy mở Antidetect trước rồi thử lại").
- `secrets.dpapi`, `runtime.dpapi` còn nguyên; `data/profiles/` và 10 backup profiles + 5 backup secrets vẫn đủ.

### 4.3 CI ĐANG ĐỎ — bug mở, cần chủ project B quyết định (việc A-1 bên dưới)
Run `41313775931` (push `51bb963`) fail ở step **"Run tests with coverage gate"**, exit 1:

```
RuntimeError: The starlette.testclient module requires the httpx2 package to be installed.
```

xảy ra trong `test_api.py`, `test_launch_gate.py`, `test_profile_audit.py`.

**Nguyên nhân đã xác minh:** `requirements-dev.txt` + `constraints.txt` **không khai báo `httpx`/`httpx2`**; starlette 1.6.0 đòi package này cho `TestClient`. Máy local **pass** (301 test) vì có sẵn `httpx 0.28.1` do package khác kéo vào — tức bộ test local xanh là **ăn may môi trường**, không phải mô tả đúng dependency.

**Cách sửa đúng (trong project B, cần xin phép):** thêm `httpx` (hoặc `httpx2` theo warning deprecation) vào `requirements-dev.txt` và pin phiên bản thật đã test vào `constraints.txt`; sau đó push và xác minh run CI mới. Sửa kèm: `.github/workflows/ci.yml` vẫn dùng `actions/checkout@v4` + `actions/setup-python@v5` → **còn cảnh báo Node.js 20** mà project A đã xử lý bằng v7; và `cache-dependency-path` phải thêm file mới nếu tách requirements.

### 4.4 Test & warning hiện tại (local, 2026-11-01)
`python -m pytest -q` → **301 passed, 1 skipped** (skip = Playwright UI smoke, chỉ bật khi `ANTIDETECT_RUN_UI_SMOKE=1`), 22.68s. 3 warning còn mở: (1) `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2`; (2) `DeprecationWarning: anyio.abc.BlockingPortal alias`; (3) `UserWarning: Duplicate name: 'release.json'` trong `test_updater.py::test_duplicate_manifest_entry_rejected` — warning này là test **cố ý** tạo zip trùng manifest, không phải lỗi.

### 4.5 Những điều đã chứng minh ở B — ĐỪNG đi ngược lại (nguyên văn `HANDOFF.md` mục 5)
- CreepJS baseline desktop = **25% like-headless** (heuristic noContentIndex/noContactsManager/noDownlinkMax thiếu trên **mọi** Chrome sạch). Tool bằng baseline. **Không coi 0% là mục tiêu.**
- Pixelscan "masking detected" ở chế độ **Unique** là đúng thiết kế (mỗi nhóm noise lẻ đều gây masking); Authentic → not detected.
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

---

## 5. QUY TẮC AN TOÀN DỮ LIỆU (áp dụng cả hai project)

1. **Không chạy live test với `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật** của B. Chỉ dùng thư mục tạm (`%TEMP%\opencode\ad-*`). Test của A phải mock `get_conn`/`add_log`, DB `:memory:`/temp, DOM Studio giả lập.
2. **Không sửa** `data/profiles.json`, `data/profiles/*`, `secrets.dpapi`, `runtime.dpapi`, `api_token.dpapi`, `data/backups/*`.
3. Sau live test: dừng browser, dọn profile tạm, kiểm tra hash + 0 process còn sống.
4. **Không bao giờ** in/log/commit: OAuth JSON, access/refresh token, cookie Chrome, proxy user/pass, Gemini key, access key Antidetect, `server.pid` nguyên văn. Không đưa secret vào URL, API response, CSV export hay prompt bàn giao.
5. DB/profile/backup của A (`data/`, `*.sqlite`, `data.db`, `logs/`, `temp/`, `output/`) luôn nằm ngoài Git — đã có `.gitignore`; kiểm tra `git status` trước khi commit.

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
4. **Sau mọi thay đổi:** chạy **toàn bộ** test của project đó; `python -m py_compile` các module đã đụng; frontend thì `node --check static/app.js` **và** tăng `app.js?v=N` (hiện tại 19) rồi yêu cầu Ctrl+F5; cập nhật `CHANGELOG.md` khi hành vi đổi.
5. **Git:** không commit/push/đổi git config/thêm remote/đổi visibility khi chưa được yêu cầu. Khi được yêu cầu: review `git status` + staged diff, loại secret, rồi mới commit. Không force-push, không reset/checkout làm mất việc. **Commit độc lập theo từng workstream** (code / CI / docs thành commit riêng).
6. **Không có `gh` CLI.** Dùng credential Git Credential Manager đã lưu qua REST (`git credential fill` → token, **không bao giờ in token**). Khi đọc log Actions: endpoint log trả **302** sang signed URL Azure → phải fetch URL đó **KHÔNG** kèm header `Authorization`. Một số API mới **từ chối** `X-GitHub-Api-Version: 2022-03-08` (chấp nhận `2026-03-10` / `2022-11-28`) — bỏ header nếu không cần.
7. **Không có git identity** trong repo. Commit bằng env vars từng lệnh: `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` = `DHB Studio <dhb-studio@localhost>`; **không** `git config`.
8. **Không tự ý dừng/khởi động lại** dashboard, bot, server, job đang chạy của chủ tool — phải xin phép, nêu rõ phạm vi.
9. **Báo cáo trung thực:** phân biệt "đã test bằng code/DOM giả lập" với "đã chạy thật trên YouTube / trên Chrome thật". Không tuyên bố selector 18+ hay upload Antidetect đã xác minh nếu chưa chạy thật.
10. Rule Antidetect (mục 3.3) là luật: CDP only, chỉ tab mình tạo, không local-launch profile của B, không API mode cho kênh Antidetect.
11. CI **xanh không có nghĩa môi trường đúng**: run của B đỏ vì thiếu dependency mà local che giấu (mục 4.3); run của A xanh nhưng `py_compile` mới phủ 8/11 module (mục 8, việc A-3). Luôn đọc log chi tiết job, phân biệt warning của runner với warning của repo.

---

## 8. BẪY KỸ THUẬT ĐÃ TRẢ GIÁ

**A — DHB:**
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

**B — Antidetect:** xem `HANDOFF.md` mục 5 (đã chép các ý quan trọng ở 4.5) và `docs/LIMITATIONS.md`.

---

## 9. CÁCH KIỂM THỬ ĐÃ DÙNG

**A:**
```bash
cd /d/DHB_Reup_YouTube_Studio && python app.py > /tmp/dhb.log 2>&1 &   # rồi sleep 7
netstat -ano | grep :8686 | grep LISTENING                              # tìm PID
taskkill //F //PID <pid>
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -v          # 107 test
python -m py_compile app.py api_security.py antidetect_client.py pipeline_manager.py \
    youtube_uploader.py database.py render_engine.py scanner_downloader.py \
    ai_engine.py config.py check_status.py
node --check static/app.js
```
Test HTTP thật: requests/curl tới `127.0.0.1:8686`; video mẫu bằng `ffmpeg -f lavfi testsrc + sine`; render thật qua `/api/pipeline/start` rồi `ffprobe` kiểm kích thước/thời lượng/audio. Playwright không cần tài khoản thật: trang HTML cục bộ + `page.route()` giả DOM Studio. Đọc DB production bằng **bản copy mở readonly** (`file:...?mode=ro`), không bao giờ mở WAL thật để ghi.

**B:**
```powershell
cd C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python
python -m pytest -q                     # 301 passed + 1 skipped
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing --cov-fail-under=58
python -m ruff check .
python -m compileall -q antidetect web tools run.py
$env:ANTIDETECT_RUN_UI_SMOKE = "1"; python -m pytest test_ui_smoke.py -q   # Chrome thật, data tạm
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"; python tools\e2e_smoke.py  # 24/24, tự chặn data/ thật
python run.py --port 8766
```

---

## 10. VIỆC CẦN GIẢI QUYẾT (theo thứ tự ưu tiên)

### Project A — P0, phải có chủ tool thao tác, không làm hộ được
- **A-1. Channel ID đang TRỐNG ở cả 7 kênh** (`youtube_channels.channel_id` rỗng 7/7) → mọi lượt upload bị fail-closed **đúng thiết kế**. Chủ tool bấm "🔄 Quét Kênh" từng profile để tool tự điền ID (đọc từ `page.url`, `ytcfg.CHANNEL_ID`/`DATASYNC_ID`, và `el.data.navigationEndpoint.browseEndpoint.browseId` của account item), hoặc nhập tay từ URL Studio. **Phần đọc `el.data` chưa từng được kiểm chứng trên profile thật** — quét vẫn trống thì nhập tay.
- **A-2. 4/7 thư mục Chrome Profile không tồn tại trên đĩa**: `Công chính` trỏ sai ổ `D:\DHB Reup Facebook\Data\Profile_Chrome\...` (đúng phải là `C:\` — đây là nguồn lỗi `[WinError 2] System cannot find the path specified`), và `Thi Đại Học`, `congnguyen`, `Lê Thị Quyên` không có ở `C:\DHB Reup Facebook\Data\Profile_Chrome\`. Chỉ `Bóng Ma Đề Cương`, `Công phụ`, `lethiquyen2` là còn. → **cần chủ tool xác nhận kênh nào còn dùng** trước khi sửa path trong DB (backup DB + sửa từng dòng, không xóa thư mục).
- **A-3. Phiên đăng nhập gần như chưa biết:** chỉ `Công phụ` = "Đã đăng nhập" và lần kiểm tra cuối là **2026-10-25 14:04** (đã 7 ngày, coi như cũ). 6 profile còn lại `login_status` rỗng. → "🌐 Mở Đăng Nhập" rồi "🔍 Kiểm Tra" từng profile.
- **A-4. MỘT lần đăng thật có giám sát** (chỉ sau A-1→A-3): xác nhận chọn đúng kênh con, đọc URL từ share dialog, Checks ra "No issues found", và đặc biệt **giới hạn 18+** trên Studio thật (radio `VIDEO_AGE_RESTRICTION_RESTRICTED` — **selector chưa từng chạy thật**; không xác nhận được thì tool hủy nháp và báo lỗi, không đăng sai tuổi).
- **A-5. Upload thật qua Antidetect: 0 lần.** `chrome_profiles.antidetect_profile_id` = **0 binding**, server B đang TẮT (pid stale). Toàn bộ đường Antidetect mới chỉ được chứng minh bằng HTTP server giả lập + fake lease. Việc: bật server B → `POST /api/antidetect/sync` → `GET /api/antidetect/status` → probe đăng nhập → **một** upload có giám sát.
- **A-6. Render branding (overlay/intro/outro) trên video DÀI >5 phút và 4K:** mới test video 3s, chưa đo hiệu năng/nhiệt độ RAM của concat + chroma-key.

### Project A — P1 (code được, không cần tài khoản)
- **A-7. CI mới `py_compile` 8 module, bỏ sót `antidetect_client.py`, `api_security.py`, `check_status.py`** — sửa `.github/workflows/tests.yml` cho khớp danh sách ở mục 9, và cân nhắc thêm `node --check static/app.js` (runner Actions đã có Node, thêm bước là được).
- **A-8. Chưa chặn ở UI** khi bấm Start lô có kênh thiếu Channel ID (backend đã fail-closed; bảng kênh mới chỉ có badge "⚠️ Thiếu Channel ID").
- **A-9. `published_history` = 0 dòng** dù đã có video Completed — kiểm tra pipeline có ghi history cho nguồn upload kéo-thả/local-path không; nếu không, dedupe "chống đăng lại" chỉ dựa trên `video_queue`.
- **A-10. Chế độ YouTube API:** OAuth JSON nhập tay, cần kiểm tra OAuth thật **theo từng kênh**; API mode chủ động từ chối video 18+ (đúng thiết kế).
- **A-11. Từ khóa Checks** phủ nhiều ngôn ngữ phổ biến; Studio ngôn ngữ lạ → fail-closed (an toàn nhưng không đăng). Bổ sung từ khóa theo phản hồi thật.
- **A-12. Dọn artifact cũ trong repo** (không tracked, cần chủ tool duyệt trước khi xóa): 4 thư mục `_backup_p*_20261026` ở gốc repo, `data.db`, `temp/test_zcode3`, `temp/test_zcode4`, `temp/wmtext_*.txt` cũ.

### Project B — cần quyết định của chủ project B (sửa code ở đây = xin phép)
- **B-1. LÀM CI ĐỎ TRƯỚC: bổ sung `httpx`/`httpx2` vào `requirements-dev.txt` + pin trong `constraints.txt`** để starlette `TestClient` chạy trên runner sạch (mục 4.3). Nghiệm thu: run CI mới trên commit đó = SUCCESS, và **bộ test không còn phụ thuộc package ăn may từ môi trường local**.
- **B-2. Nâng `actions/checkout@v4` → `v7`, `actions/setup-python@v5` → `v7`** để hết cảnh báo Node.js 20 (A đã làm y hệt, kết quả: 0 warning). Nhớ thêm file dependency mới vào `cache-dependency-path`.
- **B-3. Chốt lại baseline hash `profiles.json`** trong `HANDOFF.md`: giá trị mới `23E81C47…2F35` + ghi chú vì sao đổi (mục 4.2).
- **B-4. Ba warning còn mở** trong pytest: 2 warning deprecation của starlette/anyio (hết khi B-1 chọn đúng package) và `Duplicate name: 'release.json'` (cố ý — cân nhắc `pytest.warns`/filter để log CI sạch).
- **B-5. Chưa verify (giữ nguyên như `HANDOFF.md` mục 6):** deep sync Fanpage với page FB thật; soak 24h / >20 profile đồng thời / SOCKS5 thật / DataDome+Turnstile; Playwright UI smoke **không chạy trong CI** nhưng bắt buộc khi đổi UI; launcher `launch/stop/CDP` vẫn ~1.9k dòng coverage 29%; `web/app.py` ~1.4k dòng; ~68 inline handler CSP; bulk job single-slot cục bộ (không distributed lock đa máy); updater không auto-download, không Authenticode.

### Việc chéo
- **X-1.** Repo B vừa có remote — từ nay **push là thật**, nên trước khi push phải chắc chắn CI hiểu đúng dependency (B-1) và không có secret trong lịch sử (đã audit 24 commit/92 file ngày 2026-11-01: sạch).
- **X-2.** Hai project cùng dùng FastAPI/Playwright nhưng **phiên bản pin khác nhau** (A pin cứng trong `requirements.txt`; B dùng `>=` + `constraints.txt`). Khi sửa API chung (vd schema `/api/profiles/{id}/start`), **bumping cả hai phía** và chạy test cả hai — field `pid`/`ws_endpoint`/`already_running` là hợp đồng của `_lease_started_by_us()`.

---

## 11. HIỆN TRẠNG PHIÊN BẢN (snapshot 2026-11-01)

```text
A  DHB Reup YouTube Studio   main 34d1c87   UI v19   107/107 test   CI 41312882433 SUCCESS (0 warning)
B  Antidetect Browser Mgr    master 51bb963  v1.1.0  301 passed +1 skipped  CI 41313775931 FAILURE (thiếu httpx)
   Repo B: private, mới push, 24 commit, 0 file secret trong git (data/browsers/backups/dpapi đều untracked)
   Working tree cả hai: SẠCH — không có thay đổi chưa commit
```

## 12. SECRET KHÔNG NẰM TRONG PROMPT NÀY
OAuth JSON/token, cookie Chrome, mật khẩu proxy (kể cả IP:port proxy test), API key Gemini, access key Antidetect, nội dung DB/profile. Tất cả chỉ tồn tại local; không sao chép khi bàn giao, không in ra log/API/CSV.
