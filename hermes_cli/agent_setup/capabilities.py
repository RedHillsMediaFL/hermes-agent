"""Profile-scoped optional-client contracts owned and proved by their extensions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
import re
from typing import Any, Callable

from agent.provider_registry import ProviderRegistry, exact_key

FEATURES = frozenset({'groups', 'usage_upload', 'life_context', 'remote_push', 'live_activities'})
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientCapabilityProbe:
    name: str
    callback: Callable
    paths: tuple[str, ...] = ()
    plugin_name: str = ''

    def __post_init__(self):
        if self.name not in FEATURES or not inspect.iscoroutinefunction(self.callback):
            raise ValueError('Unsupported client capability probe')
        if (type(self.paths) is not tuple or len(self.paths) > 4 or len(set(self.paths)) != len(self.paths)
                or any(not valid_read_path(path) or not path.startswith('/api/plugins/' + self.plugin_name + '/')
                       for path in self.paths)):
            raise ValueError('Invalid client capability contract paths')


_registry = ProviderRegistry(label='Client capability', provider_cls=ClientCapabilityProbe,
                             logger=_log, normalize=exact_key)
_registry.export(globals())


@dataclass(frozen=True)
class ClientCapabilityContext:
    installation_id: str
    account_id: str
    profile: str
    request: Any
    feature: str = ''
    paths: tuple[str, ...] = ()
    plugin_name: str = ''

    def is_current(self):
        from hermes_cli.agent_setup.authority import account_id
        from hermes_cli.install_identity import get_install_id
        from hermes_cli.web_server_cron import _cron_profile_home
        from hermes_constants import get_hermes_home, hermes_home_key
        return (account_id(self.request) == self.account_id and get_install_id() == self.installation_id
                and self.request.query_params.getlist('profile') in ([], [self.profile])
                and hermes_home_key(_cron_profile_home(self.profile)[1]) == hermes_home_key(get_hermes_home()))

    def receipt(self, feature, supported):
        return {'schema_version': 1, 'contract_version': 1, 'feature': feature,
                'installation_id': self.installation_id, 'account_id': self.account_id,
                'profile': self.profile, 'supported': supported}

    async def read_json(self, path):
        """Read only a same-server plugin endpoint as this already authenticated owner.

        No socket, host discovery, arbitrary method or caller-selected profile is allowed.
        The actual ASGI route and its authorization still execute.
        """
        if not valid_read_path(path) or (self.paths and path not in self.paths):
            raise ValueError('Invalid client capability read path')
        if not self.is_current():
            return None
        import httpx
        from hermes_cli.agent_setup.models import HEADER
        from hermes_cli.dashboard_auth.native_http import HEADER as NATIVE_HEADER
        headers = {k: v for k, v in self.request.headers.items()
                   if k.lower() not in {HEADER.lower(), NATIVE_HEADER.lower(), 'content-length', 'content-type'}}
        original_state = dict(self.request.scope.get('state') or {})

        async def scoped(scope, receive, send):
            size = 0

            async def bounded_send(message):
                nonlocal size
                if message['type'] == 'http.response.body':
                    size += len(message.get('body', b''))
                    if size > 65536:
                        raise ValueError('Client capability response too large')
                await send(message)

            await self.request.app({**scope, 'state': dict(original_state)}, receive, bounded_send)

        origin = f'{self.request.url.scheme}://{self.request.url.netloc}'
        transport = httpx.ASGITransport(app=scoped, root_path=self.request.scope.get('root_path', ''))
        async with httpx.AsyncClient(transport=transport, base_url=origin, timeout=2) as client:
            result = await client.get(path, params={'profile': self.profile}, headers=headers)
        if not self.is_current() or result.status_code != 200 or len(result.content) > 65536:
            return None
        try:
            value = result.json()
        except ValueError:
            return None
        return value if type(value) is dict else None


def valid_read_path(path):
    return (type(path) is str and len(path) <= 512
            and re.fullmatch(r'/api/plugins/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', path) is not None)


def _valid(receipt, context, feature):
    expected = context.receipt(feature, True)
    return type(receipt) is dict and set(receipt) == set(expected) and all(
        type(receipt[key]) is type(value) and receipt[key] == value for key, value in expected.items())


async def extension_features(request, profile, actor):
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope(profile):
        return await _extension_features(request, profile)


async def _extension_features(request, profile):
    from hermes_cli.web_server_cron import _cron_profile_home
    from hermes_cli.install_identity import get_install_id
    from hermes_cli.agent_setup.authority import account_id, local_owner
    _, home = _cron_profile_home(profile)
    probes = _registry.list_providers(scope=str(home))
    values = dict.fromkeys(FEATURES, False)
    if not probes:
        return values
    account = request.state.native_http_principal['subject'] if local_owner(request) else account_id(request)
    for probe in probes:
        context = ClientCapabilityContext(get_install_id(), account, profile, request,
                                          probe.name, probe.paths, probe.plugin_name)
        try:
            async with asyncio.timeout(2):
                receipt = probe.callback(context)
                if inspect.isawaitable(receipt):
                    receipt = await receipt
                values[probe.name] = context.is_current() and _valid(receipt, context, probe.name)
        except Exception as exc:  # health: allow BLE001 -- optional extension failures cannot grant a feature
            _log.debug('Client capability %s unavailable (%s)', probe.name, type(exc).__name__)
    return values
