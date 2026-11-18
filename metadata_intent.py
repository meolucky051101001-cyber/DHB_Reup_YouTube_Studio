"""Versioned metadata choices and compatibility rules; no IO or application imports."""
import hashlib
import json
import re


class MetadataIntentConflict(ValueError):
    """A legacy editor must reload before changing confirmed metadata."""


CHANNEL_MODES = ('legacy_unconfirmed', 'keep', 'apply')
OPTION_MODES = ('inherit', 'keep', 'apply')
INTENT_FIELDS = frozenset(('version', 'studio_category_mode', 'video_language', 'remix'))
LEGACY_OPTION_FIELDS = frozenset(('category_id', 'language', 'disable_shorts_remix'))


def metadata_revision(row, kind):
    """Hash only metadata edit state, so names/notes do not invalidate an editor."""
    values = dict(row)
    if kind == 'option':
        fields = ('metadata_intent_json', 'category_id', 'language', 'disable_shorts_remix')
    elif kind == 'channel':
        fields = ('studio_category_mode', 'default_category_id')
    else:
        raise ValueError('Loại cấu hình metadata không hợp lệ.')
    payload = {'version': 1, 'kind': kind, 'fields': {key: values.get(key) for key in fields}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def language_tag(value):
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value)):
        raise ValueError('Ngôn ngữ video phải là mã như vi, en hoặc en-US.')
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Ý định metadata chứa khóa JSON lặp lại.')
        value[key] = item
    return value


def parse_intent(raw):
    """Accept an empty legacy object or a complete, strict version-1 choice object."""
    if isinstance(raw, str):
        if len(raw) > 4096:
            raise ValueError('Ý định metadata vượt giới hạn kích thước.')
        try:
            raw = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ValueError('Ý định metadata phải là JSON hợp lệ, không lặp khóa.') from None
    if not isinstance(raw, dict):
        raise ValueError('Ý định metadata phải là một đối tượng JSON.')
    if not raw:
        return {}
    if set(raw) != INTENT_FIELDS:
        raise ValueError('Ý định metadata cần đầy đủ version, studio_category_mode, video_language và remix; không nhận trường khác.')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise ValueError('Phiên bản ý định metadata chưa được hỗ trợ; cần phiên bản 1.')
    if raw['studio_category_mode'] not in OPTION_MODES or type(raw['studio_category_mode']) is not str:
        raise ValueError('Lựa chọn danh mục Studio phải là inherit, keep hoặc apply.')
    if raw['remix'] not in ('keep', 'disable') or type(raw['remix']) is not str:
        raise ValueError('Lựa chọn remix phải là keep hoặc disable.')
    language = raw['video_language']
    if not isinstance(language, dict) or type(language.get('mode')) is not str:
        raise ValueError('Lựa chọn ngôn ngữ video phải có mode là keep hoặc apply.')
    if language['mode'] == 'keep' and set(language) == {'mode'}:
        selected_language = {'mode': 'keep'}
    elif language['mode'] == 'apply' and set(language) == {'mode', 'value'}:
        selected_language = {'mode': 'apply', 'value': language_tag(language['value'])}
    else:
        raise ValueError('Giữ ngôn ngữ không nhận value; áp dụng ngôn ngữ phải có một value hợp lệ.')
    return {'version': 1, 'studio_category_mode': raw['studio_category_mode'],
            'video_language': selected_language, 'remix': raw['remix']}


def encode_intent(raw):
    return json.dumps(parse_intent(raw), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse_channel_mode(value):
    if type(value) is not str or value not in CHANNEL_MODES:
        raise ValueError('Lựa chọn danh mục Studio của kênh phải là legacy_unconfirmed, keep hoặc apply.')
    return value


def _legacy_equal(field, left, right):
    # Rows normally contain validated values. Normalize only representations that
    # old clients legitimately round-trip; do not equate arbitrary truthy values.
    if field == 'disable_shorts_remix':
        false = (0, False, '0', 'false')
        true = (1, True, '1', 'true')
        if left in false and right in false:
            return True
        if left in true and right in true:
            return True
    if field in ('category_id', 'default_category_id'):
        return str(left) == str(right)
    return left == right


def prepare_option_update(current, validated, supplied_fields):
    """Return atomic SQL values after distinguishing supplied fields from defaults.

    Pass an empty current mapping for create. For patch, read current inside the
    same BEGIN IMMEDIATE transaction used to write the returned values.
    """
    updates = dict(validated)
    supplied = set(supplied_fields)
    previous = parse_intent(current.get('metadata_intent_json', '{}'))
    explicit = 'metadata_intent_json' in supplied
    if not explicit:
        if previous and any(field in supplied and field in updates
                            and not _legacy_equal(field, current.get(field), updates[field])
                            for field in LEGACY_OPTION_FIELDS):
            raise MetadataIntentConflict('Ý định metadata đã được xác nhận. Tải lại bằng giao diện mới và gửi kèm lựa chọn metadata trước khi đổi danh mục, ngôn ngữ hoặc remix.')
        return updates
    intent = parse_intent(updates['metadata_intent_json'])
    if previous and not intent:
        raise MetadataIntentConflict('Không thể xóa ý định metadata đã xác nhận. Chọn giữ thiết lập hiện có một cách rõ ràng.')
    updates['metadata_intent_json'] = encode_intent(intent)
    if not intent:
        return updates
    language = intent['video_language']
    if language['mode'] == 'apply':
        if 'language' in supplied and updates['language'] != language['value']:
            raise ValueError('language và ngôn ngữ video đã xác nhận mâu thuẫn; chưa lưu thay đổi nào.')
        updates['language'] = language['value']
    remix = int(intent['remix'] == 'disable')
    if 'disable_shorts_remix' in supplied and not _legacy_equal('disable_shorts_remix', updates['disable_shorts_remix'], remix):
        raise ValueError('disable_shorts_remix và lựa chọn remix đã xác nhận mâu thuẫn; chưa lưu thay đổi nào.')
    updates['disable_shorts_remix'] = remix
    return updates


def prepare_channel_update(current, validated, supplied_fields):
    updates = dict(validated)
    supplied = set(supplied_fields)
    previous = parse_channel_mode(current.get('studio_category_mode', 'legacy_unconfirmed'))
    explicit = 'studio_category_mode' in supplied
    if explicit:
        selected = parse_channel_mode(updates['studio_category_mode'])
        if previous != 'legacy_unconfirmed' and selected == 'legacy_unconfirmed':
            raise MetadataIntentConflict('Không thể đưa lựa chọn danh mục đã xác nhận về chưa xác nhận. Chọn keep hoặc apply.')
    elif (previous != 'legacy_unconfirmed' and 'default_category_id' in supplied
          and not _legacy_equal('default_category_id', current.get('default_category_id'), updates['default_category_id'])):
        raise MetadataIntentConflict('Danh mục Studio của kênh đã được xác nhận. Tải lại bằng giao diện mới và gửi kèm studio_category_mode trước khi đổi danh mục.')
    return updates
