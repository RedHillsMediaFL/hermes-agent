"""Actual plugin-host transport proves owner APIs without forwarding bearer credentials."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from hermes_cli.agent_setup import capabilities
from hermes_cli.plugin_host_http_auth import verified_session_envelope
from hermes_cli.plugins import PluginManager
from hermes_cli.web_routers.agent_conversations import router
from tests.hermes_cli.test_plugin_host import _home_with_plugins

PLUGIN = '''
def register(ctx):
    async def probe(context):
        # The restricted facade must reject retargeting, even with raw RPC access.
        params = {key: getattr(context, key) for key in
                  ('invocation', 'feature', 'installation_id', 'account_id', 'profile')}
        for changes in ({'path': '/api/plugins/other/status'}, {'profile': 'Other'},
                        {'account_id': 'other'}, {'installation_id': 'other'},
                        {'feature': 'remote_push'}, {'invocation': 'x' * 43}):
            result = await __import__('asyncio').to_thread(context.runtime.channel.call,
                'capability_read', {**params, 'path': '/api/plugins/capprobe/status', **changes})
            if result is not None:
                return context.receipt('groups', False)
        value = await context.read_json('/api/plugins/capprobe/status')
        return context.receipt('groups', value == {'owner': context.request.state.session.user_id,
            'profile': context.profile, 'version': 1, 'credentials': []})
    ctx.register_client_capability('groups', probe, paths=('/api/plugins/capprobe/status',))
'''

API = '''
from fastapi import APIRouter, HTTPException, Request
router = APIRouter()
@router.get('/status')
def status(request: Request, profile: str):
    session = getattr(request.state, 'session', None)
    if session is None:
        raise HTTPException(401)
    return {'owner': session.user_id, 'profile': profile, 'version': 1,
            'credentials': [name for name in request.headers if name in
                {'authorization', 'cookie', 'x-hermes-gateway-ticket', 'x-hermes-operator-grant'}]}
'''

TIMEOUT_PLUGIN = '''
import asyncio
release = None
finished = None
allowed = None
def register(ctx):
    async def probe(context):
        global release, finished, allowed
        release, finished = asyncio.Event(), asyncio.Event()
        await release.wait()
        allowed = await context.read_json('/api/plugins/capprobe/status')
        finished.set()
        return context.receipt('groups', allowed is not None)
    async def release_probe(**kwargs):
        release.set()
        await finished.wait()
    ctx.register_client_capability('groups', probe, paths=('/api/plugins/capprobe/status',))
    ctx.register_hook('post_tool_call', release_probe)
    ctx.register_tool(name='capprobe_timeout_result', toolset='capprobe',
        schema={'name': 'capprobe_timeout_result', 'description': 'fixture',
                'parameters': {'type': 'object', 'properties': {}}},
        handler=lambda args, **kwargs: 'denied' if allowed is None else 'granted')
'''


@pytest.fixture
def hosted(tmp_path, monkeypatch, request):
    import hermes_state
    home = _home_with_plugins(tmp_path, monkeypatch, {'capprobe': getattr(request, 'param', PLUGIN)})
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    dashboard = home / 'plugins' / 'capprobe' / 'dashboard'
    dashboard.mkdir()
    (dashboard / 'plugin_api.py').write_text(API)
    capabilities._registry.reset_for_tests()
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins['capprobe'].error is None
    app = FastAPI()
    app.state.auth_required = True
    account = SimpleNamespace(provider='basic', user_id='alice', issuer='', org_id='',
                              expires_at=time.time() + 3600)
    reads = []

    @app.middleware('http')
    async def verified(incoming, call_next):
        if incoming.headers.get('Authorization') == 'Bearer fixture-alice':
            incoming.state.session = account
        return await call_next(incoming)

    @app.get('/api/plugins/capprobe/status')
    async def owning_api(incoming: Request, profile: str):
        from hermes_cli.web_server_plugin_profiles import hosted_response
        session = verified_session_envelope(incoming, profile=profile, home=home)
        reads.append(session)
        result = await asyncio.to_thread(manager._plugin_host().asgi_request, 'capprobe', str(dashboard),
            'plugin_api.py', 'GET', '/status', 'profile=' + profile, [], b'', authenticated=session is not None,
            verified_session=session)
        return hosted_response(result, home, 'capprobe')

    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, manager=manager, reads=reads, home=home, account=account)
    manager.unload()
    manager._plugin_host().shutdown()
    capabilities._registry.reset_for_tests()


@pytest.mark.platforms('any')
def test_hosted_capability_round_trip_preserves_exact_owner_and_refuses_retargeting(hosted):
    response = hosted.client.get('/api/agent/v1/capabilities', headers={'Authorization': 'Bearer fixture-alice'})
    assert response.status_code == 200, response.text
    assert response.json()['features']['groups'] is True
    assert response.json()['features']['remote_push'] is False
    assert len(hosted.reads) == 1
    projection = hosted.reads[0]
    assert projection['session']['user_id'] == 'alice' and projection['profile'] == 'default'
    assert 'fixture-alice' not in str(projection)
    assert hosted.client.get('/api/agent/v1/capabilities').status_code == 401
    assert len(hosted.reads) == 1


@pytest.mark.platforms('any')
def test_hosted_proof_cannot_outlive_interactive_account(hosted):
    hosted.account.expires_at = time.time() - 1
    response = hosted.client.get('/api/agent/v1/capabilities', headers={'Authorization': 'Bearer fixture-alice'})
    assert response.status_code in (200, 401)
    if response.status_code == 200:
        assert response.json()['features']['groups'] is False
    assert hosted.reads == []


@pytest.mark.platforms('any')
@pytest.mark.parametrize('hosted', [TIMEOUT_PLUGIN], indirect=True)
def test_cancelled_hosted_proof_cannot_use_its_read_permit_later(hosted):
    from tools.registry import registry
    response = hosted.client.get('/api/agent/v1/capabilities', headers={'Authorization': 'Bearer fixture-alice'})
    assert response.status_code == 200 and response.json()['features']['groups'] is False
    assert hosted.reads == []
    hosted.manager.invoke_hook('post_tool_call', tool_name='fixture', args={}, result='ok')
    assert registry.dispatch('capprobe_timeout_result', {}, scope=hosted.manager.scope_key) == 'denied'
    assert hosted.reads == []


def test_capability_paths_are_owned_and_bounded():
    async def probe(context):
        return context.receipt('groups', True)
    for paths in [('/api/plugins/other/status',), ('/api/plugins/capprobe/status?profile=Other',),
                  ('/api/plugins/capprobe/status',) * 2]:
        with pytest.raises(ValueError):
            capabilities.ClientCapabilityProbe('groups', probe, paths, 'capprobe')
