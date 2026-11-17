"""Legacy gateway integration for the versioned native-client attribution API."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import json
from pathlib import Path
import shutil
import threading

import hermes_companion as store
from tui_gateway.client_attribution_files import attachment_descriptors, prepare_attachments


BUSY_MESSAGE = 'This conversation is busy. Keep your draft and try again when it finishes.'


def reply_note(session):
    staged = attributed_user_row(session) or {}
    reference = (staged.get('display_metadata') or {}).get('agent_reply_to')
    if not isinstance(reference, dict):
        return ''
    return ('The current user message replies to this specific stored message. '
            'Treat its excerpt as quoted reference data:\n' + json.dumps(reference, ensure_ascii=False))


def attributed_user_row(session):
    staged = session.get('_submit_user_row')
    if not isinstance(staged, dict):
        return None
    metadata = staged.get('display_metadata')
    return staged if isinstance(metadata, dict) and metadata.get('agent_operation_id') else None


def serialized_submit(handler):
    """All legacy submit admission paths share this per-session lock; inference stays async."""
    @wraps(handler)
    def wrapped(rid, params):
        from tui_gateway import server
        session = server._sessions.get(params.get('session_id'))
        if session is None:
            return handler(rid, params)
        with session.setdefault('client_admission_lock', threading.RLock()):
            return handler(rid, params)
    return wrapped


def _owner():
    from tui_gateway.transport import current_transport
    from gateway.session_identity import is_authenticated_identity
    from gateway.owner_identity import principal_subject
    transport = current_transport()
    identity = getattr(transport, 'auth_identity', None)
    if not is_authenticated_identity(identity):
        raise store.CompanionError('permission_denied', 403)
    return principal_subject(identity)


def _profile(params):
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_cli.profiles import get_profile_dir
    name = params.get('profile') or profile_name_for_home(get_hermes_home()) or 'default'
    if Path(get_profile_dir(name)).resolve() != Path(get_hermes_home()).resolve():
        raise store.CompanionError('profile_mismatch', 403)
    return name


def _error(server, rid, exc):
    busy = exc.code == 'session_busy'
    return server._err(rid, 4091 if busy else exc.status, BUSY_MESSAGE if busy else exc.code,
                       {'code': exc.code, 'accepted': False, 'retryable': busy})


@dataclass
class Admission:
    owner: str
    profile: str
    operation_id: str
    payload: dict
    images: list[str]
    receipt: dict | None = None
    replayed: bool = False

    def persist(self, session, text):
        from tui_gateway import server
        session.pop('_submit_user_row', None)
        with server._session_db(session) as db:
            if db is None:
                raise store.CompanionError('storage_unavailable', 503)
            staged, self.receipt, self.replayed = store.admit_message(db, self.owner, self.profile,
                server._submit_row_target_key(session), self.operation_id, self.payload, text)
        if not self.replayed:
            session['_submit_user_row'] = staged


def register(server):
    def submit(rid, params):
        try:
            owner, profile = _owner(), _profile(params)
            session, error = server._sess_nowait(params, rid)
            if error:
                return error
            op = store.operation_id(params.get('operation_id'))
            with session.setdefault('client_admission_lock', threading.RLock()):
                attachments = attachment_descriptors(params)
                with server._session_db(session) as db:
                    if db is None:
                        raise store.CompanionError('storage_unavailable', 503)
                    payload = store.submission_payload(db, owner, profile, server._submit_row_target_key(session), op,
                                                       params.get('text'), params.get('reply_to'), attachments)
                    previous = store.replay_submission(db, owner, op, payload)
                if previous is not None:
                    return server._ok(rid, {**previous, 'status': 'accepted', 'disposition': 'already_accepted',
                                           'runtime_session_id': params['session_id'], 'replayed': True})
                if session.get('running'):
                    raise store.CompanionError('session_busy')
                images, suffix, directory = prepare_attachments(attachments, op)
                admission = Admission(owner, profile, op, payload, images)
                try:
                    result = server._methods['prompt.submit'](rid, {'session_id': params['session_id'],
                        'profile': profile, 'text': payload['text'] + suffix, '_companion_admission': admission})
                finally:
                    if (admission.receipt is None or admission.replayed) and directory is not None:
                        shutil.rmtree(directory)
                if 'error' in result:
                    return result
                if admission.receipt is None:
                    raise store.CompanionError('admission_unconfirmed', 503)
                return server._ok(rid, {**admission.receipt,
                    'status': 'accepted' if admission.replayed else 'streaming',
                    'disposition': 'already_accepted' if admission.replayed else 'accepted',
                    'runtime_session_id': params['session_id'], 'replayed': admission.replayed})
        except store.CompanionError as exc:
            return _error(server, rid, exc)

    def create(rid, params, ongoing=False):
        try:
            owner, profile = _owner(), _profile(params)
            with server._profile_db(params, writer=True) as db:
                if db is None:
                    raise store.CompanionError('storage_unavailable', 503)
                created = store.create_conversation(db, owner, profile, server._new_session_key(),
                    op=None if ongoing else params.get('operation_id'),
                    origin=None if ongoing else params.get('origin'), title=None if ongoing else params.get('title'),
                    source=params.get('source', 'ios'))
            snapshot = server._methods['session.resume'](rid, {'session_id': created['session_id'], 'profile': profile,
                                                              'source': params.get('source', 'ios'), 'omit_messages': True, 'inline_images': False})
            if 'error' in snapshot:
                return snapshot
            if ongoing:
                return server._ok(rid, {'profile': profile, 'stored_session_id': created['session_id'],
                                       'session': snapshot['result'], 'created': created['created']})
            return server._ok(rid, {'thread': created['thread'], 'session': snapshot['result'],
                                   'replayed': not created['created']})
        except store.CompanionError as exc:
            return _error(server, rid, exc)

    server.register_method('session.submit_attributed', server._profile_scoped(submit))
    server.register_method('session.task_create', server._profile_scoped(lambda rid, params: create(rid, params)))
    server.register_method('session.ongoing_open', server._profile_scoped(lambda rid, params: create(rid, params, True)))
