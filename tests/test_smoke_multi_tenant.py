from scripts.smoke_multi_tenant import _should_pin_enclave_cert


def test_smoke_uses_system_ca_for_friendly_url_when_pinning_url_differs():
    assert not _should_pin_enclave_cert(
        "https://hivemind.teleport.computer",
        "https://abc-8100s.dstack-pha-prod9.phala.network",
    )


def test_smoke_pins_enclave_cert_for_raw_pinning_url():
    assert _should_pin_enclave_cert(
        "https://abc-8100s.dstack-pha-prod9.phala.network",
        "https://abc-8100s.dstack-pha-prod9.phala.network",
    )

