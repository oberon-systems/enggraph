import { AuthenticationError } from "./service.js";
import type { AuthService } from "./service.js";

export interface Request {
  body: Record<string, string>;
  headers: Record<string, string | undefined>;
  userId?: string;
}

export interface Response {
  status: number;
  body: unknown;
}

export class AuthController {
  constructor(private readonly auth: AuthService) {}

  async login(req: Request): Promise<Response> {
    try {
      const token = await this.auth.authenticate(
        req.body.email,
        req.body.password,
        Date.now() / 1000,
      );
      return { status: 200, body: { token } };
    } catch (error) {
      if (error instanceof AuthenticationError) {
        return { status: 401, body: { error: error.message } };
      }
      throw error;
    }
  }
}
