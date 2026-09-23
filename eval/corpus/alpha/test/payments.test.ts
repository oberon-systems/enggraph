import { describe, expect, it } from "vitest";
import { RetryPolicy } from "../src/payments/retry.js";

describe("RetryPolicy", () => {
  it("backs off exponentially", () => {
    expect(new RetryPolicy(5, 100).delayFor(3)).toBe(400);
  });

  it("gives up after maxAttempts", async () => {
    let calls = 0;
    const policy = new RetryPolicy(3, 0);
    await expect(
      policy.run(
        async () => {
          calls += 1;
          throw new Error("down");
        },
        async () => undefined,
      ),
    ).rejects.toThrow("down");
    expect(calls).toBe(3);
  });
});
