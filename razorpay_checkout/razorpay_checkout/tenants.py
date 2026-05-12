"""Tenant configuration registry.

Multi-tenant webhook routing requires the producer to look up the correct
`RazorpayConfig` from the URL path's `tenant_id` before signature verification.
Lookup must be O(1) and safe under concurrent registration during application
startup (e.g., async startup hooks fanning out one task per tenant).

CPython dict lookups are atomic, so `get` does not need a lock. Writes do.
"""

from __future__ import annotations

from threading import Lock

from razorpay_checkout.config import RazorpayConfig


class TenantConfigRegistry:
    """Process-wide registry mapping `tenant_id` to `RazorpayConfig`."""

    def __init__(self, configs: list[RazorpayConfig] | None = None) -> None:
        self._configs: dict[str, RazorpayConfig] = {}
        self._lock = Lock()
        if configs:
            for config in configs:
                self.register(config)

    def register(self, config: RazorpayConfig) -> None:
        """Insert or replace a tenant config. Thread-safe."""
        with self._lock:
            self._configs[config.tenant_id] = config

    def get(self, tenant_id: str) -> RazorpayConfig | None:
        """Return the config for `tenant_id`, or `None` if unknown.

        Returning `None` for unknown tenants is the contract. The webhook route
        converts this to a `400 Bad Request` (per DESIGN Section 2.1) to avoid
        leaking tenant existence on the publicly enumerable webhook URL.
        """
        return self._configs.get(tenant_id)

    def __contains__(self, tenant_id: str) -> bool:
        return tenant_id in self._configs

    def __len__(self) -> int:
        return len(self._configs)
