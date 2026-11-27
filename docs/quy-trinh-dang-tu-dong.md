# Quy trình đăng video tự động

Tài liệu này hướng dẫn thao tác trên giao diện sáu màn của DHB Studio. Kết quả kiểm thử và triển khai được ghi riêng theo revision; các bước chuẩn bị dưới đây không thay cho nghiệm thu trên kênh YouTube thật.

## 1. Chọn đúng cách chạy

| Nhu cầu | Cách dùng | Bước bắt đầu công việc |
|---|---|---|
| Đăng ngay các tệp trên máy | **Không render** trong **Video & dự án** | Tích các video rồi bấm **Đăng ngay (N)**, hoặc bấm **Đăng** tại một dòng để chỉ đăng video đó. |
| Có liên kết hoặc nguồn cần tải và dựng | Lô **toàn trình: tải → render → đăng** | Chuẩn bị nguồn, metadata và preset; xem trước rồi xác nhận lô. |
| Muốn tiếp tục phát hiện video mới từ nguồn | **Theo dõi nguồn** (Monitor) | Tạo và lưu luồng, xem trước chính sách/đích, rồi **Xác nhận và bật nguồn**. |

Lô thủ công xử lý tập video đã đưa vào hàng đợi. Monitor chủ động quét nguồn theo chu kỳ và tạo video mới trong dự án `Monitor - <tên luồng>`. Hai cách chạy dùng chung cơ chế chống trùng và lượt upload, nhưng có nút dừng riêng.

**Đăng ngay** dùng kênh, chế độ hiển thị và lịch đã cấu hình cho từng video. Với **Không render**, DHB dùng bản render đã lưu nếu có, hoặc dùng tệp nguồn trên máy khi chưa có bản render. Nếu thiếu tệp đã chọn hoặc cấu hình bị chặn, DHB hiển thị lý do trước khi gửi lô. Chọn **Render rồi đăng** nếu cần dựng lại. Trong **Thao tác khác**, chọn **Xem trước & đăng** để kiểm tra từng dòng trước khi xác nhận.

Trong **Video & dự án**, mở **Thao tác khác → Quy trình đăng tự động** để đi theo hướng dẫn. Chọn chế độ file có sẵn hoặc toàn trình, đọc ảnh chụp phạm vi tại lúc mở hướng dẫn, rồi chọn **Tiếp tục xem trước lô**. Nút này chuyển sang bản xem trước do máy chủ kiểm tra; chưa phải xác nhận gửi video. Chọn hướng Monitor sẽ đưa đến **Theo dõi nguồn** để tạo hoặc kiểm tra luồng, chưa bật luồng.

## 2. Chuẩn bị ở sáu màn

| Màn hình | Kiểm tra hoặc thao tác trước khi chạy |
|---|---|
| **Video & dự án** | Đúng dự án, đúng video và bộ lọc; đủ nguồn/file; kiểm tra đích, hiển thị, lịch và trạng thái từng dòng. |
| **AI & nội dung** | Chỉnh tiêu đề/mô tả/tags nếu cần. Đọc số cập nhật, bỏ qua và lỗi; AI thành công một phần không có nghĩa mọi video đã sẵn sàng. |
| **Render & preset** | Chọn preset và áp dụng đúng phạm vi nếu chạy toàn trình. Lưu preset, áp dụng cấu hình và chạy render là các thao tác riêng. |
| **Kênh & profile** | Xác minh Channel ID, phương thức Studio/API, profile hoặc OAuth; kiểm tra Bộ tùy chọn và Khả năng đăng. |
| **Theo dõi nguồn** | Dùng khi cần quét lặp tự động; kiểm tra nguồn, đích, xử lý, chu kỳ và chính sách metadata trước khi bật. |
| **Hệ thống** | Kiểm tra Chẩn đoán, worker, ổ đĩa, FFmpeg/FFprobe, lượt upload và lỗi cần xử lý. Hoạt động dùng để xem/xuất log; Lưu trữ dùng để xem trước artifact đủ điều kiện dọn. |

Giữ máy chạy và thức trong thời gian tải, render, gửi file hoặc xác minh. Việc lưu cấu hình, nạp nguồn, mở hướng dẫn hay chuyển màn không tự bắt đầu lô thủ công.

