"""Same-named canonical sessions remain distinguishable in snapshots, live events and replay."""
from types import SimpleNamespace
import asyncio
import threading

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


class Transport:
    def __init__(self):
        self.frames = []
        self.received = threading.Event()

    def write(self, frame):
        self.frames.append(frame)
        self.received.set()
        return True


@pytest.mark.asyncio
async def test_same_session_ids_are_profile_bound_in_live_and_replayed_frames(tmp_path):
    databases, connections = [], []
    transport = Transport()
    try:
        for profile in ('first', 'second'):
            db = SessionDB(db_path=tmp_path / profile / 'state.db')
            databases.append(db)
            db.create_session('shared', source='test')
            authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=profile,
                instance_id='owner', db=db, epoch=begin_runtime_epoch(db, instance_id='owner'))
            authority.sessions['shared'] = LiveSession(None, 'route')
            connection = AuthorityConnection(authority, transport, {'user_id': 'viewer'})
            connections.append(connection)
            reply = await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 'shared'}})
            assert reply['result']['info']['profile_id'] == profile
            events = authority.sessions['shared'].event_stream
            epoch, mark = events.watermark()
            transport.received.clear()
            events.publish('shared', {'text': profile}, event_type='message.delta')
            assert await asyncio.to_thread(transport.received.wait, 5)
            live = transport.frames[-1]['params']
            assert live['profile_id'] == profile and live['session_id'] == 'shared'
            replay = events.since(epoch, mark)
            assert replay['events'][0]['profile_id'] == profile
            assert replay['events'][0]['payload']['text'] == profile
        assert {frame['params']['profile_id'] for frame in transport.frames} == {'first', 'second'}
    finally:
        for connection in connections:
            await connection.close()
        for db in databases:
            db.close()
