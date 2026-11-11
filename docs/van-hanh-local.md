# Vận hành DHB trên Windows

Dùng hướng dẫn này để chạy một lô có giám sát, đối soát kết quả trên YouTube Studio và quyết định khi nào tăng quy mô.

## Trước khi nhận lô

1. Mở Antidetect nếu dùng profile đã lưu ở đó, rồi chạy `START_DHB_YOUTUBE.bat`. Mở dashboard tại `http://127.0.0.1:8686`.
2. Trong **Cài Đặt & Nhật Ký Hệ Thống → Chẩn Đoán Cài Đặt & Worker**, đối chiếu revision và thư mục nguồn với bản đã nghiệm thu. Kiểm tra worker, monitor, ổ đĩa và FFmpeg/FFprobe nếu lô cần render. Nếu có cảnh báo mã đã đổi, chờ worker dừng rồi khởi động lại và kiểm tra revision.
3. Trong **Quản Lý Kênh & Profile Chrome**, kiểm tra kết nối Antidetect. Dùng **Đồng Bộ Antidetect** để lấy profile mới. Dùng **Kiểm Tra** hoặc **Quét Kênh** ở đúng profile để kiểm tra phiên và đồng bộ Channel ID. Một profile có thể chứa nhiều kênh. Chỉ đưa kênh vào lô khi ID và quyền trên Studio đã được xác minh.
4. Kiểm tra **Kiểm tra lượt upload** và các mục **Cần đối soát**. Nếu đang có lô hoặc monitor upload, chờ hoặc dừng đúng công việc đó trước khi mở phiên có giám sát. Giữ máy thức trong thời gian chạy và chuẩn bị bản sao DB đã kiểm tra trước khi cập nhật phiên bản.

## Nạp, chọn đích và chạy

1. Chọn dự án trong **Bảng Điều Khiển & Dự Án**. Bấm **Chọn Tệp Từ Máy**, kéo thả video hoặc dùng **Duyệt File Windows**. Tool tự tích những dòng mới thêm trong dự án hiện tại.
2. Mở **Sửa nhanh video đã chọn**. Sửa tiêu đề, chọn kênh cùng profile và đặt chế độ hiển thị cho mỗi dòng rồi lưu một lần. Với lượt thử, có thể bấm **Đặt tất cả Riêng tư**. Dùng ✏️ trên từng dòng để chỉnh mô tả, lịch và các tùy chọn chi tiết.
3. Kiểm tra các dòng đang tích. Chọn **Xem trước & đăng** cho video đã dựng hoặc **Chạy toàn trình tự động** khi cần tải, render và đăng. Nếu chưa tích dòng nào, giao diện có thể đề nghị chạy tất cả dòng đang hiển thị. Hủy hộp thoại và chọn từng dòng nếu phạm vi đó rộng hơn dự kiến. Khi cần giữ thứ tự lô như Preview, đặt **Số Luồng Chạy Song Song Tối Đa** thành 1 trong Cài đặt trước khi chạy.
4. Đọc Preview của nút chạy vừa chọn. So khớp ID hàng đợi, kênh, profile, chế độ hiển thị, lịch và file. Với **Xem trước & đăng**, một bản render có sẵn có thể được ưu tiên hơn file nguồn. Nếu thay đổi đích, file hoặc cấu hình, lưu rồi xem Preview mới. Xác nhận một lần khi các dòng đều đúng.
5. Theo dõi lô. Mỗi lần chỉ một video giữ lượt upload. Cooldown tối thiểu 30 phút dùng chung giữa các profile, tính từ khi lượt trước kết thúc. Lịch phát hành trên YouTube là thiết lập riêng. Với **Đã đăng** (`Completed`), mở link và kiểm tra ID, kênh, chế độ hiển thị trong Studio bằng phiên có quyền xem.
6. Để kết thúc sớm, bấm **Dừng**. Tool ngừng nhận video mới và chờ lượt upload đã bắt đầu hoàn tất. Đợi thông báo **Đang dừng lô** biến mất và Chẩn đoán báo worker batch đã dừng, active bằng 0. Nếu có monitor, dừng riêng trong tab monitor rồi kiểm tra worker và heartbeat trước khi tắt máy hoặc dịch vụ.

