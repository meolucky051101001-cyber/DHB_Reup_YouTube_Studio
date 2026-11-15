# Kế hoạch G17–G22: metadata đăng video và nghiệm thu offline

Ngày lập: 15/11/2026. Trạng thái: **G17 đã triển khai và nghiệm thu offline trong nhánh phát triển; G18–G22 chưa triển khai**. Xem [biên bản G17](G17_UPLOAD_CAPABILITIES.md). Tiếp nối G13–G16 đã hoàn tất CRUD Dự án, Profile và Render Preset; runtime chưa đổi.

## 1. Mục tiêu và phạm vi

Mốc tiếp theo là bảo đảm **cấu hình người dùng đã chọn được hiển thị đúng trước khi chạy, được backend thực hiện và có bằng chứng đọc lại**. Ưu tiên danh mục YouTube, ngôn ngữ video và quyền remix; đây là nền tảng trước khi tăng quy mô đăng hàng loạt.

Phạm vi nghiệm thu hiện được người dùng chọn là **offline và triển khai local**. Đợt này dùng SQLite tạm/bản sao, HTTP giả lập, DOM fixture, Chromium local và media tổng hợp. Không mở Profile thật, kết nối Antidetect thật, đăng video/bình luận, sửa video đã đăng hoặc tự merge PR. Các PR được commit/push riêng sau mỗi phần đạt nghiệm thu.

Có thể hoàn thành logic, kiểm thử offline, cơ chế chặn an toàn và bản local mà chưa chứng minh tương thích YouTube thực tế. Tính năng Studio mới thiếu bằng chứng giao diện hiện hành giữ tắt mặc định và mang trạng thái “đã kiểm thử offline; chưa nghiệm thu thực tế”. Không dùng kết quả fixture để đánh dấu hỗ trợ đầy đủ.

Không gộp vào đợt này: ghim bình luận, mở rộng upload song song, thay đổi cooldown 30 phút, nâng hàng loạt dependency, refactor toàn bộ app/pipeline, tự phục hồi dữ liệu thiếu hoặc nâng cấp độc lập repo Antidetect.

## 2. Baseline đã có

| Hạng mục | Baseline dùng để lập kế hoạch |
|---|---|
| Checkout phát triển | `_pr_fix_studio`, commit `eebf9ad`; thay đổi code cuối là `74005a4` |
| Bản local đã nghiệm thu | Build `2026.11.15-crud`, revision `74005a4`, xem `G16_LOCAL_CRUD_ROLLOUT.md` |
| PR đã hoàn thành | #19 Dự án, #20 Profile, #21 Preset, #22 biên bản triển khai; đang mở, không tự merge |
| Test cuối G16 | 404 phương thức: 395 đạt, 9 media opt-in bỏ qua trong lượt tổng; CI chạy media riêng và đạt |
| Dữ liệu đã đối chiếu | 15 bảng; 12 dự án, 16 Profile, 16 kênh, 34 queue; #212 vẫn NeedsReview |
| Công việc đã có test | Claim SQLite thật với 100 item, giới hạn worker, crash/restart, monitor start/stop, upload-slot, cảnh báo thumbnail, migration/restore, cài sạch |
| Hạn chế hiện hữu | File #193–196 thiếu; Channel ID #1–7 thiếu; các hàng Completed thiếu file không phải yêu cầu upload lại |

Số liệu trên là biên bản baseline, không phải số liệu được phép ghi cứng vào script triển khai mới. Trước triển khai phải đọc lại commit, trạng thái runtime, dữ liệu và PR base hiện tại. PID, port, tên nhánh rollback phải được xác minh tại thời điểm chạy.

## 3. Khoảng trống xác nhận từ mã nguồn

| Khu vực | Hiện trạng | Việc cần làm |
|---|---|---|
| `youtube_uploader.py:151`, `build_final_metadata` | Category: option → channel → `22`; chưa trả language/remix | Chuẩn hóa metadata cuối và nguồn từng giá trị |
| API payload | Có `snippet.categoryId` | Giữ hành vi hiện tại, thêm xác minh category |
| `youtube_uploader.py:1569`, `_verify_api_video` | Đọc `status,processingDetails`; chưa đọc `snippet` | So sánh category/ngôn ngữ theo đúng video ID |
| Studio uploader | Có xử lý audience/AI/tags; chưa có category/language/remix setter và readback | Adapter nhỏ, mapping có căn cứ, xác minh trước/sau lưu |
| `youtube_uploader.py:3883`, preflight | Chặn pin; API chặn age/premiere | Một nguồn quy tắc capability cho preview/worker/adapter |
| `youtube_uploader.py:3918`, capabilities | Chỉ pin và thumbnail | Ma trận chi tiết theo backend và mức bằng chứng |
| `config_validation.py:112` | Lưu language mặc định `vi`, remix mặc định `0` | Phân biệt dữ liệu cũ với yêu cầu áp dụng có chủ ý |
| Options UI | Có category; chưa có control language/remix | Control có trạng thái giữ nguyên/kế thừa/áp dụng rõ ràng |
| `upload_approval.py:53` | Schema 2 đã hash toàn bộ options và các trường channel liên quan | Giữ bảo vệ đã có; thêm giá trị hiệu lực/capability revision nếu cần |
| `monitor_policy.py:7` | Policy có visibility, audience, AI; revision đã hash options/bindings | Mở rộng preview và kiểm tra capability, không mất semantics policy cũ |
| `pipeline_manager.py`, preview | Chưa đưa category/language/remix hiệu lực ra UI | Hiển thị theo video và kênh thực sự được chọn |
| `validate_review_release.py:13` | Rehearsal mặc định chỉ 6 bảng | Chuẩn hóa coverage 15 bảng đã dùng trong script G16 riêng |

