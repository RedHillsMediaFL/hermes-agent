"""The committed user row and reply context survive the isolated-worker boundary."""
from types import SimpleNamespace
import threading
import uuid

from agent.turn_context import _stage_turn_user_message
from gateway.owner_identity import host_owner_subject
from hermes_companion import admit_message, submission_payload
from hermes_state import SessionDB
from run_agent import AIAgent
from tui_gateway import server
from tui_gateway.client_attribution import reply_note


def test_attributed_row_is_forwarded_and_adopted_without_a_second_user_write(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    owner = host_owner_subject()
    db.create_session('parent', source='ios', user_id=owner, profile_name='default')
    db.append_message('parent', 'assistant', 'The specific earlier answer')
    source = db.get_messages_as_conversation('parent', include_row_ids=True)[0]
    ref = {'session_id': 'parent', 'row_id': source['_row_id'], 'message_uid': source['message_uid']}
    op = str(uuid.uuid4())
    wanted = submission_payload(db, owner, 'default', 'parent', op, 'Explain this', ref, [])
    staged, receipt, _ = admit_message(db, owner, 'default', 'parent', op, wanted, 'Explain this')
    session = {'session_key': 'parent', 'history': [source], 'history_lock': threading.Lock(),
               'attached_images': ['unrelated-pending-image'], 'cwd': str(tmp_path), 'source': 'ios',
               'auth_user_id': owner, '_submit_user_row': staged}
    try:
        note = reply_note(session)
        assert ref['message_uid'] in note and 'The specific earlier answer' in note
        frame = server._compute_host_turn_frame('rid', 'sid', session, 'Explain this', image_paths=['explicit-image'])
        assert frame['submit_user_row'] == staged and frame['attached_images'] == ['explicit-image']
        assert session['attached_images'] == ['unrelated-pending-image']
        # This shell runs the same adoption and real SessionDB flush methods as a provider turn,
        # without invoking any model or overwriting system/provider settings.
        agent = SimpleNamespace(_session_db=db, _session_db_created=True, _persist_disabled=False,
            session_id='parent', _session_persist_lock=None, _flushed_db_message_ids=set(),
            _flushed_db_message_session_id=None, _last_flushed_db_idx=0,
            _persist_user_message_idx=None, _persist_user_message_override=None,
            _persist_user_message_timestamp=None, _pending_cli_user_message=None)
        agent._ensure_db_session = lambda: None
        agent._flush_messages_to_session_db = AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
        agent._flush_messages_to_session_db_unlocked = AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)
        worker = {**session, '_submit_user_row': frame['submit_user_row']}
        server._adopt_submit_user_row(worker, agent, 'Explain this', 'Explain this')
        user, _ = _stage_turn_user_message(agent, 'Explain this', 'Explain this', None, None, None, staged['display_metadata'])
        agent._persist_user_message_idx = 1
        agent._flush_messages_to_session_db([source, user, {'role': 'assistant', 'content': 'An explanation'}], [source])
        rows = db.get_messages_as_conversation('parent', include_row_ids=True)
        assert len(rows) == 3 and rows[1]['_row_id'] == receipt['user_message']['row_id']
        assert rows[1]['display_metadata']['agent_reply_to'] == receipt['reply_to']
        assert rows[1]['content'] == 'Explain this'
    finally:
        db.close()


def test_unattributed_turns_carry_no_native_row_or_note(tmp_path):
    session = {'history_lock': threading.Lock(), 'history': [], 'session_key': 'parent',
               'cwd': str(tmp_path), '_submit_user_row': {'content': 'Plain message', 'display_metadata': None}}
    assert reply_note(session) == ''
    assert 'submit_user_row' not in server._compute_host_turn_frame('rid', 'sid', session, 'Plain message')


def test_attributed_admission_refuses_a_turn_that_became_busy_after_the_early_probe():
    session = {'history_lock': threading.Lock(), 'running': True, 'inflight_turn': {'user': 'Other turn'},
               'attached_images': ['other-turn-image']}
    response, fields = server._lock_in_submit_turn('rid', 'sid', session, 'Reply',
        {'_companion_admission': object()}, False, None, None, None)
    assert not fields and response['error']['code'] == 4091
    assert response['error']['data'] == {'code': 'session_busy', 'accepted': False, 'retryable': True}
    assert session['inflight_turn'] == {'user': 'Other turn'}
    assert session['attached_images'] == ['other-turn-image']
