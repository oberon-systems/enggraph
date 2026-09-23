import { createHash, timingSafeEqual } from "node:crypto";

export class PasswordHasher {
  hash(password: string, salt: string): string {
    return createHash("sha256")
      .update(salt + password)
      .digest("hex");
  }

  verify(password: string, salt: string, expected: string): boolean {
    const actual = Buffer.from(this.hash(password, salt));
    return timingSafeEqual(actual, Buffer.from(expected));
  }
}
