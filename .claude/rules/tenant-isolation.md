---
description: Enforce tenant_id scoping on every DB query and webhook entry point.
globs:
  - "razorpay_checkout/razorpay_checkout/orders.py"
  - "razorpay_checkout/razorpay_checkout/webhooks.py"
  - "razorpay_checkout/razorpay_checkout/router.py"
  - "razorpay_checkout/razorpay_checkout/migrations/**"
---

# Tenant Isolation Rule

Every query against `orders`, `payments`, `refunds`, `webhook_events`, and `order_events` must include a `tenant_id` filter. Enforced by `OrderService` and `WebhookProcessor`, never by the caller.

## Source of `tenant_id`

`tenant_id` must be extracted from the URL path parameter on the webhook route per `designs/DESIGN_v5.md` Section 2.1. It must never come from the request body, query string, or header. Non-webhook routes derive `tenant_id` from the injected `RazorpayConfig`.

## Enforcement Checklist

1. Every `SELECT`, `UPDATE`, `INSERT`, and `DELETE` against the five tenant-scoped tables (`orders`, `payments`, `refunds`, `webhook_events`, `order_events`) must filter or set `tenant_id`.
2. The webhook router resolves `RazorpayConfig` from `TenantConfigRegistry` using the path parameter before any signature verification or DB lookup.
3. Reject any code path that accepts `tenant_id` from a request body, query string, or header.
4. Cross-tenant joins are forbidden. The logical FK from `*.tenant_id` to `tenants.id` is unconstrained at the DB level for performance; correctness is the application's responsibility.

## Failure Mode

A missing `tenant_id` filter leaks data across tenants and breaks the bootstrapping guarantee that the correct `webhook_secret` is used for signature verification on the multi-tenant webhook route.
