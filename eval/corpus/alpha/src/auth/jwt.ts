import { createHmac } from "node:crypto";
import type { AppConfig } from "../config.js";

export interface TokenClaims {
  sub: string;
  iss: string;
  exp: number;
}

export class JwtProvider {
  constructor(
    private readonly config: AppConfig,
    private readonly signingKey: string,
  ) {}

  sign(userId: string, now: number): string {
    const claims: TokenClaims = {
      sub: userId,
      iss: this.config.jwtIssuer,
      exp: now + this.config.jwtTtlSeconds,
    };
    const body = Buffer.from(JSON.stringify(claims)).toString("base64url");
    return `${body}.${this.signature(body)}`;
  }

  verify(token: string, now: number): TokenClaims | null {
    const [body, signature] = token.split(".");
    if (!body || signature !== this.signature(body)) {
      return null;
    }
    const claims = JSON.parse(
      Buffer.from(body, "base64url").toString(),
    ) as TokenClaims;
    return claims.exp > now ? claims : null;
  }

  private signature(body: string): string {
    return createHmac("sha256", this.signingKey)
      .update(body)
      .digest("base64url");
  }
}
