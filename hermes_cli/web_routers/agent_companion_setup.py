"""Explicit paired-app management of the fixed reviewed companion in one served profile."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from hermes_cli.agent_setup import companion
from hermes_cli.agent_setup.authority import (
    account_id, local_owner, operator_authorization, operator_store, validate_profile,
)
from hermes_cli.agent_setup.models import OperatorError, exact_object
from hermes_cli.config_backend import ConfigBackendUnavailable
from hermes_cli.plugins_cmd import PluginOperationError
from hermes_cli.web_routers.agent_operator import _body
from hermes_cli.web_server_profiles import _config_profile_scope

router = APIRouter(prefix='/api/agent/v1/setup/companion', tags=['Companion setup'])
_NO_STORE = {'Cache-Control': 'no-store'}


def _scope(request, profile):
    account = account_id(request)
    if local_owner(request):
        raise HTTPException(403, {'code': 'companion_grant_required'}, headers=_NO_STORE)
    if request.query_params.getlist('profile') != [profile]:
        raise HTTPException(400, {'code': 'profile_scope_mismatch'}, headers=_NO_STORE)
    validate_profile(request, profile)
    return {'schema_version': 1, 'installation_id': operator_store().installation_id,
            'account_id': account, 'profile': profile}


def _principal(request, profile):
    # A same-user ticket is never an app-instance approval for this route.
    if local_owner(request):
        return None
    return operator_authorization(request, profile=profile, capability='companion:manage')


def _run(request, profile, ref, mutation=None, body=None):
    try:
        with _config_profile_scope(profile):
            principal = _principal(request, profile)
            if mutation is not None:
                if principal is None:
                    raise HTTPException(403, {'code': 'companion_grant_required'}, headers=_NO_STORE)
                return companion.mutate(mutation, body)
            return {'pairing_supported': True, 'required_capability': 'companion:manage',
                    'can_manage': principal is not None, 'plugin': companion.status(),
                    'target': companion.target(ref)}
    except (OSError, sqlite3.Error, OperatorError, PluginOperationError, ConfigBackendUnavailable):
        raise HTTPException(503, {'code': 'companion_setup_unavailable'}, headers=_NO_STORE) from None


@router.get('')
async def status(request: Request, response: Response, profile: str, ref: str):
    scope = _scope(request, profile)
    if request.query_params.getlist('ref') != [ref] or not companion._SHA.fullmatch(ref):
        raise HTTPException(400, {'code': 'invalid_companion_setup_request'}, headers=_NO_STORE)
    response.headers.update(_NO_STORE)
    return {**scope, **await run_in_threadpool(_run, request, profile, ref)}


async def _mutate(operation, request, response, profile):
    scope = _scope(request, profile)
    # Refuse unapproved requests before buffering their intent; recheck in the worker.
    with _config_profile_scope(profile):
        if _principal(request, profile) is None:
            raise HTTPException(403, {'code': 'companion_grant_required'}, headers=_NO_STORE)
    body = await _body(request)
    try:
        keys = {'ref', 'expected_revision'} if operation == 'repin' else {'ref'}
        exact_object(body, keys)
        if any(type(body[key]) is not str or not companion._SHA.fullmatch(body[key]) for key in keys):
            raise OperatorError('invalid_operator_request')
    except OperatorError:
        raise HTTPException(400, {'code': 'invalid_companion_setup_request'}, headers=_NO_STORE) from None
    response.headers.update(_NO_STORE)
    return {**scope, **await run_in_threadpool(_run, request, profile, body['ref'], operation, body)}


@router.post('/install')
async def install(request: Request, response: Response, profile: str):
    return await _mutate('install', request, response, profile)


@router.post('/enable')
async def enable(request: Request, response: Response, profile: str):
    return await _mutate('enable', request, response, profile)


@router.post('/repin')
async def repin(request: Request, response: Response, profile: str):
    return await _mutate('repin', request, response, profile)
