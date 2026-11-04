"""Test-only self-signed certificate generation via `cryptography` (no openssl binary)."""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
from dataclasses import dataclass, field
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class CertBundle:
    cert_pem: str
    key_pem: str = field(repr=False)
    der: bytes
    fingerprint: str  # lower-case hex sha256 of the DER certificate


def make_self_signed(
    ip: str = "127.0.0.1",
    *,
    key_type: Literal["p256", "ed25519"] = "p256",
    days: int = 1,
    not_before: dt.datetime | None = None,
) -> CertBundle:
    """Self-signed leaf certificate with an IP SAN (usable as its own trust anchor)."""
    key: ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey
    if key_type == "p256":
        key = ec.generate_private_key(ec.SECP256R1())
        algo: hashes.SHA256 | None = hashes.SHA256()
    else:
        key = ed25519.Ed25519PrivateKey.generate()
        algo = None
    start = not_before or dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, algo)
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    return CertBundle(
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        der=der,
        fingerprint=hashlib.sha256(der).hexdigest(),
    )