Một điểm cần đưa vào cùng capability registry: Studio đang chặn tuổi 18+ ở adapter, trong khi common preflight chưa báo sớm trường hợp này. Không hiểu câu thông báo “chuyển sang Studio” là bằng chứng Studio đã hỗ trợ. Phải đối chiếu mọi guard hiện có trước khi công bố ma trận.

## 4. Quyết định thiết kế xuyên suốt

### 4.1. Một hợp đồng metadata cho mọi đường chạy

Resolver dùng chung trả về giá trị hiệu lực, nguồn (`option`, `channel`, fallback, lựa chọn đã xác nhận), ý định áp dụng, backend, mức hỗ trợ và lý do chặn. Preview, worker, uploader gọi trực tiếp và Monitor đều dùng hợp đồng này. Không để frontend tự suy ra một quyết định khác.

Category giữ thứ tự ưu tiên đang có. Chưa thêm override riêng cho từng video trong gói này để tránh mở rộng schema và UI không cần thiết. Ngôn ngữ mới được đặt tên rõ là **ngôn ngữ nói trong video**; ngôn ngữ tiêu đề/mô tả là khái niệm khác và chưa được tự động điền cùng giá trị.

Phân biệt ba trạng thái: không có yêu cầu thay đổi; yêu cầu đặt một giá trị cụ thể; dữ liệu cũ chưa xác nhận ý định. `false`, `0`, chuỗi rỗng và không có trường không được gộp bằng phép kiểm tra truthiness.

### 4.2. Dữ liệu cũ và migration

- Không tự biến `language='vi'` trong dữ liệu cũ thành thao tác đặt ngôn ngữ cho cả lô. Dữ liệu thiếu provenance được báo “chưa xác nhận”, giữ giá trị gốc.
- `disable_shorts_remix=0` cũ không được suy ra là “cho phép cả audio và video”. Giá trị `1` là yêu cầu hạn chế: nếu backend chưa thực hiện được thì chặn upload có lý do, không bỏ qua.
- Category qua API tiếp tục hành vi hiện tại. Category ở Studio đang chỉ được lưu: trước khi kích hoạt mới phải hiển thị rõ trong preview và cần preview/xác nhận mới; không để default `22` khiến mọi job Studio bị chặn mà không có bước chuyển tiếp.
- Lựa chọn chuyển tiếp cho Studio là người dùng chủ động chấp nhận giữ thiết lập hiện có bên YouTube, hoặc yêu cầu áp category và chấp nhận chỉ chạy khi capability sẵn sàng. Không tự chọn thay người dùng.
- Schema tối thiểu dự kiến: `metadata_intent_json` versioned trong options và enum `studio_category_mode` tại channel (`legacy_unconfirmed`, `keep`, `apply`). Options có lựa chọn category `inherit`, `keep`, `apply`; inherit lấy ý định từ channel, kể cả khi job không có options. Giá trị category vẫn theo option → channel → fallback hiện hữu; ý định áp dụng resolve riêng, không suy ra từ giá trị. Chưa thêm override queue hoặc ngôn ngữ/remix ở channel trong gói này.
- JSON `{}` ở options và enum legacy ở channel biểu thị chưa xác nhận. Không backfill ngôn ngữ/remix vào queue cũ. Tên module/cột là dự kiến tới khi G18 chốt migration.
- JSON đã xác nhận là nguồn canonical cho lựa chọn mới; field cũ được giữ để đọc/export và tương thích. Nếu cần mirror giá trị sang field cũ, cập nhật trong cùng transaction. Request gửi cả hai nhưng mâu thuẫn phải bị từ chối, không tự chọn một bên.
- Bản ghi chưa có canonical intent có thể lưu field legacy mà không bật tính năng, trả trạng thái cần xác nhận. Khi đã có canonical intent, client cũ sửa field liên quan mà không gửi intent tương thích phải nhận 409 trước khi ghi; yêu cầu tải/lưu lại bằng client mới. Request gửi cả legacy và canonical mâu thuẫn nhận 400 nguyên tử. Sửa tên/ghi chú không làm mất intent.
- Schema stamp và approval schema được version hóa có chủ đích. Không cập nhật lại snapshot cũ để giả vờ người dùng đã duyệt dữ liệu mới.

