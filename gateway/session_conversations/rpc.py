"""Conversation RPC adapters; canonical authority retains creation and execution ownership."""
import asyncio
from functools import partial
import uuid

from gateway.session_contract import SessionRef
from gateway.session_conversations.references import authorize_view, profile_name, source_reference
from gateway.session_conversations import store
from gateway.session_local import stage_local_session
from gateway.session_local_recovery import local_identity, restore_local_session
from gateway.session_runtime_workers import tracked_write
from hermes_state_local import commit_local_session
from hermes_state_runtime import RuntimeStoreError


def _client(params):
    source = params.get('source', 'ios')
    if source not in ('ios', 'desktop'):
        raise RuntimeStoreError('invalid_params')
    return {'source': 'gui', 'follow_profile_config': True, 'client': {'name': 'agent-' + source}}


def _operation(value):
    if not isinstance(value, str) or len(value) != 36:
        raise RuntimeStoreError('invalid_operation_id')
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise RuntimeStoreError('invalid_operation_id') from exc
    return value


async def task_create(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    operation = _operation(params.get('operation_id'))
    origin = source_reference(params.get('origin'))
    title = params.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 120 or title.strip() != title:
        raise RuntimeStoreError('invalid_params')
    creation = {**_client(params), 'request_id': 'task:' + operation}
    sid = local_identity(authority.profile_id, actor.subject, creation['request_id'])
    wanted = store.digest({'origin': origin, 'title': title, 'source': params.get('source', 'ios')})
    saved = store.task_record(authority.db, actor, sid, wanted=wanted)
    replayed = saved is not None
    if saved is None:
        parent = authorize_view(authority, actor, origin['session_id'])
        staged = stage_local_session(authority, actor, creation, client_local=connection.native_owner,
                                     desktop_local=getattr(connection.transport, 'desktop_local', False))
        if isinstance(staged, dict):
            await tracked_write(authority, partial(commit_local_session, authority.db, epoch=authority.epoch,
                receipt=staged, _on_create=store.task_creation_guard(authority, actor, parent, origin, wanted, title)))
        saved = store.task_record(authority.db, actor, sid, wanted=wanted)
        if saved is None:
            raise RuntimeStoreError('storage_unavailable')
    resolved = restore_local_session(authority, sid)
    snapshot = await connection.resume(resolved, {})
    with authority.db._read_ctx() as conn:
        thread = store.project_task(authority, conn, saved)
    return {'thread': thread, 'session': snapshot, 'resolved_session_id': snapshot['stored_session_id'],
            'replayed': replayed}


async def ongoing_open(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    _client(params)  # validate caller attribution; the native Bot Chat owns its launch policy
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not {'session:read', 'session:submit', 'session:create'} <= actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    lock = getattr(authority, '_conversation_ongoing_lock', None)
    if lock is None:
        lock = authority._conversation_ongoing_lock = asyncio.Lock()
    async with lock:
        # Check an existing reservation without claiming an absent one: the
        # native target and resume can still refuse this actor's ownership.
        store.ongoing(authority, actor)
        # The Bot Chat target owner handles exact-title selection, recovery,
        # handoff and deliberate replacement. A second selector/map must never
        # divert app input from the bot's real FIFO after one of those changes.
        from gateway.session_bot import _target
        previous = authority.db.get_session_by_title('Bot Chat')
        resolved, _live, _entry = await _target(authority, actor)
        sid = resolved.session_id
        created = previous is None
        resolved = SessionRef(authority.profile_id, sid)
        snapshot = await connection.resume(resolved, {})
        reservation = await tracked_write(authority, partial(store.reserve_ongoing, authority, actor))
        if reservation['session_id'] != sid:
            await tracked_write(authority, partial(store.bind_ongoing, authority, reservation, sid))
    return {'profile': profile_name(authority), 'stored_session_id': sid,
            'resolved_session_id': snapshot['stored_session_id'], 'session': snapshot, 'created': created}


def handlers(connection):
    return {'session.task_create': partial(task_create, connection),
            'session.ongoing_open': partial(ongoing_open, connection)}
