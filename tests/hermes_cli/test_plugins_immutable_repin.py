"""Pinned Git updates use exact source/revision CAS and preserve user state through PM."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()


def _head(repo):
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo, message):
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", message)
    return _head(repo)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Real Git + PM publication; environment construction is outside this contract."""
    from hermes_cli import plugin_catalog, plugins_cmd as pc
    from pm import paths

    home, repo, core = (tmp_path / name for name in ("home", "origin", "core"))
    for directory in (home / "plugins", repo, core):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(paths, "repo_root", lambda: core)
    monkeypatch.setattr(pc, "_plugins_dir", lambda: home / "plugins")
    monkeypatch.setattr(pc, "_scan_on_install_enabled", lambda: False)
    monkeypatch.setattr(plugin_catalog, "fetch_live_catalog", lambda **kwargs: None)
    monkeypatch.setattr(plugin_catalog, "load_catalog", lambda catalog_dir=None: [])
    monkeypatch.setattr(plugin_catalog, "load_removed_list", lambda catalog_dir=None: [])
    monkeypatch.setattr(pc, "_console", lambda: type("Console", (), {"print": lambda *a, **k: None})())

    publications = []

    def publish_without_environment(*_args, plugins=None, **_kwargs):
        from hermes_cli.runtime_state import finish_publication, runtime_lock
        from pm.plugin_inputs import Selection, StagedUpdate
        from pm.publication import PluginSelection, StagedPlugin

        with runtime_lock(core):
            if isinstance(plugins, StagedUpdate):
                change = StagedPlugin(dict(plugins.data))
            else:
                assert isinstance(plugins, Selection)
                change = PluginSelection(dict(plugins.data))
            change.publish(core)
            finish_publication(core)
        publications.append(type(change).__name__)

    monkeypatch.setattr("pm.client.sync_venv", publish_without_environment)
    _git(repo, "init", "-q")
    (repo / "plugin.yaml").write_text("name: pinned-plugin\nversion: 1.0.0\ndescription: fixture\n")
    (repo / "__init__.py").write_text("def register(ctx):\n    pass\n")
    sha1 = _commit(repo, "first")
    (repo / "__init__.py").write_text("def register(ctx):\n    pass  # second\n")
    sha2 = _commit(repo, "second")
    return {"repo": repo, "home": home, "sha1": sha1, "sha2": sha2, "publications": publications}


@pytest.fixture
def pinned(world):
    from hermes_cli import plugins_cmd as pc

    source = world['repo'].as_uri()
    target, _, name = pc._install_plugin_core(source, force=False, ref=world['sha1'])
    assert pc._read_install_metadata()[name]['pinned'] is True
    return {**world, 'source': source, 'target': target, 'name': name}


def _intent(pinned):
    return {'source': pinned['source'], 'expected_revision': pinned['sha1'], 'ref': pinned['sha2']}


@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_pin_update_preserves_state_refs_enablement_and_converges_on_retry(pinned, enabled):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin, plugin_repin_status
    from hermes_cli.plugins_state import PluginState

    target, name = pinned['target'], pinned['name']
    pc._set_plugin_enabled(name, enable=enabled)
    (target / 'user-state.json').write_text('{"saved":"keep"}')
    state = PluginState(name)
    state.set('saved', {'keep': True})
    before_data = state.path.read_bytes()
    subprocess.run(['git', 'branch', 'retained-user-history', pinned['sha1']], cwd=target,
                   check=True, capture_output=True)
    before_selection = (pc._get_enabled_set(), pc._get_disabled_set())
    before_config = (pinned['home'] / 'config.yaml').read_bytes()
    first = plugin_repin_status(name)
    assert first['can_repin'] and first['revision'] == pinned['sha1']
    saved = dashboard_repin_user_plugin(name, _intent(pinned))
    assert saved['ok'] and saved['sha'] == pinned['sha2'] and not saved['unchanged'], saved
    assert _head(target) == pinned['sha2']
    assert (target / 'user-state.json').read_text() == '{"saved":"keep"}'
    assert subprocess.check_output(['git', 'rev-parse', 'retained-user-history'],
                                   cwd=target, text=True).strip() == pinned['sha1']
    assert (pc._get_enabled_set(), pc._get_disabled_set()) == before_selection
    assert pc._read_install_metadata()[name]['pinned'] is True
    assert (pinned['home'] / 'config.yaml').read_bytes() == before_config
    assert state.path.read_bytes() == before_data
    assert pinned['publications'][-1] == 'StagedPlugin'
    assert {'activation', 'gateway_reloaded', 'restart_required'} <= saved.keys()
    retry = dashboard_repin_user_plugin(name, _intent(pinned))
    assert retry == {'ok': True, 'name': name, 'sha': pinned['sha2'], 'unchanged': True}