### 4.3. Mức hỗ trợ và việc bật tính năng

Registry tách các thuộc tính: lưu cấu hình; tạo payload/thao tác; đọc lại; kiểm thử offline; nghiệm thu thực tế; được bật ở build đang chạy. Giữ các khóa API capability cũ khi bổ sung schema mới để client cũ không hỏng.

Capability chưa bật phải chặn yêu cầu áp dụng rõ ràng trước side effect, với cách xử lý cụ thể. Các bước download/render-only vẫn dùng được; không ép người dùng có OAuth/Studio capability chỉ để render file.

Language API mới và adapter Studio mới giữ tắt mặc định ở runtime trong phạm vi offline nếu chưa có bằng chứng đủ để bật. Giữ hoạt động API category đã có, bổ sung đọc lại. Khi một tính năng thay đổi trạng thái bật/tắt, approval trước đó không còn hợp lệ nếu phụ thuộc tính năng đó.

Không ánh xạ remix sang `embeddable`, license, quyền comment hoặc field có tên gần giống. Pin comment tiếp tục chưa hỗ trợ. Thumbnail là bước phụ: có thao tác không có nghĩa đã xác minh mọi trường hợp; lỗi thumbnail không làm mất kết quả chính đã xác minh.

### 4.4. Sau khi đã gửi video

Metadata bắt buộc không khớp hoặc không đọc được sau gửi không được thành Completed. Phải giữ video ID/URL đã nhận, backend/kênh, attempt, thời điểm, expected/observed và lý do chưa chắc chắn; giữ reservation và chuyển NeedsReview. Không tự upload lại hoặc xóa bản nháp/video để làm sạch lỗi.

Video ID quan sát được chưa chứng minh thành công: dùng evidence riêng với trạng thái xác minh, không gán `published_url` như một kết quả đã xác nhận nếu các chỗ đọc field đó có thể coi là Completed. Xem lại `_verify_api_video` và propagation exception vì hiện lỗi xác minh có thể xảy ra sau khi đã có video ID.

Đề xuất lưu evidence dạng JSON có version ở attempt, chỉ chứa ID/URL chuẩn hóa, metadata không nhạy cảm và mã lỗi; không lưu OAuth, cookie, raw HTTP headers hoặc proxy credentials. Các lỗi trước gửi chỉ được đánh dấu NotSent khi có bằng chứng chưa gửi.

Persist video ID ngay khi adapter nhận response xác nhận ID, trước gọi readback, bằng callback/sink ghi vào đúng attempt đang được claim. Chỉ mang ID trong exception cuối không bảo vệ được cửa sổ crash trước exception. Nếu chết trước khi nhận ID thì giữ trạng thái chưa rõ và reservation; không dựng một video ID chưa từng quan sát.

## 5. Thứ tự thực hiện và chia PR

Chuỗi phụ thuộc: **G17 → G18 → G19 → G20 → G21 → G22**. G22a về công cụ nghiệm thu có thể làm sớm sau G18; phải hoàn tất trước mọi rollout cần migration. Không rollout từng commit thay đổi semantics khi G18 chưa xử lý legacy/approval.

| Mốc | Kết quả chính | PR dự kiến | Ưu tiên |
|---|---|---|---|
| G17 | Baseline, audit, hợp đồng capability và guard nhất quán | 1 | P1 |
| G18 | Ý định metadata, migration, preview/approval/Monitor | 1–2 | P1 |
| G19 | API category readback, language có điều kiện, evidence bền vững | 1–2 | P1 |
| G20 | Studio category có setter/readback và feature gate | 1 | P1/P2 |
| G21a | Studio ngôn ngữ video | 1 | P2 |
| G21b | Quyết định/remix adapter hoặc chặn có hướng xử lý | 1 | P2 |
| G22a | Công cụ rehearsal 15 bảng và manifest dùng chung | 1 | P1 cho rollout |
| G22b | Hồi quy tích hợp, soak offline, rollout local và biên bản | 1–2 | P1 |

Số PR thực tế phụ thuộc độ độc lập và kích thước diff; không gộp migration, adapter Studio và nâng dependency vào một PR lớn. Mỗi PR có base chính xác từ phần trước; nếu stack đã được merge trong thời gian làm, kiểm tra lại base trước khi tạo nhánh.

## 6. G17 — Baseline và capability dùng chung

**Công việc**

