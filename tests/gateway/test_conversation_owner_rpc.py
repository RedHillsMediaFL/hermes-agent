"""Ongoing/task RPCs use the real canonical writer, routes, authorization and deletion fences."""
import asyncio
from pathlib import Path
import uuid

import pytest
import pytest_asyncio

from gateway.session_controls import AuthorityConnection


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from tests.gateway.test_local_create_env_refs import _owner
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (home / 'config.yaml').write_text(
        'platform_toolsets: {cli: [], gui: []}\n'
        'model: {provider: custom, default: fixture-model, base_url: http://provider.invalid/v1}\n')
    authority, connection = await _owner(home, monkeypatch)
    authority.runner.session_authority = authority
    try:
        yield home, authority, connection
    finally:
        await connection.close()
        from gateway.session_runtime_workers import join_authority_work
        await join_authority_work(authority, timeout=5)
        authority.db.close()


async def call(connection, method, **params):
    response = await connection.dispatch({'id': str(uuid.uuid4()), 'method': method, 'params': params})
    assert 'result' in response, response
    return response['result']


async def parent(owner, title='Parent topic'):
    home, authority, connection = owner
    snapshot = await call(connection, 'session.create', request_id=str(uuid.uuid4()), source='gui',
                          cwd=str(home), title=title, toolsets=[], hidden=title == 'Bot Chat')
    sid = snapshot['session_id']
    row_id = authority.db.append_message(sid, 'assistant', 'Keep the original topic intact')
    with authority.db._read_ctx() as conn:
        uid = conn.execute('SELECT message_uid FROM messages WHERE id=?', (row_id,)).fetchone()[0]
    return snapshot, {'session_id': sid, 'row_id': row_id, 'message_uid': uid}


def task_params(origin, **changes):
    return {'operation_id': str(uuid.uuid4()), 'origin': origin, 'title': 'Separate task', 'source': 'ios', **changes}


@pytest.mark.asyncio
async def test_ongoing_open_preserves_the_existing_native_bot_chat_and_history(owner):
    _, authority, connection = owner
    snapshot, origin = await parent(owner, title='Bot Chat')
    opened = await call(connection, 'session.ongoing_open', source='ios')
    assert opened['resolved_session_id'] == snapshot['stored_session_id']
    assert opened['session']['session_id'] == snapshot['session_id']
    assert authority.db.get_session(origin['session_id'])['hidden'] == 1
    assert any(row.get('message_uid') == origin['message_uid'] for row in opened['session']['messages'])
    assert (await call(connection, 'session.ongoing_open', source='desktop'))['resolved_session_id'] == \
        opened['resolved_session_id']


@pytest.mark.asyncio
async def test_two_native_connections_open_one_ongoing_chat(owner):
    _, authority, connection = owner
    peer = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    try:
        first, second = await asyncio.gather(call(connection, 'session.ongoing_open', source='ios'),
                                             call(peer, 'session.ongoing_open', source='ios'))
        assert first['resolved_session_id'] == second['resolved_session_id']
        with authority.db._read_ctx() as conn:
            assert conn.execute("SELECT count(*) FROM sessions WHERE title='Bot Chat'").fetchone()[0] == 1
        assert first['session']['subscription_id'] != second['session']['subscription_id']
    finally:
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('capabilities', [
    [], ['session:read'], ['session:read', 'session:submit'],
    ['session:read', 'session:create'], ['session:submit', 'session:create'],
])
async def test_denied_ongoing_open_does_not_claim_the_real_owners_conversation(owner, capabilities):
    from gateway.session_conversations.store import ONGOING_KEY
    _, authority, connection = owner
    denied = AuthorityConnection(authority, object(), {'user_id': 'denied', 'capabilities': capabilities})
    try:
        response = await denied.dispatch({'id': 'denied-open', 'method': 'session.ongoing_open',
                                          'params': {'source': 'ios'}})
        assert response['error']['data']['reason'] == 'permission_denied'
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone() is None
            assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0
        opened = await call(connection, 'session.ongoing_open', source='ios')
        assert opened['created'] is True
        assert authority.db.get_session(opened['resolved_session_id'])['title'] == 'Bot Chat'
    finally:
        await denied.close()


