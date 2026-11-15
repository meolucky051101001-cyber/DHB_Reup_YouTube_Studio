# G15 — CRUD Render Preset và chống ghi đè

POST tạo mới và trả 409 khi trùng tên. PUT theo ID sửa/đổi tên với `expected_revision` lấy từ GET; DELETE cũng yêu cầu revision qua query. Cập nhật/xóa dùng transaction và kiểm tra phiên bản hiện tại, tránh ghi đè thay đổi từ cửa sổ khác. Thiếu preset trả 404. Client cũ dùng POST để upsert phải chuyển sang PUT có revision.

Tên, ghi chú, mode và config được kiểm tra kiểu. Validator chung cho Preset và áp dụng cấu hình hàng loạt kiểm tra số hữu hạn/giới hạn, cờ boolean, góc xoay, cặp kích thước/tọa độ, cú pháp màu, bitrate, độ phân giải và tên tài nguyên. Custom FFmpeg phải có câu lệnh; không thực thi lệnh khi lưu preset. Trường config không được hỗ trợ bị từ chối thay vì bị bỏ qua. Tài nguyên thực tế, font, khả năng chạy câu lệnh custom và kết quả media vẫn cần kiểm tra khi render.

GET giữ `config_json` gốc và báo `valid=false` với preset cũ hỏng; không biến lỗi thành cấu hình rỗng. Không migration/ghi lại preset đã lưu. UI tách lưu mới/cập nhật preset đã nạp, giữ trường nâng cao chưa có control, ghi chú và tên tài nguyên đang thiếu trên máy khi nạp–lưu. Dữ liệu cờ false được hiểu đúng. Xóa/sửa thư viện không thay đổi các bản config đã sao chép vào queue/Monitor.

Nghiệm thu: 404 phương thức unittest, 395 đạt và 9 media opt-in bỏ qua, gồm Chromium nạp–sửa–áp dụng–xóa, giữ framerate/bitrate/tài nguyên, và chặn sửa stale. SQLite kiểm tra race hai request, cấu hình sai không ghi một phần, giữ dữ liệu legacy và giữ nguyên bản sao queue/Monitor. Ruff critical, compile, JS syntax, diff check đạt. Smoke cài sạch dùng DB tạm và build `2026.11.15-crud`; không chạy upload hoặc Profile thật. CI vẫn chạy ma trận media độc lập.
