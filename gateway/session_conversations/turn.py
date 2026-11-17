"""One admission's reply reference; snapshots commit with the existing durable FIFO."""
from contextlib import contextmanager
from contextvars import ContextVar

from gateway.session_conversations.references import (
    current_target, family, identifier, message, profile_name, reference_note, source_reference,
)
from hermes_state_runtime import RuntimeStoreError

_turn = ContextVar('conversation_reference_turn', default=None)


def submit_fields(params):
    fields = {}
    if 'conversation_session_id' in params:
        fields['view'] = identifier(params['conversation_session_id'])
    if 'reply_to' in params:
        fields['reference'] = source_reference(params['reply_to'])
    return {'conversation_v1': fields} if fields else {}


def prepare_payload(authority, actor, request, conn, payload):
    """Resolve a NEW reply inside its admission transaction; exact replays keep their snapshot."""
    committed = payload.get('conversation_v1')
    if committed is None:
        return payload
    if not isinstance(committed, dict) or set(committed) - {'view', 'reference'}:
        raise RuntimeStoreError('invalid_params')
    target = current_target(authority.db, conn, request.ref.session_id)
    chain = family(authority.db, conn, target)
    view = committed.get('view', request.ref.session_id)
    if view not in chain and view != request.ref.session_id:
        raise RuntimeStoreError('reply_conversation_mismatch')
    saved = {**committed, 'operation_id': request.request_id}
    if 'reference' in committed:
        reference = source_reference(committed['reference'])
        if reference['session_id'] not in chain:
            raise RuntimeStoreError('reply_conversation_mismatch')
        saved['snapshot'] = message(authority.db, conn, reference, profile_name(authority))
    return {**payload, 'conversation_v1': saved}


def restore(committed):
    if committed is None:
        return None
    if (not isinstance(committed, dict) or set(committed) - {'view', 'reference', 'operation_id', 'snapshot'}
            or 'operation_id' not in committed):
        raise RuntimeStoreError('invalid_params')
    identifier(committed['operation_id'])
    if 'view' in committed:
        identifier(committed['view'])
    if ('reference' in committed) != ('snapshot' in committed):
        raise RuntimeStoreError('invalid_params')
    if 'reference' in committed:
        reference = source_reference(committed['reference'])
        anchor = committed['snapshot']
        if (not isinstance(anchor, dict) or set(anchor) != {'profile', 'session_id', 'row_id', 'message_uid', 'role', 'snapshot'}
                or any(anchor.get(key) != value for key, value in reference.items())
                or anchor['role'] not in ('user', 'assistant') or not isinstance(anchor['profile'], str)
                or not isinstance(anchor['snapshot'], dict)
                or set(anchor['snapshot']) != {'text', 'timestamp', 'truncated'}
                or not isinstance(anchor['snapshot']['text'], str) or len(anchor['snapshot']['text']) > 12_000
                or type(anchor['snapshot']['truncated']) is not bool
                or not isinstance(anchor['snapshot']['timestamp'], (int, float))):
            raise RuntimeStoreError('invalid_params')
    return committed


@contextmanager
def conversation_turn_scope(admission_id, committed):
    token = _turn.set((admission_id, restore(committed)))
    try:
        yield
    finally:
        _turn.reset(token)


def _executing(event):
    bound = _turn.get()
    message_id = event if isinstance(event, str) else getattr(event, 'message_id', None)
    return bound[1] if bound is not None and message_id == bound[0] else None


def display_metadata(event):
    saved = _executing(event)
    if not saved:
        return {}
    return {'agent_operation_id': saved['operation_id'],
            **({'agent_reply_to': saved['snapshot']} if 'snapshot' in saved else {})}


def model_note(event):
    saved = _executing(event)
    return reference_note(saved['snapshot']) if saved and 'snapshot' in saved else ''


def worker_input(text, *, visible=None):
    bound = _turn.get()
    saved = None if bound is None else bound[1]
    metadata = {} if not saved else {'agent_operation_id': saved['operation_id'],
                                   **({'agent_reply_to': saved['snapshot']} if 'snapshot' in saved else {})}
    note = reference_note(saved['snapshot']) if saved and 'snapshot' in saved else ''
    model = text
    if note:
        model = note + '\n\n' + text if isinstance(text, str) else [{'type': 'text', 'text': note}, *text]
    return (model,
            {**({'persist_user_message': visible if visible is not None else text} if note or visible is not None else {}),
             **({'persist_user_display_metadata': metadata} if metadata else {})})
