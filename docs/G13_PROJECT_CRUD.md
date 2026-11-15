# G13 — CRUD Dự án an toàn

- Tạo dự án kiểm tra tên kiểu chuỗi, độ dài, NUL và tên dành riêng; trùng tên trả 409, không ghi đè. Thao tác trên dự án không tồn tại trả 404.
- Có API PUT và nút Đổi tên. Đổi tên cập nhật bản ghi dự án và hàng đợi trong cùng transaction, giữ nguyên ID video, đường dẫn và lịch sử. Không gộp vào tên đã có dự án/hàng đợi; claim, trạng thái đang chạy và reservation chặn đổi tên. Preview upload cũ phải được tạo lại sau đổi tên.
- Xóa chỉ áp dụng cho dự án rỗng, giữ ít nhất một dự án. Không còn cascade xóa video. Dự án còn liên kết Monitor không thể đổi tên/xóa qua màn Dự án.
- Scan, tải file và nhập đường dẫn giữ ID dự án trước thao tác chậm, đọc lại tên trong transaction ghi queue: theo tên mới nếu đổi tên, trả 409 nếu dự án đã bị xóa. Không tạo queue mồ côi từ tên đã lưu trước khi probe.

Nghiệm thu offline: 385 phương thức unittest, 376 đạt và 9 media opt-in bỏ qua; gồm Chromium thao tác tạo/đổi tên/xóa và phản hồi lỗi. Regression SQLite kiểm tra rollback giữa hai UPDATE, bảo toàn queue/evidence, claim/reservation, tên Unicode có dấu `/`, dự án thiếu/trùng và nhập file đồng thời đổi tên/xóa. Ruff critical, compile, JS syntax và diff check đạt. Không đổi schema và chưa triển khai runtime trong bước này.
