"""Versioned, owner-scoped durable message attribution for native clients.

All transcript/receipt effects use the owning SessionDB writer transaction. This
module never runs inference; the legacy gateway owns turn admission and execution.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

PROTOCOL_VERSION = 1
MAX_TEXT = 131_072
MAX_SNAPSHOT = 12_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


class CompanionError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CompanionError('invalid_params', 400)
    return value


def operation_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise CompanionError('invalid_operation_id', 400)
    try:
        uuid.UUID(value)
    except ValueError:
        raise CompanionError('invalid_operation_id', 400) from None
    return value


def source_ref(value):
    if not isinstance(value, dict) or set(value) != {'session_id', 'row_id', 'message_uid'}:
        raise CompanionError('invalid_source_reference', 400)
    identifier(value['session_id']); identifier(value['message_uid'])
    if type(value['row_id']) is not int or not 0 < value['row_id'] <= 2**53 - 1:
        raise CompanionError('invalid_source_reference', 400)
    return dict(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_client_operations (
        owner TEXT NOT NULL, operation_id TEXT NOT NULL, kind TEXT NOT NULL,
        digest TEXT NOT NULL, target_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        message_row_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        receipt TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(owner,operation_id))''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS agent_operation_session_deleted BEFORE DELETE ON sessions
        BEGIN UPDATE agent_client_operations SET receipt='{}',target_session_id=NULL,message_row_id=NULL
        WHERE target_session_id=OLD.id; END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS agent_operation_message_deleted BEFORE DELETE ON messages
        BEGIN UPDATE agent_client_operations SET receipt='{}',message_row_id=NULL
        WHERE message_row_id=OLD.id; END''')
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_task_threads (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        owner TEXT NOT NULL, parent_session_id TEXT NOT NULL,
        origin_json TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_ongoing_sessions (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        owner TEXT NOT NULL, created_at REAL NOT NULL)''')


def _has_table(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _owned(conn, session_id, owner, profile):
    from gateway.owner_identity import same_owner
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (identifier(session_id),)).fetchone()
    if row is None:
        raise CompanionError('source_session_not_found', 404)
    if row['profile_name'] not in (None, profile):
        raise CompanionError('profile_mismatch', 403)
    if not same_owner(row['user_id'], owner):
        from gateway.session_contract import SessionActor
        from hermes_state_mutation_binding import authorize_history
        from hermes_state_runtime import RuntimeStoreError
        actor = SessionActor(owner, profile, frozenset({'session:read', 'session:create'}), 'native-client')
        try:
            allowed = authorize_history(conn, actor, session_id)
        except RuntimeStoreError:
            allowed = False
        if not allowed:
            raise CompanionError('permission_denied', 403)
    return row


def conversation_family(db, conn, session_id, owner, profile):
    """The verified display lineage, with every hop authorized on the same connection."""
    _owned(conn, session_id, owner, profile)
    row = _owned(conn, db._compression_tip_on(conn, session_id), owner, profile)
    chain = []
    seen = set()
    for _ in range(1000):
        if row['id'] in seen:
            raise CompanionError('invalid_conversation_lineage', 503)
        seen.add(row['id'])
        chain.append(row['id'])
        candidate = {**dict(row), 'model_config': _object(row['model_config'])}
        if not row['parent_session_id'] or db._is_explicit_fork_child_row(candidate, include_reset=True):
            break
        parent = conn.execute('SELECT * FROM sessions WHERE id=?', (row['parent_session_id'],)).fetchone()
        if parent is None or parent['end_reason'] != 'compression':
            break
        row = _owned(conn, parent['id'], owner, profile)
    else:
        raise CompanionError('conversation_lineage_limit', 503)
    if session_id not in chain:
        raise CompanionError('conversation_lineage_limit', 503)
    return list(reversed(chain))


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part.get('text', '') for part in content if isinstance(part, dict) and isinstance(part.get('text'), str))
    return ''


def _message(db, conn, ref, owner, profile, require_uid=True):
    _owned(conn, ref['session_id'], owner, profile)
    row = conn.execute('SELECT * FROM messages WHERE session_id=? AND id=?', (ref['session_id'], ref['row_id'])).fetchone()
    if row is None:
        raise CompanionError('source_message_not_found', 404)
    if row['role'] not in ('user', 'assistant') or not row['message_uid']:
        raise CompanionError('source_identity_unavailable')
    if require_uid and row['message_uid'] != ref['message_uid']:
        raise CompanionError('source_message_mismatch')
    text = _text(db._decode_content(row['content']))
    timestamp = row['timestamp']
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise CompanionError('source_identity_unavailable')
    return {'profile': profile, 'session_id': ref['session_id'], 'row_id': row['id'],
            'message_uid': row['message_uid'], 'role': row['role'],
            'snapshot': {'text': text[:MAX_SNAPSHOT], 'timestamp': timestamp, 'truncated': len(text) > MAX_SNAPSHOT}}


def resolve_message(db, owner, profile, session_id, row_id):
    with db._read_ctx() as conn:
        family = conversation_family(db, conn, identifier(session_id), owner, profile)
        row = conn.execute('SELECT session_id FROM messages WHERE id=?', (row_id,)).fetchone()
        if row is None or row['session_id'] not in family:
            raise CompanionError('source_message_not_found', 404)
        return _message(db, conn, {'session_id': row['session_id'], 'row_id': row_id}, owner, profile, False)


def _replay(conn, owner, op, kind, payload_digest):
    if not _has_table(conn, 'agent_client_operations'):
        return None
    row = conn.execute('SELECT * FROM agent_client_operations WHERE owner=? AND operation_id=?', (owner, op)).fetchone()
    if row is None:
        return None
    if row['kind'] != kind or row['digest'] != payload_digest:
        raise CompanionError('operation_conflict')
    if row['target_session_id'] is None or (kind == 'reply' and row['message_row_id'] is None):
        raise CompanionError('operation_target_deleted', 410)
    return _object(row['receipt'])


def _object(value):
    try:
        parsed = json.loads(value or '{}')
    except (ValueError, TypeError):
        raise CompanionError('storage_corrupt', 503) from None
    if not isinstance(parsed, dict):
        raise CompanionError('storage_corrupt', 503)
    return parsed


def _save(conn, owner, op, kind, payload_digest, target, receipt, row_id=None):
    conn.execute('INSERT INTO agent_client_operations VALUES (?,?,?,?,?,?,?,?)',
                 (owner, op, kind, payload_digest, target, row_id, json.dumps(receipt, separators=(',', ':')), time.time()))


def submission_payload(db, owner, profile, target, op, text, reply_to, attachments, *, view_session_id=None):
    operation_id(op)
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_TEXT:
        raise CompanionError('invalid_text', 400)
    ref = source_ref(reply_to)
    with db._read_ctx() as conn:
        family = conversation_family(db, conn, target, owner, profile)
        if view_session_id is not None and identifier(view_session_id) not in family:
            raise CompanionError('reply_conversation_mismatch', 403)
        root = family[0]
    return {'conversation_id': root, 'text': text, 'reply_to': ref, 'attachments': attachments}


def replay_submission(db, owner, op, payload):
    with db._read_ctx() as conn:
        return _replay(conn, owner, op, 'reply', digest(payload))


def admit_message(db, owner, profile, target, op, payload, model_text, *, view_session_id=None):
    """The exact accepted user row, reply reference, and retry receipt commit together."""
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
    from agent.context_compressor import _DB_PERSISTED_MARKER
    staged = stamp_message_timestamp({'role': 'user', 'content': model_text})
    stamp_message_uid(staged)
    wanted = digest(payload)

    def commit(conn):
        _schema(conn)
        _owned(conn, target, owner, profile)
        previous = _replay(conn, owner, op, 'reply', wanted)
        if previous is not None:
            return previous, True
        family = conversation_family(db, conn, target, owner, profile)
        if (payload['conversation_id'] != family[0] or payload['reply_to']['session_id'] not in family
                or (view_session_id is not None and view_session_id not in family)):
            raise CompanionError('reply_conversation_mismatch', 403)
        origin = _message(db, conn, payload['reply_to'], owner, profile)
        staged['display_metadata'] = {'agent_reply_to': origin, 'agent_operation_id': op,
                                      'agent_attachments': payload['attachments']}
        db._append_messages_in_transaction(conn, target, [staged])
        user = _message(db, conn, {'session_id': target, 'row_id': staged['_row_id'], 'message_uid': staged['message_uid']}, owner, profile)
        receipt = {'operation_id': op, 'stored_session_id': target, 'user_message': user, 'reply_to': origin}
        _save(conn, owner, op, 'reply', wanted, target, receipt, staged['_row_id'])
        return receipt, False

    receipt, replayed = db._execute_transcript_write(commit, [staged], patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
    if not replayed:
        staged[_DB_PERSISTED_MARKER] = True
        staged['_submit_row_session_id'] = target
    return staged, receipt, replayed


def _title(conn, desired):
    desired = desired.strip()[:120] or 'Task'
    candidate = desired
    suffix = 1
    while conn.execute('SELECT 1 FROM sessions WHERE title=?', (candidate,)).fetchone():
        suffix += 1
        candidate = f'{desired[:105]} ({suffix})'
    return candidate


def create_conversation(db, owner, profile, child_id, *, op=None, origin=None, title=None, source='ios'):
    """Thread/ongoing identity, optional context, and operation receipt are one transaction."""
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction
    if source not in ('ios', 'desktop'):
        raise CompanionError('invalid_source', 400)
    if op is not None:
        operation_id(op); origin = source_ref(origin)
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 120):
        raise CompanionError('invalid_title', 400)
    wanted = digest({'origin': origin, 'title': title, 'source': source})

    def commit(conn):
        _schema(conn)
        if op is None:
            existing = conn.execute('SELECT session_id,owner FROM agent_ongoing_sessions WHERE singleton=1').fetchone()
            if existing is not None:
                _owned(conn, existing['session_id'], owner, profile)
                return {'session_id': existing['session_id'], 'created': False}
        else:
            previous = _replay(conn, owner, op, 'task', wanted)
            if previous is not None:
                _owned(conn, previous['thread']['thread_id'], owner, profile)
                return {**previous, 'created': False}
        reference = _message(db, conn, origin, owner, profile) if origin is not None else None
        parent = _owned(conn, reference['session_id'], owner, profile) if reference else None
        chosen_title = _title(conn, title or 'Ongoing conversation')
        insert_session_row_in_transaction(db, conn, session_id=child_id, source=source,
            user_id=owner, parent_session_id=reference['session_id'] if reference else None,
            model=parent['model'] if parent else None,
            model_config={'_branched_from': reference['session_id']} if reference else None,
            profile_name=profile)
        conn.execute("UPDATE sessions SET title=?,title_source='user' WHERE id=?", (chosen_title, child_id))
        if reference is not None:
            context = [{'role': 'assistant', 'content': 'Reference excerpt for this separate task (' + reference['role'] + ' message):\n' + reference['snapshot']['text'],
                        'display_kind': 'hidden', 'display_metadata': {'agent_task_origin': reference}}]
            db._append_messages_in_transaction(conn, child_id, context)
            created_at = time.time()
            thread = {'thread_id': child_id, 'profile': profile, 'title': chosen_title,
                      'parent_session_id': reference['session_id'], 'origin': reference, 'created_at': created_at}
            conn.execute('INSERT INTO agent_task_threads VALUES (?,?,?,?,?,?)',
                         (child_id, owner, reference['session_id'], json.dumps(reference), chosen_title, created_at))
            receipt = {'thread': thread, 'session_id': child_id}
            _save(conn, owner, op, 'task', wanted, child_id, receipt)
            return {**receipt, 'created': True}
        conn.execute('INSERT INTO agent_ongoing_sessions VALUES (1,?,?,?)', (child_id, owner, time.time()))
        return {'session_id': child_id, 'created': True}
    return db._execute_write(commit, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)


def ongoing(db, owner, profile):
    with db._read_ctx() as conn:
        if not _has_table(conn, 'agent_ongoing_sessions'):
            return None
        row = conn.execute('SELECT session_id FROM agent_ongoing_sessions WHERE singleton=1').fetchone()
        if row is None:
            return None
        _owned(conn, row['session_id'], owner, profile)
        return row['session_id']


def threads(db, owner, profile, *, parent=None, child=None, limit=100, after=None):
    with db._read_ctx() as conn:
        parents = conversation_family(db, conn, parent, owner, profile) if parent is not None else None
        children = conversation_family(db, conn, child, owner, profile) if child is not None else None
        if not _has_table(conn, 'agent_task_threads'):
            return []
        query = 'SELECT t.*,s.title AS current_title FROM agent_task_threads t JOIN sessions s ON s.id=t.session_id WHERE t.owner=?'
        params = [owner]
        if parents is not None:
            query += ' AND t.parent_session_id IN (' + ','.join('?' for _ in parents) + ')'; params.extend(parents)
        if children is not None:
            query += ' AND t.session_id IN (' + ','.join('?' for _ in children) + ')'; params.extend(children)
        if after is not None:
            query += ' AND t.session_id>?'; params.append(after)
        query += ' ORDER BY t.session_id LIMIT ?'; params.append(limit)
        rows = conn.execute(query, params).fetchall()
        def project(row):
            _owned(conn, row['session_id'], owner, profile)
            tip = _owned(conn, db._compression_tip_on(conn, row['session_id']), owner, profile)
            return {'thread_id': row['session_id'], 'profile': profile, 'title': tip['title'] or row['title'],
                 'parent_session_id': row['parent_session_id'], 'origin': _object(row['origin_json']),
                 'created_at': row['created_at']}
        return [project(row) for row in rows]


def links(db, owner, profile, session_id, limit=100, after=0):
    with db._read_ctx() as conn:
        family = conversation_family(db, conn, session_id, owner, profile)
        rows = conn.execute('''SELECT * FROM messages
            WHERE session_id IN (''' + ','.join('?' for _ in family) + ''') AND role='user' AND json_valid(display_metadata)
              AND json_type(display_metadata,'$.agent_reply_to')='object'
              AND json_type(display_metadata,'$.agent_operation_id')='text'
            ORDER BY id''', family).fetchall()
        # Reuse the display projection's compaction/rewind semantics: copied protected-tail
        # user rows must produce one link, and rewound input must not reappear as a reply.
        rows = sorted(db._dedupe_display_generations(rows, drop_removed=True), key=lambda row: row['id'])
        rows = [row for row in rows if row['id'] > after][:limit]
        result = []
        for row in rows:
            meta = _object(row['display_metadata'])
            if meta.get('agent_reply_to'):
                result.append({'session_id': row['session_id'], 'row_id': row['id'], 'message_uid': row['message_uid'],
                               'operation_id': meta['agent_operation_id'], 'reply_to': meta['agent_reply_to']})
        return result, rows[-1]['id'] if len(rows) == limit else None
