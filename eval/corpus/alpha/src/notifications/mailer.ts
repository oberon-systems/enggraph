export interface MailTransport {
  deliver(to: string, subject: string, text: string): Promise<void>;
}

export class Mailer {
  constructor(
    private readonly transport: MailTransport,
    private readonly from: string,
  ) {}

  async send(to: string, subject: string, text: string): Promise<void> {
    await this.transport.deliver(to, `[${this.from}] ${subject}`, text);
  }

  async sendRefundReceipt(to: string, amountCents: number): Promise<void> {
    await this.send(
      to,
      "Refund issued",
      `We refunded ${(amountCents / 100).toFixed(2)}.`,
    );
  }
}
