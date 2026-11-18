"""Durable, claim-bound evidence for a video ID returned by an uploader."""
from datetime import datetime, timezone
import json
import re
import sqlite3


class EvidenceError(ValueError):
    """Sanitized rejection of an evidence callback."""


VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
BACKENDS = frozenset(('youtube_api', 'playwright'))
STATES = frozenset(('received', 'verified', 'mismatch', 'unknown'))
CODE = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
MAX_JSON = 8192
LEGACY_FIELDS = frozenset(('category_id', 'video_language'))
METADATA_FIELDS = LEGACY_FIELDS | frozenset((
    'privacy_status', 'made_for_kids', 'altered_content', 'youtube_channel_id', 'publish_at'))
OBSERVED_FIELDS = METADATA_FIELDS | frozenset(('upload_status', 'processing_status'))
FIELD_STATES = frozenset(('present', 'missing', 'null', 'invalid'))
METADATA_STATES = frozenset(('pending', 'verified', 'partial', 'mismatch', 'unknown'))


def _publish_at_matches(expected_val, observed_val):
    if expected_val is None and observed_val is None:
        return True
    if not isinstance(expected_val, str) or not isinstance(observed_val, str):
        return False
    try:
        exp_dt = datetime.fromisoformat(expected_val.replace('Z', '+00:00'))
        obs_dt = datetime.fromisoformat(observed_val.replace('Z', '+00:00'))
    except ValueError:
        return False
    return abs(exp_dt.timestamp() - obs_dt.timestamp()) <= 60


def _error(code):
    raise EvidenceError(code)


def _video_id(value):
    if not isinstance(value, str) or not VIDEO_ID.fullmatch(value):
        _error('video_id_invalid')
    return value


def _timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        _error('metadata_timestamp_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        _error('metadata_timestamp_invalid')
    if parsed.tzinfo is None:
        _error('metadata_timestamp_invalid')
    return parsed.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _metadata_value(key, value):
    if key == 'category_id':
        return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,2}', value) is not None
    if key == 'video_language':
        return (isinstance(value, str) and len(value) <= 64
                and re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value) is not None)
    if key in ('made_for_kids', 'altered_content'):
        return type(value) is bool
    if key == 'youtube_channel_id':
        return isinstance(value, str) and re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value) is not None
    if key == 'privacy_status':
        return type(value) is str and value in ('private', 'public', 'unlisted')
    if key == 'upload_status':
        return type(value) is str and value in ('uploaded', 'processed', 'failed', 'rejected', 'deleted')
    if key == 'processing_status':
        return type(value) is str and value in ('processing', 'succeeded', 'failed', 'terminated')
    if key == 'publish_at':
        if value is None:
            return True
        try:
            return _timestamp(value) == value
        except EvidenceError:
            return False
    return False


def _expected(value, *, version=2):
    if not isinstance(value, dict):
        _error('expected_invalid')
    allowed = LEGACY_FIELDS if version == 1 else METADATA_FIELDS
    if not value or set(value) - allowed:
        _error('expected_invalid')
    if any(not _metadata_value(key, item) for key, item in value.items()):
        _error('expected_invalid')
    return dict(value)


def _observed(value, *, version=2):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - (LEGACY_FIELDS if version == 1 else OBSERVED_FIELDS):
        _error('observed_invalid')
    result = {}
    for key, item in value.items():
        if item is None and key != 'publish_at':
            continue
        if not _metadata_value(key, item):
            _error('observed_invalid')
        result[key] = item
    return result


