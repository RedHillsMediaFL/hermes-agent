"""Explicit immutable updates for owned, pinned Git plugins through the normal manager."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import subprocess
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RepinIntent:
    source: str
    expected_revision: str
    ref: str


def _pc():
    from hermes_cli import plugins_cmd

    return plugins_cmd


def source_identity(source: str) -> tuple[str, str]:
    from hermes_cli.plugin_catalog import _normalize_repo

    git_url, subdir = _pc()._resolve_git_url(source)
    parsed = urlsplit(git_url) if "://" in git_url else None
    # Catalog matching folds paths and ports for a kill-list match. A repin must
    # retain filesystem path case and distinguish different service endpoints.
    identity = git_url if parsed and parsed.scheme == "file" else _normalize_repo(git_url)
    if parsed and parsed.port is not None:
        identity += f":{parsed.port}"
    return identity, subdir or ""


def _same_source(installed: str, requested: str) -> bool:
    try:
        return source_identity(installed) == source_identity(requested)
    except (ValueError, TypeError):
        return False


def _public_source(source: Any) -> str | None:
    from hermes_cli.plugins_cmd_git import _scrub_git_url

    if not isinstance(source, str):
        return None
    try:
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} and (parsed.password or parsed.query):
            return None
        return _scrub_git_url(source)
    except ValueError:
        return None


def validate_intent(value: Any) -> RepinIntent:
    pc = _pc()
    if isinstance(value, RepinIntent):
        value = asdict(value)
    if (type(value) is not dict or set(value) != {'source', 'expected_revision', 'ref'}
            or not all(type(item) is str for item in value.values())):
        raise pc.PluginOperationError('Invalid immutable plugin update fields.')
    if not all(pc._EXACT_COMMIT_RE.fullmatch(value[key]) for key in ('expected_revision', 'ref')):
        raise pc.PluginOperationError('Plugin updates require exact 40-character commit revisions.')
    source = value['source']
    if not source or len(source) > 1024 or any(ord(char) < 32 for char in source) or source != source.strip():
        raise pc.PluginOperationError('Invalid installed plugin source.')
    try:
        parsed = urlsplit(source) if '://' in source else None
        if parsed is not None and (parsed.password or parsed.query or parsed.fragment):
            raise ValueError()
        source_identity(source)
    except (ValueError, TypeError):
        raise pc.PluginOperationError('Invalid installed plugin source.') from None
    return RepinIntent(source, value['expected_revision'].lower(), value['ref'].lower())


def _catalog_update_required(target: Path, record: dict) -> bool:
    """Do not fetch or migrate catalog provenance during the local preflight."""
    from hermes_cli.plugin_catalog import _normalize_repo
    from hermes_cli.plugins_cmd_catalog import CATALOG_SIDECAR, _HANDOFF_REPOS

    if record.get("catalog") or record.get("catalog_name"):
        return True
    if _normalize_repo(str(record.get("source") or "").split("#", 1)[0]) in _HANDOFF_REPOS:
        return True
    # A legacy marker needs the normal catalog path to establish provenance;
    # immutable custom updates cannot use it to evade that review.
    marker = target / CATALOG_SIDECAR
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return True
        return isinstance(data, dict) and bool(data.get("catalog_name"))
    return False


def _checkout_status(target: Path, record: dict, result: dict) -> str | None:
    pc = _pc()
    from hermes_cli.plugins_cmd_catalog import _local_changes
    from hermes_cli.plugins_cmd_git import _refuse_foreign_checkout

    git = pc._resolve_git_executable()
    if not git:
        return "git_unavailable"
    _refuse_foreign_checkout(git, target)
    result["head_revision"] = pc._git_head_revision(target, git)
    if result["head_revision"] != result["revision"]:
        return "installed_revision_changed"
    # Git's remote get-url expands user-owned insteadOf transport mappings;
    # provenance is the configured repository, even when fetched via a mirror.
    origin = pc._git_or_raise(git, target, "config", "--get", "remote.origin.url",
                               failure_prefix="Could not inspect installed Git source: ").stdout.strip()
    if not _same_source(origin, record["source"]):
        return "installed_source_changed"
    _local, modified = _local_changes(target)
    return "local_changes_present" if modified else None


def _installed_status(name: str, target: Path, record: dict) -> dict:
    pc = _pc()
    source = record.get('source')
    result = {'ok': True, 'schema_version': 1, 'name': name,
              'source': _public_source(source),
              'revision': record.get('revision') if isinstance(record.get('revision'), str) else None,
              'head_revision': None,
              'pinned': record.get('pinned') is True, 'can_repin': False}
    if target.is_symlink() or not (target / '.git').is_dir() or (target / '.git').is_symlink():
        result['reason'] = 'owned_git_checkout_required'
        return result
    if _catalog_update_required(target, record):
        result['reason'] = 'catalog_update_required'
        return result
    if not result['pinned'] or not isinstance(source, str) or not source:
        result['reason'] = 'pinned_git_install_required'
        return result
    if not isinstance(result['revision'], str) or not pc._EXACT_COMMIT_RE.fullmatch(result['revision']):
        result['reason'] = 'installed_revision_changed'
        return result
    try:
        result['reason'] = _checkout_status(target, record, result)
    except (pc.PluginOperationError, OSError, ValueError, subprocess.TimeoutExpired):
        result['reason'] = 'checkout_inspection_failed'
    result['can_repin'] = result['reason'] is None
    return result


def validate_installed_intent(target: Path, record: dict, intent: RepinIntent, *, catalog_entry=None) -> None:
    pc = _pc()
    if catalog_entry is not None:
        raise pc.PluginOperationError('Catalog plugins use the reviewed catalog update operation.')
    status = _installed_status(target.name, target, record)
    if not status['can_repin']:
        raise pc.PluginOperationError('The installed plugin requires review before an immutable update.')
    if not _same_source(record['source'], intent.source):
        raise pc.PluginOperationError('The requested plugin source does not match its installed source.')
    if record['revision'] != intent.expected_revision:
        raise pc.PluginOperationError('The installed plugin revision changed; reload before updating.')


def plugin_repin_status(name: str) -> dict:
    pc = _pc()
    target = pc._user_installed_plugin_dir(name)
    if target is None:
        return {'ok': False, 'code': 'plugin_not_found', 'error': 'The installed plugin was not found.'}
    record = pc._read_install_metadata().get(target.name, {})
    status = _installed_status(name, target, record if isinstance(record, dict) else {})
    if (pc._plugins_dir() / name).is_symlink():
        status.update(can_repin=False, reason='owned_git_checkout_required')
    return status


def dashboard_repin_user_plugin(name: str, intent: RepinIntent | dict[str, str]) -> dict:
    pc = _pc()
    from hermes_cli.plugins_activation import activate_plugin_now
    from hermes_cli.plugins_auto_update import plugin_update_lock
    from hermes_cli.plugins_cmd_install import scan_blocked_result
    from hermes_cli.plugins_cmd_update import _post_pull_housekeeping
    from hermes_cli.plugins_transaction import update_plugin

    intent = validate_intent(intent)
    target = pc._user_installed_plugin_dir(name)
    if target is None:
        return {'ok': False, 'code': 'plugin_not_found', 'error': 'The installed plugin was not found.'}
    try:
        with plugin_update_lock(target):
            current = plugin_repin_status(name)
            if not current.get('ok'):
                return current
            record = pc._read_install_metadata().get(target.name, {})
            if not isinstance(record, dict) or not record.get('source'):
                return {'ok': False, 'code': 'plugin_repin_unavailable', 'current': current,
                        'error': 'The installed plugin has no recorded Git source.'}
            if not _same_source(str(record['source']), intent.source):
                return {'ok': False, 'code': 'plugin_source_mismatch',
                        'error': 'The requested plugin source does not match its installed source.'}
            if not current['can_repin']:
                return {'ok': False, 'code': 'plugin_repin_unavailable', 'current': current,
                        'error': 'The installed plugin requires review before an immutable update.'}
            if current['revision'] == intent.ref:
                return {'ok': True, 'name': name, 'sha': intent.ref, 'unchanged': True}
            if current['revision'] != intent.expected_revision:
                return {'ok': False, 'code': 'plugin_revision_conflict', 'current': current,
                        'error': 'The installed plugin revision changed; reload before updating.'}
            update_plugin(target, repin=intent)
            after = plugin_repin_status(name)
            if after['revision'] != intent.ref or after['head_revision'] != intent.ref:
                raise pc.PluginOperationError('Plugin update readback did not match the requested revision.')
            _post_pull_housekeeping(target, pc._console())
            activated = activate_plugin_now(name)
            return {'ok': True, 'name': name, 'sha': intent.ref, 'unchanged': False, **activated}
    except pc.PluginScanBlocked as exc:
        return {**scan_blocked_result(exc), 'name': name}
    except pc.PluginOperationError as exc:
        return {'ok': False, 'code': 'plugin_repin_failed', 'error': str(exc)}
