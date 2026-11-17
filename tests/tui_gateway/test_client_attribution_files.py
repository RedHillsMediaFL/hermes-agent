"""Explicit attachment admission obeys the same managed root and leaves no partial batch."""
from pathlib import Path
import subprocess
import uuid

import pytest

from hermes_companion import CompanionError
from tui_gateway.client_attribution_files import attachment_descriptors, prepare_attachments


def item(path, kind='file'):
    return {'path': str(path), 'kind': kind, 'name': path.name,
            'mime_type': 'application/pdf' if kind == 'pdf' else 'application/octet-stream',
            'size': path.stat().st_size}


@pytest.fixture()
def files(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    managed = tmp_path / 'managed'
    managed.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_DASHBOARD_FILES_ROOT', str(managed))
    return home, managed


def test_batch_copies_distinct_bytes_without_mutating_source_or_shared_buffers(files):
    home, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'An explicit file')
    images, suffix, directory = prepare_attachments(attachment_descriptors({'attachments': [item(source)]}), str(uuid.uuid4()))
    assert not images and suffix.startswith('\n@file:')
    copied = Path(suffix.strip().removeprefix('@file:'))
    assert copied.parent == directory and copied.read_bytes() == source.read_bytes()
    assert directory.parent == home / 'attachments'
    from agent.context_references import preprocess_context_references
    expanded = preprocess_context_references('Read this' + suffix, cwd=managed, allowed_root=managed, context_length=10000)
    assert not expanded.blocked and 'An explicit file' in expanded.message
    source.write_bytes(b'Changed later')
    assert copied.read_bytes() == b'An explicit file'


def test_managed_root_refusal_removes_all_prepared_files(files):
    home, managed = files
    good, outside = managed / 'good.txt', home.parent / 'outside.txt'
    good.write_bytes(b'good'); outside.write_bytes(b'outside')
    with pytest.raises(CompanionError, match='attachment_not_allowed'):
        prepare_attachments([item(good), item(outside)], str(uuid.uuid4()))
    assert list((home / 'attachments').iterdir()) == []


def test_changed_or_sensitive_source_is_not_accepted(files):
    home, managed = files
    source = managed / 'file.txt'
    source.write_bytes(b'old')
    descriptor = item(source)
    source.write_bytes(b'changed')
    with pytest.raises(CompanionError, match='attachment_changed'):
        prepare_attachments([descriptor], str(uuid.uuid4()))
    secret = managed / '.env'
    secret.write_bytes(b'synthetic')
    with pytest.raises(CompanionError, match='attachment_not_allowed'):
        prepare_attachments([item(secret)], str(uuid.uuid4()))
    assert list((home / 'attachments').iterdir()) == []


def test_pdf_converter_failure_does_not_publish_a_partial_batch(files, monkeypatch):
    home, managed = files
    pdf = managed / 'document.pdf'
    pdf.write_bytes(b'%PDF-1.4\nsynthetic')
    monkeypatch.setattr('tui_gateway.client_attribution_files.shutil.which', lambda _: '/synthetic/pdftoppm')
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('pdftoppm', 90)
    monkeypatch.setattr('tui_gateway.client_attribution_files.subprocess.run', fail)
    with pytest.raises(CompanionError, match='attachment_preparation_failed'):
        prepare_attachments([item(pdf, 'pdf')], str(uuid.uuid4()))
    assert list((home / 'attachments').iterdir()) == []


def test_pdf_success_keeps_pages_in_operation_owned_staging(files, monkeypatch):
    _, managed = files
    pdf = managed / 'document.pdf'
    pdf.write_bytes(b'%PDF-1.4\nsynthetic')
    monkeypatch.setattr('tui_gateway.client_attribution_files.shutil.which', lambda _: '/synthetic/pdftoppm')
    def render(args, **kwargs):
        Path(args[-1] + '-1.png').write_bytes(b'synthetic page')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr('tui_gateway.client_attribution_files.subprocess.run', render)
    images, suffix, directory = prepare_attachments([item(pdf, 'pdf')], str(uuid.uuid4()))
    assert not suffix and len(images) == 1 and Path(images[0]).parent == directory
