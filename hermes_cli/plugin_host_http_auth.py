"""Carry route-local admission over the private plugin-host channel, without credentials."""
from dataclasses import dataclass
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs

from fastapi import HTTPException


_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_SESSION_FIELDS = frozenset({'provider', 'issuer', 'user_id', 'org_id', 'expires_at'})
_ENVELOPE_FIELDS = frozenset({'schema_version', 'installation_id', 'account_id', 'profile', 'profile_id', 'session'})


@dataclass(frozen=True)
class HostedVerifiedSession:
    provider: str
    issuer: str
    user_id: str
    org_id: str
    expires_at: float


def _text(value, *, empty=False, maximum=2048):
    return (type(value) is str and (empty or bool(value)) and len(value) <= maximum
            and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _session(value):
    if type(value) is not dict or set(value) != _SESSION_FIELDS:
        return None
    if (not _text(value['provider'], maximum=128) or not _text(value['user_id'])
            or not _text(value['issuer'], empty=True) or not _text(value['org_id'], empty=True)
            or type(value['expires_at']) not in (int, float) or not math.isfinite(value['expires_at'])
            or value['expires_at'] <= time.time()):
        return None
    return HostedVerifiedSession(**{**value, 'expires_at': float(value['expires_at'])})


def _target(profile, home):
    from hermes_constants import hermes_home_key, profile_name_for_home
    if type(profile) is not str or not _PROFILE.fullmatch(profile):
        return None
    if not isinstance(home, (str, Path)) or not str(home).strip():
        return None
    key = hermes_home_key(home)
    return key if (profile_name_for_home(key) or 'default') == profile else None


def verified_session_envelope(request, *, profile, home):
    """Parent-only projection from verified request state, never wire claims or a bearer.

    ``profile`` and ``home`` are the server-resolved destination of this request.
    Only the fixed nonsecret fields needed by an owning plugin cross private IPC.
    """
    from gateway.session_identity import authenticated_subject
    from hermes_cli.install_identity import get_install_id
    key = _target(profile, home)
    source = getattr(request.state, 'session', None)
    selectors = request.query_params.getlist('profile')
    if key is None or source is None or (selectors and selectors != [profile]):
        return None
    values = {name: getattr(source, name, '') for name in _SESSION_FIELDS}
    selected = _session(values)
    installation = get_install_id()
    if selected is None or not _text(installation, maximum=128):
        return None
    return {'schema_version': 1, 'installation_id': installation,
            'account_id': authenticated_subject({'provider': selected.provider, 'issuer': selected.issuer,
                                                 'user_id': selected.user_id}),
            'profile': profile, 'profile_id': key, 'session': values}


def restore_verified_session(envelope, *, profile, home):
    """Child-only restoration against its trusted destination, with expiry rechecked.

    The object contains no access token, refresh token, cookie or native ticket.
    Return a fresh frozen value for each ASGI/RPC request; never cache it on an app.
    """
    from gateway.session_identity import authenticated_subject
    from hermes_cli.install_identity import get_install_id
    key = _target(profile, home)
    if (type(envelope) is not dict or set(envelope) != _ENVELOPE_FIELDS or key is None
            or type(envelope['schema_version']) is not int or envelope['schema_version'] != 1
            or envelope['profile'] != profile or envelope['profile_id'] != key
            or not _text(envelope['installation_id'], maximum=128)
            or envelope['installation_id'] != get_install_id()):
        return None
    selected = _session(envelope['session'])
    if selected is None:
        return None
    account = authenticated_subject({'provider': selected.provider, 'issuer': selected.issuer, 'user_id': selected.user_id})
    return selected if envelope['account_id'] == account else None


def admitted_for_sensitive_route(request) -> bool:
    """Preserve the normal guard's decision without changing unguarded plugin routes."""
    from hermes_cli.web_server import _require_token
    try:
        _require_token(request)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return False
    return True


def with_hosted_admission(app, authenticated, operator_principal=None, verified_session=None):
    """Admission belongs to this IPC request, never to the cached plugin app."""
    async def scoped(scope, receive, send):
        state = dict(scope.get("state") or {})
        state.pop('session', None)
        state.pop('native_http_principal', None)
        state["hosted_http_authenticated"] = authenticated is True
        state['hosted_operator_principal'] = operator_principal if authenticated is True else None
        if authenticated is True and type(verified_session) is dict and scope['type'] == 'http':
            from hermes_constants import get_hermes_home, profile_name_for_home
            home = get_hermes_home()
            profile = profile_name_for_home(home) or 'default'
            query = parse_qs(scope.get('query_string', b'').decode('utf-8', errors='replace'), keep_blank_values=True)
            if 'profile' not in query or query['profile'] == [profile]:
                session = restore_verified_session(verified_session, profile=profile, home=home)
                if session is not None:
                    state['session'] = session
        await app({**scope, "state": state}, receive, send)
    return scoped