Giao diện mới vẫn giữ đủ **209 control nguồn** (`input`/`select`/`textarea`) đúng ID, loại và một lần xuất hiện. Hướng dẫn và bản xem trước chỉ tổ chức cách đọc/thao tác; không thay đổi hợp đồng API. Khi upload, nguồn, output và bằng chứng được worker kiểm tra theo snapshot; không xóa hoặc thay thế file nguồn chỉ vì một lượt đăng hoàn tất.

## 3. Chuẩn bị kênh và bộ tùy chọn

**YouTube Studio:** trong **Kênh & profile → Profile**, kiểm tra đúng phiên đăng nhập; dùng các thao tác đồng bộ/quét kênh có chủ đích để lấy Channel ID. Sau đó kiểm tra dòng kênh ở mục **Kênh**. Đăng nhập thành công chưa chứng minh đã chọn đúng kênh con; dùng Channel ID trong bản xem trước để đối chiếu. Profile Antidetect cần dịch vụ Antidetect đang sẵn sàng.

**YouTube Data API:** dùng [hướng dẫn OAuth](youtube-oauth.md) để kết nối quyền với đúng dòng kênh, kiểm tra OAuth và chọn phương thức API. Token đại diện cho kênh nhận video; tool không chọn một kênh khác thay cho token khi upload. Profile được giữ trong cấu hình không thay thế quyền OAuth.

Mở **Kênh & profile → Bộ tùy chọn** và **Khả năng đăng** trước khi dùng tính năng metadata:

| Tính năng | Cách xử lý hiện tại |
|---|---|
| Danh mục API | Có thể áp dụng; API kiểm tra danh mục trước gửi và đọc lại sau upload. |
| Danh mục Studio | Chỉ tiếp tục khi người vận hành đã chủ động xác nhận giữ thiết lập bên YouTube. Yêu cầu áp danh mục hiện bị chặn. |
| Ngôn ngữ nói trong video, cấm remix, 18+, Công chiếu, ghim bình luận | Yêu cầu chưa được hỗ trợ sẽ chặn đăng. Lưu giá trị không có nghĩa đã áp dụng; giữ yêu cầu để chờ hỗ trợ hoặc chủ động sửa rồi xem trước lại. |
| Thumbnail | Có thao tác đặt ảnh; lỗi bước phụ được báo dưới dạng cảnh báo, không tự đăng lại video. Studio chưa có bước đọc lại ảnh đã lưu. |

Không đổi backend chỉ để vượt một blocker mà backend mới cũng chưa hỗ trợ. Cấu hình cũ chưa xác nhận metadata cần được người vận hành xem và xác nhận; tool không suy ra ý định từ giá trị mặc định hoặc tên bộ tùy chọn.

## 4. Chuẩn bị nguồn, phạm vi và lịch

1. Trong **Video & dự án**, chọn dự án rồi mở **Thêm video**. Đặt cấu hình nhập trước khi chọn tệp; tệp được nạp sau khi chọn. Liên kết/thư mục cần quét nguồn để tạo các dòng hàng đợi. Nạp thành công chưa chạy pipeline.
2. Chọn video cần xử lý. Tập **đã chọn** gồm cả dòng bị bộ lọc ẩn; đọc số dòng ẩn. **Bỏ chọn dòng hiển thị** chỉ bỏ các dòng hiện tại; dùng **Bỏ toàn bộ lựa chọn** nếu muốn chọn lại từ đầu.
3. Nếu không chọn thủ công, tác vụ lô có thể dùng các dòng khớp bộ lọc hiện tại sau xác nhận phạm vi. Đừng suy ra bộ lọc giới hạn một tập chọn đang ẩn. Hướng dẫn chụp **scope snapshot** để bạn đọc lại; bản xem trước máy chủ vẫn là bước xác định file/đích cuối cùng và có thể từ chối snapshot cũ.
4. Tích các video rồi chọn **Chọn profile & kênh** để đặt kênh chung. Để đổi một dòng, bấm tên kênh trong bảng. Bấm tên video để xem chi tiết. **Thao tác khác** có **Sửa nhanh** và **Gán cấu hình hàng loạt** để chỉnh thêm thuộc tính.
5. Chọn hiển thị Riêng tư, Không công khai, Công khai hoặc Lên lịch. Nếu chọn Lên lịch, nhập giờ tương lai đủ xa để kịp tải/render, upload, xác minh và chờ các lượt trước.

Ba loại thời gian có ý nghĩa khác nhau:

