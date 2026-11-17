"""Conversation metadata committed by the canonical owner's existing writer."""
from __future__ import annotations

import hashlib
import json
import time

from gateway.owner_identity import same_owner
from gateway.session_conversations.references import (
    current_target, family, message, profile_name, reference_note,
)
from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _epoch

TASK_PREFIX = 'gateway.conversation.task.v1:'
ONGOING_KEY = 'gateway.conversation.ongoing.v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc
    if not isinstance(value, dict):
        raise RuntimeStoreError('storage_unavailable')
    return value


def task_record(db, actor, sid, *, wanted=None):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (TASK_PREFIX + sid,)).fetchone()
        if row is None:
            return None
        saved = _object(row[0])
        if not same_owner(saved['owner'], actor.subject):
            raise RuntimeStoreError('permission_denied')
        if wanted is not None and saved['digest'] != wanted:
            raise RuntimeStoreError('admission_conflict')
        return saved


def task_creation_guard(authority, actor, source_owner, origin, wanted, title):
    """One trusted callback inside native local creation; no publication or nested writer."""
    def commit(conn, receipt):
        from hermes_state_mutation_binding import same_history_owner
        if not same_history_owner(conn, source_owner.session_id, origin['session_id']):
            raise RuntimeStoreError('permission_denied')
        anchor = message(authority.db, conn, origin, profile_name(authority))
        sid = receipt['session_id']
        chosen = title
        suffix = 1
        while conn.execute('SELECT 1 FROM sessions WHERE title=? AND id<>?', (chosen, sid)).fetchone():
            suffix += 1
            if suffix > 1000:
                raise RuntimeStoreError('admission_conflict')
            chosen = f'{title} #{suffix}'
        conn.execute("UPDATE sessions SET title=?,title_source='user' WHERE id=?", (chosen, sid))
        thread = {'thread_id': sid, 'profile': profile_name(authority), 'title': chosen,
                  'parent_session_id': origin['session_id'], 'origin': anchor, 'created_at': time.time()}
        # A task is an independent native route. Its origin is metadata, not a DB
        # parent edge that could merge compression lineages or cascade deletion.
        authority.db._append_messages_in_transaction(conn, sid, [{
            'role': 'assistant', 'content': reference_note(anchor, task=True), 'display_kind': 'hidden',
            'display_metadata': {'agent_task_origin': anchor}}])
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (TASK_PREFIX + sid, json.dumps({'owner': actor.subject, 'digest': wanted, 'thread': thread})))
    return commit


def reserve_ongoing(authority, actor):
    def write(conn):
        _epoch(conn, authority.epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone()
        if row is not None:
            saved = _object(row[0])
            if not same_owner(saved['owner'], actor.subject):
                raise RuntimeStoreError('permission_denied')
            return saved
        saved = {'owner': actor.subject, 'session_id': None}
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (ONGOING_KEY, json.dumps(saved)))
        return saved
    return authority.db._execute_write(write)


def bind_ongoing(authority, reservation, sid):
    def write(conn):
        _epoch(conn, authority.epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone()
        if row is None or _object(row[0]) != reservation:
            raise RuntimeStoreError('admission_conflict')
        if conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone() is None:
            raise RuntimeStoreError('not_found')
        conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                     (json.dumps({**reservation, 'session_id': sid}), ONGOING_KEY))
    return authority.db._execute_write(write)


def ongoing(authority, actor):
    with authority.db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone()
        if row is None:
            return None
        saved = _object(row[0])
        if not same_owner(saved['owner'], actor.subject):
            raise RuntimeStoreError('permission_denied')
        sid = saved['session_id']
        return sid if sid and conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone() else None


def project_task(authority, conn, saved):
    thread = saved['thread']
    target = current_target(authority.db, conn, thread['thread_id'])
    row = conn.execute('SELECT title FROM sessions WHERE id=?', (target,)).fetchone()
    return {**thread, 'title': row[0] or thread['title']} if row else thread


def threads(authority, actor, *, parent=None, child=None, limit=100, after=None):
    with authority.db._read_ctx() as conn:
        parents = family(authority.db, conn, parent) if parent is not None else None
        query = 'SELECT m.value FROM state_meta m JOIN sessions s ON s.id=substr(m.key,?) WHERE m.key GLOB ?'
        params = [len(TASK_PREFIX) + 1, TASK_PREFIX + '*']
        if parents is not None:
            query += ' AND json_extract(m.value,\'$.thread.parent_session_id\') IN (' + ','.join('?' for _ in parents) + ')'
            params.extend(parents)
        if child is not None:
            query += ' AND s.id=?'
            params.append(child)
        if after is not None:
            query += ' AND s.id>?'
            params.append(after)
        query += ' ORDER BY s.id LIMIT ?'
        params.append(limit)
        saved = [_object(row[0]) for row in conn.execute(query, params)]
        return [project_task(authority, conn, item) for item in saved if same_owner(item['owner'], actor.subject)]


def links(authority, session_id, *, limit=100, after=0):
    with authority.db._read_ctx() as conn:
        chain = family(authority.db, conn, session_id)
        rows = conn.execute('SELECT * FROM messages WHERE session_id IN (' + ','.join('?' for _ in chain) +
            ") AND role='user' AND json_valid(display_metadata) "
            "AND json_type(display_metadata,'$.agent_reply_to')='object' "
            "AND json_type(display_metadata,'$.agent_operation_id')='text' ORDER BY id", chain).fetchall()
        rows = sorted(authority.db._dedupe_display_generations(rows, drop_removed=True), key=lambda row: row['id'])
        page = [row for row in rows if row['id'] > after][:limit]
        result = []
        for row in page:
            meta = _object(row['display_metadata'])
            result.append({'session_id': row['session_id'], 'row_id': row['id'], 'message_uid': row['message_uid'],
                           'operation_id': meta['agent_operation_id'], 'reply_to': meta['agent_reply_to']})
        return result, page[-1]['id'] if len(page) == limit else None


def retire(conn, session_ids):
    """Retire copied context when its destination is deleted; source deletion preserves tasks."""
    targets = set(session_ids)
    if not targets:
        return
    rows = conn.execute('SELECT key,value FROM state_meta WHERE key GLOB ? OR key=?',
                        (TASK_PREFIX + '*', ONGOING_KEY)).fetchall()
    gone = []
    for key, raw in rows:
        saved = _object(raw)
        sid = saved.get('session_id') if key == ONGOING_KEY else saved['thread']['thread_id']
        if sid is None:
            continue
        policy = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone()
        current = _object(policy[0])['entry']['session_id'] if policy is not None else sid
        if sid in targets or current in targets:
            gone.append((key,))
    conn.executemany('DELETE FROM state_meta WHERE key=?', gone)