## Xử lý nhanh

| Tình huống | Kiểm tra và xử lý | Dấu hiệu xong |
|---|---|---|
| Mất đăng nhập hoặc profile chưa sẵn sàng | Dùng **Mở Đăng Nhập** ở đúng profile, hoàn thành đăng nhập Google rồi **Kiểm Tra** và mở Studio của kênh đích. Với lỗi kết nối, kiểm tra Antidetect đang chạy. | Profile mở được Studio, đúng kênh, Preview hợp lệ. |
| Thiếu Channel ID | Dùng **Quét Kênh** trên đúng profile. Kiểm tra kênh đích bằng ID vì nhiều kênh có thể nằm trong cùng tài khoản. | Kênh có ID, gắn đúng profile và hiện đúng trong Preview. |
| File không còn hoặc sai file | Đọc dòng file trong Preview. Tìm lại bản đúng hoặc nạp lại file vào dự án và gán lại metadata. Giữ dòng cũ để đối soát nếu có dấu hiệu đã gửi. | Preview chỉ đến bản đúng và phát thử được file. |
| Thiếu dung lượng | Xem dung lượng trong Chẩn đoán. Trong **Dung Lượng & Artifact do Tool Quản Lý**, bấm **Xem trước danh sách** và chỉ dọn mục đã chọn. Giữ file đang được hàng đợi tham chiếu và file cần đối soát. | Ổ đĩa đủ cho file nguồn, đầu ra và phần tạm, lô qua bước kiểm tra dung lượng. |
| Đang cooldown | Bấm **Kiểm tra lượt upload** để xem số giây còn lại. Sau restart, DHB có thể áp dụng lại đủ 30 phút. Để lô chờ theo bộ đếm. | Tool báo đủ khoảng nghỉ hoặc nhận lượt tiếp theo. |
| `recovery_required` | Đối chiếu video và phiên upload trước trong Studio. Sau khi xác nhận phiên cũ đã kết thúc, dùng **Kiểm tra lượt upload** để xác nhận phục hồi. Xử lý dòng `NeedsReview` riêng. | Slot chuyển sang cooldown, nhật ký ghi nhận phục hồi. |
| `NeedsReview` | Tìm theo kênh, tiêu đề, thời điểm và ID nếu có. Khi xác minh đã đăng, dùng ✅ để lưu URL và lý do. Chỉ dùng 🔁 khi đã xác nhận chưa đăng, rồi tạo Preview mới cho riêng dòng đó. Nếu kết quả vẫn mơ hồ, giữ nguyên để đối soát tiếp. | Đã ghi URL vào lịch sử hoặc đã ghi quyết định cho phép gửi lại riêng dòng đó. |
| Studio đổi giao diện hoặc đọc danh sách lỗi | Xem các log `PreUploadSnapshot` và `VerifyingResult`. Nếu không đọc được mốc trước khi gửi sau ba lần thử, lượt được ghi `NotSent` và lô dừng. Nếu đã bắt đầu gửi và kết quả chưa rõ, xử lý theo `NeedsReview`. Ghi lại giai đoạn, mã lỗi, kênh và phiên bản trước khi thử lại. | Đã xác định file chưa gửi hay cần đối soát. Ca thử Riêng tư tiếp theo được Studio xác nhận đúng ID và chế độ. |

## Nhật ký, sao lưu và phục hồi

