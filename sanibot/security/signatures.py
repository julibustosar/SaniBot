"""Verificación de firmas HMAC de los webhooks de Sanitas y de WhatsApp."""

import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, secret: str, header_value: str | None) -> bool:
    if not secret or not header_value:
        return False
    return hmac.compare_digest(sign(body, secret), header_value.strip())
