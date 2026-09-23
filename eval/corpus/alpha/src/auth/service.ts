import type { JwtProvider } from "./jwt.js";
import type { PasswordHasher } from "./password.js";
import type { UserRepository } from "../users/repository.js";

export class AuthenticationError extends Error {}

export class AuthService {
  constructor(
    private readonly users: UserRepository,
    private readonly hasher: PasswordHasher,
    private readonly jwt: JwtProvider,
  ) {}

  async authenticate(
    email: string,
    password: string,
    now: number,
  ): Promise<string> {
    const user = await this.users.findByEmail(email);
    if (user === null || user.disabled) {
      throw new AuthenticationError("unknown or disabled user");
    }
    if (!this.hasher.verify(password, user.salt, user.passwordHash)) {
      throw new AuthenticationError("wrong password");
    }
    return this.jwt.sign(user.id, now);
  }
}
