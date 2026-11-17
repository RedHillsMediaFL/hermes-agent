"""No-port authenticated native HTTP/WS admission against the real legacy dispatcher."""
import base64
import hashlib
from pathlib import Path
import threading
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

from plugins.dashboard_auth import basic
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture()
def native(monkeypatch, tmp_path):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    for name, value in {'bound_host': 'acceptance.example.test', 'bound_port': 443,
                        'auth_required': True, 'session_authority': None, 'gateway_runner': None}.items():
        monkeypatch.setattr(web_server.app.state, name, value, raising=False)
    clear_providers()
    register_provider(basic.BasicAuthProvider(username='reviewer', password_hash=basic.hash_password('synthetic'), secret=b's' * 32))
    _reset_password_rate_limit()
    db = SessionDB(db_path=home / 'state.db')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda *args: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_register_session_cwd', lambda *args: None)
    monkeypatch.setattr(server, '_start_agent_build', lambda *args: None)
    monkeypatch.setattr(server, '_restart_completed_failed_agent_build', lambda *args: False)
    called = threading.Event()
    def accepted(*args):
        called.set()  # Admission is the subject; this test intentionally runs no inference.
    monkeypatch.setattr(server, '_run_after_agent_ready', accepted)
    client = TestClient(web_server.app, base_url='https://acceptance.example.test', follow_redirects=False)
    verifier = base64.urlsafe_b64encode(b'synthetic-verifier-0123456789-0123456789').rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    auth = client.get('/auth/native/authorize', params={'provider': 'basic', 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'redirect_uri': 'http://127.0.0.1:8765/callback', 'state': 'review'})
    assert auth.status_code == 302
    login = client.post('/auth/password-login', json={'provider': 'basic', 'username': 'reviewer', 'password': 'synthetic'})
    assert login.status_code == 200
    code = parse_qs(urlparse(login.json()['next']).query)['code'][0]
    exchange = client.post('/auth/native/token', json={'code': code, 'code_verifier': verifier})
    assert exchange.status_code == 200
    client.cookies.clear()
    headers = {'Authorization': 'Bearer ' + exchange.json()['access_token']}
    sessions_before = set(server._sessions)
    yield client, headers, db, called
    for key in set(server._sessions) - sessions_before:
        server._sessions.pop(key, None)
    client.close()
    db.close()
    clear_providers()


