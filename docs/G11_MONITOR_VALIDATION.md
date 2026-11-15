# G11 — Validation cấu hình Monitor

API tạo Monitor trước đây lưu được chu kỳ -1 phút, số video 1.001 và kênh đích không tồn tại. Dữ liệu sai kiểu có thể phát sinh HTTP 500; tên trùng gây lỗi SQLite và connection chưa được đóng. Worker lấy gấp đôi số video yêu cầu có thể vượt giới hạn 1.000 của extractor G7.

## Hành vi

- Schema kiểm tra số nguyên, không cắt phần thập phân hoặc coi bool là ID. Chu kỳ 5–10.080 phút, số video 1–1.000, min_views không âm và nằm trong SQLite int64. Chuỗi số nguyên vẫn được nhận; optional ID null/rỗng/0 nghĩa là không chọn.
- Nguồn phải là URL HTTP/HTTPS hợp lệ theo quy tắc nguồn hiện có hoặc file/thư mục local tồn tại; danh sách có ít nhất một nguồn. Giữ nguyên query string của URL đã ký, không chuẩn hóa lại thứ tự tham số. Thông báo lỗi chỉ nêu số dòng, không echo URL/token.
- Kiểm tra kiểu tên, policy, mode render/upload, AI action/provider/language. Metadata policy cần version số nguyên 1; boolean true không được coi là version 1.
- Kênh/nhóm đích và bộ tùy chọn phải tồn tại trong cùng giao dịch với insert. Nhóm trống được phép lưu để cấu hình sau, nhưng preview/khởi chạy vẫn bị chặn đến khi có kênh hợp lệ. Kênh cụ thể vẫn ưu tiên khi client gửi kèm nhóm như form hiện tại.
- Tên trùng trả 409 và không giữ transaction/connection. Lỗi validation trả 400 trước ghi dữ liệu. Worker giới hạn số metadata lấy thêm để lọc trùng ở 1.000.
- Form không dùng parseInt để âm thầm biến 5.5 thành 5; kiểm tra min/max/step trước gửi. Tạo Monitor không chạy worker và không tự xác nhận metadata.

## Nghiệm thu

Tái hiện code tại G10 trong DB tạm: cả -1, 1.001 và ID kênh 999999 được lưu. Sau sửa, 6 test mới gồm 28 subcase kiểm tra dữ liệu lỗi không tạo monitor, ID/nhóm thiếu, tên trùng và writer sau đó, nguồn local và URL giữ nguyên, preview/confirm, giới hạn quét và hủy worker. Chromium thao tác form Monitor, từ chối số thập phân rồi lưu cấu hình hợp lệ ở trạng thái dừng.

Full unittest trong Python 3.10 virtualenv sạch: 375 phương thức, 366 đạt, 9 media opt-in bỏ qua. Startup sạch, compile, Ruff critical và JavaScript syntax đạt. CI G10 unit/browser/media đều xanh. Không đổi schema hoặc dữ liệu vận hành, không gửi video/bình luận thật.

Build `2026.11.15-monitor-validation` gồm G10 và G11. Phần CRUD project/profile/preset, lifecycle xóa tham chiếu và capability Studio category/language/remix vẫn nằm trong roadmap; đợt này không tuyên bố đã nghiệm thu các capability đó.
