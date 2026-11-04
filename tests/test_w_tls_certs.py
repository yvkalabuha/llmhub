import hashlib
import ipaddress
import ssl

import pytest
from cryptography import x509

from tls_certs import make_self_signed


@pytest.mark.parametrize("kt", ["p256", "ed25519"])
def test_cert_fields_and_fingerprint(kt: str) -> None:
    b = make_self_signed("10.1.2.3", key_type=kt)  # type: ignore[arg-type]
    cert = x509.load_pem_x509_certificate(b.cert_pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("10.1.2.3")]
    assert b.fingerprint == hashlib.sha256(ssl.PEM_cert_to_DER_cert(b.cert_pem)).hexdigest()
    assert "PRIVATE KEY" in b.key_pem and "PRIVATE KEY" not in b.cert_pem


def test_usable_as_trust_anchor() -> None:
    b = make_self_signed()
    ctx = ssl.create_default_context(cadata=b.cert_pem)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_fresh_keys_each_call() -> None:
    assert make_self_signed().fingerprint != make_self_signed().fingerprint
