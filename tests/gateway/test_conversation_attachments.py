"""Canonical explicit files retain exact bytes and identity through native FIFO retries."""
from pathlib import Path
import os

import pytest

from gateway.session_conversations import attachments
from gateway.session_ingress_media import capture_lease, native_inputs_root, restore_attachments
from hermes_state_runtime import RuntimeStoreError

PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082')


@pytest.fixture
def files(tmp_path, monkeypatch):
    home, managed = tmp_path / '.hermes', tmp_path / 'managed'
    home.mkdir()
    managed.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_DASHBOARD_FILES_ROOT', str(managed))
    return home, managed


def item(path, kind='file', mime='text/plain'):
    return {'path': str(path), 'kind': kind, 'name': path.name, 'mime_type': mime,
            'size': path.stat().st_size}


def admit(manifest, previous=None):
    with capture_lease():
        return attachments.admit_conversation_attachments(manifest, admitted=lambda: previous)


def test_mixed_selection_uses_native_retained_bytes_and_original_names(files):
    _, managed = files
    text, pdf, image = managed / 'notes.txt', managed / 'brief.pdf', managed / 'photo.png'
    text.write_bytes(b'Explicit notes')
    pdf.write_bytes(b'%PDF-1.4\nsynthetic fixture')
    image.write_bytes(PNG)
    manifest = [item(text), item(pdf, 'pdf', 'application/pdf'), item(image, 'image', 'image/png')]
    payload = admit(manifest)
    media = restore_attachments(payload)
    assert media['media_types'] == ['text/plain', 'application/pdf', 'image/png']
    assert [Path(path).name for path in media['media_urls']] == ['notes.txt', 'brief.pdf', 'photo.png']
    assert [Path(path).read_bytes() for path in media['media_urls']] == [text.read_bytes(), pdf.read_bytes(), PNG]
    text.write_bytes(b'Changed after acceptance')
    assert Path(media['media_urls'][0]).read_bytes() == b'Explicit notes'
    assert payload['conversation_attachments_v1']['manifest'] == manifest


@pytest.mark.parametrize('status', ['queued', 'started', 'unknown', 'terminal'])
def test_exact_retry_survives_expired_source_without_reopening_shared_buffers(files, status):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'Already admitted')
    manifest = [item(source)]
    payload = admit(manifest)
    source.unlink()
    if status == 'terminal':
        Path(payload['attachments_v1']['media'][0]['path']).unlink()
    assert admit(manifest, {'payload': payload, 'status': status}) == payload


def test_changed_retry_cannot_rebind_a_receipt_to_another_file_or_bytes(files):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'original')
    manifest = [item(source)]
    payload = admit(manifest)
    row = {'payload': payload, 'status': 'queued'}
    source.write_bytes(b'modified')
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        admit(manifest, row)
    source.unlink()
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        admit([{**manifest[0], 'name': 'different.txt'}], row)


def test_corrupt_retained_bytes_refuse_execution_retry(files):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'original')
    manifest = [item(source)]
    payload = admit(manifest)
    source.unlink()
    Path(payload['attachments_v1']['media'][0]['path']).write_bytes(b'modified')
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        admit(manifest, {'payload': payload, 'status': 'queued'})


@pytest.mark.parametrize('refusal', ['outside', 'sensitive', 'size', 'type', 'symlink', 'hardlink'])
def test_bad_batch_publishes_no_earlier_member(files, refusal):
    home, managed = files
    good = managed / 'good.txt'
    good.write_bytes(b'good')
    source = managed / 'other.txt'
    source.write_bytes(b'other')
    descriptor = item(source)
    def write_file(path, data):
        path.write_bytes(data)
        return item(path)

    def symlink():
        source.unlink()
        source.symlink_to(home.parent / 'missing.txt')
        return descriptor

    def hardlink():
        os.link(source, managed / 'alias.txt')
        return descriptor

    descriptor = {
        'outside': lambda: write_file(home.parent / 'outside.txt', b'outside'),
        'sensitive': lambda: write_file(managed / '.env', b'synthetic credential fixture'),
        'size': lambda: {**descriptor, 'size': descriptor['size'] + 1},
        'type': lambda: {**descriptor, 'kind': 'image', 'mime_type': 'image/png'},
        'symlink': symlink,
        'hardlink': hardlink,
    }[refusal]()
    with pytest.raises(RuntimeStoreError):
        admit([item(good), descriptor])
    assert not list(native_inputs_root().rglob('*'))


def test_source_replacement_during_copy_is_refused(files, monkeypatch):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'original')
    manifest = [item(source)]
    real = attachments._source

    def replace_after_open(descriptor):
        opened = real(descriptor)
        replacement = managed / 'replacement.txt'
        replacement.write_bytes(b'modified')
        os.replace(replacement, source)
        return opened

    monkeypatch.setattr(attachments, '_source', replace_after_open)
    with pytest.raises(RuntimeStoreError, match='attachment_changed'):
        admit(manifest)
    assert not list(native_inputs_root().rglob('*'))


def test_whole_selection_cap_precedes_file_reads(files, monkeypatch):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'1234')
    monkeypatch.setattr('gateway.platforms.base.get_inbound_media_max_bytes', lambda: 7)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        admit([item(source), item(source)])
    assert not list(native_inputs_root().rglob('*'))


@pytest.mark.parametrize('mime', ['image/png', 'application/pdf'])
def test_generic_file_kind_cannot_bypass_declared_media_type_validation(files, mime):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'ordinary text')
    with pytest.raises(RuntimeStoreError, match='invalid_attachment_type'):
        admit([item(source, mime=mime)])
    assert not list(native_inputs_root().rglob('*'))


def test_disk_failure_before_capture_is_retryable_without_retained_bytes(files, monkeypatch):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'ordinary text')

    def unavailable(*args, **kwargs):
        raise OSError('synthetic disk full')

    monkeypatch.setattr(attachments.tempfile, 'TemporaryDirectory', unavailable)
    with pytest.raises(RuntimeStoreError, match='storage_unavailable'):
        admit([item(source)])
    assert not list(native_inputs_root().rglob('*'))


@pytest.mark.parametrize('change', [
    {'name': '../escape.txt'}, {'name': '..\\escape.txt'}, {'size': True},
    {'path': 'bad\x00path'}, {'mime_type': 'bad mime'}, {'kind': 'unknown'},
])
def test_invalid_manifest_cannot_create_a_snapshot(files, change):
    _, managed = files
    source = managed / 'notes.txt'
    source.write_bytes(b'fixture')
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        admit([{**item(source), **change}])
    assert not list(native_inputs_root().rglob('*'))
