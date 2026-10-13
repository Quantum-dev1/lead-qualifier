import pytest

from lead_qualifier.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, sign, verify

BODY = b'{"message": "hi"}'


def test_round_trip() -> None:
    headers = sign("s3cret", BODY, timestamp=1_700_000_000)
    verify(["s3cret"], BODY, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], now=1_700_000_010)


def test_key_rotation_accepts_any_active_secret() -> None:
    headers = sign("new", BODY, timestamp=100)
    verify(["old", "new"], BODY, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], now=100)


@pytest.mark.parametrize(
    ("secret", "body", "now", "match"),
    [
        ("wrong", BODY, 1_700_000_000, "mismatch"),
        ("s3cret", BODY + b" ", 1_700_000_000, "mismatch"),
        ("s3cret", BODY, 1_700_001_000, "replay"),
    ],
)
def test_rejections(secret: str, body: bytes, now: float, match: str) -> None:
    headers = sign(secret, BODY, timestamp=1_700_000_000)
    with pytest.raises(SignatureError, match=match):
        verify(["s3cret"], body, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], now=now)


def test_missing_or_malformed_headers() -> None:
    with pytest.raises(SignatureError, match="missing"):
        verify(["s"], BODY, None, None)
    with pytest.raises(SignatureError, match="sha256="):
        verify(["s"], BODY, "1", "md5=abc", now=1)
