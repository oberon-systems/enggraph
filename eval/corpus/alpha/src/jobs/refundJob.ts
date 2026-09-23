import type { PaymentService } from "../payments/service.js";

export interface PendingRefund {
  transactionId: string;
  amountCents: number;
  email: string;
}

export class RefundJob {
  constructor(
    private readonly payments: PaymentService,
    private readonly pending: () => Promise<PendingRefund[]>,
  ) {}

  async execute(): Promise<number> {
    let done = 0;
    for (const item of await this.pending()) {
      await this.payments.refund(
        item.transactionId,
        item.amountCents,
        item.email,
      );
      done += 1;
    }
    return done;
  }
}
