"""Typed render-preset input and deterministic revisions, without running media."""
import hashlib
import json
import math
import re

from pydantic import Field, ConfigDict

from config_validation import ConfigInput, Name, Text, _flag, _text
from phase4_rules import VALID_RENDER_MODES, VALID_RESOLUTIONS


class PresetUpdate(ConfigInput):
    model_config = ConfigDict(extra='forbid')
    name: Name = ''
    render_mode: Text = 'shorts_9_16'
    config: dict = Field(default_factory=dict)
    note: Text = ''
    expected_revision: Text = ''


class PresetCreate(PresetUpdate):
    name: Name


NUMBERS = {
    'speed': (.5, 2, False), 'volume': (0, 3, False), 'brightness': (-1, 1, False),
    'framerate': (1, 120, True), 'max_shorts_duration': (1, 600, False),
    'audio_original_percent': (0, 300, False), 'bg_music_percent': (0, 300, False),
    'bg_music_volume': (0, 3, False), 'watermark_image_opacity': (0, 1, False),
    'logo_video_similarity': (.01, 1, False), 'logo_video_blend': (0, 1, False),
    'watermark_text_size': (8, 400, True), 'text_scroll_size': (8, 400, True),
    'text_scroll_speed': (1, 1000, True), 'text_scroll_height': (30, 7680, True),
    'pad_size': (0, 300, True), 'move_camera_speed': (.1, 3, False),
    'rotation_angle': (0, 270, True),
}
FLAGS = set('hflip audio_pitch_shift enforce_shorts_duration shorts_visual_pop shorts_vibrant_colors '
            'bg_video_random intro_enabled intro_random outro_enabled outro_random '
            'crop_enabled crop_center pad_enabled move_camera'.split())
RESOURCES = set('bg_video_name bg_music_name watermark_image_name logo_video_name intro_name outro_name '
                'watermark_text_font text_scroll_font'.split())
SIZES = {'watermark_image_size', 'logo_video_size', 'crop_size', 'size'}
POSITIONS = {'watermark_image_pos', 'logo_video_pos', 'watermark_text_pos', 'crop_location'}
COLORS = {'watermark_text_color', 'text_scroll_color', 'pad_color'}
TEXTS = {'watermark_text', 'text_scroll', 'custom_command', 'logo_video_chroma', 'cut_start', 'cut_end',
         'bitrate', 'preset', 'resolution', 'shorts_fit_mode'}
FIELDS = set(NUMBERS) | FLAGS | RESOURCES | SIZES | POSITIONS | COLORS | TEXTS


