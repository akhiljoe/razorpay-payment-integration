"""Tests for TenantConfigRegistry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.tenants import TenantConfigRegistry


def _make_config(tenant_id: str) -> RazorpayConfig:
    return RazorpayConfig(
        tenant_id=tenant_id,
        key_id=f"rzp_test_{tenant_id}",
        key_secret=f"key_secret_{tenant_id}",
        webhook_secret=f"webhook_secret_{tenant_id}",
        db_dsn="postgresql://u:p@localhost:5432/d",
    )


def test_empty_registry_returns_none() -> None:
    registry = TenantConfigRegistry()
    assert registry.get("t_unknown") is None
    assert "t_unknown" not in registry
    assert len(registry) == 0


def test_register_and_get_round_trip() -> None:
    registry = TenantConfigRegistry()
    config = _make_config("t1")
    registry.register(config)
    fetched = registry.get("t1")
    assert fetched is config
    assert "t1" in registry
    assert len(registry) == 1


def test_register_replaces_existing() -> None:
    registry = TenantConfigRegistry()
    first = _make_config("t1")
    second = _make_config("t1")
    registry.register(first)
    registry.register(second)
    assert registry.get("t1") is second
    assert len(registry) == 1


def test_constructor_accepts_initial_configs() -> None:
    configs = [_make_config(f"t{i}") for i in range(3)]
    registry = TenantConfigRegistry(configs)
    assert len(registry) == 3
    for i in range(3):
        fetched = registry.get(f"t{i}")
        assert fetched is not None
        assert fetched.tenant_id == f"t{i}"


def test_concurrent_register_is_safe() -> None:
    registry = TenantConfigRegistry()
    configs = [_make_config(f"t{i:03d}") for i in range(50)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(registry.register, c) for c in configs]
        for future in futures:
            future.result()

    assert len(registry) == 50
    for i in range(50):
        assert registry.get(f"t{i:03d}") is not None


def test_get_unknown_returns_none_after_writes() -> None:
    registry = TenantConfigRegistry([_make_config("t1"), _make_config("t2")])
    assert registry.get("t3") is None
    assert "t3" not in registry
