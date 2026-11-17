"""Optional real-port PKCE acceptance; all listeners and inference are private fixtures."""
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlparse
import uuid

import httpx
import pytest
from websockets.asyncio.client import connect

from tests.gateway.fixtures.local_recovery_probe import Model, seed_offline_models_dev
from tests.waiting import async_wait_until


@contextmanager
def claimed_port(service):
    if not shutil.which('portclaim'):
        pytest.skip('Real listener acceptance requires the portclaim CLI')
    port = json.loads(subprocess.check_output([
        'portclaim', 'acquire', '--service', service, '--project', 'hermes-native-acceptance',
        '--pid', str(os.getpid()), '--json',
    ], text=True))['port']
    try:
        yield port
    finally:
        subprocess.run(['portclaim', 'release', str(port)], check=True, capture_output=True)


@contextmanager
def synthetic_model():
    with claimed_port('native-acceptance-model') as port:
        peer = ThreadingHTTPServer(('127.0.0.1', port), Model)
        peer.requests = []
        peer.blocked, peer.release = threading.Event(), threading.Event()
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        try:
            yield peer, f'http://127.0.0.1:{port}/v1'
        finally:
            peer.release.set()
            peer.shutdown()
            peer.server_close()
            thread.join(timeout=5)


@asynccontextmanager
async def owner_runtime(home, beta):
    from gateway.config import GatewayConfig
    from gateway.control_socket import GatewayControlServer
    from gateway.run import GatewayRunner
    from gateway.run_api import start_gateway_api
    from gateway.run_runtime import initialize_gateway_runtime, publish_gateway_runtime_ready
    from gateway.runtime_ownership import process_ownership
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from plugins.dashboard_auth import basic

    clear_providers()
    register_provider(basic.BasicAuthProvider(username='reviewer',
        password_hash=basic.hash_password('synthetic'), secret=b's' * 32))
    process_ownership.reserve([home, beta])
    config = GatewayConfig(multiplex_profiles=True, sessions_dir=home / 'sessions')
    config._runtime_profile_homes = (('beta', beta),)
    runner = GatewayRunner(config)
    with claimed_port('native-acceptance-owner') as port:
        try:
            await initialize_gateway_runtime(runner)
            runner.session_control_server = GatewayControlServer()
            assert await runner.session_control_server.start()
            runner.session_api = await start_gateway_api(runner, host='127.0.0.1', port=port)
            assert await runner.start()
            publish_gateway_runtime_ready(runner)
            yield runner, f'http://127.0.0.1:{port}'
        finally:
            await runner.stop()
            if hasattr(runner, 'session_control_server'):
                await runner.session_control_server.stop()
            process_ownership.close()
            clear_providers()


async def pkce(client):
    verifier = base64.urlsafe_b64encode(b'synthetic-verifier-0123456789-0123456789').rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    auth = await client.get('/auth/native/authorize', params={'provider': 'basic',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'redirect_uri': 'http://127.0.0.1:8765/callback', 'state': 'review'})
    assert auth.status_code == 302, auth.text
    login = await client.post('/auth/password-login', json={
        'provider': 'basic', 'username': 'reviewer', 'password': 'synthetic'})
    assert login.status_code == 200, login.text
    code = parse_qs(urlparse(login.json()['next']).query)['code'][0]
    exchange = await client.post('/auth/native/token', json={'code': code, 'code_verifier': verifier})
    assert exchange.status_code == 200, exchange.text
    client.cookies.clear()
    return {'Authorization': 'Bearer ' + exchange.json()['access_token']}


class Socket:
    def __init__(self, ws):
        self.ws, self.events = ws, []

    async def call(self, method, *, expected_reason=None, **params):
        rid = str(uuid.uuid4())
        await self.ws.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}))
        async with asyncio.timeout(60):
            while True:
                frame = json.loads(await self.ws.recv())
                if frame.get('id') == rid:
                    if expected_reason is not None:
                        assert frame['error']['data']['reason'] == expected_reason, frame
                        return frame['error']
                    assert 'error' not in frame, frame
                    return frame['result']
                self.events.append(frame)


