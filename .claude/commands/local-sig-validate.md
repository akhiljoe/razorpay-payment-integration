---
description: Validate Razorpay HMAC-SHA256 webhook signature logic locally without a live endpoint.
---

# /project:local-sig-validate

Test `verify_webhook_signature` in `razorpay_checkout/razorpay_checkout/signatures.py` without a live Razorpay endpoint.

## Test Plan

1. Read `razorpay_checkout/razorpay_checkout/signatures.py`.
2. Confirm the implementation uses `hmac.compare_digest` for constant-time comparison. Reject any use of `==`, `!=`, `is`, or string equality on the digest.
3. Confirm the function tries the current secret first, then falls back to `webhook_secret_previous` when set (per Section 2.1 of `designs/DESIGN_v3.md` and Runbook 6 in Section 12).
4. Run the local test vectors below against the function in isolation. No FastAPI app, no DB, no network.

## Test Vectors

Deterministic fixtures, computed via `hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()`. Reproducible offline; no live Razorpay account required.

**Secrets:**

- `secret_current = "whsec_test_current_v1"`
- `secret_previous = "whsec_test_previous_v0"`

**Vector A (canonical valid case, current secret):**

- `raw_body` (bytes):
  ```
  {"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test_001","order_id":"order_test_001","amount":50000,"currency":"INR","status":"captured"}}}}
  ```
- `signature` (hex): `367afa4837c3e9729fe83cd0f3dff9c786f7133fb15828082bf833a8ca815588`
- Expected: `verify_webhook_signature(raw_body, signature, secret_current, secret_previous) == True`

**Vector B (rotation fallback, previous secret signed it):**

- `raw_body` (bytes): same as Vector A
- `signature` (hex): `094e9a3676c751842645f2081f1b21e55f2eacece223d31b6d3152a7ddcd9d4c`
- Expected: `verify_webhook_signature(raw_body, signature, secret_current, secret_previous) == True`

**Vector C (no fallback configured, current secret valid):**

- `raw_body` (bytes): same as Vector A
- `signature` (hex): `367afa4837c3e9729fe83cd0f3dff9c786f7133fb15828082bf833a8ca815588`
- Expected: `verify_webhook_signature(raw_body, signature, secret_current, None) == True`

**Vector D (tampered body):**

- `raw_body` (bytes): amount field changed from 50000 to 99999
  ```
  {"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test_001","order_id":"order_test_001","amount":99999,"currency":"INR","status":"captured"}}}}
  ```
- `signature` (hex): `367afa4837c3e9729fe83cd0f3dff9c786f7133fb15828082bf833a8ca815588` (Vector A signature)
- Expected: `verify_webhook_signature(raw_body, signature, secret_current, secret_previous) == False`

**Vector E (signature signed by an unrelated secret):**

- `raw_body` (bytes): same as Vector A
- `signature` (hex): `41b33af756109f61268ec43350f8b6256fcb9bbdb6c9c241a2abffcc0d7c2b96`
- Expected: `verify_webhook_signature(raw_body, signature, secret_current, secret_previous) == False`

**Vector F (boundary: empty body, current secret):**

- `raw_body` (bytes): `b""`
- `signature` (hex): `b07c2754a5fc96f1416a267e07eee13473292478d550782e9faf60755e3bbc9b`
- Expected: `verify_webhook_signature(b"", signature, secret_current, secret_previous) == True`

## Local Runner

```python
from razorpay_checkout.signatures import verify_webhook_signature

SECRET_CURRENT = "whsec_test_current_v1"
SECRET_PREVIOUS = "whsec_test_previous_v0"

BODY_A = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test_001","order_id":"order_test_001","amount":50000,"currency":"INR","status":"captured"}}}}'
BODY_D = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test_001","order_id":"order_test_001","amount":99999,"currency":"INR","status":"captured"}}}}'

SIG_A = "367afa4837c3e9729fe83cd0f3dff9c786f7133fb15828082bf833a8ca815588"
SIG_B_PREVIOUS = "094e9a3676c751842645f2081f1b21e55f2eacece223d31b6d3152a7ddcd9d4c"
SIG_E_WRONG = "41b33af756109f61268ec43350f8b6256fcb9bbdb6c9c241a2abffcc0d7c2b96"
SIG_F_EMPTY = "b07c2754a5fc96f1416a267e07eee13473292478d550782e9faf60755e3bbc9b"

assert verify_webhook_signature(BODY_A, SIG_A,         SECRET_CURRENT, SECRET_PREVIOUS) is True,  "Vector A failed"
assert verify_webhook_signature(BODY_A, SIG_B_PREVIOUS, SECRET_CURRENT, SECRET_PREVIOUS) is True,  "Vector B failed"
assert verify_webhook_signature(BODY_A, SIG_A,         SECRET_CURRENT, None)            is True,  "Vector C failed"
assert verify_webhook_signature(BODY_D, SIG_A,         SECRET_CURRENT, SECRET_PREVIOUS) is False, "Vector D failed"
assert verify_webhook_signature(BODY_A, SIG_E_WRONG,   SECRET_CURRENT, SECRET_PREVIOUS) is False, "Vector E failed"
assert verify_webhook_signature(b"",    SIG_F_EMPTY,   SECRET_CURRENT, SECRET_PREVIOUS) is True,  "Vector F failed"
print("OK: all 6 vectors passed")
```

## Output

Report PASS or FAIL per vector (A through F), plus the constant-time comparison check from step 2 and the rotation fallback check from step 3. Do not skip the constant-time check even if all vectors PASS.
