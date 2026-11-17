"""Retained native PDFs/documents reach managed turns as documents, with image bytes kept intact."""
import uuid

from gateway.session_conversations.media import managed_input
from gateway.session_conversations.turn import conversation_turn_scope, worker_input
from gateway.session_ingress_media import capture_lease, capture_native_media
from tests.gateway.test_conversation_attachments import PNG


def test_mixed_native_media_never_encodes_pdf_or_text_as_an_image(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    paths = []
    for name, data in [('picture.png', PNG), ('report.pdf', b'%PDF-1.4 fixture'), ('notes.txt', b'Visible file data')]:
        path = tmp_path / name
        path.write_bytes(data)
        paths.append(path)
    with capture_lease():
        retained = capture_native_media(paths)
    payload = {'attachments_v1': {'media': retained, 'media_types': ['image/png', 'application/pdf', 'text/plain']}}
    model, visible = managed_input('Review these files', payload)
    image_parts = [part for part in model if part['type'] == 'image_url']
    assert len(image_parts) == 1 and image_parts[0]['image_url']['url'].startswith('data:image/png;base64,')
    rendered = '\n'.join(part.get('text', '') for part in model)
    assert retained[1]['path'] in rendered and retained[2]['path'] in rendered
    assert 'report.pdf' in rendered and 'notes.txt' in rendered
    assert 'report.pdf' not in str(visible) and 'notes.txt' not in str(visible)
    assert len([part for part in visible if part['type'] == 'image_url']) == 1
    anchor = {'profile': 'default', 'session_id': 'source', 'row_id': 1, 'message_uid': 'message', 'role': 'assistant',
              'snapshot': {'text': 'Exact replied message', 'timestamp': 1.0, 'truncated': False}}
    envelope = {'reference': {'session_id': 'source', 'row_id': 1, 'message_uid': 'message'},
                'operation_id': str(uuid.uuid4()), 'snapshot': anchor}
    with conversation_turn_scope('admission', envelope):
        content, kwargs = worker_input(model, visible=visible)
    assert content[0]['type'] == 'text' and 'Exact replied message' in content[0]['text']
    assert len([part for part in content if part['type'] == 'image_url']) == 1
    assert kwargs['persist_user_message'] == visible
    assert 'agent_reply_to' in kwargs['persist_user_display_metadata']


def test_document_only_turn_does_not_invoke_image_builder(tmp_path, monkeypatch):
    import gateway.session_conversations.media as materializer
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'report.pdf'
    path.write_bytes(b'%PDF-1.4 fixture')
    with capture_lease():
        retained = capture_native_media([path])
    def no_image(*args, **kwargs):
        raise AssertionError('A PDF must never be sent to the image encoder')
    monkeypatch.setattr(materializer, 'build_native_content_parts', no_image)
    model, visible = managed_input('Page three?', {'attachments_v1': {'media': retained, 'media_types': ['application/pdf']}})
    assert retained[0]['path'] in model and 'Page three?' in model
    assert visible == 'Page three?'