@asynccontextmanager
async def phone_socket(client, origin, headers):
    ticket = await client.post('/api/auth/ws-ticket', headers=headers)
    assert ticket.status_code == 200, ticket.text
    async with connect(origin.replace('http:', 'ws:') + '/api/ws', subprotocols=[
            'hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket.json()['ticket']]) as ws:
        yield Socket(ws)


async def terminal(socket, profile, sid, admission):
    async def completed():
        receipt = await socket.call('prompt.receipt', profile=profile,
            session_id=sid, admission_id=admission['admission_id'])
        if receipt['status'] == 'terminal':
            assert receipt['outcome'] == 'completed', receipt
            return receipt
        return None
    return await async_wait_until(completed, timeout=75, what='native admission completion')


async def projections(client, headers, sessions):
    for profile, sid in sessions.items():
        response = await client.get('/api/agent/v1/capabilities', headers=headers, params={'profile': profile})
        assert response.status_code == 200, response.text
        assert response.json()['core_chat_compatible']
        assert response.json()['core_chat_protocol'] == 'canonical-session-authority'
        links = await client.get(f'/api/agent/v1/sessions/{sid}/links', headers=headers, params={'profile': profile})
        assert links.status_code == 200, links.text
    roster = await client.get('/api/agent/v1/roster', headers=headers)
    assert roster.status_code == 200, roster.text
    found = {row['name']: row['ongoing_session_id'] for row in roster.json()['profiles']}
    assert {name: found[name] for name in sessions} == sessions


async def stage_file(client, headers, profile, workspace):
    cwd = await client.get('/api/fs/default-cwd', headers=headers, params={'profile': profile})
    assert cwd.status_code == 200 and cwd.json()['cwd'] == str(workspace), cwd.text
    managed = await client.get('/api/files', headers=headers, params={'path': cwd.json()['cwd']})
    assert managed.status_code == 200 and managed.json()['path'] == str(workspace), managed.text
    content = ('Private fixture notes for ' + profile).encode()
    path = workspace / '.agent-native-uploads' / str(uuid.uuid4()) / 'notes.txt'
    uploaded = await client.post('/api/files/upload-stream', headers=headers,
        data={'path': str(path), 'overwrite': 'false'}, files={'file': ('notes.txt', content, 'text/plain')})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()['entry']['size'] == len(content) and path.read_bytes() == content
    return {'path': str(path), 'kind': 'file', 'name': 'notes.txt', 'mime_type': 'text/plain', 'size': len(content)}


@pytest.mark.asyncio
async def test_native_http_boundary_on_a_claimed_port(tmp_path, monkeypatch):
    from gateway import run_api
    from tests.gateway import test_native_http_auth
    start = run_api.start_gateway_api
    with claimed_port('native-http-boundary') as port:
        async def start_claimed(runner, **kwargs):
            return await start(runner, **dict(kwargs, port=port))
        monkeypatch.setattr(run_api, 'start_gateway_api', start_claimed)
        await test_native_http_auth.test_native_http_preserves_gated_auth_and_actual_socket_boundary(
            tmp_path)


@pytest.mark.asyncio
async def test_pkce_two_profiles_real_inference_and_restart_reply_replay(tmp_path, monkeypatch):
    import hermes_state
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    beta = home / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setenv('HERMES_BASE_HOME', str(home))
    sessions, submissions, staged = {}, {}, {}
    with synthetic_model() as (model, url):
        monkeypatch.setenv('OPENAI_API_KEY', 'loopback-only')
        monkeypatch.setenv('OPENAI_BASE_URL', url)
        config = {'gateway': {'multiplex_profiles': True},
            'model': {'provider': 'custom', 'default': 'synthetic-native', 'base_url': url},
            'dashboard': {'public_url': 'https://acceptance.example.test'},
            'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': [], 'gui': []}}
        for profile_home in (home, beta):
            workspace = profile_home / 'workspace'
            workspace.mkdir()
            (profile_home / 'config.yaml').write_text(json.dumps({**config, 'terminal': {'cwd': str(workspace)}}))
            (profile_home / '.env').write_text(f'OPENAI_API_KEY=loopback-only\nOPENAI_BASE_URL={url}\n')
        seed_offline_models_dev(home)
        async with owner_runtime(home, beta) as (runner, origin), httpx.AsyncClient(
                base_url=origin, trust_env=False, follow_redirects=False, timeout=60) as client:
            assert (await client.get('/api/agent/v1/capabilities')).status_code == 401
            headers = await pkce(client)
            async with phone_socket(client, origin, headers) as socket:
                for profile in ('default', 'beta'):
                    runtime = await socket.call('runtime.describe', profile=profile)
                    assert 'session-conversations-v1' in runtime['capabilities']
                    ongoing = await socket.call('session.ongoing_open', profile=profile, source='ios')
                    sid = sessions[profile] = ongoing['stored_session_id']
                    assert ongoing['session']['info']['profile_id'] == str(home if profile == 'default' else beta)
                    staged[profile] = await stage_file(client, headers, profile,
                        (home if profile == 'default' else beta) / 'workspace')
                    submissions[profile] = {'profile': profile, 'session_id': sid,
                        'submission_id': str(uuid.uuid4()), 'text': 'Original ' + profile,
                        'conversation_attachments': [staged[profile]]}
                    sent = await socket.call('prompt.submit', **submissions[profile])
                    await terminal(socket, profile, sid, sent)
                snapshot = await socket.call('session.resume', profile='beta', session_id=sessions['beta'])
                source = next(row for row in reversed(snapshot['messages']) if row['role'] == 'assistant')
                reference = {'session_id': sessions['beta'], 'row_id': source['row_id'],
                             'message_uid': source['message_uid']}
                params = {'profile': 'beta', 'session_id': sessions['beta'], 'submission_id': str(uuid.uuid4()),
                    'text': 'Follow up beta', 'conversation_session_id': sessions['beta'], 'reply_to': reference}
                admitted = await socket.call('prompt.submit', **params)
                await terminal(socket, 'beta', sessions['beta'], admitted)
                task_params = {'profile': 'beta', 'operation_id': str(uuid.uuid4()), 'origin': reference,
                               'title': 'Independent task', 'source': 'ios'}
                task = await socket.call('session.task_create', **task_params)
                assert task['session']['stored_session_id'] != sessions['beta']
                await projections(client, headers, sessions)
                resolved = await client.get('/api/agent/v1/messages/resolve', headers=headers,
                    params={'profile': 'beta', 'session_id': sessions['beta'], 'row_id': reference['row_id']})
                assert resolved.status_code == 200 and resolved.json()['message']['message_uid'] == reference['message_uid']
                grant = runner.session_ticket_store.mint(profile_id=str(home), subject='uid:fixture', purpose='native-http')
                wrong = await client.get('/api/agent/v1/capabilities?profile=beta',
                    headers={'X-Hermes-Gateway-Ticket': grant})
                assert wrong.status_code == 403, wrong.text
                local_ticket = runner.session_ticket_store.mint(profile_id=str(home), subject='uid:fixture', purpose='interactive')
                async with connect(origin.replace('http:', 'ws:') + '/api/ws', subprotocols=[
                        'hermes-gateway-v1', 'hermes-gateway-ticket.' + local_ticket]) as local_ws:
                    await Socket(local_ws).call('runtime.describe', profile='beta', expected_reason='profile_mismatch')
                profile_events = [frame['params'] for frame in socket.events if
                    frame.get('method') == 'event' and frame.get('params', {}).get('session_id')]
                assert profile_events and all(event.get('profile_id') in (str(home), str(beta)) for event in profile_events)
        inference_count = len(model.requests)
        Path(staged['beta']['path']).unlink()
        # Simulate deletion of the original excerpt while the native owner is stopped.
        import sqlite3
        with sqlite3.connect(beta / 'state.db') as db:
            db.execute('DELETE FROM messages WHERE id=?', (reference['row_id'],))
        async with owner_runtime(home, beta) as (_runner, origin), httpx.AsyncClient(
                base_url=origin, trust_env=False, follow_redirects=False, timeout=60) as client:
            headers = await pkce(client)
            async with phone_socket(client, origin, headers) as socket:
                await socket.call('session.resume', profile='beta', session_id=sessions['beta'])
                replay = await socket.call('prompt.submit', **params)
                assert replay['admission_id'] == admitted['admission_id'] and replay['status'] == 'terminal'
                attachment_replay = await socket.call('prompt.submit', **submissions['beta'])
                assert attachment_replay['status'] == 'terminal'
                task_replay = await socket.call('session.task_create', **task_params)
                assert task_replay['replayed'] and task_replay['thread']['thread_id'] == task['thread']['thread_id']
                await projections(client, headers, sessions)
                snapshot = await socket.call('session.resume', profile='beta', session_id=sessions['beta'])
                reply = next(row for row in snapshot['messages'] if row.get('display_metadata', {}).get('agent_operation_id') == params['submission_id'])
                assert reply['content'] == 'Follow up beta' and reply['display_metadata']['agent_reply_to']['message_uid'] == reference['message_uid']
                assert len(model.requests) == inference_count
