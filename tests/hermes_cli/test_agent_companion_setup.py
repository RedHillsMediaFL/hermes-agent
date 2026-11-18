"""Paired app setup in synthetic profiles; TestClient never opens a listener or uses real credentials."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from hermes_cli.agent_setup import authority, companion
from hermes_cli.agent_setup.models import (
    CAPABILITIES, HEADER, SUPPORTED_CAPABILITIES, OperatorError, PairingInput, pkce_challenge,
)
from hermes_cli.agent_setup.storage import OperatorStore
from hermes_cli.web_routers import agent_companion_setup as routes, agent_operator

PREFIX = '/api/agent/v1/setup/companion'
DEVICE = '85005a25-e6c4-41c7-a55a-32b82c05355a'
VERIFIER = 'a' * 43
OLD = '1' * 40
CURRENT = '2' * 40


def login(user='alice', issuer=''):
    return SimpleNamespace(provider='basic', issuer=issuer, org_id='', user_id=user,
                           expires_at=time.time() + 3600)


def incoming(session, token=None):
    return Request({'type': 'http', 'method': 'GET', 'path': '/fixture',
        'query_string': b'profile=assistant', 'headers': [] if token is None else [(HEADER.lower().encode(), token.encode())],
        'state': {'session': session}})


def body(capabilities=None):
    value = {'device_id': DEVICE, 'profiles': ['assistant'], 'code_challenge': pkce_challenge(VERIFIER),
             'code_challenge_method': 'S256'}
    if capabilities is not None:
        value['capabilities'] = list(capabilities)
    return value


def plugin(revision=CURRENT, **changes):
    return {'name': companion.NAME, 'installed': True, 'enabled': False, 'version': '0.3.6',
            'source': companion.REPOSITORY, 'revision': revision, 'head_revision': revision,
            'pinned': True, 'can_repin': True, 'reason': None, **changes}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('plugins: {enabled: [], disabled: []}\n')
    for name in ('assistant', 'other'):
        selected = home / 'profiles' / name
        selected.mkdir(parents=True)
        (selected / 'config.yaml').write_text('plugins: {enabled: [], disabled: []}\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    from hermes_cli import web_server_profiles
    monkeypatch.setattr(web_server_profiles, 'get_process_hermes_home', lambda: home)
    store = OperatorStore(home, 'fixture-installation')
    for module in (authority, agent_operator, routes):
        monkeypatch.setattr(module, 'operator_store', lambda: store)
    trusted = {'session': login()}
    calls = []
    def read_plugin():
        from hermes_constants import get_hermes_home
        calls.append(('status', Path(get_hermes_home())))
        return plugin()
    def mutate(operation, intent):
        from hermes_constants import get_hermes_home
        calls.append((operation, Path(get_hermes_home()), deepcopy(intent)))
        return {'ok': True, 'operation': operation, 'unchanged': False, 'restart_required': False,
                'sha': CURRENT, 'plugin': plugin(), 'target': companion.target(intent['ref'])}
    monkeypatch.setattr(companion, 'status', read_plugin)
    monkeypatch.setattr(companion, 'mutate', mutate)
    app = FastAPI()
    @app.middleware('http')
    async def verified_fixture_session(request, call_next):
        # This is a synthetic verified Basic session, never a local-owner ticket.
        for key, value in trusted.items():
            setattr(request.state, key, value)
        return await call_next(request)
    app.include_router(agent_operator.router)
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield SimpleNamespace(home=home, store=store, trusted=trusted, calls=calls, client=client, app=app)


def grant(environment, requested=('companion:manage',)):
    account = authority.account_id(incoming(login()))
    pending = environment.store.create(account, PairingInput.parse(body(requested)))
    environment.store.decide(pending['pairing_id'], 'approved', ('assistant',), tuple(sorted(requested)))
    return environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER), account


def test_legacy_and_combined_grants_keep_exact_approved_capabilities_through_renewal(environment):
    account = authority.account_id(incoming(login()))
    pending = environment.store.create(account, PairingInput.parse(body()))
    environment.store.decide(pending['pairing_id'], 'approved', ('assistant',))
    legacy = environment.store.redeem(pending['pairing_id'], account, DEVICE, VERIFIER)
    assert set(legacy['capabilities']) == CAPABILITIES
    assert authority.operator_authorization(incoming(login(), legacy['operator_grant']),
        profile='assistant', capability='companion:manage') is None
    combined = environment.store.create(account, PairingInput.parse(body(sorted(SUPPORTED_CAPABILITIES))))
    with pytest.raises(OperatorError, match='operator_capability_mismatch'):
        environment.store.decide(combined['pairing_id'], 'approved', ('assistant',))
    environment.store.decide(combined['pairing_id'], 'approved', ('assistant',), tuple(sorted(SUPPORTED_CAPABILITIES)))
    approved = environment.store.redeem(combined['pairing_id'], account, DEVICE, VERIFIER)
    renewed = environment.store.renew(approved['operator_grant'], account, DEVICE, str(uuid.uuid4()))
    assert renewed['capabilities'] == approved['capabilities'] == sorted(SUPPORTED_CAPABILITIES)
    assert renewed['profiles'] == ['assistant'] and renewed['device_id'] == DEVICE
    environment.store.revoke(approved['grant_id'])
    assert environment.store.principal(token=renewed['operator_grant']) is None


def test_preupgrade_null_scope_cannot_gain_companion_capability(environment):
    token, account = 'hop1_fixture_legacy_token', authority.account_id(incoming(login()))
    with environment.store.transaction() as connection:
        connection.execute('INSERT INTO grants '
            '(id,token_hash,installation,account,device,profiles,expires,revoked,capabilities) VALUES (?,?,?,?,?,?,?,0,NULL)',
            (str(uuid.uuid4()), environment.store.token_hash(token), 'fixture-installation', account,
             DEVICE, '["assistant"]', int(time.time()) + 3600))
    existing = environment.store.principal(token=token)
    renewed = environment.store.renew(token, account, DEVICE, str(uuid.uuid4()))
    assert existing['capabilities'] == renewed['capabilities'] == sorted(CAPABILITIES)
    denied = environment.client.post(PREFIX + '/enable?profile=assistant', json={'ref': CURRENT}, headers={HEADER: renewed['operator_grant']})
    assert denied.status_code == 403
    assert not environment.calls


def test_original_grant_schema_migrates_without_scope_elevation(tmp_path):
    store = OperatorStore(tmp_path, 'legacy-installation')
    store.path.parent.mkdir()
    token = 'hop1_fixture_before_capability_column'
    with sqlite3.connect(store.path) as connection:
        connection.execute('CREATE TABLE grants (id TEXT PRIMARY KEY,token_hash TEXT UNIQUE,'
            'installation TEXT,account TEXT,device TEXT,profiles TEXT,expires INTEGER,revoked INTEGER)')
        connection.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,0)',
            (str(uuid.uuid4()), store.token_hash(token), 'legacy-installation', 'fixture-account', DEVICE,
             '["assistant"]', int(time.time()) + 3600))
    original = store.principal(token=token)
    assert original['capabilities'] == sorted(CAPABILITIES)
    renewed = store.renew(token, 'fixture-account', DEVICE, str(uuid.uuid4()))
    assert renewed['capabilities'] == original['capabilities']
    assert renewed['installation_id'] == 'legacy-installation' and renewed['device_id'] == DEVICE


def test_corrupt_persisted_capabilities_fail_closed_as_unavailable(environment):
    approved, _ = grant(environment)
    with environment.store.transaction() as connection:
        connection.execute('UPDATE grants SET capabilities=? WHERE id=?',
                           ('["shell:run"]', approved['grant_id']))
    from hermes_cli.web_server_profiles import _config_profile_scope
    request = incoming(login(), approved['operator_grant'])
    request.scope['app'] = environment.app
    with _config_profile_scope('assistant'), pytest.raises(HTTPException) as failure:
        authority.operator_authorization(request,
                                         profile='assistant', capability='companion:manage')
    assert failure.value.status_code == 503


def test_combined_pairing_http_approval_requires_the_exact_requested_set(environment):
    prefix = '/api/agent/v1/operator/pairings'
    requested = sorted(SUPPORTED_CAPABILITIES)
    created = environment.client.post(prefix, json=body(requested))
    assert created.status_code == 200
    pending = created.json()
    environment.trusted['native_http_principal'] = {'purpose': 'native-http', 'capabilities': frozenset({'http:owner'})}
    approve = prefix + '/' + pending['pairing_id'] + '/approve'
    assert environment.client.post(approve, json={'profiles': ['assistant']}).status_code == 403
    assert environment.client.post(approve, json={'profiles': ['assistant'], 'capabilities': ['companion:manage']}).status_code == 403
    accepted = environment.client.post(approve, json={'profiles': ['assistant'], 'capabilities': requested})
    assert accepted.status_code == 200 and accepted.json()['capabilities'] == requested
    environment.trusted.pop('native_http_principal')
    redeemed = environment.client.post(prefix + '/' + pending['pairing_id'] + '/redeem',
        json={'device_id': DEVICE, 'code_verifier': VERIFIER})
    assert redeemed.status_code == 200 and redeemed.json()['capabilities'] == requested
    result = environment.client.post(PREFIX + '/enable?profile=assistant', json={'ref': CURRENT},
        headers={HEADER: redeemed.json()['operator_grant']})
    assert result.status_code == 200


def test_supported_capabilities_and_status_do_not_grant_management(environment):
    value = environment.client.get('/api/agent/v1/operator/status?profile=assistant')
    assert value.status_code == 200
    assert value.json()['supported_capabilities'] == sorted(SUPPORTED_CAPABILITIES)
    setup = environment.client.get(PREFIX + '?profile=assistant&ref=' + CURRENT)
    assert setup.status_code == 200 and setup.headers['cache-control'] == 'no-store'
    assert setup.json()['can_manage'] is False
    assert setup.json()['required_capability'] == 'companion:manage'
    assert setup.json()['profile'] == 'assistant'


def test_verified_session_and_grant_select_only_the_approved_profile(environment):
    approved, account = grant(environment)
    headers = {HEADER: approved['operator_grant']}
    operator = environment.client.get('/api/agent/v1/operator/status?profile=assistant', headers=headers)
    assert operator.status_code == 200 and operator.json()['can_manage_policy'] is False
    checked = environment.client.get(PREFIX + '?profile=assistant&ref=' + CURRENT, headers=headers)
    assert checked.status_code == 200 and checked.json()['can_manage'] is True
    result = environment.client.post(PREFIX + '/repin?profile=assistant', headers=headers, json={'expected_revision': OLD, 'ref': CURRENT})
    assert result.status_code == 200 and result.json()['account_id'] == account
    assert result.json()['sha'] == CURRENT
    assert environment.calls[-1] == ('repin', environment.home / 'profiles' / 'assistant', {'expected_revision': OLD, 'ref': CURRENT})
    before = list(environment.calls)
    assert environment.client.post(PREFIX + '/enable?profile=other', headers=headers, json={'ref': CURRENT}).status_code == 403
    assert environment.client.post(PREFIX + '/enable?profile=assistant&profile=other', headers=headers, json={'ref': CURRENT}).status_code == 400
    assert environment.calls == before


def test_wrong_account_expired_revoked_and_local_ticket_requests_never_mutate(environment):
    approved, _ = grant(environment)
    headers = {HEADER: approved['operator_grant']}
    environment.trusted['session'] = login('bob')
    assert environment.client.post(PREFIX + '/install?profile=assistant', headers=headers, content='not-json').status_code == 403
    environment.trusted['session'] = login()
    environment.store.revoke(approved['grant_id'])
    assert environment.client.post(PREFIX + '/install?profile=assistant', headers=headers, json={'ref': CURRENT}).status_code == 403
    approved, _ = grant(environment)
    with environment.store.transaction() as connection:
        connection.execute('UPDATE grants SET expires=0 WHERE id=?', (approved['grant_id'],))
    assert environment.client.post(PREFIX + '/install?profile=assistant', headers={HEADER: approved['operator_grant']}, json={'ref': CURRENT}).status_code == 403
    environment.trusted.clear()
    environment.trusted['native_http_principal'] = {'purpose': 'native-http', 'capabilities': frozenset({'http:owner'})}
    assert environment.client.post(PREFIX + '/install?profile=assistant', json={'ref': CURRENT}).status_code == 401
    environment.trusted['session'] = login()
    # Even a fixture containing both forms must provide an actual app grant.
    assert environment.client.post(PREFIX + '/install?profile=assistant', json={'ref': CURRENT}).status_code in {401, 403}
    assert not environment.calls


def test_capability_is_rechecked_after_body_before_manager_and_intents_are_closed(environment, monkeypatch):
    approved, _ = grant(environment)
    headers = {HEADER: approved['operator_grant']}
    for operation, intent in [('install', {'ref': CURRENT, 'identifier': 'foreign/repository'}),
                              ('enable', {'ref': CURRENT, 'force': True}),
                              ('repin', {'expected_revision': OLD, 'ref': CURRENT, 'source': 'foreign'})]:
        result = environment.client.post(PREFIX + '/' + operation + '?profile=assistant', headers=headers, json=intent)
        assert result.status_code == 400
    original_body = routes._body
    async def revoke_during_body(request):
        value = await original_body(request)
        environment.store.revoke(approved['grant_id'])
        return value
    monkeypatch.setattr(routes, '_body', revoke_during_body)
    result = environment.client.post(PREFIX + '/enable?profile=assistant', headers=headers, json={'ref': CURRENT})
    assert result.status_code == 403
    assert not environment.calls


def test_app_target_requires_exact_full_immutable_ref(environment):
    approved, _ = grant(environment)
    headers = {HEADER: approved['operator_grant']}
    for invalid in ['HEAD', 'A' * 40, '1' * 39, 'https://example.invalid/source']:
        assert environment.client.get(PREFIX + '?profile=assistant&ref=' + invalid, headers=headers).status_code == 400
        assert environment.client.post(PREFIX + '/install?profile=assistant', headers=headers, json={'ref': invalid}).status_code == 400
    repeated = environment.client.get(PREFIX + '?profile=assistant&ref=' + CURRENT + '&ref=' + OLD, headers=headers)
    assert repeated.status_code == 400
    checked = environment.client.get(PREFIX + '?profile=assistant&ref=' + OLD, headers=headers)
    assert checked.status_code == 200 and checked.json()['target'] == companion.target(OLD)
    assert all(call[0] == 'status' for call in environment.calls)


def test_enable_requires_owned_clean_source_and_reports_actual_old_pin(tmp_path, monkeypatch):
    state = plugin(OLD)
    monkeypatch.setattr(companion, 'status', lambda: deepcopy(state))
    calls = []
    monkeypatch.setattr(companion.plugins_cmd, 'dashboard_set_agent_plugin_enabled',
                        lambda name, enabled: calls.append((name, enabled)) or {'ok': True, 'unchanged': False})
    monkeypatch.setattr(companion, '_get_dashboard_plugins', lambda **kwargs: None)
    monkeypatch.setattr(companion, '_invalidate_plugins_hub_cache', lambda: None)
    with pytest.raises(HTTPException) as mismatch:
        companion.mutate('enable', {'ref': CURRENT})
    assert mismatch.value.status_code == 409 and not calls
    result = companion.mutate('enable', {'ref': OLD})
    assert result['sha'] == OLD and result['plugin']['can_repin'] is True
    assert calls == [(companion.NAME, True)]
    for refused in [plugin(source=None, reason='companion_source_mismatch', can_repin=False),
                    plugin(can_repin=False, reason='local_changes_present'),
                    plugin(can_repin=False, reason='companion_name_conflict')]:
        state.clear()
        state.update(refused)
        calls.clear()
        with pytest.raises(HTTPException) as failure:
            companion.mutate('enable', {'ref': OLD})
        assert failure.value.status_code == 409
        assert not calls


def test_status_rejects_foreign_name_and_source_without_affecting_clean_current_pin(tmp_path, monkeypatch):
    own = tmp_path / 'plugins' / companion.NAME
    own.mkdir(parents=True)
    monkeypatch.setattr(companion, 'get_hermes_home', lambda: tmp_path)
    manifest = {'name': companion.NAME, 'version': 'fixture'}
    raw = {'ok': True, 'source': companion.REPOSITORY, 'revision': CURRENT,
           'head_revision': CURRENT, 'pinned': True, 'can_repin': True, 'reason': None}
    monkeypatch.setattr(companion.plugins_cmd, '_read_manifest', lambda path: manifest)
    monkeypatch.setattr(companion.plugins_cmd, '_get_enabled_set', lambda: {companion.NAME})
    monkeypatch.setattr(companion.plugins_cmd, '_get_disabled_set', lambda: set())
    monkeypatch.setattr(companion, 'plugin_repin_status', lambda name: raw)
    assert companion.status() == plugin(version='fixture', enabled=True)
    raw['source'] = 'https://example.invalid/foreign'
    assert companion.status()['reason'] == 'companion_source_mismatch'
    assert companion.status()['source'] is None and companion.status()['can_repin'] is False
    manifest['name'] = 'another-plugin'
    assert companion.status()['reason'] == 'companion_name_conflict'


def test_fixed_install_and_repin_delegate_without_force_or_arbitrary_targets(monkeypatch):
    state = plugin(installed=False, source=None, revision=None, head_revision=None, pinned=False, can_repin=False)
    monkeypatch.setattr(companion, 'status', lambda: deepcopy(state))
    calls = []
    def install(repository, **kwargs):
        calls.append(('install', repository, kwargs))
        state.clear()
        state.update(plugin())
        return {'ok': True, 'restart_required': False}
    monkeypatch.setattr(companion.plugins_cmd, 'dashboard_install_plugin', install)
    monkeypatch.setattr(companion, '_get_dashboard_plugins', lambda **kwargs: None)
    monkeypatch.setattr(companion, '_invalidate_plugins_hub_cache', lambda: None)
    assert companion.mutate('install', {'ref': CURRENT})['sha'] == CURRENT
    assert calls == [('install', companion.REPOSITORY, {'ref': CURRENT, 'force': False, 'enable': True})]
    state.clear()
    state.update(plugin(OLD))
    def repin(name, intent):
        calls.append(('repin', name, intent))
        state.clear()
        state.update(plugin())
        return {'ok': True, 'unchanged': False}
    monkeypatch.setattr(companion, 'dashboard_repin_user_plugin', repin)
    assert companion.mutate('repin', {'expected_revision': OLD, 'ref': CURRENT})['sha'] == CURRENT
    assert calls[-1] == ('repin', companion.NAME, {'source': companion.REPOSITORY,
                                                'expected_revision': OLD, 'ref': CURRENT})
    state.clear()
    state.update(plugin(OLD))
    def wrong_readback(name, intent):
        state.clear()
        state.update(plugin(head_revision=OLD, can_repin=False, reason='installed_revision_changed'))
        return {'ok': True, 'unchanged': False}
    monkeypatch.setattr(companion, 'dashboard_repin_user_plugin', wrong_readback)
    with pytest.raises(HTTPException) as failed_readback:
        companion.mutate('repin', {'expected_revision': OLD, 'ref': CURRENT})
    assert failed_readback.value.status_code == 503