1. Ghi manifest commit/build, Python, dependency, browser, FFmpeg/ffprobe và schema. Xác nhận checkout sạch trước khi thay đổi.
2. Lập ma trận cả hai backend cho category, video language, remix và các guard đang có: age, premiere, pin, thumbnail. Mỗi ô phải trỏ đến code/test hoặc ghi chưa có bằng chứng.
3. Viết audit chỉ đọc cho options/kênh/job/Monitor: có language mặc định, remix=1, category khác fallback, backend và số job sẽ bị ảnh hưởng bởi guard mới. Báo cáo tổng hợp có thể vào PR; dữ liệu vận hành chi tiết ở ngoài Git.
4. Tái hiện đúng lỗi hiện tại bằng test: language/remix lưu được nhưng không vào metadata; category API chưa readback; unsupported intent không chặn sớm.
5. Tạo registry và quyết định preflight dùng chung; phân biệt mức bằng chứng với việc bật tính năng. Guard lỗi configuration có mã ổn định để UI không phải phân tích câu tiếng Việt.
6. Bổ sung `/api/upload-capabilities` theo cách tương thích; UI hiển thị lý do và phạm vi, không chỉ “hỗ trợ/không hỗ trợ”.
7. Hợp nhất thông báo tuổi 18+/premiere với guard thực tế; không trỏ người dùng sang backend cũng đang chặn.

**Điều kiện đạt**: ma trận không có ô “supported” thiếu căn cứ; cùng đầu vào cho cùng quyết định ở preview/worker/direct call; yêu cầu chưa hỗ trợ bị chặn trước mở browser/khởi tạo resumable; download/render-only không bị ảnh hưởng. Audit không sửa DB và không làm lộ secret. Commit/PR G17; chưa đổi runtime.

## 7. G18 — Legacy intent, preview, approval và Monitor

**Công việc**

1. Chốt JSON schema và compatibility của field cũ như mục 4.2; validator từ chối enum sai, cấu trúc thừa/mâu thuẫn và boolean không hợp lệ.
2. Additive migration trong transaction, stamp sau khi hoàn tất. Chạy hai lần phải không thay dữ liệu lần hai; schema mới hơn bị từ chối rõ ràng.
3. Thêm control options cho “giữ thiết lập bên YouTube”, “đặt ngôn ngữ video”, yêu cầu remix đã được hỗ trợ; control category Studio ở channel cho job không có options/kế thừa. Hiển thị giá trị legacy để xem lại; sửa ghi chú/tên không tự xác nhận metadata.
4. Resolver trả giá trị và nguồn. API language chỉ dùng ý định mới đã xác nhận; chưa tự diễn giải field `language` cũ.
5. Preview batch hiển thị theo từng video/kênh: backend, category và nguồn, ngôn ngữ video/giữ nguyên, remix/giữ nguyên, cảnh báo hoặc lý do chặn. Nhóm có nhiều backend phải kiểm tra theo phân phối thực tế.
6. Approval snapshot ghi metadata hiệu lực và revision capability ngoài các hash đã có. Đổi options, backend, group membership hoặc gate làm stale; không bypass khi gọi start trực tiếp.
7. Monitor dùng cùng resolver cho mọi kênh ứng viên. Giữ inheritance visibility/audience/AI hiện có; policy cũ không tự trở thành đã xác nhận các trường mới. Chỉ yêu cầu xác nhận lại phần bị thay đổi ngữ nghĩa.
8. Queue cũ giữ nguyên ID/paths/attempt/history và policy envelope. UI cần nêu rõ hàng nào phải preview lại; không bulk rewrite hoặc tự chạy Monitor.
9. Viết hướng dẫn client: field được giữ, field mới, lỗi 400/409, nguồn revision và cách xác nhận lại.

**Điều kiện đạt**: fixture legacy `vi/0`, remix=1, explicit false, không có trường, chuỗi rỗng đều có kết quả được định nghĩa; mọi giá trị cũ giữ nguyên qua migration. Job không có options vẫn lưu/resolve lựa chọn category Studio. Client cũ không thể làm field hiển thị khác canonical intent. Chuyển canonical disable sang keep phải chủ động, nguyên tử và làm stale approval. Old approval stale đúng lúc; không invalidate do sửa field hiển thị không liên quan nếu hợp đồng không yêu cầu. Nhóm trộn API/Studio không áp metadata lẫn nhau. Chromium thao tác thật với API local; rollback khi cập nhật options thất bại giữ toàn bộ dữ liệu trước đó.

## 8. G19 — API readback và evidence khi kết quả không chắc chắn

**Công việc**