@pytest.mark.asyncio
async def test_wrong_profile_ongoing_open_does_not_leave_a_persistent_claim(owner):
    from gateway.session_conversations.store import ONGOING_KEY
    _, authority, connection = owner
    denied = AuthorityConnection(authority, object(), {'user_id': 'denied', 'profile_id': 'another-profile'})
    try:
        response = await denied.dispatch({'id': 'wrong-owner-profile', 'method': 'session.ongoing_open',
                                          'params': {'source': 'ios'}})
        assert response['error']['data']['reason'] == 'profile_mismatch'
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone() is None
            assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0
        assert (await call(connection, 'session.ongoing_open', source='ios'))['created'] is True
    finally:
        await denied.close()


@pytest.mark.asyncio
async def test_foreign_ongoing_open_cannot_claim_an_existing_owners_bot_chat(owner):
    from gateway.session_conversations.store import ONGOING_KEY
    _, authority, connection = owner
    original, origin = await parent(owner, title='Bot Chat')
    denied = AuthorityConnection(authority, object(), {'user_id': 'denied'})
    try:
        response = await denied.dispatch({'id': 'foreign-open', 'method': 'session.ongoing_open',
                                          'params': {'source': 'ios'}})
        assert response['error']['data']['reason'] == 'permission_denied'
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (ONGOING_KEY,)).fetchone() is None
            assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1
        opened = await call(connection, 'session.ongoing_open', source='ios')
        assert opened['resolved_session_id'] == original['stored_session_id']
        assert any(row.get('message_uid') == origin['message_uid'] for row in opened['session']['messages'])
    finally:
        await denied.close()


@pytest.mark.asyncio
async def test_ongoing_open_never_adopts_a_topic_that_only_shares_the_bot_chat_prefix(owner):
    _, authority, connection = owner
    topic, _ = await parent(owner, title='Bot Chat #2')
    opened = await call(connection, 'session.ongoing_open', source='ios')
    assert opened['resolved_session_id'] != topic['stored_session_id']
    assert authority.db.get_session(topic['stored_session_id'])['title'] == 'Bot Chat #2'
    assert authority.db.get_session(topic['stored_session_id'])['message_count'] == 1


@pytest.mark.asyncio
async def test_task_creation_has_an_independent_native_route_and_exact_reconnect_replay(owner):
    _, authority, connection = owner
    original, origin = await parent(owner)
    params = task_params(origin)
    result = await call(connection, 'session.task_create', **params)
    sid = result['session']['session_id']
    assert result['replayed'] is False
    assert sid != original['session_id']
    assert result['thread']['origin']['message_uid'] == origin['message_uid']
    assert authority.db.get_session(sid)['parent_session_id'] is None
    assert authority.sessions[sid].route != authority.sessions[original['session_id']].route
    assert authority.db.get_session(original['session_id'])['message_count'] == 1
    authority.db.append_message(sid, 'user', 'Work belonging only to the new task')
    peer = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    try:
        replay = await call(peer, 'session.task_create', **params)
        assert replay['replayed'] is True
        assert replay['thread']['thread_id'] == result['thread']['thread_id']
        assert replay['session']['session_id'] == sid
        assert len(authority.db.get_messages(original['session_id'])) == 1
        assert len(authority.db.get_messages(sid)) == 2
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_task_replay_does_not_recreate_a_deleted_destination(owner):
    _, authority, connection = owner
    _, origin = await parent(owner)
    params = task_params(origin)
    result = await call(connection, 'session.task_create', **params)
    snapshot = result['session']
    deleted = await call(connection, 'session.mutate', session_id=snapshot['session_id'],
                         request_id=str(uuid.uuid4()), expected_revision=snapshot['revision'],
                         expected_generation=snapshot['execution_generation'], operation='delete', payload={})
    assert snapshot['session_id'] in deleted['deleted_ids']
    response = await connection.dispatch({'id': 'retry', 'method': 'session.task_create', 'params': params})
    assert response['error']['data']['reason'] == 'not_found'
    assert authority.db.get_session(snapshot['stored_session_id']) is None
    assert authority.db.get_session(origin['session_id']) is not None


