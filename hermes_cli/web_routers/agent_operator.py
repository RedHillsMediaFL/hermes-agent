"""Authenticated app-instance pairing with explicit local operator approval."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response

from hermes_cli.agent_setup.authority import (
    account_id, local_owner, operator_authorization, operator_store, require_local_owner, validate_profile,
)
from hermes_cli.agent_setup.models import (
    CAPABILITIES, HEADER, SUPPORTED_CAPABILITIES, OperatorError, PairingInput, capabilities,
    exact_object, identifier, profiles,
)

router = APIRouter(prefix='/api/agent/v1/operator', tags=['Operator delegation'])
_NO_STORE = {'Cache-Control': 'no-store'}


def _run(operation):
    try:
        return operation()
    except OperatorError as exc:
        raise HTTPException(exc.status, exc.code, headers=_NO_STORE) from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, 'operator_authority_unavailable', headers=_NO_STORE) from None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


async def _body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, 'operator_request_too_large', headers=_NO_STORE)
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, 'invalid_operator_request', headers=_NO_STORE) from None


@router.post('/pairings')
async def create_pairing(request: Request, response: Response):
    account = account_id(request)
    body = await _body(request)
    value = _run(lambda: PairingInput.parse(body))
    for profile in value.profiles:
        validate_profile(request, profile)
    response.headers.update(_NO_STORE)
    return _run(lambda: operator_store().create(account, value))


@router.get('/pairings/{pairing_id}')
def pairing_status(pairing_id: str, request: Request, response: Response):
    account = None if local_owner(request) else account_id(request)
    key = _run(lambda: identifier(pairing_id))
    response.headers.update(_NO_STORE)
    return _run(lambda: operator_store().pairing(key, account))


@router.post('/pairings/{pairing_id}/approve')
async def approve_pairing(pairing_id: str, request: Request, response: Response):
    require_local_owner(request)
    body = await _body(request)
    def apply():
        if type(body) is not dict or set(body) not in ({'profiles'}, {'profiles', 'capabilities'}):
            raise OperatorError('invalid_operator_request')
        requested = capabilities(body.get('capabilities', sorted(CAPABILITIES)))
        return operator_store().decide(identifier(pairing_id), 'approved', profiles(body['profiles']), requested)
    response.headers.update(_NO_STORE)
    return _run(apply)


@router.post('/pairings/{pairing_id}/deny')
async def deny_pairing(pairing_id: str, request: Request, response: Response):
    require_local_owner(request)
    body = await _body(request)
    def apply():
        exact_object(body, set())
        return operator_store().decide(identifier(pairing_id), 'denied')
    response.headers.update(_NO_STORE)
    return _run(apply)


@router.post('/pairings/{pairing_id}/redeem')
async def redeem_pairing(pairing_id: str, request: Request, response: Response):
    account = account_id(request)
    body = await _body(request)
    def apply():
        exact_object(body, {'device_id', 'code_verifier'})
        return operator_store().redeem(identifier(pairing_id), account,
                                       identifier(body['device_id']), body['code_verifier'])
    response.headers.update(_NO_STORE)
    return _run(apply)


@router.get('/status')
def status(request: Request, response: Response, profile: str):
    account = account_id(request)
    validate_profile(request, profile)
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope(profile):
        principal = operator_authorization(request, profile=profile, capability='recipient-policy:read')
    response.headers.update(_NO_STORE)
    return {'schema_version': 1, 'installation_id': operator_store().installation_id,
            'account_id': account, 'profile': profile, 'pairing_supported': True,
            'can_manage_policy': principal is not None,
            'supported_capabilities': sorted(SUPPORTED_CAPABILITIES)}


def _current(request):
    account = account_id(request)
    values = request.headers.getlist(HEADER)
    principal = _run(lambda: operator_store().principal(token=values[0])) if len(values) == 1 else None
    if principal is None or principal['account_id'] != account:
        raise HTTPException(403, 'operator_grant_required', headers=_NO_STORE)
    return principal


@router.post('/grants/current/renew')
async def renew_grant(request: Request, response: Response):
    account = account_id(request)
    values = request.headers.getlist(HEADER)
    if len(values) != 1 or not values[0].startswith('hop1_') or len(values[0]) > 128:
        raise HTTPException(403, 'operator_grant_required', headers=_NO_STORE)
    body = await _body(request)
    def apply():
        exact_object(body, {'device_id', 'operation_id'})
        return operator_store().renew(values[0], account, identifier(body['device_id']),
                                       identifier(body['operation_id']))
    response.headers.update(_NO_STORE)
    return _run(apply)


@router.get('/grants/current')
def current_grant(request: Request, response: Response):
    response.headers.update(_NO_STORE)
    return _current(request)


@router.delete('/grants/current')
def revoke_current(request: Request, response: Response):
    principal = _current(request)
    _run(lambda: operator_store().revoke(principal['grant_id']))
    response.headers.update(_NO_STORE)
    return {'schema_version': 1, 'ok': True, 'revoked': True, 'grant_id': principal['grant_id']}


@router.delete('/grants/{grant_id}')
def revoke_grant(grant_id: str, request: Request, response: Response):
    require_local_owner(request)
    key = _run(lambda: identifier(grant_id))
    _run(lambda: operator_store().revoke(key))
    response.headers.update(_NO_STORE)
    return {'schema_version': 1, 'ok': True, 'revoked': True, 'grant_id': key}
