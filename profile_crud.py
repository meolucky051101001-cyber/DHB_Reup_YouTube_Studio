"""Validation and reference checks for local profile mutations."""
import json
import re
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict

from config_validation import ConfigInput, Name, Text
from phase4_rules import normalize_group_names


def local_profile_name(value):
    if not isinstance(value, str) or not value or value != value.strip() or value.endswith('.'):
        raise ValueError('Tên Profile phải là văn bản, không có khoảng trắng đầu/cuối hoặc dấu chấm cuối.')
    if len(value) > 100 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value) or value in ('.', '..'):
        raise ValueError('Tên Profile phải là tên thư mục đơn, tối đa 100 ký tự, không chứa ký tự đặc biệt của Windows.')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])', value.split('.')[0].rstrip(), re.I):
        raise ValueError('Tên Profile trùng tên thiết bị dành riêng của Windows.')
    return value


class ProfileUpdate(ConfigInput):
    # Name/path/source identify the Chrome session and are immutable here.
    model_config = ConfigDict(extra='forbid')
    proxy: Text = ''
    note: Text = ''


class ProfileCreate(ProfileUpdate):
    name: Annotated[Name, BeforeValidator(local_profile_name)]


def binding_references(conn, channel_ids, profile_name=''):
    """Check direct, frozen and group targets plus durable publication records."""
    ids = set(channel_ids)
    groups = set()
    for row in conn.execute('SELECT channel_id,group_name FROM channel_group_memberships'):
        if row['channel_id'] in ids:
            groups.add(row['group_name'])
    for row in conn.execute('SELECT id,group_name FROM youtube_channels'):
        if row['id'] in ids and row['group_name']:
            groups.add(row['group_name'])
    queue_ids = []
    for row in conn.execute('SELECT * FROM video_queue'):
        direct = row['target_channel_id'] in ids or row['assigned_channel_id'] in ids
        direct = direct or bool(profile_name and row['target_profile_name'] == profile_name)
        is_group = (not row['target_channel_id'] and not row['target_profile_name']
                    and str(row['target_type'] or 'GROUP').upper() == 'GROUP')
        selected = normalize_group_names(row['target_group_names_json'], legacy_name=row['target_group_name']) if is_group else []
        if direct or groups.intersection(selected):
            queue_ids.append(row['id'])
    monitors = []
    for row in conn.execute('SELECT id,target_channel_id,target_group_name,is_running FROM monitors'):
        if row['target_channel_id'] in ids or (not row['target_channel_id'] and (row['target_group_name'] or 'Mặc định') in groups):
            monitors.append(dict(row))
    evidence = []
    for table in ('published_history', 'upload_attempts', 'content_reservations'):
        if any(row[0] in ids for row in conn.execute(f'SELECT DISTINCT channel_id FROM {table}')):
            evidence.append(table)
    for row in conn.execute('SELECT snapshot_json FROM upload_approval_items'):
        try:
            snapshot = json.loads(row[0])
        except (ValueError, TypeError):
            continue  # Malformed approvals cannot be used to launch a worker.
        if isinstance(snapshot, dict) and (snapshot.get('channel_id') in ids or
                                          (profile_name and snapshot.get('profile_name') == profile_name)):
            evidence.append('upload_approval_items')
            break
    return queue_ids, monitors, evidence
