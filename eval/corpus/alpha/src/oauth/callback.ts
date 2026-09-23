import type { JwtProvider } from "../auth/jwt.js";
import type { UserRepository } from "../users/repository.js";

export interface OAuthProfile {
  email: string;
  provider: string;
}

export async function exchangeCode(
  code: string,
  fetchToken: (code: string) => Promise<OAuthProfile>,
): Promise<OAuthProfile> {
  if (code.length === 0) {
    throw new Error("missing authorization code");
  }
  return fetchToken(code);
}

export async function handleOAuthCallback(
  code: string,
  state: string,
  expectedState: string,
  users: UserRepository,
  jwt: JwtProvider,
  fetchToken: (code: string) => Promise<OAuthProfile>,
): Promise<string> {
  if (state !== expectedState) {
    throw new Error("oauth state mismatch");
  }
  const profile = await exchangeCode(code, fetchToken);
  const user = await users.findByEmail(profile.email);
  if (user === null) {
    throw new Error("no account for this oauth profile");
  }
  return jwt.sign(user.id, Date.now() / 1000);
}