def _normalize_event(event, *, version=None):
    if not isinstance(event, dict):
        _error('event_invalid')
    allowed = {'backend', 'video_id', 'verification_state', 'expected', 'observed', 'code',
               'version', 'field_states', 'upload_state', 'metadata_state'}
    if set(event) - allowed or not {'backend', 'video_id', 'verification_state', 'expected'} <= set(event):
        _error('event_invalid')
    if (type(event['backend']) is not str or event['backend'] not in BACKENDS
            or type(event['verification_state']) is not str or event['verification_state'] not in STATES):
        _error('event_invalid')
    code = event.get('code')
    if code is not None and (not isinstance(code, str) or not CODE.fullmatch(code)):
        _error('code_invalid')
    if event['verification_state'] == 'received' and event.get('observed') not in (None, {}):
        _error('event_invalid')
    if version is None:
        version = event.get('version', 2 if ('field_states' in event
                            or (isinstance(event['expected'], dict) and set(event['expected']) - LEGACY_FIELDS)) else 1)
    if type(version) is not int or version not in (1, 2) or event.get('version', version) != version:
        _error('event_version_invalid')
    if version == 1 and any(key in event for key in ('field_states', 'upload_state', 'metadata_state')):
        _error('event_invalid')
    expected = _expected(event['expected'], version=version)
    observed = _observed(event.get('observed'), version=version)
    fields = {}
    ai_omitted_when_false = False
    if version == 2:
        fields = event.get('field_states', {})
        if (not isinstance(fields, dict) or set(fields) - OBSERVED_FIELDS
                or any(type(item) is not str or item not in FIELD_STATES for item in fields.values())):
            _error('field_states_invalid')
        if any((state == 'present') != (key in observed and observed[key] is not None)
               for key, state in fields.items()):
            _error('field_states_conflict')
        if event['verification_state'] == 'received' and fields:
            _error('event_invalid')
    if event['verification_state'] == 'verified':
        for key, value in expected.items():
            if key == 'publish_at' and version == 2 and _publish_at_matches(value, observed.get('publish_at')):
                continue
            if observed.get(key) == value:
                continue
            if (version == 2 and key == 'altered_content' and value is False
                    and 'altered_content' not in observed and fields.get('altered_content') == 'missing'
                    and (observed.get('upload_status') == 'processed'
                         or observed.get('processing_status') == 'succeeded')):
                ai_omitted_when_false = True
                continue
            _error('verified_metadata_mismatch')
    # Never accept caller-supplied URLs: the normalized URL is derived from the ID.
    video_id = _video_id(event['video_id'])
    result = {'version': version, 'backend': event['backend'], 'video_id': video_id,
            'video_url': f'https://youtu.be/{video_id}',
            'verification_state': event['verification_state'], 'expected': expected,
            'observed': observed, 'code': code or f"EVIDENCE_{event['verification_state'].upper()}"}
    if version == 2:
        upload_state = ('processed' if observed.get('upload_status') == 'processed'
                        or observed.get('processing_status') == 'succeeded' else 'received')
        metadata_state = ('partial' if ai_omitted_when_false else
                          {'received': 'pending', 'verified': 'verified', 'mismatch': 'mismatch',
                           'unknown': 'unknown'}[event['verification_state']])
        if (event.get('upload_state', upload_state) != upload_state
                or event.get('metadata_state', metadata_state) != metadata_state):
            _error('verification_summary_conflict')
        result.update(field_states=dict(fields), upload_state=upload_state, metadata_state=metadata_state)
    return result


def _same_identity(old, new):
    return (old.get('version') == new['version']
            and old.get('backend') == new['backend']
            and old.get('video_id') == new['video_id']
            and old.get('expected') == new['expected'])


def _safe_json(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_JSON:
        _error('evidence_too_large')
    return encoded


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _error('evidence_corrupt')
        value[key] = item
    return value


def _decode_existing(raw, queue_id, attempt_id, channel_id):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JSON:
        _error('evidence_corrupt')
    try:
        old = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, RecursionError):
        _error('evidence_corrupt')
    if old == {}:
        return {}
    keys = {'version', 'backend', 'video_id', 'video_url', 'verification_state', 'expected',
            'observed', 'code', 'queue_id', 'attempt_id', 'channel_id', 'received_at', 'updated_at'}
    if not isinstance(old, dict) or type(old.get('version')) is not int or old['version'] not in (1, 2):
        _error('evidence_corrupt')
    if old['version'] == 2:
        keys |= {'field_states', 'upload_state', 'metadata_state'}
    if set(old) != keys:
        _error('evidence_corrupt')
    if (any(type(old[key]) is not int or old[key] <= 0 for key in ('queue_id', 'attempt_id', 'channel_id'))
            or (old['queue_id'], old['attempt_id'], old['channel_id']) != (queue_id, attempt_id, channel_id)):
        _error('evidence_corrupt')
    try:
        event_keys = ['backend', 'video_id', 'verification_state', 'expected', 'observed', 'code']
        if old['version'] == 2:
            event_keys += ['field_states', 'upload_state', 'metadata_state']
        normalized = _normalize_event({key: old[key] for key in event_keys}, version=old['version'])
        for key, value in normalized.items():
            if old[key] != value:
                _error('evidence_corrupt')
        received = datetime.fromisoformat(old['received_at'].replace('Z', '+00:00'))
        updated = datetime.fromisoformat(old['updated_at'].replace('Z', '+00:00'))
        if received.tzinfo is None or updated.tzinfo is None or updated < received:
            _error('evidence_corrupt')
    except (ValueError, TypeError, AttributeError):
        _error('evidence_corrupt')
    return old


