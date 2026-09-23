import { describe, expect, it } from "vitest";
import { handleOAuthCallback } from "../src/oauth/callback.js";
import type { JwtProvider } from "../src/auth/jwt.js";
import type { UserRepository } from "../src/users/repository.js";

describe("handleOAuthCallback", () => {
  it("rejects a state mismatch", async () => {
    const users = {} as UserRepository;
    const jwt = {} as JwtProvider;
    await expect(
      handleOAuthCallback("code", "x", "y", users, jwt, async () => ({
        email: "",
        provider: "p",
      })),
    ).rejects.toThrow("state mismatch");
  });
});
