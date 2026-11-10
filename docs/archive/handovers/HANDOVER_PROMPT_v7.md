# BÀN GIAO HIỆN TẠI — DHB REUP YOUTUBE STUDIO

**Cập nhật:** 04/11/2026 22:57 (Asia/Bangkok) — bản cuối sau nghiệm thu tài liệu 22:46; chỉ sửa diễn đạt mục 3.1–3.3, source/test không đổi. Snapshot sau khi đăng thành công video #211, dọn sạch bản nháp/lượt tải dở, hợp nhất R1–R7 và xử lý review D1–D5 vào working tree. PID 8136 khởi động 22:34:49; theo lịch sử chỉnh sửa và khởi động trong phiên, process được cho là đang dùng bản trước D1–D4 — chưa có hash runtime để xác nhận. Source hiện tại nhận diện bằng SHA256 tại mục 5; cần restart khi chủ tool cho phép<br>
**Repo code thật:** C:/Users/admin/Projects/Tool Workspaces/DHB Reup YouTube Studio<br>
**HEAD:** 256dd7a452a7500785633b654eb92d142c3636c8 — docs: add current project handover prompt<br>
**Baseline code đã nghiệm thu:** 5d0525febd2080378206d6c85b066f464aa1d0a2 — fix: reconcile monitor snapshots and preserve complete URLs<br>
**Build:** 2026.11.04-p7-fix2 (app.py:246)

> Tài liệu này tổng hợp bằng chứng kiểm tra trực tiếp ngày 04/11/2026. Khi tiếp nhận: kiểm tra Git, DB và dịch vụ trước khi sửa; nếu thực tế khác, ghi nhận khác biệt thay vì copy số liệu ở đây.

## 1. Tóm tắt tình trạng

- Tool là dashboard FastAPI cục bộ tại cổng 8686: quản lý kênh/profile, nạp nguồn, render FFmpeg, điều phối upload YouTube Studio; kênh đăng nhập theo profile Chrome/Antidetect.
- **P0 của bản v6 đã xử lý:** `DEFAULT_ANTIDETECT_HOME` trong `antidetect_client.py:19` nay trỏ `C:\Projects\Tools\Antidetect Python`; upload thật hôm nay chạy được qua profile Antidetect, engine đang nghe cổng nội bộ 8000 với 9 profile.
- **Việc chính hôm nay — đăng video #211 lên kênh #65 — đã hoàn tất:**
  - Video đã **đăng Công khai** ngày 04/11/2026, đúng tiêu đề đã chốt; DB `video_queue #211` = **Completed**, `published_url` đã ghi; `published_history` có 1 dòng; `upload_attempts` id 8 = `manual_review / ConfirmedPublished`.
  - URL video **không in trong tài liệu** theo quy tắc dữ liệu; đọc trực tiếp trong DB (`video_queue.published_url`, `published_history.published_url`).
