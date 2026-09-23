import type { AuthController, Request, Response } from "./auth/controller.js";
import type { RefundController } from "./payments/controller.js";

export type Handler = (req: Request) => Promise<Response>;

export function buildRoutes(
  auth: AuthController,
  refunds: RefundController,
): Map<string, Handler> {
  return new Map<string, Handler>([
    ["POST /login", (req) => auth.login(req)],
    ["POST /refunds", (req) => refunds.refund(req)],
  ]);
}
