"""Exact reply references travel through the real native owner FIFO and ordinary persistence."""
from dataclasses import asdict
from types import SimpleNamespace
import uuid

import pytest

from gateway.response_filters import persisted_user_metadata_for_event
from gateway.session_controls import AuthorityConnection
from gateway.session_conversations import store
from gateway.session_conversations.turn import model_note
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_surface_admission import _authority


def _reference(db, sid='s'):
    db.append_message(sid, 'assistant', 'Original stored answer')
    with db._read_ctx() as conn:
        row = conn.execute('SELECT id,message_uid FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
    return {'session_id': sid, 'row_id': row[0], 'message_uid': row[1]}


@pytest.mark.asyncio
async def test_reply_snapshot_and_operation_survive_fifo_execution_and_lost_ack_retry(tmp_path, monkeypatch):
    seen = []
    authority = None
    async def answer(event):
        metadata = persisted_user_metadata_for_event(event)
        seen.append((event.text, metadata, model_note(event)))
        authority.db.append_message('s', 'user', event.text, display_metadata=metadata)
        # Another event on the same executing task must not borrow this reference.
        assert persisted_user_metadata_for_event(SimpleNamespace(message_id='different', internal=False, metadata={})) == {}
        return 'ok'
    authority = await _authority(tmp_path, monkeypatch, answer)
    reference = _reference(authority.db)
    viewer = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    operation = str(uuid.uuid4())
    params = {'session_id': 's', 'submission_id': operation, 'text': 'Follow up',
              'conversation_session_id': 's', 'reply_to': reference}
    try:
        assert 'result' in await viewer.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
        accepted = await viewer.dispatch({'id': 2, 'method': 'prompt.submit', 'params': params})
        assert accepted['result']['status'] == 'queued', accepted
        await authority.sessions['s'].task
        authority.db._write_sql('DELETE FROM messages WHERE id=?', (reference['row_id'],))
        replay = await viewer.dispatch({'id': 3, 'method': 'prompt.submit', 'params': params})
        assert replay['result']['admission_id'] == accepted['result']['admission_id'], replay
        assert replay['result']['status'] == 'terminal'
        conflict = await viewer.dispatch({'id': 4, 'method': 'prompt.submit', 'params': {**params, 'text': 'Changed retry'}})
        assert conflict['error']['message'] == 'admission_conflict', conflict
    finally:
        await viewer.close()
    assert len(seen) == 1
    text, metadata, note = seen[0]
    assert text == 'Follow up'
    assert metadata['agent_operation_id'] == operation
    assert {key: metadata['agent_reply_to'][key] for key in reference} == reference
    assert metadata['agent_reply_to']['snapshot']['text'] == 'Original stored answer'
    assert 'quoted reference data' in note and 'Original stored answer' in note
    rows = list_session_admissions(authority.db, session_id='s', pending_only=False)
    assert len(rows) == 1 and rows[0]['payload']['conversation_v1']['snapshot'] == metadata['agent_reply_to']
    links, cursor = store.links(authority, 's')
    assert len(links) == 1 and links[0]['operation_id'] == operation and cursor is None


@pytest.mark.asyncio
async def test_reference_mismatch_and_unrelated_conversation_never_enter_the_fifo(tmp_path, monkeypatch):
    async def answer(event):
        raise AssertionError('A refused reference must never run')
    authority = await _authority(tmp_path, monkeypatch, answer)
    reference = _reference(authority.db)
    authority.db.create_session('other', source='telegram')
    foreign = _reference(authority.db, 'other')
    viewer = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    try:
        await viewer.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
        for index, source, extra, reason in (
                (2, {**reference, 'message_uid': 'different'}, {}, 'source_message_mismatch'),
                (3, foreign, {}, 'reply_conversation_mismatch'),
                (4, reference, {'conversation_session_id': 'other'}, 'reply_conversation_mismatch')):
            reply = await viewer.dispatch({'id': index, 'method': 'prompt.submit', 'params': {
                'session_id': 's', 'submission_id': str(uuid.uuid4()), 'text': 'No admission',
                'conversation_session_id': 's', 'reply_to': source, **extra}})
            assert reply['error']['message'] == reason, reply
        assert list_session_admissions(authority.db, session_id='s', pending_only=False) == []
    finally:
        await viewer.close()


def test_managed_worker_keeps_plain_user_text_and_server_reference_metadata(tmp_path):
    from gateway.session_conversations.turn import conversation_turn_scope
    from gateway.session_kanban import _run_worker_turns
    from gateway.session_policy import build_policy
    anchor = {'profile': 'default', 'session_id': 'origin', 'row_id': 1, 'message_uid': 'source-uid',
              'role': 'assistant', 'snapshot': {'text': 'Quoted source', 'timestamp': 1.0, 'truncated': False}}
    committed = {'view': 'origin', 'reference': {'session_id': 'origin', 'row_id': 1, 'message_uid': 'source-uid'},
                 'snapshot': anchor, 'operation_id': str(uuid.uuid4())}
    policy = build_policy({'source': 'gui', 'cwd': str(tmp_path), 'model': 'fixture-model', 'toolsets': []}, {})
    frame = {'text': 'My own words', 'policy': asdict(policy), 'scope': {'session_id': 'task'},
             'route': 'local:task', 'home': str(tmp_path)}
    # Exercise the real worker adapter without loading a provider or invoking tools.
    seen = {}
    class Agent:
        def run_conversation(self, text, **kwargs):
            seen.update(text=text, **kwargs)
            return {'ok': True}
    with conversation_turn_scope('admission', committed):
        _run_worker_turns(Agent(), frame, [])
    assert 'Quoted source' in seen['text']
    assert seen['persist_user_message'] == 'My own words'
    assert seen['persist_user_display_metadata'] == {'agent_operation_id': committed['operation_id'], 'agent_reply_to': anchor}
