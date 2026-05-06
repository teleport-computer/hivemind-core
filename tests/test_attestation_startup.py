import time

from hivemind import attestation
from hivemind import server
from hivemind import tls


def _reset_attestation_state():
    attestation._state.update(
        {
            "ready": False,
            "reason": None,
            "attestation": None,
            "run_signer_priv": None,
            "run_signer_pub": None,
            "disabled": False,
        }
    )


def test_bounded_attestation_bootstrap_disables_on_timeout(monkeypatch):
    _reset_attestation_state()

    def slow_bootstrap():
        time.sleep(2)

    monkeypatch.setattr(attestation, "bootstrap", slow_bootstrap)

    start = time.time()
    ready = server._bootstrap_attestation_bounded(1)

    assert ready is False
    assert time.time() - start < 1.5
    bundle = attestation.get_bundle()
    assert bundle["ready"] is False
    assert "exceeded 1s" in bundle["reason"]


def test_ephemeral_tls_cert_is_usable():
    bundle = tls.generate_ephemeral_tls_cert_and_key()

    assert bundle["cert_pem"].startswith(b"-----BEGIN CERTIFICATE-----")
    assert bundle["key_pem"].startswith(b"-----BEGIN PRIVATE KEY-----")
    assert len(bundle["fingerprint"]) == 32