- **Bài học cốt lõi của ngày:** lần 6 (20:01) tool báo NeedsReview nhưng YouTube **đã đăng thành công**. Hộp thoại chia sẻ của Studio không hiện (bị ẩn) nên code cũ kết luận thất bại sau 20 giây; lần kiểm tra 20:12 lại chỉ nhìn tab **Video** của Studio nên không thấy video. Đây là **Shorts**: video Shorts không nằm trong tab Video và lần kiểm tra **RSS công khai ngày 04/11 không thấy mục tương ứng** (chỉ là quan sát tại thời điểm đó; RSS không phải nguồn xác nhận đáng tin cho Shorts) — phải kiểm tra tab **Shorts** (hoặc trang kênh công khai) mới đúng.
- **Lần 7 (20:31–20:40) chạy trùng nhưng không tạo video công khai nào.** Sau khi đối soát, đã dọn sạch 1 bản nháp + 2 lượt tải dở còn sót (chủ tool đã duyệt); tab Shorts hiện chỉ còn video công khai vừa đăng và các video cũ riêng tư.
- Dịch vụ (kiểm tra 22:44): dashboard PID **8136** (cổng 8686, HTTP 200, khởi động 22:34:49) được cho là đang dùng bản trước D1–D4 (chưa có hash runtime để xác nhận); engine Antidetect PID **21576** (cổng 8000, khởi động 19:50:06). `hide_browser` đã khôi phục **true**.
- Working tree: **11 file modified + 2 file untracked** (`HANDOVER_PROMPT_v6.md`, file này) — **chưa commit, chưa push**.
- **Review R1–R7 và biên bản nghiệm thu D1–D5 (04/11):**
  - Đã sửa hoàn chỉnh: R1 (regex Channel ID), R4 (mô tả > 5.000 ký tự → ValueError), R6 (`upload_to` trong metadata), R7 (`checks_elapsed` dùng `time.monotonic()`), cùng việc tab Shorts dùng `/videos/short` (số ít).
  - R2/R3/R5 đã sửa và **được gia cố thêm theo D1–D4** (working tree 22:40, chưa commit):
    - D1: cả snapshot lẫn fallback yêu cầu đọc được **cả hai tab** Video/Shorts; tab không tải được danh sách (timeout và 0 hàng) hoặc tab đạt 100 hàng (chưa đọc hết phân trang) → fail closed.
    - D2: snapshot fail closed khi có hàng trùng tiêu đề nhưng **không đọc được ID**; fallback chỉ chấp nhận **đúng 1 ID mới không nằm trong snapshot đã quan sát**. Đây là loại trừ tương đối: chưa chứng minh ID đó do lần upload này tạo ra, và mỗi tab chỉ đọc tối đa 100 hàng.
    - D3: đếm ID mới **trước** khi lọc visibility; Public + Draft cùng tiêu đề → 2 ứng viên → không kết luận. `schedule` nhận theo nhãn "Đã lên lịch/Scheduled"; giờ hẹn được đối chiếu riêng trên form bằng `_schedule_values_match`.
    - D4: `discard_and_cancel_upload` chỉ báo `deleted` khi đã quan sát destructive dialog, bấm nút xác nhận trong dialog bằng `#confirm-button` hoặc nhãn Delete forever/Xóa vĩnh viễn được hỗ trợ, rồi đọc lại không lỗi và không còn thấy dialog. Không thấy hộp thoại → `not_found`/`uncertain`, không log thành công. **Chưa xác minh xóa theo video ID.**
  - D5 — luồng xác minh publish có ba nguồn: `extract_published_video_url` (`youtube_uploader.py:2317`) đọc share dialog và URL route; nếu chờ không có kết quả, `upload_via_playwright_studio` (`youtube_uploader.py:2937`) gọi `_find_published_video_in_studio_content` (fallback danh sách nội dung có snapshot).
    1. Hộp thoại chia sẻ: input/href và text ("Đường liên kết của video …").
    2. URL route đổi sau khi bấm đăng: chỉ host `studio.youtube.com`/`youtube.com`/`www.youtube.com`, URL khác `previous_url`, ID từ query `videoId`/`video_id`/`v` hoặc path `/video(s)/{id}`.
    3. Fallback danh sách nội dung với snapshot trước upload (đúng 1 ID mới, đúng tiêu đề, đúng visibility đã yêu cầu).
    - Giới hạn: (1) phụ thuộc hộp thoại có render; (2) chỉ là bằng chứng URL hiện tại của Studio, không xác nhận video thuộc lần upload này nếu URL đổi do điều hướng khác; (3) phụ thuộc snapshot đọc được, tối đa 100 hàng/tab và không chứng minh video do lần upload này tạo ra. Không có bước nào xác minh theo ID khi xóa nháp.

## 2. Nơi làm việc và sự thật Git

| Mục | Giá trị đã kiểm tra 04/11 22:44 |
|---|---|
| Repo code | C:/Users/admin/Projects/Tool Workspaces/DHB Reup YouTube Studio |
| Shortcut desktop `Open tool.lnk` | nằm ở C:/Projects/Tools/DHB Reup YouTube Studio (thư mục tài liệu/bằng chứng, **không phải repo Git**) trỏ vào app |
| Branch | main |
| HEAD | 256dd7a — docs: add current project handover prompt |
| Baseline nghiệm thu | 5d0525f — fix: reconcile monitor snapshots and preserve complete URLs |
| origin | repo GitHub riêng của dự án (không in URL trong tài liệu) |
| `git ls-remote origin` | HEAD và refs/heads/main đều = 256dd7a |
| ahead/behind | 0/0 |
| Working tree | 11 file modified chưa commit + 2 untracked (`HANDOVER_PROMPT_v6.md`, `HANDOVER_PROMPT_v7.md`) |
| Refs local | main (256dd7a), codex/fix-acceptance-d850cd7 (5d0525f), origin/main (256dd7a) |
| CI | run #6 (push 256dd7a) — workflow `tests`, job `unittest`: **success**, 0 annotation, 04/11 04:11 UTC. Các thay đổi chưa commit **chưa** được CI kiểm chứng |
| `git diff --check` | PASS (chỉ còn cảnh báo LF→CRLF của Git) |