1. Đối chiếu tài liệu YouTube Data API chính thức ở lúc triển khai; ghi URL/ngày đọc/phạm vi field. Đây là đọc tài liệu công khai, không dùng credential hoặc gọi kênh thật.
2. Category: thêm `snippet` vào readback, so sánh `categoryId` theo đúng ID vừa tạo. Tiếp tục kiểm tra processing, visibility, audience, AI và schedule hiện hữu.
3. Phân biệt cú pháp category 1–999 với category thực sự assignable. Khi upload thật được người dùng khởi chạy, xác minh catalog/khả dụng trước khởi tạo phiên upload nếu cần; offline dùng catalog fixture có nguồn, không đoán ID.
4. Ngôn ngữ nói trong video dự kiến dùng `snippet.defaultAudioLanguage`, sau đối chiếu hợp đồng API. `defaultLanguage` cho tiêu đề/mô tả để ngoài gói này. Chuẩn hóa tag theo quy tắc được ghi rõ, không tự quy mọi `vi-*` thành `vi`.
5. Payload chỉ gửi ngôn ngữ khi người dùng đã xác nhận và feature bật. Chế độ giữ nguyên không ép `vi`, không gửi chuỗi rỗng để vô tình xóa field.
6. Lưu evidence/video ID ngay khi response đủ căn cứ; lỗi đọc lại mang theo evidence cấu trúc qua wrapper tới pipeline. Transaction kết thúc attempt/queue/reservation phải nhất quán và phục hồi được sau restart.
7. Readback có deadline/backoff hữu hạn. Thiếu field, ID sai, permission denied hoặc mismatch không được được coi là success; retry đọc có giới hạn, không thêm POST upload để chữa lỗi đọc.
8. API remix vẫn unsupported trừ khi có phương thức chính thức đúng nghĩa và đã được xác minh; không ghép một API field khác để giả lập.

**Điều kiện đạt**: HTTP fixtures kiểm tra payload chính xác, readback đúng/sai/thiếu, ID khác, 429/5xx/timeout, response hỏng, catalog không khả dụng và token sai kênh. Test đếm số request xác nhận không upload lần hai khi verification lỗi. Sau crash ở điểm đã nhận ID nhưng chưa xác minh, attempt/evidence/reservation còn; nguồn file hash/path không đổi. Thumbnail warning vẫn giữ kết quả chính đã xác minh. Language mới ghi nhãn offline, gate mặc định theo mục 4.3.

## 9. G20 — Studio category

**Công việc**

1. Kiểm kê fixture giao diện đã có và nguồn của từng selector/mapping. Không kết nối Profile thật để lấy fixture trong phạm vi này. Fixture tự viết phải được ghi là synthetic, không phải bằng chứng DOM live.
2. Tách helper nhỏ cho mở Show more, xác định section category, đọc giá trị, chọn đúng option và đọc lại; không refactor toàn bộ uploader.
3. Selector phải có phạm vi section, xác nhận duy nhất và semantic value. Không lấy phần tử đầu tiên từ selector rộng hoặc đoán bằng nhãn gần giống.
4. Mapping category ID ↔ option/nhãn có nguồn rõ; kiểm thử English/Vietnamese và nhãn trùng. Category lạ/disabled phải chặn, không chọn mục đầu tiên.
5. Trước Publish: xác minh giá trị DOM, binding kênh và approval còn đúng. Control không tìm thấy, không đổi sau click hoặc biến mất phải dừng.
6. Sau Save/Publish: dùng đúng video ID để mở trang chi tiết hoặc nguồn đọc lại chính thức được hỗ trợ. Kết quả trong danh sách Contents không đủ chứng minh category đã lưu.
7. Mismatch/mất kết nối sau side effect đi qua evidence/NeedsReview G19; giữ draft/video để đối soát. Không tự sửa video đã đăng bằng request bổ sung trong pha này.
8. Tích hợp gate và thông báo rõ trong preview. Nếu không có fixture/readback đủ căn cứ, bàn giao contract, tests và trạng thái chưa bật; không đặt lịch hứa hoàn tất live.

**Điều kiện đạt**: DOM fixture gồm dropdown mở muộn, Show more đóng, nhiều option trùng nhãn, section sai, control disabled, click không đổi, ngôn ngữ không nhận diện, giá trị tự đổi trước publish và readback sau lưu sai. Chạy Chromium local với mạng ngoài bị chặn; không chỉ mock hàm setter trả True.

## 10. G21 — Ngôn ngữ Studio và quyết định remix

**G21a: ngôn ngữ video**

1. Phân biệt control ngôn ngữ video với subtitle/caption certification và ngôn ngữ title/description.
2. Dùng cùng normalized value/nguồn như API; có mapping có căn cứ giữa tag và giá trị Studio, không so tên tự do với locale trình duyệt.
3. Thao tác set/readback trước publish và theo đúng video ID sau lưu; reuse cơ chế G20, không sao chép một nhánh xử lý lỗi riêng.
4. Test giữ nguyên, đặt `vi`, `en`, tag vùng được hỗ trợ, giá trị không hỗ trợ, control thiếu/trùng, stale approval và readback khác yêu cầu. Giữ gate nếu chỉ có offline evidence.

**G21b: remix**