@pytest.mark.parametrize('change,code', [
    ({'expected_revision': '0' * 40}, 'plugin_revision_conflict'),
    ({'source': 'https://github.com/another-owner/another-plugin.git'}, 'plugin_source_mismatch'),
])
def test_source_or_revision_conflict_cannot_fetch_or_change_installed_state(pinned, monkeypatch, change, code):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    before = pc._read_install_metadata()
    monkeypatch.setattr('hermes_cli.plugin_catalog.fetch_live_catalog',
                        lambda **kwargs: pytest.fail('conflicting update reached catalog fetch'))
    monkeypatch.setattr(pc, '_checkout_exact_revision',
                        lambda *args, **kwargs: pytest.fail('conflicting update reached Git fetch'))
    result = dashboard_repin_user_plugin(pinned['name'], {**_intent(pinned), **change})
    assert not result['ok'] and result['code'] == code
    assert _head(pinned['target']) == pinned['sha1']
    assert pc._read_install_metadata() == before


@pytest.mark.parametrize('local_change', ['tracked_edit', 'unique_commit'])
def test_local_work_is_not_reset_by_an_explicit_pin_update(pinned, local_change, monkeypatch):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    target = pinned['target']
    (target / '__init__.py').write_text('def register(ctx):\n    pass  # preserved local work\n')
    if local_change == 'unique_commit':
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-am', 'local work'], cwd=target, check=True, capture_output=True)
    before = (_head(target), (target / '__init__.py').read_bytes(), pc._read_install_metadata())
    monkeypatch.setattr(pc, '_checkout_exact_revision',
                        lambda *args, **kwargs: pytest.fail('local work reached Git fetch'))
    result = dashboard_repin_user_plugin(pinned['name'], _intent(pinned))
    assert not result['ok'] and result['code'] == 'plugin_repin_unavailable'
    assert (_head(target), (target / '__init__.py').read_bytes(), pc._read_install_metadata()) == before


def test_scan_refusal_leaves_pin_user_data_and_metadata_unchanged(pinned, monkeypatch):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    target = pinned['target']
    (target / 'user-state.json').write_text('{"saved":"keep"}')
    before = pc._read_install_metadata()

    def refuse(*args, **kwargs):
        raise pc.PluginScanBlocked('fixture scan refusal')

    monkeypatch.setattr(pc, '_scan_merged_tree', refuse)
    result = dashboard_repin_user_plugin(pinned['name'], _intent(pinned))
    assert not result['ok'] and result['scan_blocked']
    assert _head(target) == pinned['sha1']
    assert (target / 'user-state.json').read_text() == '{"saved":"keep"}'
    assert pc._read_install_metadata() == before


def test_status_and_update_refuse_changed_origin_before_fetch(pinned, monkeypatch):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin, plugin_repin_status

    _git(pinned['target'], 'remote', 'set-url', 'origin', 'https://example.invalid/another.git')
    monkeypatch.setattr(pc, '_checkout_exact_revision',
                        lambda *args, **kwargs: pytest.fail('changed source reached Git fetch'))
    assert plugin_repin_status(pinned['name'])['reason'] == 'installed_source_changed'
    assert dashboard_repin_user_plugin(pinned['name'], _intent(pinned))['code'] == 'plugin_repin_unavailable'
    assert _head(pinned['target']) == pinned['sha1']


