"""The supported request-local operator boundary, including private hosted IPC."""
from __future__ import annotations

import math
import sqlite3
import time
from types import MappingProxyType

from fastapi import HTTPException

from hermes_cli.agent_setup.models import CAPABILITIES, HEADER, PROFILE
from hermes_cli.agent_setup.storage import OperatorStore


def account_id(request):
    session = getattr(request.state, 'session', None)
    expiry = getattr(session, 'expires_at', None)
    if (session is None or type(expiry) not in (int, float) or not math.isfinite(expiry)
            or expiry <= time.time() or not getattr(session, 'provider', None)
            or not getattr(session, 'user_id', None)):
        raise HTTPException(401, 'Authentication required.', headers={'Cache-Control': 'no-store'})
    from gateway.session_identity import authenticated_subject
    # Conversation ownership intentionally merges host logins; delegated admin
    # authority must instead remain bound to this exact verified login.
    return authenticated_subject({'user_id': session.user_id, 'provider': session.provider,
                                  'issuer': getattr(session, 'issuer', '')})


def local_owner(request):
    value = getattr(request.state, 'native_http_principal', None)
    return (type(value) is dict and value.get('purpose') == 'native-http'
            and type(value.get('capabilities')) is frozenset
            and value['capabilities'] == frozenset({'http:owner'}))


def require_local_owner(request):
    if not local_owner(request):
        raise HTTPException(403, 'Verified local operator authority required.',
                            headers={'Cache-Control': 'no-store'})


def operator_store():
    from hermes_constants import get_default_hermes_root
    from hermes_cli.install_identity import get_install_id
    installation = get_install_id()
    if not installation:
        raise HTTPException(503, 'operator_authority_unavailable')
    return OperatorStore(get_default_hermes_root(), installation)


def validate_profile(request, profile):
    if type(profile) is not str or not PROFILE.fullmatch(profile):
        raise HTTPException(400, 'invalid_operator_profiles')
    from hermes_cli.web_server_sessions import _session_mutation_context
    from hermes_cli.web_server_cron import _cron_profile_home
    canonical, _ = _cron_profile_home(profile)
    if canonical != profile:
        raise HTTPException(400, 'invalid_operator_profiles')
    _session_mutation_context(request, profile)


def _bound_profile(request, profile):
    """A grant never selects a different plugin store from the request's bound home."""
    from hermes_constants import get_hermes_home, hermes_home_key
    from hermes_cli.web_server_cron import _cron_profile_home
    if request.query_params.getlist('profile') != [profile]:
        return False
    canonical, home = _cron_profile_home(profile)
    return canonical == profile and hermes_home_key(home) == hermes_home_key(get_hermes_home())


def _local_principal(request, profile):
    from hermes_constants import hermes_home_key
    from hermes_cli.web_server_cron import _cron_profile_home
    grant = request.state.native_http_principal
    if (type(grant.get('profile_id')) is not str or not grant['profile_id']
            or type(grant.get('subject')) is not str or not grant['subject']):
        return None
    if hermes_home_key(grant.get('profile_id', '')) != hermes_home_key(_cron_profile_home(profile)[1]):
        return None
    return {'schema_version': 1, 'installation_id': operator_store().installation_id,
            'grant_id': None, 'account_id': grant['subject'], 'device_id': None,
            'profiles': [profile], 'capabilities': sorted(CAPABILITIES), 'expires_at': None,
            'source': 'local-native-owner'}


def _matches(principal, claimed):
    return set(principal) == set(claimed) and all(principal.get(k) == claimed.get(k) for k in principal)


def _verified(request, profile):
    forwarded = getattr(request.state, 'hosted_operator_principal', None)
    store = operator_store()
    if forwarded is not None:
        # This field is carried only by the existing private parent/child IPC,
        # never from HTTP headers or JSON. Recheck persisted revocation here too.
        if (getattr(request.state, 'hosted_http_authenticated', False) is not True
                or type(forwarded) is not dict):
            return None
        if forwarded.get('source') == 'local-native-owner':
            fields = {'schema_version', 'installation_id', 'grant_id', 'account_id', 'device_id',
                      'profiles', 'capabilities', 'expires_at', 'source'}
            if (set(forwarded) == fields and forwarded['schema_version'] == 1
                    and forwarded['installation_id'] == store.installation_id
                    and forwarded['grant_id'] is None and forwarded['device_id'] is None
                    and forwarded['expires_at'] is None and forwarded['profiles'] == [profile]
                    and forwarded['capabilities'] == sorted(CAPABILITIES)
                    and type(forwarded['account_id']) is str and forwarded['account_id']):
                return forwarded
            return None
        if type(forwarded.get('grant_id')) is not str:
            return None
        principal = store.principal(grant_id=forwarded.get('grant_id'))
        return principal if principal is not None and _matches(principal, forwarded) else None
    account = account_id(request)
    validate_profile(request, profile)
    values = request.headers.getlist(HEADER)
    if len(values) != 1 or not values[0].startswith('hop1_') or len(values[0]) > 128:
        return None
    principal = store.principal(token=values[0])
    return principal if principal is not None and principal['account_id'] == account else None


def operator_authorization(request, *, profile, capability):
    """Immutable scoped authority, None on refusal, HTTP503 on unavailable storage."""
    if capability not in CAPABILITIES or type(profile) is not str or not PROFILE.fullmatch(profile):
        return None
    if (not local_owner(request) and not request.headers.getlist(HEADER)
            and getattr(request.state, 'hosted_operator_principal', None) is None):
        return None
    try:
        if not _bound_profile(request, profile):
            return None
        principal = _local_principal(request, profile) if local_owner(request) else _verified(request, profile)
    except HTTPException as exc:
        if exc.status_code in (400, 401, 403, 404):
            return None
        raise
    except (OSError, sqlite3.Error):
        raise HTTPException(503, 'operator_authority_unavailable',
                            headers={'Cache-Control': 'no-store'}) from None
    if principal is None or profile not in principal['profiles']:
        return None
    return MappingProxyType({**principal, 'purpose': 'recipient-policy', 'profile': profile,
                             'profiles': tuple(principal['profiles']), 'capabilities': CAPABILITIES})


def hosted_operator_envelope(request, profile):
    principal = operator_authorization(request, profile=profile, capability='recipient-policy:read')
    if principal is None:
        return None
    return {key: sorted(value) if isinstance(value, (tuple, frozenset)) else value
            for key, value in principal.items() if key not in {'purpose', 'profile'}}