1. Ghi rõ từng mức mà backend thực sự chứng minh: giữ thiết lập hiện có, không cho remix, audio-only hoặc audio+video. Gói tối thiểu chỉ cần giữ nguyên/không cho remix; không tự thêm mức chưa có bằng chứng.
2. Field legacy=1 phải tiếp tục được nhìn thấy như yêu cầu hạn chế. Nếu không thể thực hiện đúng, preflight báo blocked với cách bỏ yêu cầu/chọn capability phù hợp; không tự hạ từ “cấm” sang “audio-only”.
3. Nếu có thao tác và readback đáng tin cậy, triển khai adapter theo G20 và test Short/Video/control không khả dụng do eligibility. Nếu chưa có, hoàn tất UI/API rejection minh bạch và giữ unsupported — đó là đầu ra hợp lệ của pha quyết định, không phải chức năng remix đã hoàn thành.
4. Test mọi route gọi trực tiếp không bypass guard; khi người dùng chỉ lưu options không phát sinh thao tác online.

**Điều kiện đạt**: mỗi capability có kết luận riêng `offline_ready_gated` hoặc `unsupported_with_reason`; có bằng chứng cho mọi mapping đã code. PR G21a và G21b tách nhau để ngôn ngữ không chờ một giả định về remix.

## 11. G22 — Nghiệm thu, soak và rollout local

### G22a: công cụ phát hành dùng chung

1. Nâng `validate_review_release.py` từ coverage mặc định 6 bảng thành policy rõ cho 15 bảng G16, cộng các trường/bảng mới G18–G19.
2. Với bảng cũ: so mọi field cũ. Với migration: chỉ chấp nhận thay đổi cụ thể đã dự kiến, ví dụ cột ý định mới `{}`; không miễn kiểm tra cả bảng.
3. Thêm policy cho `app_settings`, `upload_slot`, schema stamps và logs: credentials phải giữ, auto-start chỉ được tắt theo guard hiện hữu; thay đổi cooldown/recovery phải có nguyên nhân, không xóa lock/claim đang hoạt động. Log khởi động được phép append theo quy tắc, không xóa lịch sử ngoài kế hoạch.
4. Tool rehearsal mặc định chỉ đọc nguồn, backup nhất quán, làm việc trên clone và restore vào clone; không sửa runtime. Rehearse hai lần, xử lý lỗi giữa migration, schema mới hơn, DB lock, thiếu dung lượng.
5. Manifest ghi revision/source root/fingerprint/build, Python/dependency/browser/tool versions, schema, backup hash và kết quả test. DB/secret/screenshot vận hành vẫn ngoài Git.
6. Script rollout nhận tham số và tự xác minh PID/cwd/port/commit/idle. Không tái dùng PID hoặc hash cũ đã hardcode. Có dry-run và rollback về code tương thích, không restore DB cũ đè evidence mới.

### G22b: kiểm thử tích hợp và soak có giới hạn

Tận dụng tests 100-item/claim/crash hiện có; chỉ thêm ca đi qua hợp đồng metadata mới, không viết lại bộ test cũ. Mỗi PR chạy focused tests phù hợp; trước rollout chạy full suite, Chromium, cài sạch và bắt buộc CI đạt. Media matrix CI hiện có tiếp tục chạy; chỉ chạy lại media local khi thay render/timing/cleanup hoặc có lỗi mới cần điều tra.

**Hai chế độ soak:**

- Deterministic suite: fake clock tại ranh giới test cho cooldown 30 phút, heartbeat và housekeeping 6 giờ; real SQLite, crash checkpoints và deadline mỗi ca. Không đổi clock/limit của runtime.
- Wall-clock 60–120 phút: process/SQLite/polling thật, dữ liệu tổng hợp, không credentials. Chạy ít nhất 10 vòng × 100 job chuẩn bị/claim/queue/render nhẹ với `do_upload=False`, phân bổ cấu hình 1/4/8 worker; giới hạn tổng để không thành bài chạy vô hạn.
- Làn upload riêng dùng boundary giả lập nhưng giữ global slot và cooldown 30 phút thật, chỉ chạy số ít lượt phù hợp cửa sổ thời gian, đo timestamp thực giữa các lần. Ma trận tải nhiều upload giả lập nằm trong deterministic suite với clock được kiểm soát. Báo cáo tách throughput chuẩn bị với upload; không hạ cooldown production để đạt số lượng test.

Sau warmup, chốt baseline và ngưỡng tài nguyên trước lượt chính. Báo cáo RSS, handles, threads/process còn sống sau drain, DB/WAL/log/artifact growth, lock latency, số retry/attempt/history/reservation. Không nâng tolerance để làm một lượt đang fail trở thành pass. Soak 60–120 phút không chứng minh housekeeping tự nhiên 6 giờ hoặc vận hành 24/7; nếu cần bằng chứng đó, tạo lượt ≥6 giờ riêng.

**Các bất biến bắt buộc:**

