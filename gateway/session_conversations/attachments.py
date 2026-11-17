"""Managed files captured by the canonical owner's ordinary media admission.

The manifest binds retries to the caller's exact file selection; retained bytes use
the existing attachment leases, execution restoration and retirement machinery.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from fastapi import HTTPException

from gateway.session_ingress_media import (
    capture_native_media, restore_native_media, sniff_image_mime, validate_media_batch_size,
)
from hermes_state_runtime import ADMITTED_REQUEST_CONFLICT, RuntimeStoreError

_MAX_FILES = 16
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MIME = re.compile(r'[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+')
_IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/gif', 'image/webp'})


@dataclass(frozen=True)
class ConversationAttachment:
    path: str
    kind: str
    name: str
    mime_type: str
    size: int

    def wire(self):
        return {'path': self.path, 'kind': self.kind, 'name': self.name,
                'mime_type': self.mime_type, 'size': self.size}


def _invalid():
    return RuntimeStoreError('invalid_params', field='conversation_attachments')


def _text(value, limit):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return False
    try:
        return len(value.encode('utf-8')) <= limit
    except UnicodeError:
        return False


def _attachment(value):
    if not isinstance(value, dict) or set(value) != {'path', 'kind', 'name', 'mime_type', 'size'}:
        raise _invalid()
    if (not _text(value['path'], 4096) or not _text(value['name'], 255)
            or not _text(value['mime_type'], 255) or _MIME.fullmatch(value['mime_type']) is None):
        raise _invalid()
    if (value['kind'] not in ('image', 'file', 'pdf') or type(value['size']) is not int
            or not 0 < value['size'] <= _MAX_FILE_BYTES):
        raise _invalid()
    name = value['name']
    if name in ('.', '..') or '/' in name or '\\' in name:
        raise _invalid()
    mime = value['mime_type']
    if ((value['kind'] == 'image' and mime not in _IMAGE_MIMES)
            or (value['kind'] == 'pdf' and mime != 'application/pdf')):
        raise _invalid()
    return ConversationAttachment(**value)


def attachment_descriptors(items):
    """Parse the entire selection before touching any file or retaining any bytes."""
    if items is None:
        return ()
    if not isinstance(items, list) or len(items) > _MAX_FILES:
        raise _invalid()
    descriptors = tuple(_attachment(item) for item in items)
    validate_media_batch_size(item.size for item in descriptors)
    return descriptors


def _managed(path):
    # AuthorityConnection has already authorized the actor and bound the owning
    # profile. Reuse the same managed-root and sensitive-file policy as HTTP.
    from hermes_cli.web_routers.files import _managed_readable_file
    try:
        return _managed_readable_file(None, path, create_root=False)[1]
    except HTTPException as exc:
        reason = 'attachment_not_found' if exc.status_code == 404 else 'attachment_not_allowed'
        raise RuntimeStoreError(reason, field='conversation_attachments') from None


def _identity(saved):
    return (saved.st_dev, saved.st_ino, saved.st_size, saved.st_mtime_ns, saved.st_ctime_ns)


def _source(item):
    source = _managed(item.path)
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        handle = os.fdopen(fd, 'rb')
        saved = os.fstat(handle.fileno())
        if not stat.S_ISREG(saved.st_mode) or saved.st_nlink != 1 or saved.st_size != item.size:
            handle.close()
            raise RuntimeStoreError('attachment_changed')
        return handle, saved
    except OSError as exc:
        raise RuntimeStoreError('attachment_changed') from exc


def _unchanged(item, handle, before):
    try:
        after = os.fstat(handle.fileno())
        current = _managed(item.path).stat(follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise RuntimeStoreError('attachment_changed')
    except OSError as exc:
        raise RuntimeStoreError('attachment_changed') from exc


def _verify_type(item, prefix):
    if item.mime_type in _IMAGE_MIMES and sniff_image_mime(prefix) != item.mime_type:
        raise RuntimeStoreError('invalid_attachment_type')
    if item.mime_type == 'application/pdf' and not prefix.lstrip().startswith(b'%PDF-'):
        raise RuntimeStoreError('invalid_attachment_type')


def _snapshot(item, destination):
    handle, before = _source(item)
    with handle:
        prefix = handle.read(1024)
        _verify_type(item, prefix)
        remaining = item.size
        handle.seek(0)
        with destination.open('xb') as output:
            os.chmod(destination, 0o600)
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeStoreError('attachment_changed')
                output.write(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise RuntimeStoreError('attachment_changed')
        _unchanged(item, handle, before)


def _retry(items, row):
    payload = row['payload']
    manifest = payload.get('conversation_attachments_v1', {}).get('manifest')
    if manifest != [item.wire() for item in items]:
        raise RuntimeStoreError('admission_conflict', **ADMITTED_REQUEST_CONFLICT)
    retained = payload.get('attachments_v1', {})
    references = retained.get('media', [])
    if len(references) != len(items) or retained.get('media_types') != [item.mime_type for item in items]:
        raise RuntimeStoreError('storage_unavailable')
    for item, reference in zip(items, references):
        try:
            handle, before = _source(item)
        except RuntimeStoreError as exc:
            if exc.reason == 'attachment_not_found':
                continue
            raise
        with handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            _unchanged(item, handle, before)
        if digest != reference['sha256'] or item.size != reference['size']:
            raise RuntimeStoreError('admission_conflict', **ADMITTED_REQUEST_CONFLICT)
    if row['status'] != 'terminal':
        restore_native_media(references)
    return {'attachments_v1': retained, 'conversation_attachments_v1': {'manifest': manifest}}


def admit_conversation_attachments(items, *, admitted=None):
    """Return immutable payload fields inside the owner's capture lease/profile scope.

    Run off-loop: bounded file copies and hashing must not block other profiles.
    The caller commits these fields with the ordinary FIFO receipt transaction.
    """
    descriptors = attachment_descriptors(items)
    if not descriptors:
        return {}
    row = admitted() if admitted is not None else None
    if row is not None:
        return _retry(descriptors, row)
    try:
        with tempfile.TemporaryDirectory(prefix='hermes-conversation-input-') as temporary:
            paths = []
            for index, item in enumerate(descriptors):
                directory = Path(temporary) / str(index)
                directory.mkdir(mode=0o700)
                destination = directory / item.name
                _snapshot(item, destination)
                paths.append(destination)
            references = capture_native_media(paths)
    except OSError as exc:
        raise RuntimeStoreError('storage_unavailable') from exc
    return {'attachments_v1': {'media': references, 'media_types': [item.mime_type for item in descriptors]},
            'conversation_attachments_v1': {'manifest': [item.wire() for item in descriptors]}}
