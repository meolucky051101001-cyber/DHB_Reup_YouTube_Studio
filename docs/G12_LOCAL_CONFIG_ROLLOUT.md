# G12 — Triển khai local G10–G11

Đã triển khai commit `10f236e626bd0deb0ecb30bdbf6de9107c4793ee`, build `2026.11.15-monitor-validation`, lên nhánh local `codex/local-config-validation-20261115`. Dashboard: `http://127.0.0.1:8686`. Giữ nhánh local trước để rollback. Các PR được xếp chồng, không tự merge; toàn bộ nghiệm thu offline, không gửi video/bình luận thật.

## Kết quả

- G10 sửa validation tạo/sửa kênh và bộ tùy chọn, boolean dạng chuỗi, cập nhật chỉ nhóm, OAuth local và rollback. G11 kiểm tra cấu hình Monitor, giới hạn số nguyên, nguồn/đích và trần quét metadata. Chi tiết trong `G10_CONFIG_VALIDATION.md` và `G11_MONITOR_VALIDATION.md`.
- Full suite bằng Python 3.10 virtualenv sạch: 375 phương thức, 366 đạt và 9 media opt-in bỏ qua. Có Chromium thực hiện form tạo/sửa kênh, tùy chọn và monitor. Startup sạch, compile, Ruff critical và JavaScript syntax đạt. CI G10 unit/browser/media đã xanh trước rollout.
- Kiểm tra dữ liệu cũ chỉ đọc: 16 kênh và 3 bộ tùy chọn hợp lệ với schema mới. Không suy ra ý định gốc để tự sửa cờ đã lưu từ trước.
- Xác minh PID/cwd/port của app cũ, checkout sạch, không có queue claim/monitor/slot upload hoạt động; giữ app lock khi backup và chuyển code. Backup nhất quán và rehearsal migration/restore đạt.
- Sau restart, tất cả trường của 9 bảng giữ nguyên: 34 queue, 19 history, 32 attempts, 1 reservation, 16 channels, 0 monitors, 3 options, 10 memberships, 16 profiles. `quick_check=ok`; video #212 giữ `NeedsReview`.
- Health/diagnostics xác nhận đúng revision/root, fingerprint local `4da9294ec84043abc95e6d579f558072624cf2ab3779dafff3be368a2003678f`, `restart_required=false`. Queue/monitor không tự chạy. Hai checkout cùng nội dung sau chuẩn hóa LF/CRLF và sạch theo Git.
- Chromium mở cả 6 tab trên server local: không lỗi JavaScript/HTTP; đọc đủ 16 kênh và 3 bộ tùy chọn, giới hạn input Monitor mới. Chặn mọi request ghi trong lượt kiểm tra giao diện này; không gửi request ghi nào.
- Backup riêng SHA-256: `3c9be6d10b83fb01d9c9a8d5898c74c06f8265103443b9573a4339114badbee8`. DB, log và báo cáo chi tiết nằm ngoài Git.

## Phạm vi còn lại

CRUD project/profile/preset và chính sách xóa tham chiếu cần đợt riêng. Studio category/language/remix vẫn thiếu thao tác/readback; không coi việc field lưu được là capability đã nghiệm thu. Dependency warning, quy trình repo/release, refactor và soak vẫn theo roadmap. Video Error #193–196 thiếu file và kênh #1–7 thiếu Channel ID chưa có dữ liệu để tự khôi phục.

Rollback: dừng app/worker, xác nhận không có claim/slot hoạt động và backup DB hiện tại; quay lại nhánh local trước rồi restart. G10–G12 không đổi schema; ưu tiên giữ DB hiện tại và evidence mới. Không restore backup cũ khi chưa bảo toàn các lần đăng phát sinh sau backup.
