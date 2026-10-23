"""Kalshi API-key request signing (only used for demo/live order placement; scanning is unauthenticated).

Message = timestamp_ms + METHOD + path (including ``/trade-api/v2``, excluding the query string). RSA keys sign with
RSA-PSS/SHA-256 (MGF1-SHA-256, salt = digest length); Ed25519 keys sign the message directly. Signature is base64.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit


class SigningUnavailable(RuntimeError):
    pass


def signing_message(timestamp_ms: int, method: str, url_or_path: str) -> bytes:
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
    return f"{timestamp_ms}{method.upper()}{path}".encode()


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes, clock: Callable[[], float] = time.time):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
            raise SigningUnavailable("install the 'exec' extra (cryptography) to sign Kalshi requests") from exc
        self.key_id = key_id
        self.clock = clock
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if isinstance(key, rsa.RSAPrivateKey):

            def sign(msg: bytes) -> bytes:
                return key.sign(
                    msg,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                    hashes.SHA256(),
                )

            self.algorithm = "RSA-PSS-SHA256"
        elif isinstance(key, ed25519.Ed25519PrivateKey):

            def sign(msg: bytes) -> bytes:
                return key.sign(msg)

            self.algorithm = "Ed25519"
        else:
            raise SigningUnavailable("Kalshi keys must be RSA or Ed25519")
        self._sign = sign

    @classmethod
    def from_file(cls, key_id: str, path: str | Path) -> KalshiSigner:
        p = Path(path).expanduser()
        if p.stat().st_mode & 0o077:
            raise SigningUnavailable(f"{p} must not be readable by group/other (chmod 600)")
        return cls(key_id, p.read_bytes())

    def headers(self, method: str, url: str) -> dict[str, str]:
        ts = int(self.clock() * 1000)
        signature = base64.b64encode(self._sign(signing_message(ts, method, url))).decode()
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
