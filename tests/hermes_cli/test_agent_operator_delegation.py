"""Operator delegation with dummy verified sessions and a real temporary SQLite store.

No provider, model, listener or production Hermes home is used. Run only inside the
owner's source-test sandbox; TestClient communicates with in-memory ASGI apps.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
from copy import deepcopy
from types import SimpleNamespace
import sqlite3
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
import pytest

from hermes_cli.agent_setup import authority
from hermes_cli.agent_setup.models import HEADER, OperatorError, PairingInput, pkce_challenge
from hermes_cli.agent_setup.storage import OperatorStore
from hermes_cli.plugin_host_http_auth import with_hosted_admission
from hermes_cli.web_routers import agent_operator

PREFIX = '/api/agent/v1/operator'
DEVICE = '85005a25-e6c4-41c7-a55a-32b82c05355a'
VERIFIER = 'a' * 43


def session(user='alice', provider='basic', issuer=''):
    return SimpleNamespace(user_id=user, provider=provider, issuer=issuer,
                           expires_at=time.time() + 3600)


def request(*, login=None, token=None, state=None, profile='Assistant'):
    headers = [] if token is None else [(HEADER.lower().encode(), token.encode())]
    values = dict(state or {})
    if login is not None:
        values['session'] = login
    return Request({'type': 'http', 'method': 'GET', 'path': '/fixture',
                    'query_string': ('profile=' + profile).encode(),
                    'headers': headers, 'state': values})


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    store = OperatorStore(home, 'fixture-installation')
    monkeypatch.setattr(authority, 'operator_store', lambda: store)
    monkeypatch.setattr(agent_operator, 'operator_store', lambda: store)
    import hermes_constants
    from hermes_cli import web_server_cron
    selected = home / 'profiles' / 'Assistant'
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: selected)
    monkeypatch.setattr(web_server_cron, '_cron_profile_home',
                        lambda profile: (profile, home / 'profiles' / profile))

    def validate_profile(_request, profile):
        if profile not in {'Assistant', 'Other'}:
            raise HTTPException(404, 'profile_missing')

    monkeypatch.setattr(authority, 'validate_profile', validate_profile)
    monkeypatch.setattr(agent_operator, 'validate_profile', validate_profile)
    trusted = {'session': session()}
    app = FastAPI()

    @app.middleware('http')
    async def verified_fixture_context(incoming, call_next):
        # Tests vary the trusted middleware result, never accept an owner header.
        for name, value in trusted.items():
            setattr(incoming.state, name, value)
        return await call_next(incoming)

    app.include_router(agent_operator.router)
    with TestClient(app) as client:
        yield SimpleNamespace(store=store, trusted=trusted, client=client)


def pairing_body():
    return {'device_id': DEVICE, 'profiles': ['Assistant'],
            'code_challenge': pkce_challenge(VERIFIER), 'code_challenge_method': 'S256'}


def approved(environment):
    account = authority.account_id(request(login=session()))
    pending = environment.store.create(account, PairingInput.parse(pairing_body()))
    environment.store.decide(pending['pairing_id'], 'approved', ('Assistant',))
    return pending, account


def granted(environment):
    pending, account = approved(environment)
    value = environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
    return value, account


def test_pairing_requires_interactive_auth_and_owner_approval_before_parsing(environment):
    environment.trusted.clear()
    response = environment.client.post(PREFIX + '/pairings', content=b'not-json')
    assert response.status_code == 401
    environment.trusted['session'] = session()
    pending = environment.client.post(PREFIX + '/pairings', json=pairing_body()).json()
    denied = environment.client.post(PREFIX + '/pairings/' + pending['pairing_id'] + '/approve',
        content=b'not-json', headers={'X-Owner': 'true', 'X-Hermes-Native-Principal': 'http:owner'})
    assert denied.status_code == 403
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'pending'
    environment.trusted['native_http_principal'] = {
        'purpose': 'native-http', 'capabilities': frozenset({'http:owner'})}
    wrong = environment.client.post(PREFIX + '/pairings/' + pending['pairing_id'] + '/approve',
                                    json={'profiles': ['Other']})
    assert wrong.status_code == 403
    accepted = environment.client.post(PREFIX + '/pairings/' + pending['pairing_id'] + '/approve',
                                       json={'profiles': ['Assistant']})
    assert accepted.status_code == 200 and accepted.json()['status'] == 'approved'
    assert accepted.headers['cache-control'] == 'no-store'


def test_exact_login_account_cannot_inspect_redeem_or_use_another_accounts_grant(environment):
    pending, account = approved(environment)
    other = authority.account_id(request(login=session('bob')))
    assert account != other
    assert account != authority.account_id(request(login=session('alice', issuer='another-issuer')))
    environment.trusted['session'] = session('bob')
    assert environment.client.get(PREFIX + '/pairings/' + pending['pairing_id']).status_code == 404
    response = environment.client.post(PREFIX + '/pairings/' + pending['pairing_id'] + '/redeem',
                                       json={'device_id': DEVICE, 'code_verifier': VERIFIER})
    assert response.status_code == 404
    grant = environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
    response = environment.client.get(PREFIX + '/grants/current', headers={HEADER: grant['operator_grant']})
    assert response.status_code == 403
    assert authority.operator_authorization(request(login=session('bob'), token=grant['operator_grant']),
        profile='Assistant', capability='recipient-policy:write') is None


def test_wrong_pkce_device_and_replay_do_not_consume_or_duplicate_grants(environment):
    pending, account = approved(environment)
    for device, verifier in [(str(uuid.uuid4()), VERIFIER), (DEVICE, 'b' * 43)]:
        with pytest.raises(OperatorError):
            environment.store.redeem(pending['pairing_id'], account, device, verifier)
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'approved'

    def redeem_once(_):
        try:
            return environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
        except OperatorError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem_once, range(2)))
    assert sum(value is not None for value in results) == 1
    with sqlite3.connect(environment.store.path) as connection:
        rows = connection.execute('SELECT token_hash FROM grants').fetchall()
    assert len(rows) == 1
    token = next(value['operator_grant'] for value in results if value is not None)
    assert token not in environment.store.path.read_bytes().decode('latin-1')


def test_expired_pairing_cannot_be_approved_or_redeemed(environment, monkeypatch):
    account = authority.account_id(request(login=session()))
    pending = environment.store.create(account, PairingInput.parse(pairing_body()))
    monkeypatch.setattr('hermes_cli.agent_setup.storage.time.time', lambda: pending['expires_at'] + 1)
    with pytest.raises(OperatorError):
        environment.store.decide(pending['pairing_id'], 'approved', ('Assistant',))
    with pytest.raises(OperatorError):
        environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'expired'


def test_renewal_replays_once_and_revoking_ancestor_revokes_descendants(environment):
    original, account = granted(environment)
    operation = str(uuid.uuid4())
    rotated = environment.store.renew(original['operator_grant'], account, DEVICE, operation)
    replay = environment.store.renew(original['operator_grant'], account, DEVICE, operation)
    assert replay == rotated
    assert environment.store.principal(token=original['operator_grant']) is None
    assert environment.store.principal(token=rotated['operator_grant']) is not None
    for user, device, action in [(account + '-other', DEVICE, operation),
            (account, str(uuid.uuid4()), operation), (account, DEVICE, str(uuid.uuid4()))]:
        with pytest.raises(OperatorError):
            environment.store.renew(original['operator_grant'], user, device, action)
    descendant = environment.store.renew(rotated['operator_grant'], account, DEVICE, str(uuid.uuid4()))
    environment.store.revoke(original['grant_id'])
    assert environment.store.principal(token=descendant['operator_grant']) is None
    with pytest.raises(OperatorError):
        environment.store.renew(original['operator_grant'], account, DEVICE, operation)


def test_http_renewal_requires_exact_account_device_and_explicit_operation(environment):
    original, _ = granted(environment)
    url = PREFIX + '/grants/current/renew'
    body = {'device_id': DEVICE, 'operation_id': str(uuid.uuid4())}
    assert environment.client.post(url, content=b'not-json').status_code == 403
    headers = {HEADER: original['operator_grant']}
    environment.trusted['session'] = session('bob')
    assert environment.client.post(url, json=body, headers=headers).status_code == 403
    environment.trusted['session'] = session()
    wrong_device = {**body, 'device_id': str(uuid.uuid4())}
    assert environment.client.post(url, json=wrong_device, headers=headers).status_code == 403
    assert environment.client.post(url, json={**body, 'capabilities': ['shell:run']}, headers=headers).status_code == 400
    rotated = environment.client.post(url, json=body, headers=headers)
    assert rotated.status_code == 200 and rotated.headers['cache-control'] == 'no-store'
    assert environment.client.post(url, json=body, headers=headers).json() == rotated.json()
    assert environment.client.get(PREFIX + '/grants/current', headers=headers).status_code == 403
    current_headers = {HEADER: rotated.json()['operator_grant']}
    current = environment.client.get(PREFIX + '/grants/current', headers=current_headers)
    assert current.json()['grant_id'] == rotated.json()['grant_id']
    assert environment.client.delete(PREFIX + '/grants/current', headers=current_headers).json()['revoked'] is True
    assert environment.client.post(url, json=body, headers=headers).status_code == 403
    assert environment.client.post(url, json={**body, 'operation_id': str(uuid.uuid4())},
                                   headers=current_headers).status_code == 403


def test_local_cli_requires_typed_approval_and_supports_inspection_denial_and_revocation(environment, monkeypatch, capsys):
    from hermes_cli.agent_setup import cli
    from hermes_cli.subcommands.operator import build_operator_parser
    monkeypatch.setattr(cli, 'local_store', lambda: environment.store)
    parser = argparse.ArgumentParser()
    build_operator_parser(parser.add_subparsers())

    def invoke(*args):
        parsed = parser.parse_args(['operator', *args])
        parsed.func(parsed)

    account = authority.account_id(request(login=session()))
    pending = environment.store.create(account, PairingInput.parse(pairing_body()))
    invoke('list')
    listed = capsys.readouterr().out
    assert pending['pairing_id'] in listed and DEVICE in listed
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SystemExit):
        invoke('approve', pending['verification_code'])
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'pending'
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: 'approve')
    invoke('approve', pending['verification_code'])
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'approved'
    grant = environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
    invoke('revoke', grant['grant_id'])
    assert environment.store.principal(token=grant['operator_grant']) is None
    assert grant['operator_grant'] not in capsys.readouterr().out
    pending = environment.store.create(account, PairingInput.parse(pairing_body()))
    invoke('deny', pending['verification_code'])
    assert environment.store.pairing(pending['pairing_id'])['status'] == 'denied'


def test_local_cli_storage_failure_is_bounded(environment, monkeypatch, capsys):
    from hermes_cli.agent_setup import cli
    def unavailable():
        raise sqlite3.OperationalError('fixture private storage path')
    monkeypatch.setattr(cli, 'local_store', unavailable)
    with pytest.raises(SystemExit):
        cli.command(SimpleNamespace(operator_action='list'))
    assert capsys.readouterr().err.strip() == 'operator_authority_unavailable'


def test_operator_is_wired_into_the_normal_cli_parser(environment, monkeypatch):
    from hermes_cli import main
    monkeypatch.setattr(main, '_register_plugin_cli_commands', lambda subparsers: None)
    parser, _ = main._build_cli_parser()
    parsed = parser.parse_args(['operator', 'approve', '1234ABCD'])
    assert parsed.operator_action == 'approve' and parsed.code == '1234ABCD'
    assert callable(parsed.func)


def test_delegation_is_immutable_exact_profile_and_fixed_capability(environment):
    grant, _ = granted(environment)
    incoming = request(login=session(), token=grant['operator_grant'])
    principal = authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write')
    assert principal is not None
    assert principal['profiles'] == ('Assistant',)
    with pytest.raises(TypeError):
        principal['account_id'] = 'replacement'
    assert authority.operator_authorization(incoming, profile='Other', capability='recipient-policy:write') is None
    assert authority.operator_authorization(incoming, profile='Assistant', capability='shell:run') is None
    expired = session(); expired.expires_at = time.time() - 1
    assert authority.operator_authorization(request(login=expired, token=grant['operator_grant']),
        profile='Assistant', capability='recipient-policy:write') is None


def test_hosted_envelope_is_request_local_and_rechecks_revocation(environment):
    grant, _ = granted(environment)
    envelope = authority.hosted_operator_envelope(request(login=session(), token=grant['operator_grant']), 'Assistant')
    assert envelope is not None and 'operator_grant' not in envelope
    child = FastAPI()

    @child.get('/probe')
    def probe(incoming: Request):
        principal = authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write')
        return {'allowed': principal is not None, 'raw_grant': HEADER in incoming.headers}

    with TestClient(with_hosted_admission(child, True, envelope)) as client:
        assert client.get('/probe?profile=Assistant').json() == {'allowed': True, 'raw_grant': False}
    forged = deepcopy(envelope); forged['account_id'] = 'another-account'
    with TestClient(with_hosted_admission(child, True, forged)) as client:
        assert client.get('/probe?profile=Assistant').json()['allowed'] is False
    environment.store.revoke(grant['grant_id'])
    with TestClient(with_hosted_admission(child, True, envelope)) as client:
        assert client.get('/probe?profile=Assistant').json()['allowed'] is False
    with TestClient(with_hosted_admission(child, False, envelope)) as client:
        assert client.get('/probe?profile=Assistant').json()['allowed'] is False
    with TestClient(with_hosted_admission(child, True)) as client:
        assert client.get('/probe?profile=Assistant', headers={'X-Hermes-Hosted-Operator-Principal': 'forged'}).json()['allowed'] is False


def test_bound_home_and_single_profile_selector_are_part_of_authority(environment, monkeypatch):
    import hermes_constants
    grant, _ = granted(environment)
    incoming = request(login=session(), token=grant['operator_grant'])
    incoming.scope['query_string'] = b'profile=Assistant&profile=Assistant'
    assert authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write') is None
    incoming.scope['query_string'] = b''
    assert authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write') is None
    incoming = request(login=session(), token=grant['operator_grant'])
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: environment.store.path.parent.parent / 'profiles' / 'Other')
    assert authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write') is None


def test_local_owner_scoped_helper_cannot_write_another_bound_profile(environment):
    home = environment.store.path.parent.parent
    incoming = request(state={'native_http_principal': {'purpose': 'native-http',
        'capabilities': frozenset({'http:owner'}), 'subject': 'local-fixture',
        'profile_id': str(home / 'profiles' / 'Assistant')}})
    assert authority.operator_authorization(incoming, profile='Assistant', capability='recipient-policy:write') is not None
    assert authority.operator_authorization(incoming, profile='Other', capability='recipient-policy:write') is None


def test_malformed_nested_and_duplicate_json_is_bounded_and_does_not_write(environment):
    for body in [b'[' * 1500 + b']' * 1500,
                 b'{"device_id":"first","device_id":"second"}', b'{"value":NaN}']:
        response = environment.client.post(PREFIX + '/pairings', content=body)
        assert response.status_code == 400
        assert response.headers['cache-control'] == 'no-store'
    assert environment.store.pending() == []


def test_launch_profile_hosting_strips_raw_grant_and_projects_verified_authority(environment, monkeypatch):
    from hermes_cli import web_server_dashboard
    from hermes_cli import web_server_plugin_profiles
    from hermes_cli import plugin_host_http_auth
    import hermes_constants

    grant, _ = granted(environment)
    calls = []

    class Host:
        home = environment.store.path.parent.parent

        def asgi_request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'status': 200}

    monkeypatch.setattr(web_server_dashboard, '_hosted_plugin_for_request', lambda _: (Host(), '/fixture', 'api.py'))
    monkeypatch.setattr(hermes_constants, 'profile_name_for_home', lambda _: 'Assistant')
    monkeypatch.setattr(plugin_host_http_auth, 'admitted_for_sensitive_route', lambda _: True)

    async def scoped(_connection: HTTPConnection, profile: str | None = None):
        yield

    async def run_hosted(_home, _name, callback):
        callback(1)
        return JSONResponse({'ok': True})

    monkeypatch.setattr(web_server_dashboard, '_plugin_route_secret_scope', scoped)
    monkeypatch.setattr(web_server_plugin_profiles, 'run_hosted_request', run_hosted)
    app = FastAPI()

    @app.middleware('http')
    async def logged_in(incoming, call_next):
        incoming.state.session = session()
        return await call_next(incoming)

    web_server_dashboard._mount_hosted_plugin_api(app, {'name': 'fixture'}, 'api.py')
    with TestClient(app) as client:
        response = client.get('/api/plugins/fixture/probe?profile=Assistant', headers={HEADER: grant['operator_grant']})
    assert response.status_code == 200
    args, kwargs = calls[0]
    assert all(name.lower() != HEADER.lower() for name, _ in args[6])
    assert kwargs['authenticated'] is True
    assert kwargs['operator_principal']['grant_id'] == grant['grant_id']


@pytest.mark.parametrize('selector,allowed', [('profile=Assistant', True),
    ('profile=Other', False), ('profile=Assistant&profile=Assistant', False)])
def test_secondary_host_retains_exact_profile_and_strips_raw_credentials(environment, monkeypatch, selector, allowed):
    from hermes_cli import web_server_plugin_profiles as forwarding
    from hermes_cli import plugin_host_http_auth, plugins
    import hermes_constants
    grant, _ = granted(environment)
    home = environment.store.path.parent.parent / 'profiles' / 'Assistant'
    calls = []

    class Host:
        def asgi_request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'status': 200}

    monkeypatch.setattr(forwarding, '_secondary_hosted_plugin',
        lambda name: {'_api_file': 'api.py', '_dir': '/fixture'})
    monkeypatch.setattr(forwarding, 'secondary_profile_home', lambda: home)
    monkeypatch.setattr(hermes_constants, 'profile_name_for_home', lambda value: 'Assistant')
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: SimpleNamespace(_plugin_host=lambda: Host()))
    monkeypatch.setattr(plugin_host_http_auth, 'admitted_for_sensitive_route', lambda incoming: True)

    async def run_hosted(home, name, callback):
        callback(1)
        return JSONResponse({'ok': True})

    monkeypatch.setattr(forwarding, 'run_hosted_request', run_hosted)
    app = FastAPI()

    @app.get('/api/plugins/fixture/probe')
    async def forward(incoming: Request):
        incoming.state.session = session()
        return await forwarding.forward_secondary_plugin_request(incoming, 'fixture')

    with TestClient(app) as client:
        response = client.get('/api/plugins/fixture/probe?' + selector + '&%74oken=secret&ordinary=kept',
            headers={HEADER: grant['operator_grant'], 'Authorization': 'Bearer fixture-secret'})
    assert response.status_code == 200
    args, kwargs = calls[0]
    assert args[5] == selector + '&ordinary=kept'
    assert all(name.lower() not in {HEADER.lower(), 'authorization', 'cookie'} for name, _ in args[6])
    assert kwargs['authenticated'] is True
    assert (kwargs['operator_principal'] is not None) is allowed
