# G10 — Validation cấu hình kênh và bộ tùy chọn

Hai nhóm API tạo/sửa kênh và bộ tùy chọn dùng chung schema Pydantic trong `config_validation.py`. Trước sửa, chuỗi `"false"`/`"0"` được coi là bật, dữ liệu sai kiểu có thể gây HTTP 500 hoặc bị biến thành chuỗi, cập nhật chỉ `group_names` bị bỏ qua, và tạo bộ tùy chọn tự chèn hashtag dù người dùng cố ý để trống.

## Hành vi sau sửa

- Cờ chỉ nhận bool, số nguyên 0/1 hoặc chuỗi true/false, 1/0, yes/no, on/off, Có/Không (không phân biệt hoa thường). Giữ null/chuỗi trống là false để tương thích client cũ; không chấp nhận float hoặc container.
- Text phải là string hoặc null để xóa trường tùy chọn. Tên bắt buộc có nội dung, tối đa 200 ký tự; không nhận NUL. Privacy, upload mode, Channel ID khi có, mã category, language và link Affiliate được kiểm tra trước ghi DB. Channel ID trống vẫn được giữ cho kênh đang cấu hình; đây không phải xác nhận đủ điều kiện upload.
- Patch chỉ ghi trường đã gửi. Trường lạ được bỏ qua như trước và không thành cột SQL. Category nhận số nguyên hoặc chuỗi số dương tối đa 999; language có dạng vi/en-US. Kiểm tra định dạng không chứng minh backend Studio đã hỗ trợ capability tương ứng.
- Cập nhật chỉ danh sách nhóm hoạt động; alias nhóm mâu thuẫn, nhóm thiếu hoặc danh sách rỗng bị từ chối. Tên nhóm và memberships nằm trong một giao dịch; lỗi rollback toàn bộ và đóng connection. Giữ trigger chặn sửa kênh đang được worker sử dụng.
- Tạo kênh ở mode API và thay mode/token của kênh API dùng cùng kiểm tra OAuth local. Token để trống khi sửa giữ token cũ; proxy đã che mật khẩu giữ secret cũ. Binding Antidetect vẫn quyết định path/proxy/mobile.
- Ô hashtag/CTA gửi rỗng được giữ rỗng ở DB; form sửa CTA không tự điền lại giá trị mặc định. Giá trị mặc định chỉ dùng khi tạo mà không gửi trường đó. Quy tắc dựng nội dung uploader không đổi.
- Bật Affiliate cần URL HTTP/HTTPS hợp lệ; sửa một phần kiểm tra trạng thái cuối trước commit. Error validation không trả lại raw input chứa token/password.

## Kiểm chứng

Baseline tái hiện các lỗi HTTP/SQLite trước sửa. Sau sửa: 9 test API với 95 subcase; thử rollback cả tạo và sửa sau lỗi membership, rồi xác nhận writer kế tiếp không bị kẹt. Chromium thao tác form tạo/sửa kênh và bộ tùy chọn thật trên API/DB tạm. Full unittest trong Python 3.10 virtualenv sạch: 369 phương thức, 360 đạt, 9 media opt-in bỏ qua. Startup sạch, compile, Ruff critical và JavaScript syntax đạt.

Đọc DB local chỉ để kiểm tra schema: 16 kênh và 3 bộ tùy chọn hiện có đều hợp lệ; không sửa giá trị cờ đã lưu trước đây vì không có bằng chứng để suy ra ý định gốc. Không đổi schema DB, không đăng YouTube thật. Các route project/profile/monitor/preset và chính sách xóa tham chiếu vẫn là những phần tiếp theo của roadmap CRUD.
