from __future__ import annotations

import threading
from collections import OrderedDict
from unittest.mock import MagicMock

from hivemind.config import Settings
from hivemind.tenants import TenantRegistry


def test_tenant_hive_construction_is_singleflight(monkeypatch):
    registry = object.__new__(TenantRegistry)
    registry.settings = Settings(autoload_default_agents=False)
    registry._lock = threading.RLock()
    registry._cache = OrderedDict()
    registry._cache_inflight = {}
    registry._cache_max = 4
    registry.sealer = object()

    start_count = 0
    constructing = threading.Event()
    release = threading.Event()

    class FakeHivemind:
        def __init__(self):
            self.db = MagicMock()

    def fake_hivemind(*args, **kwargs):
        nonlocal start_count
        start_count += 1
        constructing.set()
        assert release.wait(timeout=2)
        return FakeHivemind()

    monkeypatch.setattr("hivemind.tenants.Hivemind", fake_hivemind)

    results = []
    errors = []

    def worker():
        try:
            results.append(registry._get_or_create_hive("t_demo", "tenant_t_demo"))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert constructing.wait(timeout=2)
    t2.start()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert errors == []
    assert start_count == 1
    assert len(results) == 2
    assert results[0] is results[1]
