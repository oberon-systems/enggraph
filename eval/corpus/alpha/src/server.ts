import { AuthController } from "./auth/controller.js";
import { JwtProvider } from "./auth/jwt.js";
import { PasswordHasher } from "./auth/password.js";
import { AuthService } from "./auth/service.js";
import { loadConfig } from "./config.js";
import { Mailer } from "./notifications/mailer.js";
import type { MailTransport } from "./notifications/mailer.js";
import { RefundController } from "./payments/controller.js";
import { HttpPaymentGateway } from "./payments/gateway.js";
import { RetryPolicy } from "./payments/retry.js";
import { PaymentService } from "./payments/service.js";
import { buildRoutes } from "./routes.js";
import { UserRepository } from "./users/repository.js";
import type { Database } from "./users/repository.js";

export function createServer(
  db: Database,
  transport: MailTransport,
  signingKey: string,
) {
  const config = loadConfig(process.env);
  const jwt = new JwtProvider(config, signingKey);
  const auth = new AuthService(
    new UserRepository(db),
    new PasswordHasher(),
    jwt,
  );
  const payments = new PaymentService(
    new HttpPaymentGateway("https://payments.example.com"),
    RetryPolicy.fromConfig(config),
    new Mailer(transport, config.mailFrom),
  );
  return {
    port: config.port,
    routes: buildRoutes(
      new AuthController(auth),
      new RefundController(payments),
    ),
  };
}
