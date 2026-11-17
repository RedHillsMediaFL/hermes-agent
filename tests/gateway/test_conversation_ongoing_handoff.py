"""The app's ongoing entry follows the native Bot Chat through actual messaging handoff."""
import pytest

from gateway.config import Platform
from tests.gateway.test_authority_cold_route_recovery import _source
from tests.gateway.test_conversation_owner_rpc import call, parent, owner as conversation_owner
from tests.gateway.test_handoff_local_receipt import _handoff

owner = conversation_owner


@pytest.mark.asyncio
async def test_ongoing_open_resumes_handed_off_bot_chat_without_a_local_receipt(owner):
    _, authority, connection = owner
    original, _ = await parent(owner, title='Bot Chat')
    first = await call(connection, 'session.ongoing_open', source='ios')
    assert first['stored_session_id'] == original['stored_session_id']
    route = await _handoff(authority.runner, authority, first['stored_session_id'], _source())
    opened = await call(connection, 'session.ongoing_open', source='ios')
    sid = opened['stored_session_id']
    assert sid == first['stored_session_id']
    assert authority.sessions[sid].route == route
    assert authority.sessions[sid].source.platform == Platform.TELEGRAM
    with authority.db._read_ctx() as conn:
        assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', ('gateway.local_policy.v1:' + sid,)).fetchone() is None
    assert opened['session']['info']['profile_id'] == authority.profile_id
