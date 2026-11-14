"""Explicit monitor intent and a revision of its approved destination configuration."""
import hashlib
import json

from group_distribution import resolve_target_candidates

FIELDS = ('visibility', 'made_for_kids', 'altered_content')
DEFAULT_POLICY = {'version': 1, 'visibility': 'inherit', 'made_for_kids': 'inherit', 'altered_content': 'inherit'}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def policy(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise ValueError('Chính sách monitor không phải JSON hợp lệ.') from exc
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Monitor cần xác nhận chính sách metadata phiên bản 1.')
    result = {'version': 1}
    for key in FIELDS:
        selected = value.get(key, 'inherit')
        allowed = ('inherit', 'public', 'private', 'unlisted') if key == 'visibility' else ('inherit', True, False)
        if selected not in allowed or (key != 'visibility' and type(selected) not in (str, bool)):
            raise ValueError(f'Lựa chọn monitor không hợp lệ: {key}.')
        result[key] = selected
    return result


def context(mon):
    return {'target_type': 'CHANNEL' if mon.get('target_channel_id') else 'GROUP',
            'target_channel_id': mon.get('target_channel_id'),
            'target_group_name': mon.get('target_group_name') or 'Mặc định',
            'option_group_id': mon.get('option_group_id')}


def configuration(conn, target):
    channels, groups = resolve_target_candidates(conn, target)
    option_id = target.get('option_group_id')
    row = conn.execute('SELECT * FROM upload_option_groups WHERE id=?', (option_id,)).fetchone() if option_id else None
    if option_id and not row:
        raise ValueError('Bộ tùy chọn monitor không còn tồn tại.')
    options = dict(row) if row else None
    # Credentials are hashed with the revision, never returned in the preview.
    bindings = []
    for channel in channels:
        binding = {key: value for key, value in channel.items() if key not in ('status', 'note', 'created_at', 'name', 'email', 'handle')}
        profile = conn.execute('SELECT * FROM chrome_profiles WHERE name=?', (channel.get('profile_name') or '',)).fetchone()
        bindings.append({'channel': binding, 'profile': dict(profile) if profile else None})
    revision = hashlib.sha256(encode({'target': target, 'groups': groups, 'bindings': bindings, 'options': options}).encode()).hexdigest()
    return channels, options, revision


def effective(choices, channel, options):
    values, sources = {}, {}
    for key in FIELDS:
        value = choices[key]
        source = 'monitor'
        if value == 'inherit':
            source = 'channel'
            if key == 'visibility':
                value = channel.get('default_privacy')
            elif options is not None:
                source = 'option'
                value = options.get('ai_label' if key == 'altered_content' else key)
            else:
                value = channel.get(key)
        if key == 'visibility':
            if value not in ('public', 'private', 'unlisted'):
                raise ValueError('Kênh monitor thiếu chế độ hiển thị hợp lệ.')
        else:
            if value not in (0, 1, False, True):
                raise ValueError(f'Giá trị {key} của monitor không hợp lệ.')
            value = int(value)
        values[key], sources[key] = value, source
    return values, sources


def preview(conn, mon, raw_policy=None):
    choices = policy(raw_policy if raw_policy is not None else mon.get('metadata_policy_json'))
    target = context(mon)
    channels, options, revision = configuration(conn, target)
    rows = []
    for channel in channels:
        values, sources = effective(choices, channel, options)
        rows.append({'channel_id': channel['id'], 'channel_name': channel['name'], **values, 'sources': sources})
    envelope = {'version': 1, 'choices': choices, 'context': target, 'revision': revision}
    return {'policy': choices, 'revision': revision, 'channels': rows, 'envelope': envelope}


def validate_session(conn, mon):
    result = preview(conn, mon)
    if result['revision'] != mon.get('metadata_revision'):
        raise ValueError('Cấu hình/nhóm kênh monitor đã đổi. Xem trước và xác nhận lại trước khi chạy.')
    return result


def resolve_item(conn, item, channel):
    raw = item.get('monitor_metadata_policy_json')
    if not raw:
        return
    try:
        envelope = json.loads(raw)
        if envelope['version'] != 1:
            raise ValueError('Phiên bản chính sách monitor chưa được hỗ trợ.')
        choices = policy(envelope['choices'])
        channels, options, revision = configuration(conn, envelope['context'])
        if revision != envelope['revision'] or channel['id'] not in [c['id'] for c in channels]:
            raise ValueError('Cấu hình monitor đã đổi; cần xem lại đích và metadata của video.')
        values, _ = effective(choices, channel, options)
        item.update(values)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Chính sách của video monitor không hợp lệ; cần xác nhận lại.') from exc


def persist_values(conn, item):
    if item.get('monitor_metadata_policy_json'):
        conn.execute('UPDATE video_queue SET visibility=?,made_for_kids=?,altered_content=? WHERE id=?',
                     (*[item[key] for key in FIELDS], item['id']))


def manual_override(current, updates):
    """A deliberate queue edit adopts explicit values and releases monitor inheritance."""
    if current.get('monitor_metadata_policy_json') and any(key in updates for key in (*FIELDS, 'target_type', 'target_channel_id', 'target_group_names_json', 'option_group_id')):
        # The editor submits effective values. Clearing intent prevents later monitor
        # resolution from overwriting them; a fresh manual preview is still required.
        updates['monitor_metadata_policy_json'] = ''
