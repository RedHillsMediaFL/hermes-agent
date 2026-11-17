"""Durable native-client attribution: writer atomicity, restart replay and owner fences."""
from concurrent.futures import ThreadPoolExecutor
import threading
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
    db.append_message('parent', 'assistant', 'A persisted answer')
    row = db.get_messages_as_conversation('parent', include_row_ids=True)[0]
    ref = {'session_id': 'parent', 'row_id': row['_row_id'], 'message_uid': row['message_uid']}
    yield db, owner, ref
    db.close()


def payload(db, owner, ref, op):
    return companion.submission_payload(db, owner, 'default', 'parent', op, 'Reply', ref, [])


def test_atomic_admission_restarts_with_same_reference_and_no_duplicate(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, ref, op)
    staged, receipt, replayed = companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    assert not replayed and staged['_row_id'] == receipt['user_message']['row_id']
    assert receipt['reply_to']['message_uid'] == ref['message_uid']
    reopened = SessionDB(db_path=db.db_path)
    try:
        _, again, replayed = companion.admit_message(reopened, owner, 'default', 'parent', op, wanted, 'Reply')
        assert replayed and again == receipt
        assert len(reopened.get_messages_as_conversation('parent')) == 2
        links, cursor = companion.links(reopened, owner, 'default', 'parent')
        assert cursor is None and links[0]['reply_to'] == receipt['reply_to']
        changed = {**wanted, 'text': 'Changed'}
        with pytest.raises(companion.CompanionError, match='operation_conflict'):
            companion.admit_message(reopened, owner, 'default', 'parent', op, changed, 'Changed')
    finally:
        reopened.close()


def test_receipt_failure_rolls_back_user_row_and_counters(state, monkeypatch):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, ref, op)
    def fail(*args, **kwargs):
        raise RuntimeError('receipt write failed')
    monkeypatch.setattr(companion, '_save', fail)
    with pytest.raises(RuntimeError, match='receipt write failed'):
        companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    assert len(db.get_messages_as_conversation('parent')) == 1
    assert db.get_session('parent')['message_count'] == 1
    assert companion.replay_submission(db, owner, op, wanted) is None


def test_concurrent_retry_commits_one_row(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, ref, op)
    barrier = threading.Barrier(2)
    def admit():
        handle = SessionDB(db_path=db.db_path)
        try:
            barrier.wait(timeout=10)
            return companion.admit_message(handle, owner, 'default', 'parent', op, wanted, 'Reply')[1:]
        finally:
            handle.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: admit(), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(value[1] for value in results) == [False, True]
    assert len(db.get_messages_as_conversation('parent')) == 2


@pytest.mark.parametrize('mutation,expected', [
    ({'row_id': 9999}, 'source_message_not_found'),
    ({'message_uid': 'wrong'}, 'source_message_mismatch'),
    ({'session_id': 'missing'}, 'source_session_not_found'),
])
def test_untrusted_reference_never_writes(state, mutation, expected):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, {**ref, **mutation}, op)
    with pytest.raises(companion.CompanionError, match=expected):
        companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    assert len(db.get_messages_as_conversation('parent')) == 1


def test_profile_foreign_owner_and_routed_unowned_fences(state):
    db, owner, ref = state
    with pytest.raises(companion.CompanionError, match='profile_mismatch'):
        companion.resolve_message(db, owner, 'secondary', 'parent', ref['row_id'])
    db.create_session('foreign', source='ios', user_id='uid:999999')
    db.create_session('remote', source='telegram', user_id=None, chat_id='remote-chat')
    for key in ('foreign', 'remote'):
        db.append_message(key, 'assistant', 'Private')
        row = db.get_messages_as_conversation(key, include_row_ids=True)[0]
        with pytest.raises(companion.CompanionError, match='permission_denied'):
            companion.resolve_message(db, owner, 'default', key, row['_row_id'])
    db.create_session('local', source='cli')
    db.append_message('local', 'assistant', 'Local history')
    row = db.get_messages_as_conversation('local', include_row_ids=True)[0]
    assert companion.resolve_message(db, owner, 'default', 'local', row['_row_id'])['snapshot']['text'] == 'Local history'