def read_evidence(raw, queue_id, attempt_id, channel_id):
    """Read sanitized persisted evidence for trusted positive integer row IDs.

    Returns a validated dictionary, or None for the empty '{}' legacy sentinel.
    Malformed, oversized, foreign-field or wrong-row evidence raises EvidenceError
    with a stable sanitized code. This function does not access SQLite or providers.
    """
    if any(type(value) is not int or value <= 0 for value in (queue_id, attempt_id, channel_id)):
        _error('evidence_context_invalid')
    try:
        return _decode_existing(raw, queue_id, attempt_id, channel_id) or None
    except (TypeError, ValueError, RecursionError):
        _error('evidence_corrupt')


def make_evidence_sink(connect, queue_id, claim_token, attempt_id, channel_id, *, backend, expected):
    """Bind an owned attempt to the worker's approved backend and metadata."""
    if (not callable(connect) or not isinstance(claim_token, str) or not claim_token or len(claim_token) > 256
            or any(type(value) is not int or value <= 0 for value in (queue_id, attempt_id, channel_id))
            or type(backend) is not str or backend not in BACKENDS):
        _error('sink_context_invalid')
    try:
        approved_expected = _expected(expected)
    except EvidenceError:
        _error('sink_context_invalid')

    def record(event):
        evidence = _normalize_event(event)
        if evidence['backend'] != backend or evidence['expected'] != approved_expected:
            _error('evidence_identity_conflict')
        conn = None
        try:
            conn = connect()
            conn.row_factory = sqlite3.Row
            if conn.in_transaction:
                _error('evidence_connection_busy')
            conn.execute('PRAGMA synchronous=FULL')
            if conn.execute('PRAGMA synchronous').fetchone()[0] != 2:
                _error('evidence_durability_unavailable')
            conn.execute('BEGIN IMMEDIATE')
            queue = conn.execute('SELECT claim_token,last_attempt_id,content_key FROM video_queue WHERE id=?', (queue_id,)).fetchone()
            attempt = conn.execute('SELECT queue_id,channel_id,content_key,outcome,evidence_json FROM upload_attempts WHERE id=?', (attempt_id,)).fetchone()
            reservation = None
            if queue and attempt:
                reservation = conn.execute('''SELECT state FROM content_reservations
                    WHERE content_key=? AND channel_id=? AND queue_id=? AND claim_token=?''',
                                            (attempt['content_key'], channel_id, queue_id, claim_token)).fetchone()
            if (not queue or queue['claim_token'] != claim_token or queue['last_attempt_id'] != attempt_id
                    or not attempt or attempt['queue_id'] != queue_id or attempt['channel_id'] != channel_id
                    or not attempt['content_key'] or queue['content_key'] != attempt['content_key']
                    or attempt['outcome'] != 'InProgress' or not reservation or reservation['state'] != 'Active'):
                _error('evidence_ownership_lost')
            old = read_evidence(attempt['evidence_json'], queue_id, attempt_id, channel_id) or {}
            if old:
                if not _same_identity(old, evidence):
                    _error('evidence_identity_conflict')
                old_state = old.get('verification_state')
                if old_state in ('verified', 'mismatch', 'unknown'):
                    if evidence['verification_state'] != old_state:
                        _error('evidence_terminal_transition')
                    if any(old.get(key) != evidence.get(key) for key in
                           ('observed', 'code', 'field_states', 'upload_state', 'metadata_state')):
                        _error('evidence_terminal_conflict')
                    conn.rollback()
                    return old
                if evidence['verification_state'] == 'received':
                    if old['code'] != evidence['code']:
                        _error('evidence_received_conflict')
                    conn.rollback()
                    return old
            elif evidence['verification_state'] != 'received':
                _error('evidence_received_required')
            timestamp = _now()
            evidence.update(queue_id=queue_id, attempt_id=attempt_id, channel_id=channel_id,
                            received_at=old.get('received_at') or timestamp,
                            updated_at=max(timestamp, old.get('updated_at', timestamp)))
            encoded = _safe_json(evidence)
            cursor = conn.execute('UPDATE upload_attempts SET evidence_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND outcome=?',
                                  (encoded, attempt_id, 'InProgress'))
            if cursor.rowcount != 1:
                _error('evidence_write_lost')
            conn.commit()
            return evidence
        except EvidenceError:
            if conn is not None:
                conn.rollback()
            raise
        except Exception:
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            _error('evidence_write_failed')
        finally:
            if conn is not None:
                conn.close()
    return record