| Thời gian | Ý nghĩa |
|---|---|
| Lúc bấm xác nhận chạy | Tool bắt đầu xử lý lô và đợi lượt upload nếu cần. Không có bộ hẹn giờ tự bật lô thủ công vào `schedule_time`. |
| Cooldown upload | Mỗi lần chỉ một video giữ lượt upload. Lô bấm chạy upload khi tệp sẵn sàng và lượt upload trống. Monitor và các lượt tự động chờ tối thiểu **30 phút sau khi lượt trước kết thúc**. |
| Lịch phát hành | Giờ YouTube chuyển video sang trạng thái đã hẹn. Với API, video được tải lên ở trạng thái private kèm `publishAt`; việc đặt lịch không chờ đến giờ hẹn mới gửi file. |

Ô ngày giờ hiện gửi giá trị địa phương không kèm múi giờ. Máy chạy DHB diễn giải giá trị đó theo múi giờ hệ điều hành; gợi ý cạnh ô nhập chỉ phản ánh múi giờ trình duyệt, không thực hiện chuyển đổi. Khi trình duyệt và máy DHB khác múi giờ, nhập giờ tương ứng trên máy DHB và đối chiếu lại giờ trong YouTube Studio. Không coi gợi ý của trình duyệt là cam kết lịch đã chuyển múi giờ.

Lệnh **Lên lịch giãn cách** lưu lịch phát hành cho các dòng; nó không bắt đầu upload. Khi nhiều video chạy song song, thứ tự nhận lượt upload không được bảo đảm giống thứ tự bảng. Nếu cần thứ tự tuần tự như bản xem trước, đặt số luồng pipeline thành 1 trước khi chạy.

## 5. Xem trước và xác nhận lô

1. Mở **Thao tác khác → Quy trình đăng tự động**, chọn file có sẵn hoặc toàn trình rồi **Tiếp tục xem trước lô**. Có thể dùng trực tiếp **Xem trước & đăng** hoặc **Chạy toàn trình** trong nhóm Xử lý để đến cùng luồng xem trước.
2. Đọc từng dòng trong **Xem trước lô đăng YouTube**: ID hàng đợi, file sẽ gửi, kênh/profile/phương thức, hiển thị, lịch, metadata hiệu lực, cảnh báo và lý do bị chặn. Chế độ file có sẵn ưu tiên đường dẫn bản render đã lưu; chỉ dùng file nguồn khi chưa có đường dẫn bản render. Nếu đường dẫn đã lưu không còn file, xem trước sẽ chặn thay vì tự chuyển sang file nguồn. Luôn kiểm tra file được nêu.
3. Nếu có blocker, đóng bản xem trước, sửa dữ liệu rồi xem trước lại. Tăng worker, sửa nhãn hoặc đổi bộ lọc không bỏ qua điều kiện máy chủ. Video Completed hoặc có URL đã đăng cần được cho phép đăng lại riêng từng dòng; NeedsReview phải đối soát trước.
4. Chỉ xác nhận chạy khi toàn bộ phạm vi và cấu hình đúng. Sau xác nhận, tool kiểm tra lại snapshot; đổi kênh, file, bộ tùy chọn hoặc capability có thể làm bản xem trước hết hiệu lực và yêu cầu xem trước lại.
5. Theo dõi từng bước tải/render/upload trong hàng đợi. Mở **Thao tác khác → Kiểm tra lượt đăng** khi cần biết đang có video giữ slot, đang cooldown hay cần phục hồi. Nếu lô đã chạy, theo dõi trạng thái hiện tại.

## 6. Bật nguồn theo dõi tự động

1. Trong **Theo dõi nguồn**, chọn **Tạo luồng**. Điền tên, mỗi dòng một nguồn, kênh cụ thể hoặc nhóm kênh, bộ tùy chọn, cấu hình render/AI, metadata và chu kỳ quét. Với nhóm, kiểm tra mọi kênh có thể nhận video.
2. Lưu cấu hình. Luồng mới được tạo ở trạng thái dừng; lưu không tự quét hoặc đăng.
3. Bấm **Bật nguồn** ở đúng luồng để mở **Xem trước nguồn tự động**. Đọc các đích có thể được chọn và metadata hiệu lực: hiển thị, trẻ em, nhãn AI, danh mục/ngôn ngữ/remix, nguồn kế thừa và các blocker. Phạm vi Monitor là nguồn và các video mới sẽ được phát hiện, không phải tập video đang chọn ở màn Video & dự án.
4. Nếu bị chặn, sửa cấu hình rồi xem trước lại. Nếu đúng, chọn **Xác nhận và bật nguồn**. Máy chủ xác nhận policy/revision trước khi khởi chạy. Hủy bản xem trước không bật luồng.
5. Theo dõi lifecycle, thời điểm cập nhật và log. Luồng quét nguồn, chống trùng, tạo hàng đợi, xử lý rồi dùng slot upload chung. Chu kỳ quét, khoảng nghỉ riêng của Monitor và cooldown upload chung là các khoảng chờ riêng; tổng thời gian có thể dài hơn chu kỳ đã nhập.
6. Sửa cấu hình kênh/nhóm/bộ tùy chọn liên quan có thể làm policy cũ hết hiệu lực. Dừng luồng, chờ worker kết thúc, xem trước và xác nhận lại trước khi bật. Không xác nhận khi luồng còn Starting/Stopping hoặc worker cũ chưa thoát.