def test_status_is_read_only_and_catalog_installs_cannot_bypass_review(pinned, monkeypatch):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_git import _install_metadata_path
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin, plugin_repin_status

    monkeypatch.setattr('hermes_cli.plugin_catalog.fetch_live_catalog',
                        lambda **kwargs: pytest.fail('local preflight fetched catalog'))
    before = _install_metadata_path().read_bytes()
    assert plugin_repin_status(pinned['name'])['can_repin']
    assert _install_metadata_path().read_bytes() == before
    metadata = pc._read_install_metadata()
    metadata[pinned['name']]['catalog'] = {'name': pinned['name'], 'pin': pinned['sha1']}
    pc._write_install_metadata(metadata)
    before = _install_metadata_path().read_bytes()
    status = plugin_repin_status(pinned['name'])
    assert not status['can_repin'] and status['reason'] == 'catalog_update_required'
    assert dashboard_repin_user_plugin(pinned['name'], _intent(pinned))['code'] == 'plugin_repin_unavailable'
    assert _install_metadata_path().read_bytes() == before


def test_mutable_update_cannot_move_an_explicit_pin(pinned, monkeypatch):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_transaction import update_plugin

    monkeypatch.setattr(pc, '_git_pull_plugin_dir',
                        lambda *args, **kwargs: pytest.fail('pinned install reached mutable update'))
    with pytest.raises(pc.PluginOperationError, match='pinned'):
        update_plugin(pinned['target'])
    assert _head(pinned['target']) == pinned['sha1']


def test_existing_custom_git_update_still_publishes_through_pm(world):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_transaction import update_plugin

    target, _, name = pc._install_plugin_core(world['repo'].as_uri(), force=False)
    (world['repo'] / '__init__.py').write_text('def register(ctx):\n    pass  # third\n')
    revision = _commit(world['repo'], 'third')
    update_plugin(target)
    assert _head(target) == revision
    assert pc._read_install_metadata()[name]['revision'] == revision
    assert pc._read_install_metadata()[name]['pinned'] is False
    assert world['publications'][-1] == 'StagedPlugin'


def test_linked_checkout_is_unavailable_without_modifying_its_owner(pinned):
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin, plugin_repin_status

    alias = pinned['target'].parent / 'linked-plugin'
    alias.symlink_to(pinned['target'], target_is_directory=True)
    assert plugin_repin_status(alias.name)['reason'] == 'owned_git_checkout_required'
    result = dashboard_repin_user_plugin(alias.name, _intent(pinned))
    assert result['code'] == 'plugin_repin_unavailable'
    assert _head(pinned['target']) == pinned['sha1']


def test_install_removed_before_lock_acquisition_returns_not_found(pinned, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import plugins_auto_update
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    lock = plugins_auto_update.plugin_update_lock

    @contextmanager
    def remove_before_acquiring_lock(target):
        with lock(target):
            target.rename(pinned['home'] / 'removed-plugin')
            yield

    monkeypatch.setattr(plugins_auto_update, 'plugin_update_lock', remove_before_acquiring_lock)
    assert dashboard_repin_user_plugin(pinned['name'], _intent(pinned))['code'] == 'plugin_not_found'
    assert _head(pinned['home'] / 'removed-plugin') == pinned['sha1']


def test_tag_object_is_refused_before_publication(pinned):
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    _git(pinned['repo'], 'tag', '-a', 'release', '-m', 'annotated release', pinned['sha2'])
    tag = _git(pinned['repo'], 'rev-parse', 'release')
    result = dashboard_repin_user_plugin(pinned['name'], {**_intent(pinned), 'ref': tag})
    assert result['ok'] is False and result['code'] == 'plugin_repin_failed'
    assert _head(pinned['target']) == pinned['sha1']


def test_new_dependencies_are_refused_before_publication(pinned):
    from hermes_cli import plugins_cmd as pc
    from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin

    pc._set_plugin_enabled(pinned['name'], enable=True)
    (pinned['repo'] / 'pyproject.toml').write_text(
        '[project]\nname="pinned-plugin"\nversion="2.0.0"\ndependencies=["new-package>=1,<2"]\n')
    revision = _commit(pinned['repo'], 'new dependency')
    before = pc._read_install_metadata()
    result = dashboard_repin_user_plugin(pinned['name'], {**_intent(pinned), 'ref': revision})
    assert result['ok'] is False and 'new Python dependencies' in result['error']
    assert pc._read_install_metadata() == before
    assert _head(pinned['target']) == pinned['sha1']


@pytest.fixture
def browser(monkeypatch):
    from starlette.testclient import TestClient
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, '_get_dashboard_plugins', lambda **kwargs: [])
    with TestClient(web_server.app, raise_server_exceptions=False,
                    headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}) as client:
        yield client