def test_authenticated_native_receipts_busy_task_ongoing_and_http_links(native):
    client, headers, db, called = native
    assert client.get('/api/agent/v1/capabilities').status_code == 401
    receipt = client.get('/api/agent/v1/capabilities?profile=default', headers=headers)
    assert receipt.status_code == 200 and receipt.json()['features']['message_replies']
    assert receipt.headers['cache-control'] == 'no-store'
    ticket = client.post('/api/auth/ws-ticket', headers=headers).json()['ticket']
    with client.websocket_connect('wss://acceptance.example.test/api/ws', subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket]) as ws:
        ready = ws.receive_json()
        assert ready['params']['type'] == 'gateway.ready'
        assert not ready['params']['payload'].get('session_authority', False)
        def call(method, params, *, error=None):
            rid = str(uuid.uuid4())
            ws.send_json({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
            for _ in range(100):
                frame = ws.receive_json()
                if frame.get('id') == rid:
                    if error:
                        assert frame['error']['data']['code'] == error, frame
                        return frame['error']
                    assert 'error' not in frame, frame
                    return frame['result']
            pytest.fail('response missing within bounded event budget')
        created = call('session.create', {'source': 'ios'})
        sid, key = created['session_id'], created['stored_session_id']
        assert server._ensure_session_db_row(server._sessions[sid])
        db.append_message(key, 'assistant', 'A persisted answer')
        row = db.get_messages_as_conversation(key, include_row_ids=True)[0]
        ref = {'session_id': key, 'row_id': row['_row_id'], 'message_uid': row['message_uid']}
        params = {'profile': 'default', 'session_id': sid, 'operation_id': str(uuid.uuid4()),
                  'text': 'A reply', 'reply_to': ref, 'attachments': []}
        first = call('session.submit_attributed', params)
        assert first['disposition'] == 'accepted' and not first['replayed'] and called.wait(timeout=10)
        retry = call('session.submit_attributed', params)
        assert retry['replayed'] and retry['user_message'] == first['user_message']
        assert retry['reply_to'] == first['reply_to']
        busy = call('session.submit_attributed', {**params, 'operation_id': str(uuid.uuid4())}, error='session_busy')
        assert busy['code'] == 4091 and busy['data']['accepted'] is False
        assert len(db.get_messages_as_conversation(key)) == 2
        task_params = {'profile': 'default', 'operation_id': str(uuid.uuid4()), 'origin': ref,
                       'title': 'Separate task', 'source': 'desktop'}
        task = call('session.task_create', task_params)
        task_retry = call('session.task_create', task_params)
        assert task_retry['replayed'] and task_retry['thread'] == task['thread']
        assert db.get_session(task['thread']['thread_id'])['source'] == 'desktop'
        ongoing = call('session.ongoing_open', {'profile': 'default', 'source': 'desktop'})
        again = call('session.ongoing_open', {'profile': 'default', 'source': 'ios'})
        assert ongoing['created'] and not again['created']
        assert ongoing['stored_session_id'] == again['stored_session_id']
        assert db.get_session(ongoing['stored_session_id'])['source'] == 'desktop'
    resolved = client.get('/api/agent/v1/messages/resolve', headers=headers,
                          params={'profile': 'default', 'session_id': key, 'row_id': row['_row_id']})
    assert resolved.status_code == 200 and resolved.json()['message'] == first['reply_to']
    links = client.get(f'/api/agent/v1/sessions/{key}/links', headers=headers, params={'profile': 'default'})
    assert links.status_code == 200, links.text
    assert links.json()['links'][0]['reply_to'] == first['reply_to']
    detail = client.get('/api/agent/v1/threads/' + task['thread']['thread_id'], headers=headers, params={'profile': 'default'})
    assert detail.status_code == 200 and detail.json()['thread'] == task['thread']
    roster = client.get('/api/agent/v1/roster', headers=headers)
    assert roster.status_code == 200
    assert roster.json()['profiles'][0]['ongoing_session_id'] == ongoing['stored_session_id']


def test_canonical_capability_receipt_never_advertises_legacy_support(native, monkeypatch):
    client, headers, _, _ = native
    from hermes_cli.web_routers import agent_companion
    monkeypatch.setattr(agent_companion, '_identity', lambda *args: (object(), 'owner', 'default'))
    receipt = client.get('/api/agent/v1/capabilities', headers=headers).json()
    assert not receipt['core_chat_compatible'] and not receipt['features']['message_replies']
    assert not receipt['features']['conversation_lineage']
    assert receipt['core_chat_protocol'] == 'canonical-session-authority'


def _rpc(ws, method, params):
    rid = str(uuid.uuid4())
    ws.send_json({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    for _ in range(100):
        frame = ws.receive_json()
        if frame.get('id') == rid:
            assert 'error' not in frame, frame
            return frame['result']
    pytest.fail('response missing within bounded event budget')


def _rotate(db, parent, child):
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='ios',
        messages=[{'role': 'assistant', 'content': 'Compression summary'}],
        profile_name='default', require_compression_lease=False)


def test_native_lineage_negotiation_alias_echoes_and_task_resume_after_compression(native):
    client, headers, db, _ = native
    capabilities = client.get('/api/agent/v1/capabilities?profile=default', headers=headers).json()
    assert capabilities['features']['conversation_lineage'] is True
    ticket = client.post('/api/auth/ws-ticket', headers=headers).json()['ticket']
    with client.websocket_connect('wss://acceptance.example.test/api/ws', subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket]) as ws:
        assert ws.receive_json()['params']['type'] == 'gateway.ready'
        created = _rpc(ws, 'session.create', {'source': 'ios'})
        sid, key = created['session_id'], created['stored_session_id']
        session = server._sessions[sid]
        assert server._ensure_session_db_row(session)
        db.append_message(key, 'assistant', 'An earlier answer')
        row = db.get_messages_as_conversation(key, include_row_ids=True)[0]
        source = {'session_id': key, 'row_id': row['_row_id'], 'message_uid': row['message_uid']}
        params = {'profile': 'default', 'session_id': sid, 'operation_id': str(uuid.uuid4()),
                  'text': 'Reply', 'reply_to': source, 'conversation_session_id': key}
        first = _rpc(ws, 'session.submit_attributed', params)
        assert first['conversation_session_id'] == key
        session.update(running=False)
        session.pop('_submit_user_row', None)
        _rotate(db, key, 'continuation')
        session['session_key'] = 'continuation'
        replay = _rpc(ws, 'session.submit_attributed', params)
        assert replay['replayed'] and replay['conversation_session_id'] == key
        assert replay['user_message'] == first['user_message']
        second = _rpc(ws, 'session.submit_attributed', {**params, 'operation_id': str(uuid.uuid4())})
        assert second['conversation_session_id'] == key and second['stored_session_id'] == 'continuation'
        assert second['user_message']['session_id'] == 'continuation' and second['reply_to']['session_id'] == key
        task_params = {'profile': 'default', 'operation_id': str(uuid.uuid4()), 'origin': source, 'title': 'Task'}
        task = _rpc(ws, 'session.task_create', task_params)
        task_id = task['thread']['thread_id']
        _rotate(db, task_id, 'task-continuation')
        task_replay = _rpc(ws, 'session.task_create', task_params)
        assert task_replay['thread']['thread_id'] == task_id and task_replay['resolved_session_id'] == 'task-continuation'
        assert task_replay['session']['session_key'] == 'task-continuation'
    resolved = client.get('/api/agent/v1/messages/resolve', headers=headers,
        params={'profile': 'default', 'session_id': 'continuation', 'row_id': source['row_id']}).json()
    assert resolved['conversation_session_id'] == 'continuation' and resolved['message']['session_id'] == key
    links = client.get(f'/api/agent/v1/sessions/{key}/links', headers=headers, params={'profile': 'default'}).json()
    assert links['conversation_session_id'] == key
    assert {link['session_id'] for link in links['links']} == {key, 'continuation'}
    threads = client.get('/api/agent/v1/threads', headers=headers,
        params={'profile': 'default', 'origin_session_id': 'continuation'}).json()
    assert threads['conversation_session_id'] == 'continuation' and threads['threads'][0]['thread_id'] == task_id
    detail = client.get('/api/agent/v1/threads/task-continuation', headers=headers, params={'profile': 'default'}).json()
    assert detail['conversation_session_id'] == 'task-continuation' and detail['thread']['thread_id'] == task_id
