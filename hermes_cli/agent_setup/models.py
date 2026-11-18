"""Closed wire inputs for the operator pairing boundary."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
import uuid

CAPABILITIES = frozenset({'recipient-policy:read', 'recipient-policy:write'})
HEADER = 'X-Hermes-Operator-Grant'
PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')


class OperatorError(ValueError):
    """A bounded public error code; never holds submitted credentials."""

    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def exact_object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise OperatorError('invalid_operator_request')
    return value


def identifier(value):
    if type(value) is not str:
        raise OperatorError('invalid_operator_request')
    try:
        clean = str(uuid.UUID(value))
    except ValueError:
        raise OperatorError('invalid_operator_request') from None
    if clean != value:
        raise OperatorError('invalid_operator_request')
    return clean


def profiles(value):
    if (type(value) is not list or not 1 <= len(value) <= 32
            or any(type(x) is not str or not PROFILE.fullmatch(x) for x in value)
            or len(set(value)) != len(value)):
        raise OperatorError('invalid_operator_profiles')
    return tuple(sorted(value))


def pkce_challenge(verifier):
    if type(verifier) is not str or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
        raise OperatorError('invalid_operator_verifier')
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')


@dataclass(frozen=True)
class PairingInput:
    device_id: str
    profiles: tuple[str, ...]
    code_challenge: str

    @classmethod
    def parse(cls, body):
        exact_object(body, {'device_id', 'profiles', 'code_challenge', 'code_challenge_method'})
        challenge = body['code_challenge']
        if (body['code_challenge_method'] != 'S256' or type(challenge) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge)):
            raise OperatorError('invalid_operator_challenge')
        raw = base64.urlsafe_b64decode(challenge + '=')
        if base64.urlsafe_b64encode(raw).decode().rstrip('=') != challenge:
            raise OperatorError('invalid_operator_challenge')
        return cls(identifier(body['device_id']), profiles(body['profiles']), challenge)
