"""Operation-owned attachment copies, admitted only through the managed-file policy."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from fastapi import HTTPException

from hermes_companion import CompanionError


def attachment_descriptors(params):
    items = params.get('attachments') or []
    if not isinstance(items, list) or len(items) > 16:
        raise CompanionError('invalid_attachments', 400)
    for item in items:
        if not isinstance(item, dict) or set(item) != {'path', 'kind', 'name', 'mime_type', 'size'}:
            raise CompanionError('invalid_attachments', 400)
        if (item['kind'] not in ('image', 'file', 'pdf') or type(item['size']) is not int
                or not 0 < item['size'] <= 25 * 1024 * 1024):
            raise CompanionError('invalid_attachments', 400)
        if any(not isinstance(item[key], str) or not item[key] or '\x00' in item[key]
               for key in ('path', 'name', 'mime_type')):
            raise CompanionError('invalid_attachments', 400)
    return [dict(item) for item in items]


def _managed(path):
    # The stock policy is env/home scoped, not HTTP-identity scoped; the authenticated WS owner
    # has already been checked. None does not fabricate a browser request or identity.
    from hermes_cli.web_routers.files import _managed_readable_file
    try:
        return _managed_readable_file(None, path, create_root=False)[1]
    except HTTPException as exc:
        code = 'attachment_not_allowed' if exc.status_code == 403 else 'attachment_not_found'
        raise CompanionError(code, exc.status_code) from None


def _copy(item, destination):
    source = _managed(item['path'])
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != item['size']:
            raise CompanionError('attachment_changed')
        with destination.open('xb') as outgoing:
            os.chmod(destination, 0o600)
            remaining = item['size']
            while remaining:
                chunk = incoming.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise CompanionError('attachment_changed')
                outgoing.write(chunk)
                remaining -= len(chunk)
            if incoming.read(1):
                raise CompanionError('attachment_changed')
        after = os.fstat(incoming.fileno())
        current = _managed(item['path']).stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise CompanionError('attachment_changed')


def prepare_attachments(attachments, op):
    if not attachments:
        return [], '', None
    from hermes_constants import get_hermes_home, mkdir_under_hermes_home
    # The stock attachments root participates in context-reference allowlisting and remote
    # backend mounts. Browser upload staging alone does not provide either guarantee.
    root = Path(get_hermes_home()) / 'attachments'
    mkdir_under_hermes_home(root)
    directory = Path(tempfile.mkdtemp(prefix='agent-' + op + '-', dir=root))
    images, refs = [], []
    try:
        for index, item in enumerate(attachments):
            suffix = Path(item['name']).suffix[:12]
            destination = directory / f'{index}{suffix}'
            _copy(item, destination)
            if item['kind'] == 'image':
                images.append(str(destination))
            elif item['kind'] == 'pdf':
                images.extend(_pdf_pages(destination, directory, index))
            else:
                from tui_gateway.prompt_attachments import _format_ref_value
                refs.append('@file:' + _format_ref_value(str(destination)))
        return images, ('\n' + '\n'.join(refs)) if refs else '', directory
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(directory)
        raise CompanionError('attachment_preparation_failed', 422) from exc
    except BaseException:
        shutil.rmtree(directory)
        raise


def _pdf_pages(source, directory, index):
    converter = shutil.which('pdftoppm')
    if converter is None:
        raise CompanionError('pdf_support_unavailable', 422)
    with source.open('rb') as handle:
        if not handle.read(1024).lstrip().startswith(b'%PDF-'):
            raise CompanionError('invalid_pdf', 422)
    result = subprocess.run([converter, '-png', '-scale-to', '1600', '-f', '1', '-l', '25',
        str(source), str(directory / f'pdf-{index}')], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
    pages = sorted(directory.glob(f'pdf-{index}-*.png'))
    if result.returncode or not pages:
        raise CompanionError('pdf_preparation_failed', 422)
    return [str(page) for page in pages]
