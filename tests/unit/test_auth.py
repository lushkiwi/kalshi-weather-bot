from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_weather_bot.kalshi.auth import build_auth_headers, load_private_key, sign_pss


def _make_key() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key


def test_load_private_key_roundtrip() -> None:
    pem, key = _make_key()
    loaded = load_private_key(pem)
    assert loaded.key_size == key.key_size


def test_signature_verifies_with_public_key() -> None:
    pem, key = _make_key()
    loaded = load_private_key(pem)
    message = "1700000000000GET/trade-api/v2/markets"
    sig_b64 = sign_pss(loaded, message)
    sig = base64.b64decode(sig_b64)
    key.public_key().verify(
        sig,
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_build_auth_headers_includes_all_three_and_strips_query() -> None:
    pem, key = _make_key()
    loaded = load_private_key(pem)
    headers = build_auth_headers(
        "key-id-123",
        loaded,
        "GET",
        "/trade-api/v2/markets?series_ticker=KXHIGHNY",
        timestamp_ms=1700000000000,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    # Verify signature is over the query-stripped path
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        sig,
        b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
