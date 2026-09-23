import type { JwtProvider } from "./jwt.js";
import type { Request, Response } from "./controller.js";

export function requireAuth(jwt: JwtProvider) {
  return (req: Request): Response | null => {
    const header = req.headers.authorization ?? "";
    const token = header.startsWith("Bearer ") ? header.slice(7) : "";
    const claims = jwt.verify(token, Date.now() / 1000);
    if (claims === null) {
      return { status: 401, body: { error: "invalid or expired token" } };
    }
    req.userId = claims.sub;
    return null;
  };
}