### Quy tắc Git và dữ liệu

1. Đọc `git status --short --branch`, `git log -5 --oneline`, diff và nội dung file trước khi làm.
2. Không reset, checkout làm mất thay đổi, dọn file, commit hoặc push nếu chưa được yêu cầu.
3. Không đưa DB, backup, log, cookie/profile, OAuth, proxy credential, API key hoặc bearer token vào Git, prompt, log hay API response.
4. `data/`, `logs/`, `temp/`, `output/` là dữ liệu chạy máy; chỉ đọc khi cần, không xóa/di chuyển nếu chưa được yêu cầu.
5. Chỉ đọc tài nguyên ở `C:/DHB Reup Facebook`; không ghi sửa vào đó.
6. Khi YouTube yêu cầu xác minh hoặc không chắc trạng thái an toàn, dừng và báo; không vượt qua biện pháp bảo vệ của dịch vụ.
7. **Chạy ẩn theo yêu cầu chủ tool (04/11):** quét/tìm/cập nhật chạy nền, cửa sổ trình duyệt dời ra ngoài màn hình, không tự mở dashboard hay trình duyệt lên màn hình nếu chưa được yêu cầu.

## 3. Diễn biến trong ngày 04/11 (sau snapshot v6 lúc 14:38)

1. **Sửa lỗi uploader qua các lần chạy thật (15:23–17:08):**
   - Không mở được menu tài khoản Studio để xác minh kênh → thêm chờ avatar trong `_open_studio_account_menu` (`youtube_uploader.py:618`).
   - Nút "Tạo" khớp nhầm phần tử → `STUDIO_CREATE_BUTTON_SELECTOR` khớp nhãn chính xác (`youtube_uploader.py:553`).
   - File 80,6 MB vượt giới hạn 50 MB của Playwright → `attach_upload_file` dùng CDP `DOM.setFileInputFiles` (`youtube_uploader.py:566`).
   - Các radio bắt buộc phải bấm **True** (3 call site: dòng 3101, 3136, 3165) thay vì bỏ trống.
   - Nhãn nội dung AI: Studio dùng `VIDEO_HAS_ALTERED_CONTENT_YES/NO` (không phải `VIDEO_ALTERED_CONTENT_NO`) — sửa tại dòng 3156–3165.
2. **Probe 19:52 (đã được chủ tool duyệt):** đăng thử file giả 3 KB ở chế độ Unlisted, dump DOM hộp thoại chia sẻ, xác nhận URL hiển thị dạng **text** trong hộp thoại ("Đường liên kết của video …"), nút cuối hiển thị "Lưu"; sau đó xoá video probe.
3. **Lần 6 (19:57–20:01):** bấm Xuất bản lúc ~20:01:17; YouTube **đăng thành công lúc 20:01:19**. Hộp thoại chia sẻ bị ẩn suốt 20 giây nên tool kết luận NeedsReview. Kiểm tra 20:12 chỉ xem tab Video → kết luận sai là "chưa đăng"; sau đó xoá 2 video thử (chủ tool duyệt).
4. **Sửa xác minh publish (trước 20:31):**
   - `PUBLISH_CONFIRM_TIMEOUT_SECONDS = 300` (`youtube_uploader.py:563`).
   - Regex đọc URL từ text hộp thoại `_PUBLISHED_URL_IN_TEXT_RE` (`youtube_uploader.py:2277`, dùng tại 2355).
   - `_wait_for_publish_completion` (`youtube_uploader.py:2390`) chờ tối đa 300 giây, có log khi còn hộp thoại xử lý; bước đóng hộp thoại sau đó là tuỳ chọn.
   - Thêm 2 test trong `tests/test_p3_uploader.py`.
