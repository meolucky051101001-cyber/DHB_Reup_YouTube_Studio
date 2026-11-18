# G12 — Triển khai local G10–G11

Đã triển khai commit cuối `6cedb16407f6d075c81f4ab61f96f4249925c361`, build `2026.11.15-monitor-validation.1`, lên nhánh local `codex/local-config-validation-cleanup-20261115`. Dashboard: `http://127.0.0.1:8686`. Giữ nhánh local trước để rollback. Các PR được xếp chồng, không tự merge; toàn bộ nghiệm thu offline, không gửi video/bình luận thật.

## Kết quả

- G10 sửa validation tạo/sửa kênh và bộ tùy chọn, boolean dạng chuỗi, cập nhật chỉ nhóm, OAuth local và rollback. G11 kiểm tra cấu hình Monitor, giới hạn số nguyên, nguồn/đích và trần quét metadata. Chi tiết trong `G10_CONFIG_VALIDATION.md` và `G11_MONITOR_VALIDATION.md`.
- Full suite bằng Python 3.10 virtualenv sạch: 375 phương thức, 366 đạt và 9 media opt-in bỏ qua. Có Chromium thực hiện form tạo/sửa kênh, tùy chọn và monitor. Startup sạch, compile, Ruff critical và JavaScript syntax đạt. CI G10 unit/browser/media đã xanh trước rollout.
- Kiểm tra dữ liệu cũ chỉ đọc: 16 kênh và 3 bộ tùy chọn hợp lệ với schema mới. Không suy ra ý định gốc để tự sửa cờ đã lưu từ trước.
- Xác minh PID/cwd/port của app cũ, checkout sạch, không có queue claim/monitor/slot upload hoạt động; giữ app lock khi backup và chuyển code. Backup nhất quán và rehearsal migration/restore đạt.
- Sau restart, tất cả trường của 9 bảng giữ nguyên: 34 queue, 19 history, 32 attempts, 1 reservation, 16 channels, 0 monitors, 3 options, 10 memberships, 16 profiles. `quick_check=ok`; video #212 giữ `NeedsReview`.
- Health/diagnostics của bản cuối xác nhận đúng revision/root, fingerprint local `4b7a5621fcc43e2191cb1a2b85ddc1ef397604a57c43abd1d653d3da01386754`, `restart_required=false`. Queue/monitor không tự chạy. Hai checkout cùng nội dung sau chuẩn hóa LF/CRLF và sạch theo Git.
- Chromium mở cả 6 tab trên server local: không lỗi JavaScript/HTTP; đọc đủ 16 kênh và 3 bộ tùy chọn, giới hạn input Monitor mới. Chặn mọi request ghi trong lượt kiểm tra giao diện này; không gửi request ghi nào.
- Backup trước lượt đầu SHA-256: `3c9be6d10b83fb01d9c9a8d5898c74c06f8265103443b9573a4339114badbee8`. Backup trước bản bổ sung: `fe360d48f15ccf3c46ed1ec3f8374a002d68718d7125091b2bd3f3526d78cdb1`. Cả hai lượt rehearsal và đối chiếu 9 bảng đều đạt. DB, log và báo cáo chi tiết nằm ngoài Git.

## Phạm vi còn lại

CRUD project/profile/preset và chính sách xóa tham chiếu cần đợt riêng. Studio category/language/remix vẫn thiếu thao tác/readback; không coi việc field lưu được là capability đã nghiệm thu. Dependency warning, quy trình repo/release, refactor và soak vẫn theo roadmap. Video Error #193–196 thiếu file và kênh #1–7 thiếu Channel ID chưa có dữ liệu để tự khôi phục.

Rollback: dừng app/worker, xác nhận không có claim/slot hoạt động và backup DB hiện tại; quay lại nhánh local trước rồi restart. G10–G12 không đổi schema; ưu tiên giữ DB hiện tại và evidence mới. Không restore backup cũ khi chưa bảo toàn các lần đăng phát sinh sau backup.

## Bổ sung cleanup render

Một job CI media ghi nhận file render dở dang còn lại sau hủy, trong khi lượt PR và 12 lượt local cùng ca đều đạt. Chưa xác định được lock cụ thể của lần CI đó. Đã tái hiện điểm yếu cleanup một lần bằng Windows handle thật không cho SHARE_DELETE: code cũ bỏ qua lỗi và giữ file dù lock đã được thả.

Cleanup hiện thử tối đa 5 lần khi PermissionError, tổng chờ 0,75 giây; nếu vẫn thất bại thì ghi cảnh báo, giữ nguyên lỗi render/hủy ban đầu và không đụng file khác. Hai regression kiểm tra lock thật được thả và lock kéo dài không che lỗi dừng. Build bổ sung `2026.11.15-monitor-validation.1`.

Nghiệm thu bản bổ sung: full suite 377 phương thức (368 đạt, 9 media opt-in bỏ qua), gồm Chromium; ma trận riêng 28 ca FFmpeg thật qua 9 phương thức đều đạt. Compile và Ruff critical đạt. Không đổi tolerance media hoặc bỏ kiểm tra cleanup khỏi CI.
