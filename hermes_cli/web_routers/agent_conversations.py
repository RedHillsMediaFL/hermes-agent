"""Authenticated native-owner projections for ongoing conversations and exact message links."""
from fastapi import APIRouter, HTTPException, Query, Request, Response

from gateway.session_conversations import store
from gateway.session_conversations.references import (
    authorize_view, current_target, resolve_message,
)
from hermes_state_runtime import RuntimeStoreError

router = APIRouter(prefix='/api/agent/v1', tags=['Native conversations'])


def _identity(request, profile):
    from hermes_cli.web_server_sessions import _session_mutation_context
    return _session_mutation_context(request, profile)


def _standalone(request, profile):
    """Dispatch only when the existing owner gate confirms a standalone store.

    Import the legacy adapter after application startup; importing it during
    router registration would cycle through the legacy server's registrations.
    A composed gateway without an authority is refused by the identity gate.
    """
    authority, _actor = _identity(request, profile)
    if authority is not None:
        return None
    from hermes_cli.web_routers import agent_companion
    return agent_companion


def _failure(exc):
    missing = {'not_found', 'source_message_not_found', 'thread_not_found'}
    forbidden = {'permission_denied', 'profile_mismatch'}
    invalid = {'invalid_params', 'invalid_source_reference'}
    status = 404 if exc.reason in missing else 403 if exc.reason in forbidden else 400 if exc.reason in invalid else 409
    if exc.reason in {'storage_unavailable', 'session_authority_unavailable'}:
        status = 503
    return HTTPException(status, exc.reason)


async def _read(request, profile, callback):
    from gateway.session_authorities import async_owner_scope
    from hermes_cli.web_server_sessions import _native_history_transport
    authority, actor = _identity(request, profile)
    if authority is None:
        raise HTTPException(409, 'chat_protocol_unsupported')
    try:
        async with async_owner_scope(authority):
            with _native_history_transport(request, authority, actor):
                return callback(authority, actor)
    except RuntimeStoreError as exc:
        raise _failure(exc) from None


@router.get('/capabilities')
async def capabilities(request: Request, response: Response, profile: str | None = None):
    if legacy := _standalone(request, profile):
        return await legacy.capabilities(request, response, profile)
    from hermes_cli.install_identity import get_install_id
    from hermes_cli.web_routers.sessions import _serving_profile
    authority, actor = _identity(request, profile)
    supported = authority is not None and 'session:read' in actor.capabilities
    from hermes_cli.agent_setup.capabilities import extension_features
    extensions = await extension_features(request, _serving_profile(profile), actor)
    response.headers['Cache-Control'] = 'no-store'
    return {'protocol_version': 1, 'serving_profile': _serving_profile(profile),
            'installation_id': get_install_id(), 'core_chat_protocol': 'canonical-session-authority',
            'core_chat_compatible': supported,
            'features': {'message_replies': supported, 'task_threads': supported,
                         'conversation_lineage': supported, 'ongoing_conversations': supported,
                         'agent_management': True, **extensions}}


@router.get('/messages/resolve')
async def resolve(request: Request, response: Response, session_id: str,
                  row_id: int = Query(..., ge=1, le=2**53 - 1), profile: str | None = None):
    if legacy := _standalone(request, profile):
        return await legacy.resolve(request, response, session_id, row_id, profile)
    response.headers['Cache-Control'] = 'no-store'
    value = await _read(request, profile, lambda authority, actor: resolve_message(authority, actor, session_id, row_id))
    return {'conversation_session_id': session_id, 'message': value}


@router.get('/sessions/{session_id}/links')
async def links(request: Request, response: Response, session_id: str, profile: str | None = None,
                limit: int = Query(100, ge=1, le=200), after: int = Query(0, ge=0)):
    if legacy := _standalone(request, profile):
        return await legacy.message_links(request, response, session_id, profile, limit, after)
    response.headers['Cache-Control'] = 'no-store'
    def read(authority, actor):
        ref = authorize_view(authority, actor, session_id)
        with authority.db._read_ctx() as conn:
            target = current_target(authority.db, conn, ref.session_id)
        return store.links(authority, target, limit=limit, after=after)
    rows, cursor = await _read(request, profile, read)
    return {'conversation_session_id': session_id, 'links': rows, 'next_cursor': cursor}


@router.get('/threads')
async def threads(request: Request, response: Response, profile: str | None = None,
                  origin_session_id: str | None = None, limit: int = Query(100, ge=1, le=200), after: str | None = None):
    if legacy := _standalone(request, profile):
        return await legacy.thread_list(request, response, profile, origin_session_id, limit, after)
    response.headers['Cache-Control'] = 'no-store'
    def read(authority, actor):
        parent = None
        if origin_session_id is not None:
            ref = authorize_view(authority, actor, origin_session_id)
            with authority.db._read_ctx() as conn:
                parent = current_target(authority.db, conn, ref.session_id)
        return store.threads(authority, actor, parent=parent, limit=limit, after=after)
    rows = await _read(request, profile, read)
    return {'conversation_session_id': origin_session_id, 'threads': rows,
            'next_cursor': rows[-1]['thread_id'] if len(rows) == limit else None}


@router.get('/threads/{session_id}')
async def thread(request: Request, response: Response, session_id: str, profile: str | None = None):
    if legacy := _standalone(request, profile):
        return await legacy.thread_detail(request, response, session_id, profile)
    response.headers['Cache-Control'] = 'no-store'
    def read(authority, actor):
        ref = authorize_view(authority, actor, session_id)
        return store.threads(authority, actor, child=ref.session_id)
    rows = await _read(request, profile, read)
    if not rows:
        raise HTTPException(404, 'thread_not_found')
    return {'conversation_session_id': session_id, 'thread': rows[0]}


@router.get('/roster')
async def roster(request: Request, response: Response):
    if legacy := _standalone(request, None):
        return await legacy.roster(request, response)
    from hermes_cli.web_routers.profiles import _read_profiles
    response.headers['Cache-Control'] = 'no-store'
    entries = await _read_profiles()
    rows = []
    for entry in entries['profiles']:
        def read(authority, actor):
            sid = store.ongoing(authority, actor)
            if sid is None:
                return None, None
            authorize_view(authority, actor, sid)
            with authority.db._read_ctx() as conn:
                return sid, current_target(authority.db, conn, sid)
        try:
            sid, tip = await _read(request, entry['name'], read)
            rows.append({**entry, 'ongoing_session_id': sid, 'ongoing_resolved_session_id': tip})
        except HTTPException as exc:
            if exc.status_code not in (404, 409, 503):
                raise
            rows.append({**entry, 'ongoing_session_id': None, 'ongoing_resolved_session_id': None,
                         'ongoing_error': 'store_unavailable'})
    return {'protocol_version': 1, 'profiles': rows}
