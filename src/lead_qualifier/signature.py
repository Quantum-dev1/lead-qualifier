"""HMAC-SHA256 webhook signatures with replay protection.

Senders compute::

    timestamp = str(int(time.time()))
    signature = hmac_sha256(secret, f"{timestamp}.".encode() + raw_body).hexdigest()

and send the headers ``X-Lead-Timestamp: <timestamp>`` and
``X-Lead-Signature: sha256=<signature>``. Several secrets can be active at the
same time (comma-separated) to rotate keys without downtime.
"""

from __future__ import annotations

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Lead-Timestamp"
SIGNATURE_HEADER = "X-Lead-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    """Headers a sender must attach. Also used by the CLI and the generic webhook output."""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: f"sha256={digest}"}


def verify(
    secrets_: list[str],
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Raise ``SignatureError`` unless the body was signed by one of ``secrets_`` recently."""
    if not timestamp or not signature:
        raise SignatureError(f"missing {TIMESTAMP_HEADER} or {SIGNATURE_HEADER} header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp is not an integer") from exc
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        raise SignatureError("timestamp outside the allowed window (possible replay)")
    scheme, _, given = signature.partition("=")
    if scheme != "sha256" or not given:
        raise SignatureError("signature must look like sha256=<hex>")
    for secret in secrets_:
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, given):
            return
    raise SignatureError("signature mismatch")
