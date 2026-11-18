"""Optional feature claims require actual authenticated, scoped extension contracts."""
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from hermes_cli.agent_setup import capabilities
from hermes_cli.web_routers.agent_conversations import router


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('model:\n  default: fixture\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    capabilities._registry.reset_for_tests()
    app = FastAPI()
    app.state.auth_required = True

    @app.middleware('http')
    async def verified_fixture(request, call_next):
        if request.headers.get('Authorization') == 'Bearer fixture-alice':
            request.state.session = SimpleNamespace(provider='basic', user_id='alice', issuer='',
                                                   expires_at=time.time() + 3600)
        return await call_next(request)

    @app.get('/api/plugins/fixture/status')
    def extension_status(request: Request, profile: str):
        if getattr(request.state, 'session', None) is None:
            raise HTTPException(401)
        return {'contract_version': 1, 'owner': request.state.session.user_id, 'profile': profile}

    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, home=home, tmp=tmp_path)
    capabilities._registry.reset_for_tests()


def checked(environment):
    result = environment.client.get('/api/agent/v1/capabilities',
                                     headers={'Authorization': 'Bearer fixture-alice'})
    assert result.status_code == 200, result.text
    return result.json()['features']


def test_real_owner_bound_read_is_required_before_enabling_extension(environment):
    assert checked(environment)['groups'] is False
    assert environment.client.get('/api/plugins/fixture/status?profile=default').status_code == 401
    assert environment.client.get('/api/agent/v1/capabilities').status_code == 401
    seen = []

    async def probe(context):
        data = await context.read_json('/api/plugins/fixture/status')
        seen.append(data)
        return context.receipt('groups', data == {'contract_version': 1, 'owner': 'alice', 'profile': 'default'})

    capabilities.register_provider(capabilities.ClientCapabilityProbe('groups', probe), scope=str(environment.home))
    assert checked(environment)['groups'] is True
    assert seen == [{'contract_version': 1, 'owner': 'alice', 'profile': 'default'}]
    assert checked(environment)['remote_push'] is False


@pytest.mark.parametrize('change', [
    {'profile': 'Other'}, {'installation_id': 'another-install'}, {'account_id': 'another-account'},
    {'contract_version': 2}, {'schema_version': True}, {'supported': 'true'}, {'unexpected': True},
])
def test_wrong_scope_version_or_unclosed_claim_never_enables_feature(environment, change):
    async def probe(context):
        return {**context.receipt('usage_upload', True), **change}
    capabilities.register_provider(capabilities.ClientCapabilityProbe('usage_upload', probe), scope=str(environment.home))
    assert checked(environment)['usage_upload'] is False


def test_other_profile_registry_and_failed_provider_do_not_leak_features(environment):
    async def yes(context):
        return context.receipt('life_context', True)
    capabilities.register_provider(capabilities.ClientCapabilityProbe('life_context', yes),
                                   scope=str(environment.home / 'profiles' / 'Other'))
    assert checked(environment)['life_context'] is False

    async def failed(context):
        raise RuntimeError('synthetic failure')
    capabilities.register_provider(capabilities.ClientCapabilityProbe('remote_push', failed), scope=str(environment.home))
    assert checked(environment)['remote_push'] is False


def test_selected_nondefault_profile_binds_the_proof_and_owning_read(environment):
    home = environment.home / 'profiles' / 'other'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('model:\n  default: fixture\n')
    seen = []

    async def probe(context):
        from hermes_constants import get_hermes_home
        seen.append(get_hermes_home())
        value = await context.read_json('/api/plugins/fixture/status')
        return context.receipt('groups', value == {'contract_version': 1, 'owner': 'alice', 'profile': 'other'})

    capabilities.register_provider(capabilities.ClientCapabilityProbe('groups', probe), scope=str(home))
    response = environment.client.get('/api/agent/v1/capabilities?profile=other',
                                     headers={'Authorization': 'Bearer fixture-alice'})
    assert response.status_code == 200 and response.json()['features']['groups'] is True
    assert seen == [home]
    assert checked(environment)['groups'] is False


def test_probe_cannot_retarget_read_transport(environment):
    async def probe(context):
        with pytest.raises(ValueError):
            await context.read_json('/api/plugins/fixture/../../files')
        with pytest.raises(ValueError):
            await context.read_json('https://external.invalid/api/plugins/fixture/status')
        return context.receipt('groups', False)
    capabilities.register_provider(capabilities.ClientCapabilityProbe('groups', probe), scope=str(environment.home))
    assert checked(environment)['groups'] is False


def test_capability_body_is_bounded_before_transport_buffers_it(environment):
    @environment.client.app.get('/api/plugins/fixture/oversized')
    def oversized():
        return {'payload': 'x' * 65537}

    async def probe(context):
        value = await context.read_json('/api/plugins/fixture/oversized')
        return context.receipt('groups', value is not None)

    capabilities.register_provider(capabilities.ClientCapabilityProbe('groups', probe), scope=str(environment.home))
    assert checked(environment)['groups'] is False
