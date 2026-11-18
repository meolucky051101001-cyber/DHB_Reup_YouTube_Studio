# G7 — Dừng và giới hạn thời gian quét nguồn

Quét metadata URL chạy trong `scan_worker.py` riêng, bao gồm cả iterator playlist lười của yt-dlp. Mỗi nguồn có deadline 300 giây; socket timeout 15 giây, retry hữu hạn, tối đa 1.000 video. Monitor có thể dừng trong lúc extractor chờ mạng. Parent dừng cây tiến trình và thu hồi worker trước khi xóa thư mục job; URL ký được truyền qua file tạm thay vì command line.

API từ chối giới hạn không phải số nguyên 1–1.000 bằng HTTP 400. Quá hạn trả 504; extractor lỗi hoặc kết quả không hợp lệ trả 502. Nếu một nguồn trong lượt quét thất bại, lượt đó không thêm một phần hàng đợi. Hủy monitor là chuyển trạng thái dừng, không ghi thành lỗi extractor. Log lỗi không chứa URL/token hoặc response từ dịch vụ ngoài.

Giữ bộ lọc view/like/comment/Shorts và chuẩn hóa URL video YouTube. Không cắt ngắn URL hay ID quá dài để tránh thay đổi nguồn. Quét file/thư mục local vẫn dùng đường probe hiện tại; không tuyên bố deadline 300 giây cho toàn bộ thư mục local hoặc cả danh sách nhiều URL. API quét thủ công có deadline nhưng chưa có nút hủy riêng.

Kiểm chứng offline trên Windows: 355 unittest, 9 phương thức media opt-in được bỏ qua, không có lỗi; gồm Chromium, worker bị treo thật, tiến trình cháu ghi heartbeat, hủy/timeout/crash/result lỗi, giới hạn playlist và HTTP không chèn một phần dữ liệu. Kiểm thử cây tiến trình chạy ngoài sandbox vì sandbox giới hạn `taskkill /T`. Compile và Ruff critical checks đạt. Không đăng video hoặc bình luận thật.

Triển khai cần có file worker mới cùng các module ứng dụng; restart sau khi cập nhật. Không đổi schema DB. PR được xếp trên G6, chưa tự merge.
