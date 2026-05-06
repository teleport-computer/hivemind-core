"""dstack-KMS-bound TLS cert derivation for hivemind-core.

Feedling's first attestation binding, ported. The cert is derived
deterministically from dstack-KMS so its ``sha256(cert.DER)``
fingerprint is stable across restarts and identical for every replica
that gets the same KMS-released key.

The bundle's ``report_data`` (v2) embeds ``sha256(cert_der)`` so the
CLI can:

1. Fetch the attestation bundle over the enclave-terminated TLS
   connection (gateway passes through on ``-<port>s`` SNI suffix).
2. Verify the TDX quote via DCAP.
3. Extract the fingerprint from the verified ``report_data``.
4. Compare with the TLS session's actual peer cert fingerprint.

Mismatch → abort: a MITM either saw a different cert or forged the
bundle.

Ported from feedling-mcp-v1/backend/dstack_tls.py (same RFC-6979
deterministic ECDSA, same 10-year validity, same key-path discipline).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HIVEMIND_TLS_KEY_PATH = "hivemind-tls-v1"


def derive_tls_cert_and_key(dstack: Any) -> dict[str, Any]:
    """Return ``{cert_pem, key_pem, cert_der, fingerprint}``.

    ``dstack`` is a ``DstackClient`` instance; the caller wires it up
    against ``/var/run/dstack.sock`` (or the simulator). Output is
    byte-identical within a deploy because the cert body is fully
    deterministic and the ECDSA signature uses RFC-6979.
    """
    seed_resp = dstack.get_key(HIVEMIND_TLS_KEY_PATH, "")
    seed = (
        bytes.fromhex(seed_resp.key)
        if isinstance(seed_resp.key, str)
        else seed_resp.key
    )
    scalar_bytes = hashlib.sha256(
        HIVEMIND_TLS_KEY_PATH.encode() + b"|" + seed[:32]
    ).digest()
    scalar = int.from_bytes(scalar_bytes, "big")
    priv_key = ec.derive_private_key(scalar, ec.SECP256R1())

    subject_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "hivemind-enclave"),
            x509.NameAttribute(
                NameOID.ORGANIZATION_NAME, "Hivemind (TDX CVM)"
            ),
        ]
    )
    not_before = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    not_after = _dt.datetime(2036, 1, 1, tzinfo=_dt.timezone.utc)
    pub_der = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    serial = (
        int.from_bytes(hashlib.sha256(pub_der).digest()[:8], "big") | 1
    )

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(subject_name)
        .public_key(priv_key.public_key())
        .serial_number(serial)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("hivemind-enclave"),
                    x509.DNSName("*.dstack-pha-prod9.phala.network"),
                    x509.DNSName("*.dstack-pha-prod5.phala.network"),
                    x509.DNSName("*.app.phala.network"),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )
    cert = builder.sign(
        private_key=priv_key,
        algorithm=_hashes.SHA256(),
        ecdsa_deterministic=True,
    )

    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "cert_der": cert_der,
        "fingerprint": hashlib.sha256(cert_der).digest(),
    }


def generate_ephemeral_tls_cert_and_key() -> dict[str, Any]:
    """Return a temporary self-signed TLS cert for degraded startup.

    This is intentionally not attested and must never be advertised as
    quote-bound TLS material. It only keeps the TCP-passthrough health/API
    surface reachable when dstack KMS/quote bootstrap is unavailable.
    """
    priv_key = ec.generate_private_key(ec.SECP256R1())
    subject_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "hivemind-degraded"),
            x509.NameAttribute(
                NameOID.ORGANIZATION_NAME, "Hivemind (degraded TLS)"
            ),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    serial = x509.random_serial_number()
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(subject_name)
        .public_key(priv_key.public_key())
        .serial_number(serial)
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("hivemind-degraded"),
                    x509.DNSName("*.dstack-pha-prod9.phala.network"),
                    x509.DNSName("*.dstack-pha-prod5.phala.network"),
                    x509.DNSName("*.app.phala.network"),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )
    cert = builder.sign(private_key=priv_key, algorithm=_hashes.SHA256())
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "cert_der": cert_der,
        "fingerprint": hashlib.sha256(cert_der).digest(),
    }


__all__ = [
    "HIVEMIND_TLS_KEY_PATH",
    "derive_tls_cert_and_key",
    "generate_ephemeral_tls_cert_and_key",
]