- Trong **Cài Đặt & Nhật Ký Hệ Thống**, lọc theo phân hệ hoặc tiêu đề rồi bấm **Xuất CSV Log Đang Lọc**. Lưu revision, khoảng thời gian, `run_id`, `queue_id`, `attempt_id`, `phase` và `error_code` trước khi khởi động lại hoặc dọn log. Bảo trì định kỳ giữ khoảng 5.000 dòng gần nhất trong DB.
- Bản sao SQLite tự động nằm trong `data/backups/`. Để sao lưu khi DHB đang chạy, dùng SQLite Backup API hoặc chọn bản tự động đã hoàn tất. Mở bản sao độc lập, chạy `PRAGMA integrity_check` và đối chiếu số dòng queue, lịch sử đăng, attempt, reservation và trạng thái slot. Giữ bản sao DB và profile ở nơi được bảo vệ. CSV hỗ trợ đối soát nhưng không chứa đủ dữ liệu để phục hồi DB.
- Khi cần rollback, dừng nhận việc và đợi worker cùng monitor dừng. Sao lưu DB hiện tại rồi kiểm tra bản mã tương thích với schema đó. Ưu tiên quay về mã đã xác minh và giữ DB mới nhất để bảo toàn lịch sử chống trùng. Nếu phải phục hồi DB, thử bản sao ở thư mục cô lập, đối soát các lượt đăng mới hơn bản sao, lưu DB đang dùng cùng WAL/SHM (nếu có), rồi thay DB khi dịch vụ đã dừng. Giữ WAL/SHM cũ đi cùng bản lưu của chính nó.
- Sau khôi phục, xác minh revision, schema, lịch sử đăng, worker, monitor và slot trước khi nhận lô mới. Tạo Preview cho một video thử để kiểm tra đích, file và chế độ. Upload cũ chưa xác định kết quả vẫn cần đối soát trên Studio.

## Tăng số video và profile

| Mức | Điều kiện |
|---|---|
| Thử có giám sát | Bản chạy và backup đã xác minh. Chọn một hoặc hai video Riêng tư, gán từng kênh, xem Preview và đối chiếu từng kết quả. |
| Chạy lô thường xuyên | Các lượt thử trên nhiều profile tự xác nhận đúng kênh và chế độ. Đã kiểm tra lệnh dừng, cooldown, phục hồi và một phiên dài có công việc thực tế. Bắt đầu theo tải tuần tự đã đo. |
| Tăng quy mô | Ghi nhiều lô liên tiếp với số lượt gửi, số lượt `Completed`, `NeedsReview`, lượt dừng trước khi gửi, RAM trống thấp nhất và dung lượng. Tăng một yếu tố mỗi lần, chẳng hạn thêm một video hoặc một profile đã kiểm tra, rồi đối chiếu trước khi tăng tiếp. |

Tính tỷ lệ `NeedsReview` trên số lượt upload có thể đã gửi file. Ghi riêng `NotSent` và các dòng chưa có lượt gửi. Dừng tăng quy mô khi thấy sai kênh, sai chế độ, đăng trùng, `Completed` thiếu bằng chứng, mất chống trùng sau restart hoặc không xác định được bản chạy. Một lô có N lượt upload cần ít nhất N − 1 khoảng nghỉ 30 phút, cộng thời gian upload và xác minh từng lượt.

## Bảo trì mã

Giữ Python **3.10** như CI. Rà Ruff theo phạm vi cố định để kết quả không phụ thuộc cấu hình cá nhân.

```powershell
ruff check --isolated --target-version py310 --select F821,F822,F823,E9,B006,B023 .
ruff check --isolated --target-version py310 --select B008,BLE001,S110,S112,UP,I,SIM,RUF100 --statistics .
```

| Nhóm | Quyết định |
|---|---|
| Lỗi có thể làm sai kết quả | Ưu tiên lỗi có ca tái hiện, ví dụ ghi `Completed` thiếu bằng chứng hoặc xử lý sai trạng thái upload. Rà `BLE001`, `S110`, `S112` quanh preflight, cập nhật DB và cleanup để xác định mỗi lỗi có được báo và chặn đúng không. Sửa theo từng nhánh và kiểm tra lại ca tái hiện. |
| Thiết kế có chủ đích | `B008` ở khai báo `Body(...)` và `File(...)` là metadata FastAPI. Một số nhánh bắt ngoại lệ cần giữ để báo kết quả chưa xác định hoặc chặn slot. Chỉ thu hẹp nhánh sau khi xác minh các trạng thái lỗi cần giữ. |
| Phong cách | Sắp import (`I001`), đổi kiểu annotation (`UP006`, `UP045`), hoặc bỏ tiền tố f-string thừa (`F541`) có thể xử lý trong thay đổi nhỏ riêng. Kiểm tra tương thích Python 3.10 trước khi autofix. |

Chạy kiểm tra liên quan và CI khi sửa mã. Mức giảm số cảnh báo Ruff chỉ đo dọn mã. Nghiệm thu upload dựa vào Preview, kết quả Studio, lịch sử đăng và khả năng phục hồi.