5. **Restart dashboard** (PID 9948, chạy ẩn, log trong `logs/server.out.log`/`logs/server.error.log`) để nạp code mới; engine Antidetect do chủ tool bật lại (PID 21576).
6. **Lần 7 (20:31–20:40):** reconcile #211 và chạy upload-only. Đã chờ đủ 300 giây nhưng hộp thoại chia sẻ vẫn không hiện; kết quả NeedsReview, **không tạo thêm video công khai**.
7. **Đối soát cuối (20:45–20:52):**
   - Trang kênh công khai + oembed xác nhận video Public đúng tiêu đề, phát hành 04/11/2026, hơn 500 lượt xem.
   - Tab **Shorts** trong Studio: 1 hàng Công khai (video thật) + 1 bản nháp + 2 lượt tải dở 38%/75% (rác từ các lần thử).
   - Gọi API `POST /api/queue/211/manual-reupload` với `action=confirmed_published` + URL lấy từ Studio → queue **Completed**, ghi `published_history`, tạo attempt 8.
   - Khôi phục `hide_browser=true` qua API settings.
   - Dọn rác (chủ tool duyệt "Dọn sạch"): huỷ 2 lượt tải dở bằng nút Huỷ tải lên + xác nhận `#confirm-button`; xoá vĩnh viễn bản nháp bằng `ytcp-button.delete-draft-button` + tick checkbox + `#confirm-button`. Xác minh lại: tab Shorts chỉ còn video công khai và các video cũ riêng tư.

## 4. Kiến trúc, luồng và bản đồ code

Stack: Windows x64, Python 3.10, FastAPI/Uvicorn, SQLite WAL, HTML/CSS/JS thuần, Playwright, FFmpeg/ffprobe, yt-dlp. API cục bộ bảo vệ bằng token + kiểm tra Host/Origin (`api_security.py`); `app_instance_lock.py` chống hai instance.

Luồng: nạp URL/tệp/CSV/Excel/thư mục → quét & tải → nhận dạng/chống trùng & claim → metadata/AI → render FFmpeg → giải quyết kênh đích + điều kiện an toàn → Playwright YouTube Studio hoặc YouTube Data API → ghi trạng thái/lịch sử/log, giải phóng claim. Upload thiết kế **fail-closed**: thiếu Channel ID hoặc không xác nhận được bước bắt buộc thì không báo thành công.

File chính: `app.py` (API/dashboard), `config.py`, `database.py` (schema/migration/log), `api_security.py`, `app_instance_lock.py`, `pipeline_manager.py`, `download_worker.py`, `scanner_downloader.py`, `queue_import.py`, `content_identity.py`, `group_distribution.py`, `phase4_rules.py`, `render_engine.py`, `storage_manager.py`, `youtube_uploader.py`, `antidetect_client.py`, `ai_engine.py`, `check_status.py`.

Chi tiết uploader đáng lưu ý:
- `upload_to == "Shorts"` tự thêm `#shorts` vào tiêu đề/mô tả nếu chưa có (`youtube_uploader.py:201`).
- Selector/radio Studio thực tế 2026: `VIDEO_MADE_FOR_KIDS_MFK/NOT_MFK`, `VIDEO_AGE_RESTRICTION_NONE`, `VIDEO_HAS_ALTERED_CONTENT_YES/NO`, `PUBLIC/UNLISTED/PRIVATE`; `#next-button`, `#done-button` (nhãn hiển thị có thể là "Lưu").
- Luồng xác minh publish có ba nguồn: `extract_published_video_url` (`youtube_uploader.py:2317`) đọc share dialog (input/href + text) và URL route đổi sau khi bấm đăng (chỉ host YouTube/Studio, khác URL trước đó); nếu `_wait_for_publish_completion` không có kết quả, `upload_via_playwright_studio` gọi `_find_published_video_in_studio_content` (fallback danh sách nội dung có snapshot). `_wait_for_publish_completion` poll tới 300 giây.
- Hộp thoại chia sẻ có thể **không bao giờ visible** với Shorts/4K dù video đã đăng — đây là gốc của mọi báo NeedsReview sai hôm nay.
- Đường dẫn Antidetect: `AntidetectConfig.load()` ưu tiên settings `antidetect_home`/`antidetect_data_dir`/`antidetect_base_url`, rồi env `ANTIDETECT_HOME`/`ANTIDETECT_DATA_DIR`/`ANTIDETECT_BASE_URL`, rồi constant mặc định (đã trỏ đúng `C:\Projects\Tools\Antidetect Python`).

