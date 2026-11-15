# G14 — CRUD Profile và bảo toàn liên kết kênh

Tạo Profile local kiểm tra tên thư mục Windows, tên thiết bị, đường dẫn, trùng tên không phân biệt hoa/thường và thư mục đã tồn tại. Không upsert hoặc lấy lại thư mục Chrome cũ một cách ngầm định. Tạo Profile, kênh trống và membership là một transaction; lỗi chỉ dọn thư mục rỗng vừa tạo, không xóa dữ liệu có sẵn.

Cập nhật cho phép proxy/ghi chú, giữ proxy đã che bằng `***`; proxy Antidetect vẫn do Antidetect quản lý. Tên/path/source là định danh bất biến ở API sửa. Có nút tạo Profile local và sửa ghi chú trên giao diện. Tên hiển thị của Profile đồng bộ có dấu `/` vẫn sửa ghi chú/xóa được qua URL mã hóa.

Xóa Profile kiểm tra đích trực tiếp, Profile, kênh đã phân công, nhóm của hàng đợi/Monitor, history, attempts, reservations và approval snapshots trước khi xóa các kênh đi kèm cùng membership trong một transaction. Xóa Kênh sử dụng cùng guard để không bỏ qua bảo vệ qua màn khác. Thư mục Chrome luôn được giữ. Đổi proxy chặn queue đang chạy/cần đối soát và Monitor đang hoạt động.

Kết quả kiểm tra/quét kênh trả về muộn phải khớp binding hiện tại trước khi ghi, tránh tái tạo liên kết của Profile đã xóa hoặc áp dữ liệu quét bằng proxy cũ.

Nghiệm thu offline: full suite 395 phương thức, 386 đạt và 9 media opt-in bỏ qua, gồm Chromium tạo/sửa ghi chú/xóa, chuỗi Unicode và handler an toàn. Sau bổ sung URL cho tên đồng bộ có dấu `/`, cả 10 regression Profile đạt. Bao gồm rollback khi lỗi giữa cập nhật/xóa, giữ thư mục Chrome, liên kết nhóm/lịch sử và kết quả probe trả muộn. Ruff critical, compile, JS syntax, diff check đạt. Không đổi schema; không mở Profile hoặc truy cập YouTube/Antidetect thật.
