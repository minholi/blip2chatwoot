from __future__ import annotations

import hashlib
import hmac
import time


def verify_chatwoot_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    raw_body: bytes,
    max_age_seconds: int = 300,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp_int) > max_age_seconds:
        return False
    expected = (
        "sha256="
        + hmac.new(
            secret.encode(),
            f"{timestamp}.".encode() + raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)