@pytest.mark.asyncio
async def test_deleting_the_origin_keeps_the_independent_task_and_its_exact_retry(owner):
    _, _authority, connection = owner
    original, origin = await parent(owner)
    params = task_params(origin)
    result = await call(connection, 'session.task_create', **params)
    await call(connection, 'session.mutate', session_id=original['session_id'], request_id=str(uuid.uuid4()),
               expected_revision=original['revision'], expected_generation=original['execution_generation'],
               operation='delete', payload={})
    replay = await call(connection, 'session.task_create', **params)
    assert replay['replayed'] is True
    assert replay['session']['session_id'] == result['session']['session_id']
    assert replay['thread']['origin']['snapshot']['text'] == 'Keep the original topic intact'


@pytest.mark.asyncio
async def test_changed_task_retry_and_foreign_owner_cannot_take_an_existing_task(owner):
    _, authority, connection = owner
    _, origin = await parent(owner)
    params = task_params(origin)
    created = await call(connection, 'session.task_create', **params)
    changed = await connection.dispatch({'id': 'changed', 'method': 'session.task_create',
                                         'params': {**params, 'title': 'Different task'}})
    assert changed['error']['data']['reason'] == 'admission_conflict'
    stranger = AuthorityConnection(authority, object(), {'user_id': 'stranger'})
    try:
        response = await stranger.dispatch({'id': 'foreign', 'method': 'session.task_create', 'params': params})
        assert response['error']['data']['reason'] == 'permission_denied'
        assert authority.db.get_session(created['session']['stored_session_id']) is not None
    finally:
        await stranger.close()


@pytest.mark.asyncio
async def test_task_creation_does_not_interrupt_a_started_parent_admission(owner, monkeypatch):
    from hermes_state_runtime import claim_session_input, get_session_admission
    _, authority, connection = owner
    original, origin = await parent(owner)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    receipt = await call(connection, 'prompt.submit', session_id=original['session_id'],
                         submission_id=str(uuid.uuid4()), text='Continue the parent work')
    claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=original['session_id'])
    assert claimed['admission_id'] == receipt['admission_id']
    task = await call(connection, 'session.task_create', **task_params(origin))
    still_running = get_session_admission(authority.db, admission_id=receipt['admission_id'])
    assert still_running['status'] == 'started'
    assert still_running['generation'] == claimed['generation']
    assert task['session']['session_id'] != original['session_id']
    assert task['session']['running'] is False
    assert task['session']['pending'] == []


@pytest.mark.asyncio
async def test_unserved_profile_cannot_create_or_resolve_ongoing_chats(owner):
    _, authority, connection = owner
    with authority.db._read_ctx() as conn:
        before = conn.execute('SELECT count(*) FROM sessions').fetchone()[0]
    response = await connection.dispatch({'id': 'wrong-profile', 'method': 'session.ongoing_open',
                                         'params': {'profile': 'unserved-profile', 'source': 'ios'}})
    assert response['error']['data']['reason'] == 'profile_mismatch'
    with authority.db._read_ctx() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == before


@pytest.mark.asyncio
async def test_reply_replay_keeps_an_older_compression_source_after_its_message_is_deleted(owner, monkeypatch):
    from hermes_state_runtime import list_session_admissions
    _, authority, connection = owner
    original, origin = await parent(owner)
    logical = original['session_id']
    continuation = 'compressed-' + uuid.uuid4().hex
    authority.db.publish_compression_child(parent_session_id=logical, child_session_id=continuation,
        source='local', messages=[{'role': 'assistant', 'content': 'Compressed context'}],
        profile_name='default', require_compression_lease=False)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    snapshot = await call(connection, 'session.resume', session_id=logical)
    params = {'session_id': snapshot['session_id'], 'submission_id': str(uuid.uuid4()),
              'text': 'A reply to the earlier message', 'conversation_session_id': logical, 'reply_to': origin}
    first = await call(connection, 'prompt.submit', **params)
    authority.db._write_sql('DELETE FROM messages WHERE id=?', (origin['row_id'],))
    replay = await call(connection, 'prompt.submit', **params)
    assert replay['admission_id'] == first['admission_id']
    rows = list_session_admissions(authority.db, session_id=snapshot['session_id'], pending_only=False)
    assert len(rows) == 1
    saved = rows[0]['payload']['conversation_v1']['snapshot']
    assert saved['session_id'] == logical and saved['message_uid'] == origin['message_uid']
    assert saved['snapshot']['text'] == 'Keep the original topic intact'
    assert authority.db.get_session(continuation) is not None
