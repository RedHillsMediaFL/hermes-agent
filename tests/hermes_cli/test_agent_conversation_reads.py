"""Conversation read routes use the existing authenticated owner and stored row identity."""
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from hermes_cli.web_routers.agent_conversations import router
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    (home / 'config.yaml').write_text('model:\n  default: fixture-model\n')
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('conversation', source='test')
    db.create_session('unrelated', source='test')
    db.append_message('conversation', 'assistant', 'Stored source')
    with db._read_ctx() as conn:
        row_id, uid = conn.execute('SELECT id,message_uid FROM messages WHERE session_id=?', ('conversation',)).fetchone()
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home.resolve()),
        instance_id='fixture', db=db, epoch=begin_runtime_epoch(db, instance_id='fixture'))
    authority.sessions['conversation'] = LiveSession(None, 'route')
    authority.sessions['unrelated'] = LiveSession(None, 'other-route')
    app = FastAPI()
    app.state.auth_required = True
    app.state.session_authority = authority

    @app.middleware('http')
    async def authenticated_fixture(request, call_next):
        # The production dashboard middleware owns authentication. The route
        # consumes its verified session object, never a second password/token.
        if request.headers.get('Authorization') == 'Bearer fixture-owner':
            request.state.session = SimpleNamespace(user_id='fixture', provider='basic', issuer='')
        return await call_next(request)
    app.include_router(router)
    with TestClient(app) as connected:
        yield connected, row_id, uid
    db.close()


def test_read_routes_require_auth_and_resolve_exact_stored_identity(client):
    connected, row_id, uid = client
    path = '/api/agent/v1/messages/resolve'
    params = {'session_id': 'conversation', 'row_id': row_id}
    assert connected.get(path, params=params).status_code == 401
    authorized = {'Authorization': 'Bearer fixture-owner'}
    result = connected.get(path, params=params, headers=authorized)
    assert result.status_code == 200, result.text
    assert result.headers['Cache-Control'] == 'no-store'
    message = result.json()['message']
    assert message['session_id'] == 'conversation' and message['row_id'] == row_id and message['message_uid'] == uid
    assert message['snapshot']['text'] == 'Stored source'
    wrong = connected.get(path, params={**params, 'session_id': 'unrelated'}, headers=authorized)
    assert wrong.status_code == 404, wrong.text


def test_capability_requires_the_native_authority_and_links_are_paged(client):
    connected, _row_id, _uid = client
    headers = {'Authorization': 'Bearer fixture-owner'}
    assert connected.get('/api/agent/v1/capabilities').status_code == 401
    result = connected.get('/api/agent/v1/capabilities', headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['core_chat_protocol'] == 'canonical-session-authority'
    assert result.json()['features']['message_replies'] is True
    links = connected.get('/api/agent/v1/sessions/conversation/links', headers=headers)
    assert links.status_code == 200 and links.json()['links'] == []
    assert links.json()['next_cursor'] is None
    connected.app.state.session_authority = None
    standalone = connected.get('/api/agent/v1/capabilities', headers=headers)
    assert standalone.status_code == 200 and standalone.json()['core_chat_compatible'] is True
    assert standalone.json()['core_chat_protocol'] == 'legacy-dashboard-v1'
    assert connected.get('/api/agent/v1/threads', headers=headers).status_code == 200
    # A gateway waiting for its owner must not become a standalone second writer.
    connected.app.state.gateway_runner = object()
    assert connected.get('/api/agent/v1/capabilities', headers=headers).status_code == 503
    assert connected.get('/api/agent/v1/threads', headers=headers).status_code == 503