Lỗi tiềm ẩn còn lại:
- `age_restriction=True` vẫn tìm radio `VIDEO_AGE_RESTRICTION_RESTRICTED` (`youtube_uploader.py:3129`); tên này **không quan sát thấy** trong DOM Studio ngày 04/11. Nhánh hạn chế tuổi thật **chưa được kiểm chứng**; không suy đoán tên radio thay thế cho tới khi probe lại. Chưa sửa.
- D1–D4 mới nằm ở working tree (22:40); theo lịch sử chỉnh sửa và khởi động, PID 8136 được cho là đang dùng bản trước D1–D4 — chưa có hash runtime để xác nhận, nên chưa xác nhận được D1–D4 có hiệu lực khi vận hành.

## 5. Bằng chứng kiểm thử hiện tại

- Môi trường: Python **3.10.11**, pytest **9.1.1** (Windows x64).
- `PYTHONIOENCODING=utf-8 python -m pytest -q tests/test_p3_uploader.py` — **33 passed, 7 subtests passed**, exit **0** (04/11 22:45), gồm regression R1–R7, D1–D4 và bằng chứng route-change (D5).
- `PYTHONIOENCODING=utf-8 python -m pytest -q tests --ignore=tests/test_antidetect_studio_sync.py --ignore=tests/test_guard_round2_smoke.py --ignore=tests/test_p0_render_hang_and_stop.py` — **197 passed, 16 subtests passed**, exit **0**, 8,86 giây (22:45) khi dashboard đang giữ instance lock. Ba module bị loại cần import `app`; chạy trực tiếp trong cùng data directory sẽ xung đột instance lock — có thể thiết kế môi trường test riêng.
- `python -m compileall -q app.py youtube_uploader.py antidetect_client.py database.py app_instance_lock.py` — PASS; `node --check static/app.js` — PASS; `git diff --check` — PASS (chỉ cảnh báo LF→CRLF của Git).
- SHA256 source lúc lập bảng (22:43): `youtube_uploader.py` = `530F749E0D9ECD23ECBB9217AFC5FA7633FA19E85B4EDA571D496F6818D89AAC`; `tests/test_p3_uploader.py` = `6CE625A0DCB4706E4B071C40675F87480AE03E162DB1740140BDABAC65CFCEE6`. Đối chiếu hash trước khi dùng số liệu.
- Probe DOM thật tab `/videos/short` (04/11, chạy ẩn, chỉ đọc, không in tiêu đề/URL): **30 hàng**, tất cả có href `/video/{id}/edit`; bộ phân loại trả **29 private + 1 public**, khớp trạng thái kênh tại thời điểm probe.
- End-to-end thật: video #211 Public trên kênh #65, hiển thị đúng tiêu đề; DB queue **Completed**, `published_history` 1 dòng, attempt 8 ConfirmedPublished (đối soát 20:52).
- **Chưa chạy full suite** vì dashboard đang giữ instance lock. **Chưa upload thật sau R1–R7/D1–D4.**
- CI trên HEAD 256dd7a: success, 0 annotation. Phần chưa commit (kể cả D1–D4) **chưa có CI**.
- **Chưa nghiệm thu chính thức** các thay đổi hôm nay; baseline nghiệm thu vẫn là 5d0525f (biên bản `NGHIEM_THU_5d0525f_2026-11-04.md`).

## 6. Snapshot DB và dịch vụ trên máy (DB đọc 04/11 20:52; dịch vụ cập nhật 22:44)

DB: `data/dhb_youtube.sqlite`, mtime **04/11/2026 20:46:20**, kích thước **280 KB**.

| Nhóm | Số liệu |
|---|---:|
| Profile Chrome | 16 (9 có `antidetect_profile_id`) |
| Kênh YouTube | 16; 7 thiếu `channel_id`; 10 "Đã đăng nhập", 6 "Ready" |
| Nhóm kênh / memberships | 3 / 16 |
| Queue | 7 (2 Completed, 4 Error, 1 Pending) |
| Monitor / published_history | 0 / **1** |
| content_reservations | 0 dòng (đã giải phóng sau đối soát) |
| upload_attempts | 8 |
| system_logs | 478 |
| app_settings | 24 dòng; `hide_browser = true`, `delete_temp_after_upload = true`, `max_concurrent_threads = 2` |

