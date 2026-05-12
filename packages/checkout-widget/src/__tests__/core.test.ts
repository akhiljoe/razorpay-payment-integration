import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ensureRazorpayLoaded, runCheckout } from "../core";
import type { CheckoutOptions, PaymentError, PaymentResult } from "../types";

const SUCCESS_ORDER = {
  order_id: "ord_TEST",
  razorpay_order_id: "order_RZP_X",
  amount: 50000,
  currency: "INR",
  key_id: "rzp_test_kid",
};

function stubFetchOk(body: unknown = SUCCESS_ORDER): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    }),
  );
}

function stubFetchFail(status: number = 500): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status, json: () => Promise.resolve({}) }),
  );
}

function makeOpts(over: Partial<CheckoutOptions> = {}): CheckoutOptions {
  return {
    orderEndpoint: "/payments/orders",
    receipt: "r-test",
    onSuccess: vi.fn(),
    onFailure: vi.fn(),
    onDismiss: vi.fn(),
    ...over,
  };
}

beforeEach(() => {
  delete (window as { Razorpay?: unknown }).Razorpay;
  document.head.querySelector("#razorpay-checkout-script")?.remove();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ensureRazorpayLoaded", () => {
  it("returns true immediately when window.Razorpay is already defined", async () => {
    (window as { Razorpay?: unknown }).Razorpay = function Stub() {} as unknown;
    expect(await ensureRazorpayLoaded()).toBe(true);
  });

  it("returns true once the injected script defines window.Razorpay", async () => {
    setTimeout(() => {
      (window as { Razorpay?: unknown }).Razorpay = function Stub() {} as unknown;
    }, 50);
    expect(await ensureRazorpayLoaded(500)).toBe(true);
  });

  it("returns false when window.Razorpay never appears within the timeout", async () => {
    expect(await ensureRazorpayLoaded(150)).toBe(false);
  });
});

describe("runCheckout happy path", () => {
  it("opens the Razorpay modal with server-returned amount", async () => {
    stubFetchOk();
    const openSpy = vi.fn();
    (window as { Razorpay?: unknown }).Razorpay = function StubRzp(
      this: { options: unknown },
      options: unknown,
    ) {
      this.options = options;
      (this as unknown as { open: () => void }).open = openSpy;
    } as unknown;

    const opts = makeOpts();
    await runCheckout(opts);

    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(opts.onFailure).not.toHaveBeenCalled();
  });

  it("uses the server-returned amount, never a client value", async () => {
    stubFetchOk();
    let capturedAmount: unknown = "<unset>";
    (window as { Razorpay?: unknown }).Razorpay = function StubRzp(
      this: object,
      options: { amount: number },
    ) {
      capturedAmount = options.amount;
      (this as unknown as { open: () => void }).open = () => undefined;
    } as unknown;

    await runCheckout(makeOpts());
    expect(capturedAmount).toBe(50000);
  });
});

describe("runCheckout failure paths", () => {
  it("calls onFailure with code='order_create_failed' on /payments/orders 500", async () => {
    stubFetchFail(500);
    const opts = makeOpts();
    await runCheckout(opts);
    expect(opts.onFailure).toHaveBeenCalledTimes(1);
    const err = (opts.onFailure as ReturnType<typeof vi.fn>).mock.calls[0]![0] as PaymentError;
    expect(err.code).toBe("order_create_failed");
  });

  it("calls onFailure with code='csp_blocked' when Razorpay script never loads", async () => {
    stubFetchOk();
    // window.Razorpay deliberately undefined and never set.
    const opts = makeOpts();
    // Stub the timeout-bounded waiter via the public API path (3s default).
    // Use the small-timeout direct test on ensureRazorpayLoaded for speed,
    // and exercise runCheckout's branch with a short timeout via a vi mock:
    vi.useFakeTimers();
    const promise = runCheckout(opts);
    // Advance past 3s so the polling waiter resolves false.
    await vi.advanceTimersByTimeAsync(3100);
    await promise;
    vi.useRealTimers();
    expect(opts.onFailure).toHaveBeenCalledTimes(1);
    const err = (opts.onFailure as ReturnType<typeof vi.fn>).mock.calls[0]![0] as PaymentError;
    expect(err.code).toBe("csp_blocked");
    expect(err.metadata.orderId).toBe(SUCCESS_ORDER.order_id);
  });
});

describe("runCheckout maps the success callback to PaymentResult", () => {
  it("forwards razorpay_payment_id and signature from Razorpay handler", async () => {
    stubFetchOk();
    let capturedHandler: ((response: Record<string, unknown>) => void) | null = null;
    (window as { Razorpay?: unknown }).Razorpay = function StubRzp(
      this: object,
      options: { handler: (response: Record<string, unknown>) => void },
    ) {
      capturedHandler = options.handler;
      (this as unknown as { open: () => void }).open = () => undefined;
    } as unknown;

    const opts = makeOpts();
    await runCheckout(opts);
    expect(capturedHandler).not.toBeNull();
    capturedHandler!({
      razorpay_payment_id: "pay_TEST",
      razorpay_signature: "sig_TEST",
    });

    expect(opts.onSuccess).toHaveBeenCalledTimes(1);
    const result = (opts.onSuccess as ReturnType<typeof vi.fn>).mock.calls[0]![0] as PaymentResult;
    expect(result).toEqual({
      orderId: SUCCESS_ORDER.order_id,
      razorpayOrderId: SUCCESS_ORDER.razorpay_order_id,
      razorpayPaymentId: "pay_TEST",
      razorpaySignature: "sig_TEST",
    });
  });
});
