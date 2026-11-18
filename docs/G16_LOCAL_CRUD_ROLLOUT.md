# G16 — Triển khai local CRUD Dự án, Profile, Render Preset

Đã triển khai build `2026.11.15-crud`, commit `74005a4d6cfa898797e0f78c502d60bd6f8d8d26`, lên nhánh runtime `codex/local-crud-20261115`. Dashboard: `http://127.0.0.1:8686`. Các PR #19 (Dự án), #20 (Profile), #21 (Preset) xếp chồng và chưa merge; CI push/PR của cả ba đều xanh unittest/browser/media trước rollout.

## Hành vi đã triển khai

- Dự án: tạo có validation/trùng tên rõ ràng, đổi tên nguyên tử và theo ID trong nhập video chậm, xóa chỉ khi rỗng; giữ guard Monitor/claim/reservation. Chi tiết: `G13_PROJECT_CRUD.md`.
- Profile: tạo local không ghi đè hoặc dùng thư mục cũ, sửa proxy/ghi chú an toàn, chặn xóa liên kết hàng đợi/Monitor/history/approval; giữ thư mục Chrome và quyền quản lý proxy Antidetect. Xóa Kênh dùng cùng guard. Tên/path/source của Profile không đổi qua PUT. Chi tiết: `G14_PROFILE_CRUD.md`.
- Preset: tạo mới tách khỏi cập nhật, sửa/đổi tên/xóa cần revision, validation config dùng chung với áp dụng hàng loạt, giữ trường nâng cao qua form và giữ bản sao đã áp vào video/Monitor. Chi tiết: `G15_RENDER_PRESET_CRUD.md`.

## Nghiệm thu và dữ liệu

Full suite cuối: 404 phương thức, 395 đạt và 9 media opt-in bỏ qua; gồm Chromium CRUD và các regression rollback/race. CI chạy media riêng và đạt. Ruff critical, compile, JavaScript syntax và diff check đạt. Smoke cài sạch khởi động đúng build trên DB tạm, không tự chạy queue.

Trước rollout đã kiểm tra đúng PID/cwd/port, checkout sạch, không queue claim, Monitor hoặc upload slot hoạt động. Kiểm tra cây tiến trình nhận diện đúng `System32/conhost.exe` là tiến trình hỗ trợ của Windows; không có worker con hoạt động. Giữ app lock khi backup và chuyển code. Rehearsal migration hai lần và restore trên bản sao kiểm tra tất cả trường của 15 bảng.

Sau restart, `quick_check=ok`; toàn bộ trường của các bảng dưới đây giữ nguyên:

| Bảng | Số bản ghi |
|---|---:|
| projects | 12 |
| render_presets | 0 |
| chrome_profiles | 16 |
| youtube_channels | 16 |
| video_queue | 34 |
| published_history | 19 |
| upload_attempts | 32 |
| content_reservations | 1 |
| monitors | 0 |
| upload_option_groups | 3 |
| channel_groups | 3 |
| channel_group_memberships | 10 |
| upload_approvals | 11 |
| upload_approval_items | 18 |
| managed_artifacts | 44 |

Video #212 giữ `NeedsReview`. Không sửa các khoảng trống dữ liệu cũ: Error #193–196 thiếu file; kênh #1–7 thiếu Channel ID. Không suy đoán hoặc tự retry các video đã đăng.

Health/diagnostics xác nhận đúng source root/revision, fingerprint `4cf18d8c12a3dbf2af0db5c28aa7306998dd0b7e4f2945dd7abbf4c8fe21d4bc`, `restart_required=false`; PID sau rollout là 27640. Nội dung code ở hai checkout khớp sau chuẩn hóa LF/CRLF.

Chromium mở cả 6 tab của runtime, đọc 12 dự án/16 Profile và kiểm tra các control CRUD mới; không lỗi JavaScript/HTTP, không request ghi. Lượt nghiệm thu runtime chặn request ghi, kết nối bên ngoài và truy vấn Antidetect; không mở phiên Profile, không upload/comment. Sau lượt này đối chiếu 15 bảng vẫn khớp backup. Runtime chưa có preset thật; CRUD preset đã thử bằng DB fixture riêng.

Backup SHA-256: `8a8ebd88e74903d924b94cc8a739c8f768d003a437b7b88114009cc08002d3d5`. Backup, log, screenshot và báo cáo riêng nằm ngoài Git.

## Rollback và phần tiếp theo

Không đổi schema. Nếu cần rollback: dừng app/worker, kiểm tra idle, backup dữ liệu hiện tại; chuyển runtime về nhánh `codex/local-config-validation-cleanup-20261115` và khởi động lại. Giữ DB hiện tại; không khôi phục backup cũ đè lên evidence mới. Bản trước có các hành vi CRUD chưa được bảo vệ như mô tả ở G13–G15.

Phạm vi tiếp theo theo roadmap: capability Studio category/language/remix có readback, dependency compatibility, quy trình repo/release, refactor và soak offline. Xác minh media với tài nguyên thực tế vẫn diễn ra khi render; lưu preset không chứng minh file/font/câu lệnh custom sẽ chạy được.
