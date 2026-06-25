from __future__ import annotations

from io import BytesIO
from django.core.files.base import ContentFile
import qrcode


def make_qr_content_file(data: str, filename: str) -> ContentFile:
    img = qrcode.make(data or 'empty')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    return ContentFile(buffer.getvalue(), name=filename)
