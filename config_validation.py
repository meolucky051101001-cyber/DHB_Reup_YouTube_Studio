"""Shared create/patch schemas for channel and upload-option configuration."""
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

from phase4_rules import normalize_group_names


def _text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('Phải là chuỗi văn bản.')
    if '\0' in value:
        raise ValueError('Văn bản không được chứa ký tự NUL.')
    return value.strip()


def _flag(value):
    if value is None:
        return 0  # Existing clients use null to clear optional flags.
    if isinstance(value, bool):
        return int(value)
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in ('1', 'true', 'yes', 'on', 'có', 'co'):
            return 1
        if lowered in ('0', 'false', 'no', 'off', 'không', 'khong', ''):
            return 0
    raise ValueError('Cờ bật/tắt chỉ nhận 0/1, true/false, Có/Không.')


def _category(value):
    if type(value) is int:
        value = str(value)
    value = _text(value)
    if not re.fullmatch(r'[1-9][0-9]{0,2}', value):
        raise ValueError('Mã danh mục phải là số nguyên dương từ 1 đến 999.')
    return value


def _groups(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = _text(value)
        if not value:
            return None
        if value.startswith('['):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError('Danh sách nhóm JSON không hợp lệ.') from exc
        else:
            return normalize_group_names(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('Nhóm kênh phải là danh sách tên dạng chuỗi.')
    return normalize_group_names(value)


Text = Annotated[str, BeforeValidator(_text)]
Name = Annotated[Text, Field(min_length=1, max_length=200)]
Flag = Annotated[int, BeforeValidator(_flag)]
Category = Annotated[str, BeforeValidator(_category)]
Groups = Annotated[list[str] | None, BeforeValidator(_groups)]


class ConfigInput(BaseModel):
    # Keep compatibility with clients sending known fields alongside display data.
    # Only declared fields can reach SQL; unknown fields remain ignored.
    model_config = ConfigDict(extra='ignore')


class ChannelInput(ConfigInput):
    name: Name = ''  # Patch defaults are never written; create overrides this field.
    channel_id: Text = ''
    handle: Text = ''
    email: Text = ''
    group_name: Text = ''
    group_names: Groups = None
    upload_mode: Annotated[Literal['playwright', 'youtube_api'], BeforeValidator(_text)] = 'playwright'
    profile_name: Text = ''
    profile_path: Text = ''
    proxy: Text = ''
    oauth_token_json: Text = ''
    default_category_id: Category = '22'
    default_privacy: Annotated[Literal['public', 'unlisted', 'private'], BeforeValidator(_text)] = 'public'
    note: Text = ''
    status: Text = 'Ready'
    emulate_mobile: Flag = 0
    use_hidden_desktop: Flag = 0
    made_for_kids: Flag = 0
    altered_content: Flag = 0

    @field_validator('channel_id')
    @classmethod
    def channel_identifier(cls, value):
        if value and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
            raise ValueError('Channel ID phải bắt đầu bằng UC và có 24 ký tự, hoặc để trống để cấu hình sau.')
        return value


class ChannelCreate(ChannelInput):
    name: Name


class OptionInput(ConfigInput):
    name: Name = ''
    affiliate_enabled: Flag = 0
    affiliate_url: Text = ''
    affiliate_CTA: Text = '👉 Mua ngay sản phẩm chính hãng tại đây:'
    pin_comment_enabled: Flag = 0
    pin_comment_text: Text = ''
    append_hashtags: Text = '#shorts #trending #viral'
    default_tags: Text = ''
    category_id: Category = '22'
    language: Text = 'vi'
    made_for_kids: Flag = 0
    ai_label: Flag = 0
    disable_shorts_remix: Flag = 0

    @field_validator('language')
    @classmethod
    def language_tag(cls, value):
        if not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value) or len(value) > 64:
            raise ValueError('Mã ngôn ngữ phải có dạng vi, en hoặc en-US.')
        return value

    @field_validator('affiliate_url')
    @classmethod
    def affiliate_link(cls, value):
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            if any(ch.isspace() or ord(ch) < 32 for ch in value):
                raise ValueError()
            _ = parsed.port
        except ValueError as exc:
            raise ValueError('Link Affiliate phải là URL HTTP/HTTPS hợp lệ, không chứa thông tin đăng nhập.') from exc
        return value


class OptionCreate(OptionInput):
    name: Name


def parse_config_input(schema, data, *, partial=False):
    try:
        model = schema.model_validate(data)
    except ValidationError as exc:
        # Do not serialize ValidationError: it includes raw inputs such as tokens.
        error = exc.errors(include_input=False, include_url=False, include_context=False)[0]
        field = '.'.join(str(part) for part in error['loc'])
        raise ValueError(f"{field}: {error['msg']}") from None
    return model.model_dump(exclude_unset=partial)


def validate_config(data, kind, *, create=False):
    schemas = {'channel': (ChannelInput, ChannelCreate), 'option': (OptionInput, OptionCreate)}
    values = parse_config_input(schemas[kind][int(create)], data, partial=not create)
    if kind == 'channel' and values.get('group_names') is not None and values.get('group_name'):
        if normalize_group_names(values['group_name']) != values['group_names'][:1]:
            raise ValueError('group_name và group_names có nhóm đầu tiên mâu thuẫn.')
    return values


def validate_option_state(values):
    if values.get('affiliate_enabled') and not values.get('affiliate_url'):
        raise ValueError('Đã bật Affiliate nhưng chưa nhập link HTTP/HTTPS.')