1. Một item không bị hai worker xử lý cùng lúc; không trùng history cùng nội dung+kênh.
2. Claim của worker còn sống không bị thu hồi; không quá global upload slot hoặc giới hạn worker.
3. Uncertain publish và Completed-with-warning không tự upload lại.
4. Dừng xong không còn child FFmpeg/browser/test worker do lượt chạy sở hữu; Windows console host được nhận diện riêng.
5. Restart không tự chạy; NeedsReview/reservation/evidence còn đầy đủ, kể cả khi crash sau nhận video ID.
6. Source path và checksum giữ nguyên; dữ liệu nhóm/kênh không bị âm thầm chuyển đích.
7. Sau drain không tăng lũy tiến worker/process/handle qua các vòng; mọi job có terminal state hoặc hold có lý do.
8. Không có traffic ngoài loopback trong nghiệm thu offline; harness phải fail nếu cố gọi YouTube, OAuth/provider hoặc Antidetect thật.

### Quy trình rollout

1. Chọn commit code đã green, kiểm tra base/stack PR; không lấy nhánh có thay đổi chưa commit làm bản phát hành.
2. Audit trước thay đổi: số job có legacy intent, unsupported intent, NeedsReview và dữ liệu thiếu; lưu riêng.
3. Xác minh runtime đúng source root, PID/cwd/port, checkout sạch và không active queue/Monitor/slot. Nếu không idle, dừng rollout và để nguyên công việc đang chạy.
4. Dừng đúng PID/worker đã xác minh, chờ thoát và port đóng; lấy app lock rồi kiểm tra DB idle lại. Backup SQLite nhất quán; checksum; rehearsal 15 bảng mở rộng và restore đạt. App đang chạy giữ lock suốt vòng đời nên không lấy lock triển khai đồng thời.
5. Trong lock, chuyển branch local mới và giữ branch cũ; nhả lock rồi restart, xác minh revision/build/fingerprint/schema, không auto-run. Nếu lỗi sau dừng nhưng trước đổi code, nhả lock và khởi động lại đúng bản cũ khi chưa có migration nào trên DB thật; ghi rõ kết quả phục hồi.
6. Read-only Chromium mở 6 tab, preview/capability chỉ đọc nếu endpoint thực sự read-only. Không gọi preview ghi token trên DB thật trong kiểm tra “chỉ đọc”; dùng clone cho các endpoint có ghi approval.
7. Đối chiếu dữ liệu với policy migration; giữ #212 NeedsReview và các evidence hiện hữu. Chụp báo cáo tổng hợp, không đưa DB/cookie/token vào PR.
8. Đóng biên bản: commit triển khai, PR, checksum backup, test/CI/soak, trạng thái gate, hạn chế live và hướng rollback. Commit/PR tài liệu riêng.

## 12. Ma trận nghiệm thu tối thiểu

| ID | Tình huống | Kết quả yêu cầu |
|---|---|---|
| META-01 | Category option khác channel | Preview/payload/readback đều theo option, ghi nguồn |
| META-02 | Không có option | Category theo channel/fallback hiện có; không đổi legacy |
| META-03 | Language cũ `vi`, remix cũ `0` | Giữ dữ liệu; không tự gửi ngôn ngữ hoặc bật remix |
| META-04 | Explicit false/disable/leave | Không bị default override; giữ đúng intent |
| META-05 | Nhóm chứa API và Studio | Capability đánh giá theo đích thực; Monitor kiểm tra mọi ứng viên |
| META-06 | Sửa options/group/backend/gate sau preview | Start/worker từ chối snapshot stale |
| META-07 | Chỉ download/render | Không gọi OAuth/Studio, không bị chặn bởi upload capability |
| API-01 | Category/language readback khớp | Hoàn tất theo hợp đồng, đúng video ID |
| API-02 | Readback thiếu/sai/timeout sau gửi | NeedsReview + evidence ID/URL + reservation; không gửi lại |
| API-03 | Sai kênh OAuth/catalog không hợp lệ | Chặn trước init/upload file |
| API-04 | 429/5xx/malformed JSON | Retry đọc có giới hạn hoặc NeedsReview; không POST upload lại |
| STUDIO-01 | Dropdown đóng/chậm/VI/EN | Chọn đúng section và đúng semantic value |
| STUDIO-02 | Control mất/disabled/trùng/click không đổi | Dừng trước publish, không tự chọn gần đúng |
| STUDIO-03 | Publish đã gửi, đọc lại metadata sai | Giữ evidence và NeedsReview, không xóa/reupload |
| STUDIO-04 | Thiếu fixture có nguồn | Gate tắt; báo chưa nghiệm thu live |
| DB-01 | Migration lần hai/lỗi giữa chừng/schema mới hơn | Idempotent/rollback/stamp đúng; dữ liệu cũ giữ |
| DB-02 | Restart sau nhận ID trước Completed | Evidence/hold tồn tại; không tự retry |
| LOAD-01 | 10 × 100 job chuẩn bị/render, 1/4/8 worker | Không duplicate, claim rò hoặc chuyển đích; không suy ra throughput upload |
| LOAD-02 | Upload giả lập có cooldown thật và clock ảo ở hai lượt riêng | Giữ global slot/gap; timestamp/evidence đúng; không đổi cấu hình production |
| UI-01 | Sửa note/tên options, refresh/reopen form | Không mất intent hoặc tự bật metadata |
| RELEASE-01 | Rehearsal/restore và local restart | 15 bảng mở rộng đạt; đúng root/build; không auto-run |

