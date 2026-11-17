"""Durable native-owner replies and independent tasks (session-conversations-v1)."""
from typing import Literal

from pydantic import Field

from .base import Result
from .common import ProfileParams
from .canonical_sessions import CanonicalSnapshot
from .conversation_values import ConversationSourceRef
from .registry import canonical_method


class ConversationSourceSnapshot(Result):
    text: str
    timestamp: float
    truncated: bool


class ConversationSourceAnchor(Result):
    profile: str
    session_id: str
    row_id: int
    message_uid: str
    role: Literal['user', 'assistant']
    snapshot: ConversationSourceSnapshot


class ConversationTask(Result):
    thread_id: str
    profile: str
    title: str
    parent_session_id: str
    origin: ConversationSourceAnchor
    created_at: float


class ConversationTaskParams(ProfileParams):
    operation_id: str
    origin: ConversationSourceRef
    title: str = Field(min_length=1, max_length=120)
    source: Literal['ios', 'desktop'] = 'ios'


class ConversationTaskResult(Result):
    thread: ConversationTask
    session: CanonicalSnapshot
    resolved_session_id: str
    replayed: bool


class ConversationOngoingParams(ProfileParams):
    source: Literal['ios', 'desktop'] = 'ios'


class ConversationOngoingResult(Result):
    profile: str
    stored_session_id: str
    resolved_session_id: str
    session: CanonicalSnapshot
    created: bool


canonical_method('session.task_create', params=ConversationTaskParams, result=ConversationTaskResult,
                 doc='Create an independent native session from an exact stored source message; durable retry identity.')
canonical_method('session.ongoing_open', params=ConversationOngoingParams, result=ConversationOngoingResult,
                 doc='Open the durable per-profile ongoing conversation through its native owner.')
