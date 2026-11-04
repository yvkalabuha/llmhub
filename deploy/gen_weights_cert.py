#!/usr/bin/env python3
"""Self-signed certificate for the weights nginx (T-370 D3). Usage: gen_weights_cert.py IP OUTDIR

EC P-256, SAN IP:<ip>, CA:FALSE, digitalSignature, serverAuth, 3650 days. Writes
<outdir>/weights-cert.pem (public, goes to `ca_pem_file`) and weights-key.pem (0600).
Prints the lower-case hex sha256 of the DER certificate. Uses `cryptography`, not openssl.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DAYS = 3650


def generate(ip: str, outdir: Path, now: dt.datetime | None = None) -> str:
    addr = ipaddress.ip_address(ip)
    key = ec.generate_private_key(ec.SECP256R1())
    start = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(minutes=5)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(addr))])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(addr)]), False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    outdir.mkdir(parents=True, exist_ok=True)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(outdir / "weights-key.pem", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)
    os.chmod(outdir / "weights-key.pem", 0o600)
    (outdir / "weights-cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: gen_weights_cert.py IP OUTDIR", file=sys.stderr)
        return 2
    try:
        print(generate(args[0], Path(args[1])))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
