# G9 — Nghiệm thu và cập nhật local G7–G8

Đã triển khai build `2026.11.15-scan-csv`, commit `e24f19b549a3e6bc0860ce63072c809ff2558051`, lên nhánh local `codex/local-scan-csv-20261115`. Dashboard: `http://127.0.0.1:8686`. Giữ nhánh release trước để rollback; không merge các PR và không đăng video/bình luận thật.

## Bằng chứng

- Full regression G8: 359 phương thức, 350 đạt và 9 media opt-in bỏ qua, gồm Chromium. CI G7 và G8 đều đạt `unittest` và `media` trên cả push/PR.
- Startup sạch qua Python 3.10 virtualenv đạt với DB mới, fallback FFmpeg, đúng source root/schema và không tự chạy queue/monitor. Sửa utility smoke để dừng cả cây tiến trình: Windows virtualenv launcher có thể tạo server con còn giữ file lock sau khi launcher bị terminate.
- Xác minh PID/cwd/port của app cũ, queue/monitor/slot idle, checkout sạch. Lấy app lock và backup SQLite nhất quán trước đổi code; diễn tập migration hai lần và restore trên clone đạt.
- Sau migration và restart, tất cả trường trong sáu bảng nghiệp vụ giữ nguyên: 34 queue, 19 history, 32 attempts, 1 reservation, 16 channels, 0 monitors; quick_check `ok`. Mục #212 vẫn `NeedsReview`.
- Health/diagnostics đúng revision, `restart_required=false`, queue và monitor không tự chạy. HTTP CSV download trên dự án có 8 video trả đủ 8 dòng với marker encoding mới.
- Fingerprint checkout local: `b170e088b51764ec90aae04f59dd73fafe1a1e538697c81591b692f59c12552a`. Git tự chuyển LF/CRLF nên fingerprint byte khác checkout phát triển; đã so sánh toàn bộ file Python/static sau chuẩn hóa xuống dòng và xác nhận cùng nội dung, cùng commit, cả hai checkout sạch. Không bỏ qua khác biệt nội dung.
- Backup riêng SHA-256: `b870e57aa0a8078d3411012dddd2bda1345ed8b8bddf317ad55f9d95832cf58c`. DB, đường dẫn vận hành, log và báo cáo chi tiết ở hồ sơ ngoài Git.

## Trạng thái và bước tiếp theo

G7 scan URL stop/timeout và G8 ba đường xuất CSV đã hoàn tất trong phạm vi offline/local. Không thay đổi dữ liệu thiếu nguồn #193–196 hoặc đoán Channel ID cho kênh #1–7. Lịch sử Completed không còn file local vẫn được giữ, không tự retry.

Roadmap còn lại: validation CRUD theo nhóm route; capability Studio category/language/remix có readback; giảm cảnh báo dependency sau kiểm chứng tương thích; quy trình repo/release; refactor module sau regression; soak có kiểm soát. Ghim bình luận thật và nghiệm thu publish thật vẫn chưa thực hiện. Scan thư mục local và hủy scan thủ công là phạm vi riêng, chưa thuộc G7.

Rollback: dừng app/worker, kiểm tra không có claim/slot đang hoạt động, backup DB hiện tại, chuyển về nhánh release local trước rồi restart. Giữ DB hiện tại vì G7–G9 không đổi schema; không restore backup cũ khi đã có evidence đăng mới chưa bảo toàn.
