"""Resolve upload values and intent without touching files, providers, or the DB."""
import re

from metadata_intent import parse_intent
from upload_capabilities import (
    UploadCapabilityError, capability_matrix, normalize_flag, normalize_mode,
)


def _issue(code, feature, message):
    return {'code': code, 'feature': feature, 'message': message}


def _category_value(channel, options, issues):
    # Absence inherits; explicit invalid values must not become the fallback.
    for source, value in (
        ('option', options.get('category_id')),
        ('channel', channel.get('default_category_id')),
        ('fallback', '22'),
    ):
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        normalized = str(value) if type(value) is int else value
        if isinstance(normalized, str) and re.fullmatch(r'[1-9][0-9]{0,2}', normalized.strip()):
            return normalized.strip(), source
        issues.append(_issue(
            'UPLOAD_CATEGORY_INVALID', 'category',
            'Mã danh mục không hợp lệ. Kiểm tra danh mục trong bộ tùy chọn hoặc kênh trước khi đăng.',
        ))
        return None, source


def resolve_metadata(channel, options=None):
    """Return effective values, their provenance, and fail-closed upload issues.

    ``legacy_unconfirmed`` never requests language/remix changes. Category intent
    is separate from its value: API preserves its existing apply behavior while
    Studio requires an explicit channel or option choice, including optionless jobs.
    Malformed saved fields raise ``UploadCapabilityError`` with sanitized issues;
    valid but unsupported/unconfirmed choices remain available in ``issues`` so
    preview can explain their effective values without permitting an upload.
    """
    channel = channel or {}
    options = options or {}
    issues, warnings = [], []
    try:
        backend = normalize_mode(channel.get('upload_mode'))
    except UploadCapabilityError as exc:
        backend = 'unknown'
        issues.extend(exc.issues)
    registry = capability_matrix().get(backend)
    try:
        intent = parse_intent(options.get('metadata_intent_json', '{}'))
    except (ValueError, TypeError):
        intent = {}
        issues.append(_issue(
            'UPLOAD_METADATA_INTENT_INVALID', 'metadata_intent',
            'Lựa chọn metadata đã lưu không hợp lệ. Mở bộ tùy chọn và xác nhận lại trước khi đăng.',
        ))

    category_value, category_source = _category_value(channel, options, issues)
    if backend == 'youtube_api':
        category_mode, intent_source = 'apply', 'api_default'
    elif backend == 'playwright':
        selected = intent.get('studio_category_mode', 'inherit')
        if selected in ('keep', 'apply'):
            category_mode, intent_source = selected, 'option'
        else:
            category_mode = channel.get('studio_category_mode')
            if category_mode is None or category_mode == '':
                category_mode = 'legacy_unconfirmed'
            intent_source = 'channel'
        if category_mode not in ('legacy_unconfirmed', 'keep', 'apply'):
            category_mode = 'invalid'
            issues.append(_issue(
                'UPLOAD_CATEGORY_INTENT_INVALID', 'category',
                'Lựa chọn danh mục Studio của kênh không hợp lệ. Mở kênh và xác nhận lại.',
            ))
        elif category_mode == 'legacy_unconfirmed':
            issues.append(_issue(
                'UPLOAD_CATEGORY_CONFIRMATION_REQUIRED', 'category',
                'Danh mục Studio chưa được xác nhận. Trong kênh hoặc bộ tùy chọn, '
                'chọn giữ thiết lập hiện có bên YouTube hoặc yêu cầu áp danh mục trước khi đăng.',
            ))
    else:
        category_mode, intent_source = 'unknown', 'unknown'

    if intent:
        language = intent['video_language']
        video_language = {
            'value': language.get('value') if language['mode'] == 'apply' else None,
            'source': 'option', 'mode': language['mode'],
        }
        remix = {'value': True if intent['remix'] == 'disable' else None,
                 'source': 'option', 'mode': intent['remix']}
    else:
        video_language = {'value': None, 'source': 'legacy_option', 'mode': 'legacy_unconfirmed'}
        remix = {'value': None, 'source': 'legacy_option', 'mode': 'legacy_unconfirmed'}
        try:
            if normalize_flag(options.get('disable_shorts_remix'), 'remix'):
                remix.update(value=True, mode='disable')
        except UploadCapabilityError as exc:
            issues.extend(exc.issues)
        if options:
            warnings.append('Ngôn ngữ cũ chưa xác nhận: không tự đặt ngôn ngữ video từ giá trị đã lưu.')
            if remix['mode'] == 'legacy_unconfirmed':
                warnings.append('Remix cũ chưa xác nhận: không thay đổi quyền remix bên YouTube.')

    if registry:
        requested = {
            'category': category_mode == 'apply',
            'video_language': video_language['mode'] == 'apply',
            'remix': remix['mode'] == 'disable',
        }
        for feature, apply in requested.items():
            if apply and not registry['features'][feature]['enabled']:
                issues.append(_issue(
                    f'UPLOAD_{feature.upper()}_UNSUPPORTED', feature,
                    registry['features'][feature]['reason'],
                ))

    invalid = [issue for issue in issues if issue['code'].endswith('_INVALID')]
    if invalid:
        raise UploadCapabilityError(invalid)

    return {
        'backend': backend,
        'category': {'value': category_value, 'source': category_source,
                     'mode': category_mode, 'intent_source': intent_source},
        'video_language': video_language,
        'remix': remix,
        'capability_revision': registry['revision'] if registry else '',
        'issues': issues,
        'warnings': warnings,
    }
