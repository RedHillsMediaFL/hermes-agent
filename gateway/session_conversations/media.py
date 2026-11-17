"""Materialize retained native media without routing documents through image encoders."""
from agent.image_routing import build_native_content_parts
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.session_ingress_media import restore_attachments


def managed_input(text, payload):
    """Model content and visible user content from the same verified retained bytes."""
    media = restore_attachments(payload)
    paths, types = media.get('media_urls', []), media.get('media_types', [])
    event = MessageEvent(text=text, source=None, media_urls=paths, media_types=types)
    model_text = GatewayInboundMixin._prepend_inbound_document_notes(event, text)
    images = [path for path, mime in zip(paths, types) if mime.startswith('image/')]
    if not images:
        return model_text, text
    model, skipped = build_native_content_parts(model_text, images)
    visible, visible_skipped = build_native_content_parts(text, images)
    if skipped or visible_skipped:
        raise ValueError('managed_attachment_unavailable')
    return model, visible
