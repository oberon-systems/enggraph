import type { Request, Response } from "../auth/controller.js";
import type { PaymentService } from "./service.js";

export class RefundController {
  constructor(private readonly payments: PaymentService) {}

  async refund(req: Request): Promise<Response> {
    const amount = Number(req.body.amountCents);
    const result = await this.payments.refund(
      req.body.transactionId,
      amount,
      req.body.email,
    );
    return { status: 202, body: result };
  }
}
