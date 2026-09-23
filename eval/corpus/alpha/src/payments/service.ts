import type { Mailer } from "../notifications/mailer.js";
import type { ChargeResult, PaymentGateway } from "./gateway.js";
import type { RetryPolicy } from "./retry.js";

const sleep = (ms: number) => new Promise<void>((done) => setTimeout(done, ms));

export class PaymentService {
  constructor(
    private readonly gateway: PaymentGateway,
    private readonly retry: RetryPolicy,
    private readonly mailer: Mailer,
  ) {}

  charge(customerId: string, amountCents: number): Promise<ChargeResult> {
    return this.retry.run(
      () => this.gateway.charge(customerId, amountCents),
      sleep,
    );
  }

  async refund(
    transactionId: string,
    amountCents: number,
    email: string,
  ): Promise<ChargeResult> {
    if (amountCents <= 0) {
      throw new Error("refund amount must be positive");
    }
    const result = await this.retry.run(
      () => this.gateway.refund(transactionId, amountCents),
      sleep,
    );
    await this.mailer.sendRefundReceipt(email, amountCents);
    return result;
  }
}
