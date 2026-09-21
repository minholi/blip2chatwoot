import hashlib
import hmac
import time

from app.security import verify_chatwoot_signature


def test_chatwoot_signature_uses_timestamp_and_raw_body() -> None:
    body = b'{"event":"message_created"}'
    timestamp = str(int(time.time()))
    signature = (
        "sha256="
        + hmac.new(
            b"secret",
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
    )

    assert verify_chatwoot_signature(
        secret="secret",
        timestamp=timestamp,
        signature=signature,
        raw_body=body,
    )
    assert not verify_chatwoot_signature(
        secret="secret",
        timestamp=timestamp,
        signature=signature,
        raw_body=b"{}",
    )
