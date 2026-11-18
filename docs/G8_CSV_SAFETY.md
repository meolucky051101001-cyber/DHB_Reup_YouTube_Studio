# G8 — Xuất CSV an toàn cho bảng tính

Ba đường xuất CSV (hàng đợi, nhật ký, toàn bộ bảng) thêm dấu nháy đơn trước chuỗi có thể bị hiểu là công thức: `=`, `+`, `-`, `@`, cả khi có khoảng trắng/BOM/ký tự điều khiển phía trước. Chuỗi bắt đầu bằng tab, CR, LF hoặc dấu nháy đơn cũng được escape. Kiểu số, ô rỗng, Unicode, dấu phẩy, dấu nháy kép và xuống dòng dùng quy tắc CSV chuẩn. Dữ liệu trong DB không bị sửa.

CSV hàng đợi và nhật ký thêm cột `__DHB_CSV_Encoding` với giá trị `apostrophe-v1`. Import hàng đợi chỉ gỡ một ký tự escape đúng quy tắc khi dòng có marker này. Dấu nháy đơn nguyên bản được nhân đôi để round-trip không mất ký tự. CSV ngoài không có marker hoặc marker không nhận diện giữ nguyên dấu nháy đơn. Quy tắc validate/trim metadata hiện có vẫn áp dụng; không thay đổi chính sách nhập URL, nhóm, độ dài tiêu đề hay các cờ upload.

CSV xuất toàn bộ bảng giữ nguyên danh sách cột, ghi `cell_encoding: apostrophe-v1` trong `manifest.json`; công cụ đọc dữ liệu máy cần giải mã theo manifest. Việc che token/cookie/secret vẫn chạy trước khi escape. Đây là bản xuất đã che bí mật, không thay thế backup SQLite để phục hồi đầy đủ.

Nghiệm thu round-trip áp dụng cho CSV do tool xuất rồi nhập trực tiếp. Phần mềm bảng tính có thể tự đổi kiểu hoặc loại bỏ ký tự khi lưu lại CSV; dùng workbook XLSX của tool nếu cần chỉnh sửa có kiểu ô text rõ ràng. Không tuyên bố đã kiểm chứng mọi ứng dụng bảng tính.

Kiểm chứng: tái hiện 11 trường hợp thiếu escape trước sửa. Sau sửa, HTTP export + parse CSV kiểm tra toàn bộ cột text, preview không ghi DB, apply giữ metadata công thức/dấu nháy đơn/Unicode/CRLF, CSV ngoài không bị bỏ dấu nháy; nhật ký và CSV toàn bộ bảng vẫn che bí mật. Full suite chạy 359 phương thức: 350 đạt, 9 media opt-in bỏ qua; có Chromium. Compile và Ruff critical checks đạt. CI G7 (unit/browser/media) đều xanh. Không upload/bình luận thật.

Build: `2026.11.15-scan-csv`. PR xếp trên G7; không đổi schema hoặc tự merge. Kết quả triển khai local ghi riêng sau khi backup, kiểm tra idle và xác nhận health/revision.