def test_task_and_ongoing_identities_survive_retry_and_do_not_copy_parent_history(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    result = companion.create_conversation(db, owner, 'default', 'task', op=op, origin=ref, title='Separate task')
    again = companion.create_conversation(db, owner, 'default', 'unused', op=op, origin=ref, title='Separate task')
    assert result['created'] and not again['created'] and again['session_id'] == 'task'
    rows = db.get_messages_as_conversation('task')
    assert len(rows) == 1 and rows[0]['role'] == 'assistant' and rows[0]['display_kind'] == 'hidden'
    assert companion.threads(db, owner, 'default', parent='parent')[0]['origin'] == result['thread']['origin']
    first = companion.create_conversation(db, owner, 'default', 'ongoing')
    next_one = companion.create_conversation(db, owner, 'default', 'unused-ongoing')
    assert first['created'] and not next_one['created']
    assert companion.ongoing(db, owner, 'default') == 'ongoing'
    assert next_one['session_id'] == 'ongoing'


def test_deleted_admitted_message_has_tombstone_and_cannot_be_recreated_by_retry(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, ref, op)
    _, receipt, _ = companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (receipt['user_message']['row_id'],)))
    with pytest.raises(companion.CompanionError, match='operation_target_deleted'):
        companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    row = db._conn.execute('SELECT receipt FROM agent_client_operations WHERE operation_id=?', (op,)).fetchone()
    assert row['receipt'] == '{}'


def test_replay_retains_exact_accepted_reference_after_origin_is_removed(state):
    db, owner, ref = state
    op = str(uuid.uuid4())
    wanted = payload(db, owner, ref, op)
    _, receipt, _ = companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')
    db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (ref['row_id'],)))
    retry = payload(db, owner, ref, op)
    assert companion.replay_submission(db, owner, op, retry) == receipt


def test_links_paginate_matching_rows_instead_of_unrelated_history(state):
    db, owner, ref = state
    receipts = []
    for _ in range(2):
        for index in range(3):
            db.append_message('parent', 'assistant', f'Unrelated answer {index}')
        op = str(uuid.uuid4())
        wanted = payload(db, owner, ref, op)
        receipts.append(companion.admit_message(db, owner, 'default', 'parent', op, wanted, 'Reply')[1])
    first, cursor = companion.links(db, owner, 'default', 'parent', limit=1)
    assert first[0]['operation_id'] == receipts[0]['operation_id'] and cursor == first[0]['row_id']
    second, _ = companion.links(db, owner, 'default', 'parent', limit=1, after=cursor)
    assert second[0]['operation_id'] == receipts[1]['operation_id']


def test_task_receipt_failure_rolls_back_child_and_deleted_task_retry_cannot_recreate_it(state, monkeypatch):
    db, owner, ref = state
    op = str(uuid.uuid4())
    original = companion._save
    def fail(*args, **kwargs):
        raise RuntimeError('receipt write failed')
    monkeypatch.setattr(companion, '_save', fail)
    with pytest.raises(RuntimeError, match='receipt write failed'):
        companion.create_conversation(db, owner, 'default', 'task', op=op, origin=ref, title='Task')
    assert db.get_session('task') is None
    monkeypatch.setattr(companion, '_save', original)
    companion.create_conversation(db, owner, 'default', 'task', op=op, origin=ref, title='Task')
    db.delete_session('task')
    with pytest.raises(companion.CompanionError, match='operation_target_deleted'):
        companion.create_conversation(db, owner, 'default', 'replacement', op=op, origin=ref, title='Task')
    assert db.get_session('replacement') is None


def test_ongoing_map_keeps_its_explicit_root_across_compression(state):
    db, owner, _ = state
    companion.create_conversation(db, owner, 'default', 'ongoing')
    db.end_session('ongoing', end_reason='compression')
    db.create_session('continuation', source='ios', user_id=owner, parent_session_id='ongoing', profile_name='default')
    assert companion.ongoing(db, owner, 'default') == 'ongoing'
    assert db.get_compression_tip('ongoing') == 'continuation'
