---
description: Enforce tenant_id scoping on every DB query and on webhook entry points.
globs:
  - "razorpay_checkout/razorpay_checkout/orders.py"
  - "razorpay_checkout/razorpay_checkout/webhooks.py"
  - "razorpay_checkout/razorpay_checkout/router.py"
  - "razorpay_checkout/razorpay_checkout/migrations/**"
---

# Tenant Isolation Rule

Every query against orders, payments, webhook_events, and order_events must include a tenant_id filter. This is enforced by OrderService and WebhookProcessor, never by the caller.

## Source of `tenant_id`

`tenant_id` must be extracted from the URL path parameter per Fix V3-2 in `designs/DESIGN_v3.md`. It must never be taken from the request body, query string, or header.

## Enforcement Checklist

1. Every `SELECT`, `UPDATE`, `INSERT`, and `DELETE` against `tenants`, `orders`, `payments`, `webhook_events`, and `order_events` must filter or set `tenant_id` explicitly.
2. The webhook router (`POST /payments/webhook/{tenant_id}`) must resolve `RazorpayConfig` from `TenantConfigRegistry` using the path parameter before any signature verification or DB lookup.
3. The order creation route, callback verify route, reconcile route, and admin routes must derive `tenant_id` from the injected `RazorpayConfig`, not from request fields.
4. Reject any code path that accepts a `tenant_id` value from a request body, query string, or header.
5. Cross-tenant joins are forbidden. The logical FK from `orders.tenant_id` to `tenants.id` is unconstrained at the DB level for performance reasons; correctness is the application's responsibility.

## Failure Mode

A missing `tenant_id` filter leaks data across tenants and breaks the bootstrapping guarantee that the correct `webhook_secret` is used for signature verification on the multi-tenant webhook route.