## 13. Điều kiện dừng, rollback và cách xử lý vướng mắc

- Dừng phase nếu resolver/preview/adapter khác nhau, xảy ra ghi đè dữ liệu cũ, mất evidence/ID, giải phóng reservation sai, retry upload ngoài dự kiến hoặc UI chọn nhầm control. Giữ regression tái hiện, sửa trước khi tiến tiếp.
- Nếu thiếu DOM/API evidence: không đoán selector, payload hoặc đổi nghĩa field. Đóng phần framework/offline và giữ feature gate/unsupported, ghi rõ phần cần bằng chứng tiếp theo. Các phần độc lập vẫn tiếp tục.
- Nếu phát hiện migration làm đổi field cũ ngoài whitelist: không chạy vào runtime. Điều tra trên clone, bổ sung test và backup mới khi thực sự rollout.
- Nếu rollout đã khởi động nhưng health/data check fail: dừng nhận job mới, giữ DB/log/evidence sau lỗi; quay về code có thể đọc schema mới. Không chạy bản cũ bỏ qua metadata intent mới với worker enabled. Nếu không chứng minh tương thích, giữ app dừng để sửa forward thay vì restore mù.
- Chỉ khi người dùng sau này mở phạm vi online mới có mốc live riêng với kênh/video/chế độ hiển thị cụ thể. Kế hoạch hiện tại không yêu cầu hỏi lại quyền đăng và không dùng “chờ live” để bỏ dở phần offline.

## 14. Ước lượng và đầu ra mỗi mốc

Ước lượng là thời gian kỹ thuật, mỗi buổi khoảng 3–4 giờ; không phải cam kết ngày hoàn thành. Chưa gồm chờ CI, thu thập bằng chứng live hoặc xử lý bất thường ngoài phạm vi.

| Mốc | Ước lượng | Đầu ra review được |
|---|---:|---|
| G17 | 1–2 buổi | Ma trận, audit chỉ đọc, registry/guard, regression |
| G18 | 2–3 buổi | Policy/migration, preview UI, approval/Monitor, compatibility docs |
| G19 | 2–3 buổi | API payload/readback, evidence và fault tests |
| G20 | 2–4 buổi | Adapter category/fixtures hoặc gate có lý do thiếu evidence |
| G21 | 2–4 buổi | Ngôn ngữ tách remix, kết luận hỗ trợ theo từng backend |
| G22 | 2–3 buổi + lượt soak | Rehearsal chung, manifest, báo cáo tích hợp/soak/rollout |

Tổng dự kiến **11–19 buổi**, tùy bằng chứng Studio và độ tương thích migration. G17–G19 là phần có thể hoàn thành chủ động bằng code và fixture; không hứa số buổi sẽ tự tạo ra bằng chứng live cho G20–G21.

Mỗi mốc hoàn tất gồm: vấn đề tái hiện → thay đổi nhỏ có phạm vi → focused tests → kiểm tra rộng phù hợp → commit/push → PR với kết quả và hạn chế. CI fail phải xử lý trước khi chốt; PR không tự merge. Nhánh plan này chỉ thêm tài liệu, không thay code hoặc runtime.

## 15. Sau G22

1. **Dependency compatibility:** xử lý cảnh báo Starlette TestClient/httpx trên môi trường sạch; freeze baseline, kiểm tra release notes và `pip check`, đổi một nhóm tương thích mỗi PR. Chưa có bằng chứng đây là lỗi runtime; không suppress toàn bộ warnings hoặc nâng tất cả package cùng lúc. Cân nhắc constraints/lock cho dependency gián tiếp sau khi chọn tổ hợp ổn định.
2. **Quy trình repo/release:** chuẩn hóa nguồn phát hành, stack/base PR, launcher và manifest; bảo toàn snapshot cũ, không xóa checkout tùy tiện.
3. **Refactor:** tách module theo hợp đồng đã có test, từng PR giữ nguyên hành vi; tránh trộn refactor với sửa uploader.
4. **Mở rộng hàng loạt:** tăng quy mô chuẩn bị/queue/render và đo tài nguyên theo soak; upload vẫn theo giới hạn/slot hiện có tới khi có kế hoạch riêng được nghiệm thu. Không dùng tăng worker để bù thiếu xác minh metadata.

**Bước đầu tiên khi triển khai kế hoạch: G17 — lập audit chỉ đọc và regression cho capability/metadata, trước mọi migration hoặc thao tác uploader mới.**
