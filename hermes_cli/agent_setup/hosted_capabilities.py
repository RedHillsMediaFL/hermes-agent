"""Request-lifetime, read-only capability proof RPC over the private plugin channel."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import secrets
import threading
import time
from types import SimpleNamespace

from hermes_cli.agent_setup.capabilities import ClientCapabilityContext, FEATURES, valid_read_path

_active: ContextVar = ContextVar('hosted_client_capability', default=None)


@dataclass
class _Permit:
    context: ClientCapabilityContext
    loop: object
    invocation: str
    deadline: float
    active: bool = True
    seen: set = field(default_factory=set)
    lock: object = field(default_factory=threading.Lock)


async def invoke(host, ref, generation, context):
    """Only this registered callback can read its reviewed paths during this proof."""
    from hermes_cli.plugin_host_http_auth import verified_session_envelope
    if (not context.paths or not context.plugin_name or context.feature not in FEATURES
            or not context.is_current()):
        return None
    session = verified_session_envelope(context.request, profile=context.profile, home=host.home)
    if session is None:
        return None
    permit = _Permit(context, asyncio.get_running_loop(), secrets.token_urlsafe(32), time.monotonic() + 2)
    token = _active.set(permit)
    wire = {'schema_version': 1, 'invocation': permit.invocation, 'feature': context.feature,
            'installation_id': context.installation_id, 'account_id': context.account_id,
            'profile': context.profile, 'paths': list(context.paths), 'verified_session': session}
    try:
        return await asyncio.to_thread(host._call, 'capability_probe', {'ref': ref, 'context': wire},
                                       generation=generation, timeout=2)
    finally:
        with permit.lock:
            permit.active = False
        _active.reset(token)


def serve_read(params):
    """A reverse RPC cannot choose a session, profile, method, host or another proof."""
    permit = _active.get()
    fields = {'invocation', 'feature', 'installation_id', 'account_id', 'profile', 'path'}
    if permit is None or type(params) is not dict or set(params) != fields:
        return None
    context = permit.context
    path = params['path']
    with permit.lock:
        if (not permit.active or time.monotonic() >= permit.deadline
                or any(params[key] != getattr(context, key) for key in
                       ('feature', 'installation_id', 'account_id', 'profile'))
                or type(params['invocation']) is not str
                or not secrets.compare_digest(params['invocation'], permit.invocation)
                or not valid_read_path(path) or path not in context.paths or path in permit.seen):
            return None
        permit.seen.add(path)
    future = asyncio.run_coroutine_threadsafe(context.read_json(path), permit.loop)
    try:
        return future.result(timeout=max(0.001, permit.deadline - time.monotonic()))
    finally:
        if not future.done():
            future.cancel()


@dataclass(frozen=True)
class _RemoteContext:
    installation_id: str
    account_id: str
    profile: str
    request: object
    feature: str
    paths: tuple[str, ...]
    invocation: str
    runtime: object

    def receipt(self, feature, supported):
        return {'schema_version': 1, 'contract_version': 1, 'feature': feature,
                'installation_id': self.installation_id, 'account_id': self.account_id,
                'profile': self.profile, 'supported': supported}

    async def read_json(self, path):
        if not valid_read_path(path) or path not in self.paths:
            raise ValueError('Invalid client capability read path')
        params = {key: getattr(self, key) for key in
                  ('invocation', 'feature', 'installation_id', 'account_id', 'profile')}
        return await asyncio.to_thread(self.runtime.channel.call, 'capability_read', {**params, 'path': path}, timeout=2)


def child_probe(runtime, params):
    """Rebuild only the restricted proof facade, never the parent Request object."""
    from hermes_cli.plugin_host_http_auth import restore_verified_session
    from hermes_constants import get_hermes_home, profile_name_for_home
    wire = params.get('context')
    fields = {'schema_version', 'invocation', 'feature', 'installation_id', 'account_id',
              'profile', 'paths', 'verified_session'}
    if (type(wire) is not dict or set(wire) != fields or type(wire['schema_version']) is not int
            or wire['schema_version'] != 1 or wire['feature'] not in FEATURES
            or type(wire['paths']) is not list or not 1 <= len(wire['paths']) <= 4
            or len(set(wire['paths'])) != len(wire['paths'])
            or any(not valid_read_path(path) for path in wire['paths'])
            or type(wire['invocation']) is not str or len(wire['invocation']) != 43):
        return None
    home = get_hermes_home()
    if wire['profile'] != (profile_name_for_home(home) or 'default'):
        return None
    session = restore_verified_session(wire['verified_session'], profile=wire['profile'], home=home)
    if (session is None or wire['installation_id'] != wire['verified_session']['installation_id']
            or wire['account_id'] != wire['verified_session']['account_id']):
        return None
    ref = int(params['ref'])
    callback = runtime.refs.get(ref)
    if callback is None:
        return None
    context = _RemoteContext(wire['installation_id'], wire['account_id'], wire['profile'],
        SimpleNamespace(state=SimpleNamespace(session=session)), wire['feature'], tuple(wire['paths']),
        wire['invocation'], runtime)
    return runtime._encode_result(runtime.owners.get(ref, ''), runtime._run(callback(context)))
