from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def load_private_key(pem: str) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("Kalshi API key must be an RSA private key")
    return key


def sign_pss(private_key: RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def build_auth_headers(
    key_id: str,
    private_key: RSAPrivateKey,
    method: str,
    path: str,
    *,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Construct the three-header tuple Kalshi expects.

    The signed payload is ``f"{timestamp_ms}{METHOD}{path}"`` where ``path``
    excludes the query string. ``method`` must be uppercase.
    """
    ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    path_no_qs = path.split("?", 1)[0]
    message = f"{ts_ms}{method.upper()}{path_no_qs}"
    signature = sign_pss(private_key, message)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
    }
