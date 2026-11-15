"""Validate monitor inputs without network access or starting workers."""
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator

import monitor_policy
from config_validation import ConfigInput, Name, Text, parse_config_input
from phase4_rules import VALID_RENDER_MODES, validate_source_url
from scanner_downloader import MAX_SCAN_ITEMS


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r'-?[0-9]+', value.strip()):
        return int(value)
    raise ValueError('Phải là số nguyên, không nhận bool hoặc số thập phân.')


def _optional_id(value):
    if value is None or value == '':
        return None
    value = _integer(value)
    return None if value == 0 else value


Integer = Annotated[int, BeforeValidator(_integer)]
OptionalId = Annotated[Annotated[int, Field(ge=1, le=2**63-1)] | None, BeforeValidator(_optional_id)]


class MonitorCreate(ConfigInput):
    name: Name
    source_urls: Annotated[Text, Field(min_length=1, max_length=64000)]
    target_channel_id: OptionalId = None
    target_group_name: Annotated[Text, Field(min_length=1, max_length=80)] = 'Mặc định'
    upload_to: Literal['Shorts', 'Video'] = 'Shorts'
    render_mode: Text = 'shorts_9_16'
    option_group_id: OptionalId = None
    ai_Action: Literal['none', 'translate', 'rewrite_seo', 'shorts_hook'] = 'rewrite_seo'
    ai_provider: Literal['gemini', 'openai', 'deepseek', 'google_translate'] = 'gemini'
    ai_target_lang: Annotated[Text, Field(min_length=1, max_length=200)] = 'Tiếng Việt'
    min_views: Annotated[Integer, Field(ge=0, le=2**63-1)] = 0
    check_interval_minutes: Annotated[Integer, Field(ge=5, le=10080)] = 30
    max_videos_per_scan: Annotated[Integer, Field(ge=1, le=MAX_SCAN_ITEMS)] = 3
    metadata_policy: Annotated[dict, BeforeValidator(monitor_policy.policy)]

    @field_validator('render_mode')
    @classmethod
    def render_choice(cls, value):
        if value not in VALID_RENDER_MODES:
            raise ValueError('Chế độ render không hợp lệ.')
        return value

    @field_validator('source_urls')
    @classmethod
    def sources(cls, value):
        result = []
        for number, raw in enumerate(value.splitlines(), 1):
            source = raw.strip()
            if not source:
                continue
            try:
                if source.lower().startswith(('http://', 'https://')):
                    validate_source_url(source)
                    if any(ch.isspace() or ord(ch) < 32 for ch in source):
                        raise ValueError()
                elif not Path(source).is_file() and not Path(source).is_dir():
                    raise ValueError()
            except (ValueError, OSError):
                raise ValueError(f'Nguồn ở dòng {number} phải là URL HTTP/HTTPS hợp lệ hoặc file/thư mục local tồn tại.') from None
            # Keep signed query strings byte-for-byte; validation is not URL rewriting.
            result.append(source)
        if not result:
            raise ValueError('Danh sách nguồn không được để trống.')
        return '\n'.join(result)


def validate_monitor_create(data):
    values = parse_config_input(MonitorCreate, data)
    values['metadata_policy_json'] = monitor_policy.encode(values.pop('metadata_policy'))
    return values
