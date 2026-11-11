# Kết nối DHB với YouTube Data API qua OAuth 2.0

1. Trong [Google Cloud Console](https://console.cloud.google.com/apis/library/youtube.googleapis.com), chọn dự án, bật **YouTube Data API v3** và hoàn thành **Google Auth Platform**. Nếu ứng dụng là **External** và đang **Testing**, thêm tài khoản Google thực sự có quyền kênh vào **Test users**. Google giới hạn refresh token của cấu hình thử này ở 7 ngày với quyền YouTube. [Tài liệu về thời hạn OAuth](https://developers.google.com/identity/protocols/oauth2#expiration)
2. Trong **Clients**, tạo OAuth Client loại **Desktop app** rồi tải JSON về máy. Giữ file ngoài Git và thư mục chia sẻ.
3. Mở DHB local. Chọn kênh đã có YouTube Channel ID. Trong PowerShell, chạy từ thư mục mã DHB, thay đường dẫn ví dụ và ID dòng kênh:

   ```powershell
   python connect_youtube_oauth.py --client 'C:\Users\admin\Downloads\client_secret_example.json' --channel 66
   ```

4. Chrome hoặc trình duyệt mặc định sẽ mở Google. Chọn đúng tài khoản và kênh, đọc quyền và tự cấp quyền. DHB kiểm tra refresh token thực sự làm mới được và quyền đó trỏ đến đúng Channel ID trước khi lưu. Lệnh trên giữ phương thức đăng hiện tại.
5. Trên bản DHB hỗ trợ chuyển phương thức mà giữ profile, mở **Quản Lý Kênh & Profile Chrome**, sửa đúng dòng kênh, chọn **YouTube Data API v3 (OAuth2)** rồi lưu. Có thể thêm `--activate` ở bước 3 để kết nối và chuyển phương thức trong cùng một lần. Dòng kênh, hàng đợi và lịch sử chống trùng vẫn dùng cùng ID nội bộ.
6. Dùng **Kiểm tra OAuth** để kiểm tra quyền trên Google. Tạo Preview cho một clip thử **Riêng tư** và xem **Phương thức đăng** cùng Channel ID trước khi xác nhận chạy. Sau khi gửi, đối chiếu ID video và chế độ hiển thị trong Studio. Lượt nào chưa rõ kết quả vẫn cần đối soát trước khi quyết định gửi lại.

Việc cấp OAuth không đăng video. Thử nghiệm công khai hoặc hẹn lịch là một lượt chạy riêng trong DHB. [Hướng dẫn chính thức cho ứng dụng desktop](https://developers.google.com/identity/protocols/oauth2/native-app) · [Hướng dẫn upload YouTube Data API](https://developers.google.com/youtube/data/guides/uploading_a_video)
