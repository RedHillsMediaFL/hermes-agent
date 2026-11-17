"""Closed stored-message references and explicit managed-file manifests."""
from typing import Literal

from pydantic import Field

from .base import Params


class ConversationSourceRef(Params):
    session_id: str = Field(min_length=1, max_length=256)
    row_id: int = Field(gt=0, le=2**53 - 1)
    message_uid: str = Field(min_length=1, max_length=256)


class ConversationAttachment(Params):
    path: str
    kind: Literal['image', 'file', 'pdf']
    name: str
    mime_type: str
    size: int = Field(gt=0, le=25 * 1024 * 1024)