Dịch vụ (kiểm tra 04/11 22:44): dashboard PID **8136** nghe cổng 8686, HTTP **200**, khởi động **22:34:49**, `MainWindowHandle=0`. Theo lịch sử chỉnh sửa và khởi động trong phiên, process được cho là đang dùng bản trước D1–D4. Chưa có hash runtime để xác nhận chính xác source đã nạp. Source hiện tại được nhận diện bằng SHA256 tại mục 5 (`youtube_uploader.py` mtime 22:40:19). Engine Antidetect PID **21576** nghe cổng 8000, khởi động **19:50:06**. Không dừng/khởi động lại nếu chưa được phép.

Không in tên kênh, email, URL, log text hay bí mật vào tài liệu. Trước thao tác vận hành, đọc DB readonly hoặc mở dashboard để cập nhật số liệu.

## 7. Việc nên làm theo thứ tự

1. **P0 — khi chủ tool cho phép: restart dashboard (PID 8136) để nạp D1–D4, rồi chạy một upload thật/đối soát để nghiệm thu.** Theo lịch sử chỉnh sửa và khởi động, process 8136 được cho là đang dùng bản R1–R7 (chưa có hash runtime). Fallback publish chỉ chấp nhận đúng 1 ID mới không nằm trong snapshot; tab không đọc được hoặc có 100 hàng → không kết luận.
2. **P0 — commit 11 file đang sửa khi chủ tool cho phép**, tách theo workstream: (a) đường dẫn Antidetect mặc định + README; (b) ẩn cửa sổ + test Antidetect; (c) sửa uploader (create selector, CDP attach, radio True, nhãn AI, chờ publish 300s + URL text, xác minh publish D1–D5 và hủy nháp) + test; (d) thay đổi UI/tĩnh có sẵn từ trước (`static/*`, `database.py`); (e) tài liệu HANDOVER v5/v6/v7.
3. **P0 — đẩy commit và xác minh CI đúng commit mới** (workflow `tests`, job `unittest`, đếm warning/annotation).
4. **P1 — chạy lại full suite khi dashboard được phép dừng** (instance lock trống).
5. **P1 — chạy lại một lần quét kênh** (chạy ẩn) để xác nhận log quét ghi vào `system_logs` và cửa sổ nằm ngoài màn hình.
6. **P1 — xử lý 7 kênh local thiếu Channel ID:** 2 profile cần đăng nhập Google, 1 profile chưa có kênh, 4 profile trỏ thư mục không tồn tại cần sửa/xoá.
7. **P2 — cân nhắc đưa `antidetect_home` lên UI Cài đặt** để máy khác không phải dùng env var.
8. **P2 — probe lại DOM và sửa radio giới hạn tuổi** (`youtube_uploader.py:3129`), nghiệm thu sau khi commit; các mục elapsed/log xoá nháp/fallback publish đã sửa trong working tree.

## 8. Tài liệu bàn giao

- `HANDOVER_PROMPT.md` (marketing, đường dẫn sai), `_v2` (snapshot 26/10), `_v3`/`_v4` (chốt 01/11, HEAD 34d1c87), `_v5`, `_v6` — đều lỗi thời, giữ để đối chiếu, không dùng làm nguồn sự thật.
- File này (`HANDOVER_PROMPT_v7.md`) — snapshot hiện tại, chưa được commit.
- Trong thư mục bằng chứng `C:/Projects/Tools/DHB Reup YouTube Studio` có `_working_copy/` là **bản snapshot code ngày 03/11 (build 2026.11.03-p6), không phải repo Git** — đừng sửa vào đó.

**Bắt đầu task tiếp theo:** xác nhận `git status`/HEAD/remote gap; đối chiếu SHA256 `youtube_uploader.py` với mục 5 và kiểm tra source mtime so với thời điểm process 8136 khởi động (process được cho là đang dùng bản trước D1–D4; chưa có hash runtime); đọc `README.md`, `CHANGELOG.md` và module liên quan; kiểm tra kết nối Antidetect trước khi động tới tính năng Antidetect; nếu cần đối soát video #211, đọc DB readonly và tab **Shorts** của Studio. Không commit/push, không restart service, không upload thật nếu chủ tool chưa yêu cầu. Mọi quét/tìm/cập nhật chạy ẩn.
