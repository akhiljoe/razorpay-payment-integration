import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/dom";

import { RazorpayCheckout } from "../react";
import type { CheckoutOptions } from "../types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeProps(over: Partial<CheckoutOptions> = {}): CheckoutOptions {
  return {
    orderEndpoint: "/payments/orders",
    receipt: "r-react",
    onSuccess: vi.fn(),
    onFailure: vi.fn(),
    onDismiss: vi.fn(),
    ...over,
  };
}

describe("RazorpayCheckout component", () => {
  it("renders a button with default label", () => {
    render(<RazorpayCheckout {...makeProps()} />);
    expect(screen.getByTestId("razorpay-checkout-button")).toHaveTextContent("Pay");
  });

  it("honours buttonLabel override", () => {
    render(<RazorpayCheckout {...makeProps({ buttonLabel: "Checkout now" })} />);
    expect(screen.getByTestId("razorpay-checkout-button")).toHaveTextContent("Checkout now");
  });

  it("click triggers the order POST", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    });
    vi.stubGlobal("fetch", fetchSpy);
    render(<RazorpayCheckout {...makeProps()} />);
    fireEvent.click(screen.getByTestId("razorpay-checkout-button"));
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/payments/orders",
      expect.objectContaining({ method: "POST" }),
    );
    vi.unstubAllGlobals();
  });
});