Video lỗi hoặc dừng được giữ trong hàng đợi để xử lý; không có cam kết tự retry mọi video ở chu kỳ sau. Việc khôi phục một worker Monitor đã chết khác với cho phép gửi lại một lượt upload chưa rõ kết quả. Không xóa dòng lỗi/NeedsReview chỉ để buộc nguồn được nhận lại.

## 7. Đọc kết quả và dừng đúng công việc

| Trạng thái | Việc cần làm |
|---|---|
| Pending / đang tải / đang render | Đợi hoặc kiểm tra lỗi của đúng bước; chưa có nghĩa đã gửi YouTube. |
| Chờ lượt / chờ giãn cách | Đọc trạng thái slot. Giữ lô chờ hoặc dừng trước lượt kế tiếp. |
| Completed | Tool đã hoàn tất lượt theo điều kiện xác minh của phương thức đó. Xem kèm hiển thị/lịch: Riêng tư, Không công khai và Đã lên lịch đều có thể Completed; không đồng nghĩa đang Công khai. Mở link để đối chiếu đúng ID/kênh. |
| Completed có cảnh báo | Đọc cảnh báo bước phụ như thumbnail. Không đăng lại toàn video chỉ để xử lý cảnh báo. |
| Error / NotSent | Đọc phase và mã lỗi trước khi sửa, tạo bản xem trước mới cho dòng phù hợp. Không suy ra mọi Error đều chưa gửi file. |
| NeedsReview / Cần đối soát | Giữ dòng và file/bằng chứng. Xác minh trên đúng kênh trước khi cho phép đăng lại. |

Nút **Dừng lô hiện tại** dừng pipeline batch trên toàn phạm vi lô, không chỉ dòng đang chọn. Tool dừng nhận công việc kế tiếp và chờ upload đã bắt đầu hoàn tất; chưa coi bấm Dừng là mọi tiến trình đã thoát. Trong **Theo dõi nguồn**, bấm **Dừng** riêng cho từng Monitor cần dừng. Đợi lifecycle Stopped và kiểm tra **Hệ thống → Chẩn đoán** không còn worker liên quan trước khi tắt ứng dụng/máy. Yêu cầu dừng batch không thay cho dừng Monitor.

## 8. Đối soát, phục hồi và khởi động lại

Với NeedsReview, mở **Đối soát → Xem bằng chứng**. `received` chỉ nói đã nhận video ID; `verified`, `mismatch` và `unknown` nói kết quả xác minh metadata. Không có bằng chứng ID cũng không chứng minh file chưa từng được gửi.

- Đã xác minh video tồn tại trên đúng kênh: dùng nhánh **Xác nhận đã đăng**. Nếu lượt đăng đã có receipt Video ID tự động, hộp thoại tự khóa đúng URL video đó và cho phép bấm **Kiểm tra lại trên YouTube API (chỉ đọc)** để xác minh trạng thái xử lý/quyền riêng tư mà không upload lại file. Nếu lượt đăng cũ không có receipt tự động, người vận hành phải tích xác nhận rõ đã tự đối chiếu đúng video và kênh trên YouTube Studio.
- Đã xác minh chưa đăng: dùng nhánh **Xác nhận chưa đăng**, tích xác nhận và ghi lý do rồi cho phép đăng lại riêng dòng đó. Quyết định này không tự chạy; chọn dòng, tạo bản xem trước mới và xác nhận lô riêng.
- Chưa rõ: hủy quyết định, giữ NeedsReview và tiếp tục đối soát. Không dùng retry hàng loạt hoặc sửa status trong DB để mở khóa.
- Trường hợp YouTube Data API xử lý xong video ở chế độ không gắn nhãn AI (`altered_content=false`) nhưng lược bỏ trường `containsSyntheticMedia` trong phản hồi đọc lại: tool ghi nhận `Completed` kèm cảnh báo hậu kiểm `API_VIDEO_AI_OMITTED` và đánh dấu `metadata_state=partial` trong bằng chứng v2 để không làm đứt gãy lịch tự động; nếu `altered_content=true` hoặc giá trị đọc lại sai lệch thì vẫn giữ `NeedsReview`.

