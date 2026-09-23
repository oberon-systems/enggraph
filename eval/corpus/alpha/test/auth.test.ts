import { describe, expect, it } from "vitest";
import { AuthController } from "../src/auth/controller.js";
import { JwtProvider } from "../src/auth/jwt.js";
import { PasswordHasher } from "../src/auth/password.js";
import { AuthService } from "../src/auth/service.js";
import { loadConfig } from "../src/config.js";
import { UserRepository } from "../src/users/repository.js";

describe("AuthController.login", () => {
  it("returns a token for the right password", async () => {
    const hasher = new PasswordHasher();
    const user = {
      id: "u1",
      email: "a@example.com",
      salt: "s",
      passwordHash: hasher.hash("pw", "s"),
      disabled: false,
    };
    const users = new UserRepository({
      one: async () => user as never,
      run: async () => undefined,
    });
    const auth = new AuthService(
      users,
      hasher,
      new JwtProvider(loadConfig({}), "key"),
    );
    const res = await new AuthController(auth).login({
      body: { email: user.email, password: "pw" },
      headers: {},
    });
    expect(res.status).toBe(200);
  });
});
