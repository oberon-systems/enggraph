export interface ChargeResult {
  transactionId: string;
  amountCents: number;
}

export interface PaymentGateway {
  charge(customerId: string, amountCents: number): Promise<ChargeResult>;
  refund(transactionId: string, amountCents: number): Promise<ChargeResult>;
}

export class HttpPaymentGateway implements PaymentGateway {
  constructor(private readonly baseUrl: string) {}

  async charge(customerId: string, amountCents: number): Promise<ChargeResult> {
    return this.post("/charges", { customerId, amountCents });
  }

  async refund(
    transactionId: string,
    amountCents: number,
  ): Promise<ChargeResult> {
    return this.post("/refunds", { transactionId, amountCents });
  }

  private async post(path: string, body: unknown): Promise<ChargeResult> {
    const res = await fetch(this.baseUrl + path, {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(`gateway answered ${res.status}`);
    }
    return (await res.json()) as ChargeResult;
  }
}
