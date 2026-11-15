"""Upload capability evidence and guards; pure functions, no provider or DB access."""
import hashlib
import json


class UploadCapabilityError(RuntimeError, ValueError):
    """A configuration rejection before an upload side effect."""

    def __init__(self, issues):
        self.issues = issues
        self.code = issues[0]['code']
        super().__init__(' '.join(issue['message'] for issue in issues))


def normalize_mode(value):
    mode = str(value or 'playwright').strip().lower()
    if mode not in ('youtube_api', 'playwright'):
        raise UploadCapabilityError([{
            'code': 'UPLOAD_BACKEND_UNSUPPORTED', 'feature': 'backend',
            'message': 'Phương thức đăng chưa được hỗ trợ. Chọn YouTube API hoặc Studio trước khi đăng.',
        }])
    return mode


def normalize_flag(value, field):
    if value is None or value == '' or type(value) is bool:
        return bool(value)
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ('0', '1', 'false', 'true'):
        return value.strip().lower() in ('1', 'true')
    raise UploadCapabilityError([{
        'code': 'UPLOAD_METADATA_FLAG_INVALID', 'feature': field,
        'message': f'Giá trị {field} không hợp lệ; cần xác nhận lại cấu hình trước khi đăng.',
    }])


def legacy_option_requests(options):
    """Carry legacy intent without treating a stored language/default zero as opt-in."""
    options = options or {}
    return {
        'legacy_language': options.get('language'),
        'language_intent': 'legacy_unconfirmed',
        'disable_shorts_remix': normalize_flag(options.get('disable_shorts_remix'), 'remix'),
        'pin_comment_requested': normalize_flag(options.get('pin_comment_enabled'), 'pin_comment'),
    }


def _feature(label, reason, evidence, *, can_set=False, readback='none',
             offline_verified=False, enabled=False):
    return dict(label=label, stored=True, can_set=can_set, readback=readback,
                offline_verified=offline_verified, live_verified=False, enabled=enabled,
                reason=reason, evidence=evidence)


def capability_matrix():
    """Keep legacy keys; evidence describes each operation, not a blanket promise."""
    result = {}
    for mode in ('youtube_api', 'playwright'):
        api = mode == 'youtube_api'
        features = {
            'category': _feature('Danh mục',
                'API gửi categoryId; chưa đọc lại danh mục sau đăng.' if api else
                'Studio hiện chỉ lưu cấu hình danh mục; chưa có thao tác đặt và đọc lại được hỗ trợ.',
                ['youtube_uploader.py:_upload_via_youtube_api_with_session',
                 'youtube_uploader.py:_verify_api_video'] if api else
                ['youtube_uploader.py:upload_via_playwright_studio'],
                can_set=api, offline_verified=api, enabled=api),
            'video_language': _feature('Ngôn ngữ video',
                'Ngôn ngữ cũ chỉ được lưu, chưa xác nhận ý định áp dụng; chưa bật đặt ngôn ngữ video.',
                ['config_validation.py:validate_config', 'youtube_uploader.py:build_final_metadata']),
            'remix': _feature('Quyền remix',
                'Chưa hỗ trợ đặt và đọc lại quyền remix. Yêu cầu cấm remix sẽ chặn đăng; chỉ bỏ yêu cầu khi bạn chủ động chấp nhận.',
                ['upload_capabilities.py:capability_issues']),
            'age_restriction': _feature('Giới hạn độ tuổi 18+',
                'Chưa hỗ trợ thiết lập và xác minh giới hạn độ tuổi 18+ trên cả YouTube API và Studio. '
                'Giữ video chờ tính năng được hỗ trợ, hoặc chủ động sửa yêu cầu giới hạn tuổi trước khi đăng.',
                ['tests/test_p3_uploader.py:PreflightAndProxyTests']),
            'premiere': _feature('Công chiếu',
                'Công chiếu chưa có quy trình thiết lập và xác minh được hỗ trợ qua YouTube API hoặc Studio. Giữ video chờ hỗ trợ hoặc chủ động bỏ yêu cầu Công chiếu.' if api else
                'Studio có thao tác chọn Công chiếu nhưng chưa kiểm tra kết quả chọn và đọc lại sau lưu; yêu cầu Công chiếu hiện bị chặn trước đăng.',
                ['youtube_uploader.py:upload_via_playwright_studio'],
                can_set=not api),
            'pin_comment': _feature('Ghim bình luận',
                'Ghim bình luận chưa được hỗ trợ trên YouTube API hoặc Studio. '
                'Tắt yêu cầu ghim trong bộ tùy chọn trước khi đăng; nội dung bình luận vẫn được giữ.',
                ['tests/test_optional_upload_results.py:OptionalResultTests']),
            'thumbnail': _feature('Ảnh thu nhỏ',
                'API kiểm tra phản hồi đặt thumbnail; lỗi bước phụ được giữ dưới dạng cảnh báo, không đăng lại video.' if api else
                'Studio có thao tác nạp thumbnail; chưa đọc lại ảnh sau lưu. Lỗi bước phụ được giữ dưới dạng cảnh báo.',
                ['tests/test_optional_upload_results.py:OptionalResultTests'] if api else
                ['youtube_uploader.py:upload_via_playwright_studio'],
                can_set=True, readback='response' if api else 'none', offline_verified=api, enabled=True),
        }
        revision = hashlib.sha256(json.dumps(features, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result[mode] = {
            'pin_comment': False, 'thumbnail': True,
            'pin_comment_reason': features['pin_comment']['reason'],
            'schema_version': 1, 'revision': revision, 'features': features,
        }
    return result


def capability_issues(mode, metadata):
    try:
        mode = normalize_mode(mode)
        requests = {
            'pin_comment': normalize_flag(metadata.get('pin_comment_requested'), 'pin_comment') or bool(metadata.get('pin_comment')),
            'age_restriction': normalize_flag(metadata.get('age_restriction'), 'age_restriction'),
            'premiere': normalize_flag(metadata.get('is_premiere'), 'premiere'),
            'remix': normalize_flag(metadata.get('disable_shorts_remix'), 'remix'),
            # This explicit adapter field is distinct from an unconfirmed legacy language.
            'video_language': bool(metadata.get('video_language')),
        }
    except UploadCapabilityError as exc:
        return exc.issues
    features = capability_matrix()[mode]['features']
    return [dict(code=f'UPLOAD_{key.upper()}_UNSUPPORTED', feature=key, message=features[key]['reason'])
            for key, requested in requests.items() if requested and not features[key]['enabled']]


def require_capabilities(mode, metadata):
    issues = capability_issues(mode, metadata)
    if issues:
        raise UploadCapabilityError(issues)
