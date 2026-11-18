"""Private hosted session projection uses only verified, scoped, nonsecret fields."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from hermes_cli import plugin_host_http_auth as admission


def login(user='alice', **changes):
    values = {'provider': 'basic', 'issuer': '', 'user_id': user, 'org_id': '',
              'expires_at': time.time() + 3600, 'access_token': 'never-project-access',
              'refresh_token': 'never-project-refresh'}
    values.update(changes)
    return SimpleNamespace(**values)


def request(session, *, profile='default'):
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/plugins/fixture/read',
                    'query_string': ('profile=' + profile).encode(), 'headers': [(b'authorization', b'Bearer never-project-header')],
                    'state': {'session': session}})


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('model:\n  default: fixture\n')
    other = home / 'profiles' / 'Other'
    other.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    import hermes_constants
    from hermes_cli import install_identity
    selected = [home]
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: selected[0])
    monkeypatch.setattr(install_identity, 'get_install_id', lambda: 'fixture-installation')
    return SimpleNamespace(home=home, other=other, selected=selected)


def envelope(environment, session=None, *, profile='default', home=None):
    return admission.verified_session_envelope(request(session or login(), profile=profile), profile=profile,
                                               home=home or environment.home)


def owner_app():
    app = FastAPI()

    @app.get('/read')
    def read(incoming: Request):
        current = getattr(incoming.state, 'session', None)
        if current is None:
            raise HTTPException(401)
        return {'user': current.user_id, 'issuer': current.issuer, 'org': current.org_id,
                'has_bearer': hasattr(current, 'access_token'),
                'native_owner': getattr(incoming.state, 'native_http_principal', None)}

    return app


def test_projection_is_closed_nonsecret_and_restores_a_frozen_exact_account(environment):
    value = envelope(environment)
    assert set(value) == {'schema_version', 'installation_id', 'account_id', 'profile', 'profile_id', 'session'}
    assert set(value['session']) == {'provider', 'issuer', 'user_id', 'org_id', 'expires_at'}
    assert 'never-project' not in str(value)
    restored = admission.restore_verified_session(value, profile='default', home=environment.home)
    assert restored.user_id == 'alice'
    assert not hasattr(restored, 'access_token') and not hasattr(restored, 'refresh_token')
    with pytest.raises(FrozenInstanceError):
        restored.user_id = 'bob'


@pytest.mark.parametrize('changes', [
    {'schema_version': True}, {'schema_version': 2}, {'account_id': 'another-account'},
    {'installation_id': 'another-installation'}, {'profile': 'Other'}, {'profile_id': '/another/home'},
    {'unexpected': True}, {'session': {'provider': 'basic', 'user_id': 'alice'}},
])
def test_wrong_scope_version_or_open_envelope_never_establishes_session(environment, changes):
    value = {**envelope(environment), **changes}
    assert admission.restore_verified_session(value, profile='default', home=environment.home) is None


@pytest.mark.parametrize('field,value', [
    ('user_id', 'bob'), ('provider', 'another-provider'), ('issuer', 'another-issuer'),
    ('expires_at', True), ('expires_at', float('nan')), ('expires_at', float('inf')),
    ('org_id', {'untrusted': True}), ('access_token', 'never-project'),
])
def test_session_account_rebinding_and_unclosed_fields_are_rejected(environment, field, value):
    projected = deepcopy(envelope(environment))
    projected['session'][field] = value
    assert admission.restore_verified_session(projected, profile='default', home=environment.home) is None


def test_expiry_is_rechecked_at_restore_and_missing_optional_session_fields_remain_compatible(environment, monkeypatch):
    original = login()
    del original.org_id
    del original.issuer
    projected = envelope(environment, original)
    assert projected['session']['org_id'] == '' and projected['session']['issuer'] == ''
    assert admission.restore_verified_session(projected, profile='default', home=environment.home) is not None
    monkeypatch.setattr(admission.time, 'time', lambda: original.expires_at + 1)
    assert admission.restore_verified_session(projected, profile='default', home=environment.home) is None


def test_parent_requires_verified_state_and_rejects_invalid_interactive_identity(environment):
    for current in (None, login(expires_at=0), login(user=''), login(provider=''), login(expires_at=True)):
        assert admission.verified_session_envelope(request(current), profile='default', home=environment.home) is None


def test_each_hosted_request_has_its_own_account_and_client_claims_cannot_supply_it(environment):
    app = owner_app()
    for user in ('alice', 'bob'):
        projected = envelope(environment, login(user))
        with TestClient(admission.with_hosted_admission(app, True, verified_session=projected)) as client:
            response = client.get('/read?profile=default')
            assert response.status_code == 200
            assert response.json() == {'user': user, 'issuer': '', 'org': '', 'has_bearer': False, 'native_owner': None}
    with TestClient(admission.with_hosted_admission(app, True)) as client:
        assert client.get('/read', headers={'X-Hermes-Verified-Session': str(envelope(environment)),
                                           'Authorization': 'Bearer never-project-header'}).status_code == 401
    with TestClient(admission.with_hosted_admission(app, False, verified_session=envelope(environment))) as client:
        assert client.get('/read').status_code == 401


def test_query_profile_and_current_host_home_cannot_be_rebound(environment):
    projected = envelope(environment)
    wrapped = admission.with_hosted_admission(owner_app(), True, verified_session=projected)
    with TestClient(wrapped) as client:
        assert client.get('/read').status_code == 200
        assert client.get('/read?profile=Other').status_code == 401
        assert client.get('/read?profile=default&profile=Other').status_code == 401
        environment.selected[0] = environment.other
        assert client.get('/read?profile=Other').status_code == 401
    named = envelope(environment, profile='Other', home=environment.other)
    with TestClient(admission.with_hosted_admission(owner_app(), True, verified_session=named)) as client:
        assert client.get('/read?profile=Other').status_code == 200


def test_default_home_fallback_matches_existing_host_callers(environment, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'profile_name_for_home', lambda _: None)
    projected = envelope(environment)
    assert projected is not None and projected['profile'] == 'default'
    with TestClient(admission.with_hosted_admission(owner_app(), True, verified_session=projected)) as client:
        assert client.get('/read?profile=default').status_code == 200