def test_http_status_success_conflicts_and_source_mismatch(pinned, browser):
    from hermes_cli import plugins_cmd as pc

    route = f"/api/dashboard/agent-plugins/{pinned['name']}/repin"
    status = browser.get(route)
    assert status.status_code == 200 and status.json()['can_repin'], status.text
    assert {'schema_version', 'ok', 'name', 'source', 'revision', 'head_revision',
            'pinned', 'can_repin', 'reason'} <= status.json().keys()
    stale = browser.post(route, json={**_intent(pinned), 'expected_revision': '0' * 40})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'plugin_revision_conflict'
    assert stale.json()['detail']['current']['revision'] == pinned['sha1']
    mismatch = browser.post(route, json={**_intent(pinned), 'source': 'Other/Source'})
    assert mismatch.status_code == 400 and mismatch.json()['detail']['code'] == 'plugin_source_mismatch'
    saved = browser.post(route, json=_intent(pinned))
    assert saved.status_code == 200 and saved.json()['sha'] == pinned['sha2'], saved.text
    assert pc._read_install_metadata()[pinned['name']]['revision'] == pinned['sha2']
    (pinned['target'] / '__init__.py').write_text('preserved local work\n')
    unsafe = browser.post(route, json={**_intent(pinned), 'expected_revision': pinned['sha2']})
    assert unsafe.status_code == 409 and unsafe.json()['detail']['code'] == 'plugin_repin_unavailable'


def test_http_repin_authentication_precedes_body_and_immutable_validation(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from hermes_cli import plugins_cmd_repin

    calls = []
    monkeypatch.setattr(plugins_cmd_repin, 'dashboard_repin_user_plugin',
                        lambda *args, **kwargs: calls.append(args) or {'ok': True})
    with TestClient(app, raise_server_exceptions=False) as browser:
        unauthorized = browser.post('/api/dashboard/agent-plugins/example/repin', content='invalid')
        assert unauthorized.status_code in (401, 403)
        assert browser.get('/api/dashboard/agent-plugins/example/repin').status_code in (401, 403)
        for value in ({'source': 'Owner/Repo', 'expected_revision': 'a' * 40, 'ref': 'main'},
                      {'source': 'Owner/Repo', 'expected_revision': 'a' * 40, 'ref': 'b' * 40, 'force': True}):
            response = browser.post('/api/dashboard/agent-plugins/example/repin',
                headers={_SESSION_HEADER_NAME: _SESSION_TOKEN}, content=json.dumps(value))
            assert response.status_code == 400
            assert response.json()['detail']['code'] == 'invalid_plugin_repin_request'
        duplicate = browser.post('/api/dashboard/agent-plugins/example/repin',
            headers={_SESSION_HEADER_NAME: _SESSION_TOKEN}, content='{"source":"Owner/Repo","source":"Other/Repo"}')
        assert duplicate.status_code == 400
        oversized = browser.post('/api/dashboard/agent-plugins/example/repin',
            headers={_SESSION_HEADER_NAME: _SESSION_TOKEN}, content=' ' * 4097)
        assert oversized.status_code == 413
    assert calls == []
