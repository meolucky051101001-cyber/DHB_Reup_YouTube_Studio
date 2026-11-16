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


def _error(code):
    raise EvidenceError(code)


def _video_id(value):
    if not isinstance(value, str) or not VIDEO_ID.fullmatch(value):
        _error('video_id_invalid')
    return value


def _expected(value):
    if not isinstance(value, dict):
        _error('expected_invalid')
    allowed = {'category_id', 'video_language'}
    if not value or set(value) - allowed:
        _error('expected_invalid')
    category = value.get('category_id')
    if 'category_id' in value and (not isinstance(category, str) or not re.fullmatch(r'[1-9][0-9]{0,2}', category)):
        _error('expected_invalid')
    language = value.get('video_language')
    if 'video_language' in value and (not isinstance(language, str) or len(language) > 64
                                 or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', language)):
        _error('expected_invalid')
    return {**({'category_id': category} if 'category_id' in value else {}),
            **({'video_language': language} if 'video_language' in value else {})}


def _observed(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {'category_id', 'video_language'}:
        _error('observed_invalid')
    result = {}
    for key in ('category_id', 'video_language'):
        if key not in value or value[key] is None:
            continue
        if key == 'category_id' and (not isinstance(value[key], str) or not re.fullmatch(r'[1-9][0-9]{0,2}', value[key])):
            _error('observed_invalid')
        if key == 'video_language' and (not isinstance(value[key], str) or len(value[key]) > 64
                                        or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value[key])):
            _error('observed_invalid')
        result[key] = value[key]
    return result


def _normalize_event(event):
    if not isinstance(event, dict):
        _error('event_invalid')
    allowed = {'backend', 'video_id', 'verification_state', 'expected', 'observed', 'code'}
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
    expected = _expected(event['expected'])
    observed = _observed(event.get('observed'))
    if event['verification_state'] == 'verified' and any(observed.get(key) != value for key, value in expected.items()):
        _error('verified_metadata_mismatch')
    # Never accept caller-supplied URLs: the normalized URL is derived from the ID.
    video_id = _video_id(event['video_id'])
    return {'version': 1, 'backend': event['backend'], 'video_id': video_id,
            'video_url': f'https://youtu.be/{video_id}',
            'verification_state': event['verification_state'], 'expected': expected,
            'observed': observed, 'code': code or f"EVIDENCE_{event['verification_state'].upper()}"}


def _same_identity(old, new):
    return old.get('backend') == new['backend'] and old.get('video_id') == new['video_id'] \
        and old.get('expected') == new['expected']


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
    if not isinstance(old, dict) or set(old) != keys or type(old.get('version')) is not int or old['version'] != 1:
        _error('evidence_corrupt')
    if (any(type(old[key]) is not int or old[key] <= 0 for key in ('queue_id', 'attempt_id', 'channel_id'))
            or (old['queue_id'], old['attempt_id'], old['channel_id']) != (queue_id, attempt_id, channel_id)):
        _error('evidence_corrupt')
    try:
        normalized = _normalize_event({key: old[key] for key in
                                      ('backend', 'video_id', 'verification_state', 'expected', 'observed', 'code')})
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
                    if any(old[key] != evidence[key] for key in ('observed', 'code')):
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
