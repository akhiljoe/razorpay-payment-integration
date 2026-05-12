import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mountCheckout } from "../vanilla";
import type { CheckoutOptions } from "../types";

function makeOpts(over: Partial<CheckoutOptions> = {}): CheckoutOptions {
  return {
    orderEndpoint: "/payments/orders",
    receipt: "r-vanilla",
    onSuccess: vi.fn(),
    onFailure: vi.fn(),
    onDismiss: vi.fn(),
    ...over,
  };
}

beforeEach(() => {
  document.body.innerHTML = "";
  delete (window as { Razorpay?: unknown }).Razorpay;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("mountCheckout", () => {
  it("appends a button with the default label", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const unmount = mountCheckout(host, makeOpts());
    const btn = host.querySelector("button");
    expect(btn).not.toBeNull();
    expect(btn?.textContent).toBe("Pay");
    unmount();
  });

  it("honours buttonLabel override", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    mountCheckout(host, makeOpts({ buttonLabel: "Checkout now" }));
    expect(host.querySelector("button")?.textContent).toBe("Checkout now");
  });

  it("unmount removes the button from the DOM", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const unmount = mountCheckout(host, makeOpts());
    expect(host.querySelector("button")).not.toBeNull();
    unmount();
    expect(host.querySelector("button")).toBeNull();
  });

  it("clicking the button invokes runCheckout (verified via fetch stub)", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    });
    vi.stubGlobal("fetch", fetchSpy);

    const host = document.createElement("div");
    document.body.appendChild(host);
    const opts = makeOpts();
    mountCheckout(host, opts);

    const btn = host.querySelector("button") as HTMLButtonElement;
    btn.click();
    // Wait one microtask for the async click handler to fire fetch.
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/payments/orders",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