def validate_render_config(value, render_mode=None):
    if not isinstance(value, dict):
        raise ValueError('Cấu hình render phải là một object JSON.')
    if any(key not in FIELDS for key in value):
        raise ValueError('Cấu hình render có trường chưa được hỗ trợ; dữ liệu cũ được giữ nguyên.')
    if render_mode is not None and (not isinstance(render_mode, str) or render_mode not in VALID_RENDER_MODES):
        raise ValueError('Chế độ render không được hỗ trợ.')
    result = {}
    for key, raw in value.items():
        try:
            if key in FLAGS:
                if raw is None:
                    raise ValueError('Cờ bật/tắt không được là null.')
                normalized = bool(_flag(raw))
            elif key in NUMBERS:
                lo, hi, integer = NUMBERS[key]
                if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                    raise ValueError('Phải là số hữu hạn.')
                normalized = float(raw)
                if not math.isfinite(normalized) or not lo <= normalized <= hi or (integer and not normalized.is_integer()):
                    raise ValueError(f'Phải là {"số nguyên" if integer else "số hữu hạn"} trong khoảng {lo}–{hi}.')
                if integer:
                    normalized = int(normalized)
                if key == 'rotation_angle' and normalized not in (0, 90, 180, 270):
                    raise ValueError('Góc xoay chỉ nhận 0, 90, 180 hoặc 270.')
            else:
                if not isinstance(raw, str):
                    raise ValueError('Phải là chuỗi văn bản.')
                normalized = _text(raw)
                if len(normalized) > (16000 if key == 'custom_command' else 5000 if key in ('watermark_text','text_scroll') else 255):
                    raise ValueError('Văn bản vượt giới hạn độ dài.')
                if key in RESOURCES and normalized:
                    if re.search(r'[<>:"/\\|?*\x00-\x1f]',normalized) or normalized in ('.','..') or normalized.endswith(('.', ' ')):
                        raise ValueError('Tài nguyên phải là tên file trong kho, không phải đường dẫn.')
                elif key in SIZES | POSITIONS:
                    match = re.fullmatch(r'(\d{1,5})\s*[:x,]\s*(\d{1,5})',normalized)
                    minimum = 2 if key in SIZES else 0
                    if not match or any(not minimum <= int(part) <= 7680 for part in match.groups()):
                        raise ValueError(f'Cặp tọa độ/kích thước phải có dạng A:B, từ {minimum} đến 7680.')
                    normalized = ':'.join(str(int(part)) for part in match.groups())
                    if key == 'size' and int(match[2]) > 4320:
                        raise ValueError('Chiều cao đầu ra tối đa 4320.')
                elif key in COLORS:
                    if re.fullmatch(r'[0-9a-fA-F]{6}',normalized):
                        normalized = '#' + normalized
                    if not re.fullmatch(r'(?:[A-Za-z]{1,20}|#[0-9a-fA-F]{6}|0x[0-9a-fA-F]{6})',normalized):
                        raise ValueError('Màu phải là tên màu hoặc mã RGB 6 ký tự.')
                elif key == 'logo_video_chroma':
                    normalized = normalized.removeprefix('#').removeprefix('0x')
                    if not re.fullmatch(r'[0-9a-fA-F]{6}',normalized):
                        raise ValueError('Chroma phải là mã RGB 6 ký tự.')
                elif key == 'resolution' and normalized not in VALID_RESOLUTIONS:
                    raise ValueError('Độ phân giải không được hỗ trợ.')
                elif key == 'preset' and normalized not in ('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'):
                    raise ValueError('Preset mã hóa không được hỗ trợ.')
                elif key == 'shorts_fit_mode' and normalized not in ('speedup','trim'):
                    raise ValueError('Chế độ giới hạn Shorts phải là speedup hoặc trim.')
                elif key == 'bitrate' and not re.fullmatch(r'[1-9]\d{0,8}(?:[kKmM])?',normalized):
                    raise ValueError('Bitrate phải là số nguyên dương, có thể thêm k hoặc M.')
                elif key in ('cut_start','cut_end') and not re.fullmatch(r'\d{1,3}:[0-5]\d:[0-5]\d(?:\.\d{1,3})?',normalized):
                    raise ValueError('Mốc cắt phải có dạng HH:MM:SS hoặc HH:MM:SS.mmm.')
            result[key] = normalized
        except (ValueError, TypeError, OverflowError) as exc:
            # Numeric conversion errors can include raw user text: keep them generic.
            message = str(exc) if str(exc).startswith(('Phải ', 'Góc ', 'Văn ', 'Tài ', 'Cặp ', 'Chiều ', 'Màu ', 'Chroma ', 'Độ ', 'Preset ', 'Chế ', 'Bitrate ', 'Mốc ', 'Cờ ')) else 'Giá trị không hợp lệ.'
            raise ValueError(f'config.{key}: {message}') from None
    if render_mode == 'custom_ffmpeg' and not result.get('custom_command'):
        raise ValueError('Chế độ Custom FFmpeg cần có câu lệnh.')
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 65536:
        raise ValueError('Cấu hình render vượt quá 64 KiB.')
    return result


def preset_revision(row):
    content = [row[key] for key in ('id','name','render_mode','config_json','note','created_at')]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode('utf-8')).hexdigest()


def public_preset(row):
    result = dict(row)
    result['revision'] = preset_revision(row)
    try:
        result['config'] = validate_render_config(json.loads(row['config_json']), row['render_mode'])
        result.update(valid=True, validation_error='')
    except (ValueError, TypeError, OverflowError, RecursionError):
        # Preserve raw legacy JSON for repair/export; never present corrupt data as {}.
        result.update(config=None, valid=False, validation_error='Preset có cấu hình cũ không hợp lệ; dữ liệu gốc được giữ nguyên.')
    return result
