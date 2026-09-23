import type { AppConfig } from "../config.js";

export class RetryPolicy {
  constructor(
    readonly maxAttempts: number,
    readonly backoffMs: number,
  ) {}

  static fromConfig(config: AppConfig): RetryPolicy {
    return new RetryPolicy(
      config.paymentRetryMaxAttempts,
      config.paymentRetryBackoffMs,
    );
  }

  delayFor(attempt: number): number {
    return this.backoffMs * 2 ** (attempt - 1);
  }

  async run<T>(
    action: () => Promise<T>,
    sleep: (ms: number) => Promise<void>,
  ): Promise<T> {
    let lastError: unknown;
    for (let attempt = 1; attempt <= this.maxAttempts; attempt += 1) {
      try {
        return await action();
      } catch (error) {
        lastError = error;
        await sleep(this.delayFor(attempt));
      }
    }
    throw lastError;
  }
}
