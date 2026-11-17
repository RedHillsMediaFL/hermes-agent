"""Compression keeps physical source identity without crossing independent task/fork boundaries."""
import uuid

import pytest

import hermes_companion as companion
from gateway.owner_identity import host_owner_subject
from hermes_state import SessionDB


@pytest.fixture()
def state(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    owner = host_owner_subject()
    db.create_session('parent', source='ios', user_id=owner, profile_name='default')
    db.append_message('parent', 'assistant', 'An earlier answer')
    source = db.get_messages_as_conversation('parent', include_row_ids=True)[0]
    ref = {'session_id': 'parent', 'row_id': source['_row_id'], 'message_uid': source['message_uid']}
    yield db, owner, ref
    db.close()


def rotate(db, parent, child):
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source='ios',
        messages=[{'role': 'assistant', 'content': 'Compression summary'}],
        profile_name='default', require_compression_lease=False)


def submit(db, owner, target, ref, *, op=None, view=None):
    op = op or str(uuid.uuid4())
    payload = companion.submission_payload(db, owner, 'default', target, op, 'Reply', ref, [], view_session_id=view)
    return companion.admit_message(db, owner, 'default', target, op, payload, 'Reply', view_session_id=view)


def test_ancestor_reference_links_reply_and_exact_retry_survive_rotation(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    _, first, _ = submit(db, owner, 'parent', ref, op=op)
    task = companion.create_conversation(db, owner, 'default', 'task', op=str(uuid.uuid4()), origin=ref, title='Task')
    rotate(db, 'parent', 'continuation')
    source = companion.resolve_message(db, owner, 'default', 'continuation', ref['row_id'])
    assert source['session_id'] == 'parent' and source['message_uid'] == ref['message_uid']
    _, replayed, was_replay = submit(db, owner, 'continuation', ref, op=op, view='parent')
    assert was_replay and replayed == first
    _, current, was_replay = submit(db, owner, 'continuation', ref, view='parent')
    assert not was_replay and current['stored_session_id'] == 'continuation'
    assert current['reply_to']['session_id'] == 'parent'
    links, _ = companion.links(db, owner, 'default', 'parent')
    assert [row['session_id'] for row in links] == ['parent', 'continuation']
    assert companion.threads(db, owner, 'default', parent='continuation')[0]['thread_id'] == task['thread']['thread_id']
    rows = db.get_messages('continuation', include_compacted=True, include_ancestors=True)
    assert {ref['row_id'], current['user_message']['row_id']} <= {row['id'] for row in rows}


def test_task_metadata_and_origin_survive_the_task_itself_rotating(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    created = companion.create_conversation(db, owner, 'default', 'task', op=op, origin=ref, title='Task')
    rotate(db, 'task', 'task-tip')
    found = companion.threads(db, owner, 'default', child='task-tip')
    assert len(found) == 1 and found[0]['thread_id'] == 'task' and found[0]['origin'] == created['thread']['origin']
    retry = companion.create_conversation(db, owner, 'default', 'unused', op=op, origin=ref, title='Task')
    assert not retry['created'] and retry['session_id'] == 'task'
    assert db.get_compression_tip(retry['session_id']) == 'task-tip'
    db.append_message('task-tip', 'assistant', 'Task-specific answer')
    source = db.get_messages_as_conversation('task-tip', include_row_ids=True)[-1]
    own_ref = {'session_id': 'task-tip', 'row_id': source['_row_id'], 'message_uid': source['message_uid']}
    _, receipt, _ = submit(db, owner, 'task-tip', own_ref)
    assert receipt['reply_to']['session_id'] == 'task-tip'
    assert companion.links(db, owner, 'default', 'parent')[0] == []


@pytest.mark.parametrize('marker,source', [('_branched_from', 'ios'), ('_reset_from', 'ios'), ('_delegate_from', 'ios'), (None, 'tool')])
def test_unrelated_forks_never_inherit_parent_reply_or_task_authority(state, marker, source):
    db, owner, ref = state
    submit(db, owner, 'parent', ref)
    companion.create_conversation(db, owner, 'default', 'task', op=str(uuid.uuid4()), origin=ref, title='Task')
    rotate(db, 'parent', 'continuation')
    db.create_session('fork', source=source, user_id=owner, parent_session_id='parent',
                      model_config={marker: 'parent'} if marker else None, profile_name='default')
    with pytest.raises(companion.CompanionError, match='source_message_not_found'):
        companion.resolve_message(db, owner, 'default', 'fork', ref['row_id'])
    with pytest.raises(companion.CompanionError, match='reply_conversation_mismatch'):
        submit(db, owner, 'fork', ref)
    assert companion.links(db, owner, 'default', 'fork')[0] == []
    assert companion.threads(db, owner, 'default', parent='fork') == []
    with pytest.raises(companion.CompanionError, match='reply_conversation_mismatch'):
        submit(db, owner, 'continuation', ref, view='fork')


def test_protected_tail_copies_paint_one_link_and_rewind_does_not_revive_an_ancestor(state):
    db, owner, ref = state
    staged, first, _ = submit(db, owner, 'parent', ref)
    rotate(db, 'parent', 'continuation')
    copy_id = db.append_message('continuation', 'user', 'Reply', timestamp=staged['timestamp'],
                               message_uid=staged['message_uid'], display_metadata=staged['display_metadata'])
    links, _ = companion.links(db, owner, 'default', 'continuation')
    assert len(links) == 1 and links[0]['row_id'] == copy_id and links[0]['session_id'] == 'continuation'
    assert links[0]['operation_id'] == first['operation_id']
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0,compacted=0 WHERE id=?', (copy_id,)))
    assert companion.links(db, owner, 'default', 'continuation')[0] == []


@pytest.mark.parametrize('field,value,error', [('user_id', 'uid:999999', 'permission_denied'),
                                             ('profile_name', 'other', 'profile_mismatch')])
def test_every_lineage_hop_keeps_owner_and_profile_fences(state, field, value, error):
    db, owner, ref = state
    rotate(db, 'parent', 'continuation')
    db._execute_write(lambda conn: conn.execute(f'UPDATE sessions SET {field}=? WHERE id=?', (value, 'parent')))
    with pytest.raises(companion.CompanionError, match=error):
        companion.resolve_message(db, owner, 'default', 'continuation', ref['row_id'])
