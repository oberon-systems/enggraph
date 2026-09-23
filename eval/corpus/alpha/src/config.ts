export interface AppConfig {
  port: number;
  jwtTtlSeconds: number;
  jwtIssuer: string;
  paymentRetryMaxAttempts: number;
  paymentRetryBackoffMs: number;
  mailFrom: string;
}

export function loadConfig(env: Record<string, string | undefined>): AppConfig {
  return {
    port: Number(env.PORT ?? 8080),
    jwtTtlSeconds: Number(env.JWT_TTL_SECONDS ?? 3600),
    jwtIssuer: env.JWT_ISSUER ?? "alpha",
    paymentRetryMaxAttempts: Number(env.PAYMENT_RETRY_MAX_ATTEMPTS ?? 5),
    paymentRetryBackoffMs: Number(env.PAYMENT_RETRY_BACKOFF_MS ?? 250),
    mailFrom: env.MAIL_FROM ?? "noreply@example.com",
  };
}