Nếu slot là `recovery_required`, kiểm tra phiên upload cũ trong Studio và xác nhận phiên đó đã kết thúc qua **Kiểm tra lượt đăng**. Xác nhận phục hồi slot đưa nó về cooldown; không giải quyết thay quyết định NeedsReview và không tự gửi lại video.

Khởi động DHB không tự chạy hàng đợi thủ công; cờ cũ tự chạy batch lúc mở tool bị tắt. Monitor đã lưu ở trạng thái dừng không tự bật vì bạn mở hướng dẫn hay tải lại trang. Monitor trước đó đã được bật và còn cờ muốn chạy có thể được supervisor khôi phục sau khi worker chết hoặc ứng dụng khởi động lại, theo policy hiện hành. Muốn ngừng tự động, dừng Monitor và chờ xác nhận dừng bền vững trước khi thoát.

Sau restart, lượt upload đang dở có thể được giữ để đối soát và khoảng nghỉ của lượt tự động có thể tính lại đủ 30 phút. Kiểm tra revision, worker, slot và NeedsReview trước khi nhận lô mới. Khi quay lại bản mã trước, sao lưu DB hiện tại và giữ lịch sử/evidence mới; không đè bằng DB cũ chỉ để đổi giao diện.

## 9. Lần vận hành đầu và tăng quy mô

Bắt đầu bằng clip test có thể đối chiếu, gán một kênh và chọn Riêng tư. Kiểm tra bản xem trước rồi ghi ID/URL, kênh, hiển thị, metadata và cảnh báo sau lượt gửi. Kiểm chứng riêng Studio và API nếu sẽ dùng cả hai. Lượt hẹn giờ cần đối chiếu lịch trong Studio; Monitor cần kiểm tra quét lại không tạo lượt trùng và nút dừng hoạt động đúng.

Chỉ tăng số video/profile sau khi đã kiểm tra đúng đích, kết quả, cooldown, dừng và phục hồi. Ghi riêng số lượt đã gửi, NotSent, Completed và NeedsReview; một phiên soak offline không chứng minh hoạt động YouTube thật hay độ sẵn sàng 24/7.

## 10. Lịch đăng Private định kỳ đóng gói (`scheduled_upload_runner.py`)

Khi cần đăng từng video Private theo chu kỳ nhiều ngày (tối thiểu 72 giờ/video) mà không dùng `schedule_time` công khai của YouTube:

1. **Dữ liệu vận hành tách rời mã nguồn:** `manifest.json`, `state.json` và `release.json` nằm trong `data/scheduled_uploads/default` của thư mục runtime (hoặc `DHB_SCHEDULE_STATE_DIR`), không đặt trong thư mục review hay Git.
2. **Quan sát kết quả cuối:** Runner chỉ gửi tối đa một video mỗi kỳ, lưu bền vững trạng thái `submitting` trước khi gọi `POST /api/pipeline/start`, sau đó theo dõi kết quả cuối (`completed`, `needs_review`, `error`, `missed`, `finished`) mà không bao giờ tự gửi lại một lượt chưa rõ kết quả.
3. **Hiển thị trên giao diện:** Trạng thái lịch, tiến độ hoàn tất, ID video kế tiếp và cảnh báo cần đối soát được hiển thị trong **Quy trình đăng tự động** và **Hệ thống → Chẩn đoán** (`GET /api/scheduled-uploads/status`).
4. **Windows Scheduled Task (`scheduled_upload_task.py`):** Chạy dưới phiên đăng nhập hiện tại (`InteractiveToken`), hỗ trợ sao lưu XML cũ trước khi cập nhật và khôi phục khi rollback mà không reset `next_index` hay mốc thời gian 72 giờ.

Tài liệu liên quan: [hướng dẫn giao diện](UI_UX_2026_11.md), [vận hành/backup/phục hồi](van-hanh-local.md), [kết nối OAuth](youtube-oauth.md), [quy trình release](G22_RELEASE.md).
