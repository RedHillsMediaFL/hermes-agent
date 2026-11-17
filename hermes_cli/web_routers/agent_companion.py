"""Authenticated native-client attribution/threads protocol; no second chat engine."""
from fastapi import APIRouter, HTTPException, Query, Request, Response
import asyncio

from hermes_companion import CompanionError

router = APIRouter(prefix='/api/agent/v1', tags=['Native client conversations'])


def _identity(request, profile):
    from hermes_cli.web_server_sessions import _session_mutation_context
    from hermes_cli.web_routers.sessions import _serving_profile
    authority, actor = _session_mutation_context(request, profile)
    return authority, actor.subject, _serving_profile(profile)


async def _read(request, profile, callback):
    from hermes_cli.web_routers.sessions import _with_db
    authority, owner, name = _identity(request, profile)
    if authority is not None:
        raise HTTPException(409, 'chat_protocol_unsupported')
    try:
        return await asyncio.to_thread(_with_db, profile, lambda db: callback(db, owner, name), read_only=True)
    except CompanionError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get('/capabilities')
def capabilities(request: Request, response: Response, profile: str | None = None):
    from hermes_cli.install_identity import get_install_id
    authority, _, name = _identity(request, profile)
    supported = authority is None and getattr(request.app.state, 'session_authority', None) is None
    response.headers['Cache-Control'] = 'no-store'
    return {'protocol_version': 1, 'serving_profile': name, 'installation_id': get_install_id(),
            'core_chat_protocol': 'legacy-dashboard-v1' if supported else 'canonical-session-authority',
            'core_chat_compatible': supported,
            'features': {'message_replies': supported, 'task_threads': supported,
                         'conversation_lineage': supported,
                         'ongoing_conversations': supported, 'agent_management': True,
                         'groups': False, 'usage_upload': False, 'life_context': False,
                         'remote_push': False, 'live_activities': False}}


@router.get('/messages/resolve')
async def resolve(request: Request, response: Response, session_id: str,
                  row_id: int = Query(..., ge=1, le=2**53-1), profile: str | None = None):
    from hermes_companion import resolve_message
    response.headers['Cache-Control'] = 'no-store'
    return {'conversation_session_id': session_id,
            'message': await _read(request, profile, lambda db, owner, name: resolve_message(db, owner, name, session_id, row_id))}


@router.get('/sessions/{session_id}/links')
async def message_links(request: Request, response: Response, session_id: str,
                        profile: str | None = None, limit: int = Query(100, ge=1, le=200),
                        after: int = Query(0, ge=0)):
    from hermes_companion import links
    response.headers['Cache-Control'] = 'no-store'
    rows, cursor = await _read(request, profile, lambda db, owner, name: links(db, owner, name, session_id, limit, after))
    return {'conversation_session_id': session_id, 'links': rows, 'next_cursor': cursor}


@router.get('/threads')
async def thread_list(request: Request, response: Response, profile: str | None = None,
                      origin_session_id: str | None = None, limit: int = Query(100, ge=1, le=200),
                      after: str | None = None):
    from hermes_companion import threads
    response.headers['Cache-Control'] = 'no-store'
    rows = await _read(request, profile, lambda db, owner, name: threads(db, owner, name, parent=origin_session_id, limit=limit, after=after))
    return {'conversation_session_id': origin_session_id, 'threads': rows,
            'next_cursor': rows[-1]['thread_id'] if len(rows) == limit else None}


@router.get('/threads/{session_id}')
async def thread_detail(request: Request, response: Response, session_id: str, profile: str | None = None):
    from hermes_companion import threads
    response.headers['Cache-Control'] = 'no-store'
    rows = await _read(request, profile, lambda db, owner, name: threads(db, owner, name, child=session_id))
    if not rows:
        raise HTTPException(404, 'thread_not_found')
    return {'conversation_session_id': session_id, 'thread': rows[0]}


@router.get('/roster')
async def roster(request: Request, response: Response):
    from hermes_cli.web_routers.profiles import _read_profiles
    from hermes_companion import ongoing
    response.headers['Cache-Control'] = 'no-store'
    data = await _read_profiles()
    rows = []
    for entry in data['profiles']:
        name = entry['name']
        def project(db, owner, profile):
            session_id = ongoing(db, owner, profile)
            return session_id, db.get_compression_tip(session_id) if session_id else None
        try:
            session_id, tip = await _read(request, name, project)
            rows.append({**entry, 'ongoing_session_id': session_id,
                         'ongoing_resolved_session_id': tip or session_id})
        except HTTPException as exc:
            if exc.status_code not in (404, 503):
                raise
            rows.append({**entry, 'ongoing_session_id': None, 'ongoing_resolved_session_id': None,
                         'ongoing_error': 'store_unavailable'})
    return {'protocol_version': 1, 'profiles': rows}
