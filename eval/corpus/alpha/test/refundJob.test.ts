import { describe, expect, it } from "vitest";
import { RefundJob } from "../src/jobs/refundJob.js";
import type { PaymentService } from "../src/payments/service.js";

describe("RefundJob.execute", () => {
  it("refunds every pending item through PaymentService.refund", async () => {
    const seen: string[] = [];
    const payments = {
      refund: async (id: string) => {
        seen.push(id);
      },
    } as unknown as PaymentService;
    const job = new RefundJob(payments, async () => [
      { transactionId: "t1", amountCents: 100, email: "b@example.com" },
    ]);
    expect(await job.execute()).toBe(1);
    expect(seen).toEqual(["t1"]);
  });
});
