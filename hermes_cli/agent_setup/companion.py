"""Fixed reviewed companion operations through the existing profile-bound plugin manager."""
from __future__ import annotations

from pathlib import Path
import re

from fastapi import HTTPException

from hermes_cli import plugins_cmd
from hermes_cli.plugins_auto_update import plugin_update_lock
from hermes_cli.plugins_cmd_repin import dashboard_repin_user_plugin, plugin_repin_status, source_identity
from hermes_cli.web_routers.dashboard_ui import _get_dashboard_plugins, _invalidate_plugins_hub_cache
from hermes_constants import get_hermes_home

NAME = 'agent-companion-runtime'
REPOSITORY = 'https://github.com/RedHillsMediaFL/agent-hermes-plugin'
_SHA = re.compile(r'[0-9a-f]{40}\Z')


def _refused(code, status=409):
    raise HTTPException(status, {'code': code}, headers={'Cache-Control': 'no-store'})


def target(ref):
    return {'repository': REPOSITORY, 'revision': ref}


def status():
    own = Path(get_hermes_home()) / 'plugins' / NAME
    installed = own.is_dir() or own.is_symlink()
    result = {'name': NAME, 'installed': installed, 'enabled': False, 'version': None,
              'source': None, 'revision': None, 'head_revision': None, 'pinned': False,
              'can_repin': False, 'reason': 'plugin_not_found'}
    if not installed:
        return result
    if own.is_symlink():
        result['reason'] = 'owned_git_checkout_required'
        return result
    manifest = plugins_cmd._read_manifest(own)
    if manifest.get('name') != NAME:
        result['reason'] = 'companion_name_conflict'
        return result
    version = manifest.get('version')
    result['version'] = version if isinstance(version, str) else None
    result['enabled'] = (NAME in plugins_cmd._get_enabled_set()
                         and NAME not in plugins_cmd._get_disabled_set())
    current = plugin_repin_status(NAME)
    if current.get('ok') is not True:
        return result
    result.update({key: current.get(key) for key in ('pinned', 'can_repin', 'reason')})
    for key in ('revision', 'head_revision'):
        value = current.get(key)
        result[key] = value if isinstance(value, str) and _SHA.fullmatch(value) else None
    try:
        source = current.get('source')
        ours = isinstance(source, str) and source_identity(source) == source_identity(REPOSITORY)
    except (ValueError, TypeError, plugins_cmd.PluginOperationError):
        ours = False
    if ours:
        result['source'] = REPOSITORY
    else:
        result.update(can_repin=False, reason='companion_source_mismatch')
    return result


def _owned(current):
    if not current['installed']:
        _refused('plugin_not_found', 404)
    if current['source'] != REPOSITORY:
        _refused('companion_source_mismatch')
    if not current['pinned'] or not current['can_repin'] or current['revision'] is None:
        _refused('companion_review_required')


def _install(current, body):
    if current['installed']:
        _refused('companion_already_installed')
    return plugins_cmd.dashboard_install_plugin(REPOSITORY, ref=body['ref'], force=False, enable=True)


def _enable(current, body):
    with plugin_update_lock(Path(get_hermes_home()) / 'plugins' / NAME):
        installed = status()
        _owned(installed)
        if installed['revision'] != body['ref']:
            _refused('companion_revision_mismatch')
        return plugins_cmd.dashboard_set_agent_plugin_enabled(NAME, enabled=True)


def _repin(current, body):
    _owned(current)
    return dashboard_repin_user_plugin(NAME, {'source': REPOSITORY,
        'expected_revision': body['expected_revision'], 'ref': body['ref']})


_OPERATIONS = {'install': _install, 'enable': _enable, 'repin': _repin}


def mutate(operation, body):
    before = status()
    result = _OPERATIONS[operation](before, body)
    if result.get('ok') is not True:
        code = result.get('code')
        if code not in {'plugin_revision_conflict', 'plugin_repin_unavailable', 'plugin_not_found'}:
            code = 'companion_review_required'
        _refused(code, 404 if code == 'plugin_not_found' else 409)
    if result.get('missing_env'):
        _refused('companion_environment_required')
    _get_dashboard_plugins(force_rescan=True)
    _invalidate_plugins_hub_cache()
    after = status()
    if (not after['installed'] or after['source'] != REPOSITORY or not after['pinned']
            or not after['can_repin'] or after['revision'] != body['ref']
            or after['head_revision'] != body['ref']):
        _refused('companion_readback_mismatch', 503)
    return {'ok': True, 'operation': operation, 'unchanged': result.get('unchanged') is True,
            'restart_required': result.get('restart_required') is True,
            'sha': after['revision'], 'plugin': after, 'target': target(body['ref'])}
