import { Router } from "express";

import {
  badRequest,
  readBodyString,
  readQuery,
  requireBodyString,
  route,
} from "../args.js";
import { callTool, listTools } from "../mcp.js";

export const askRouter = Router();

askRouter.get(
  "/ask/tools",
  route(async (req, res) => {
    res.json({ tools: await listTools(readQuery(req, "project") ?? "") });
  }),
);

askRouter.post(
  "/ask",
  route(async (req, res) => {
    const body = req.body as unknown;
    const tool = requireBodyString(body, "tool");
    const args = (body as { arguments?: unknown }).arguments ?? {};
    if (args === null || typeof args !== "object" || Array.isArray(args)) {
      throw badRequest('Field "arguments" must be an object');
    }
    res.json(
      await callTool(
        readBodyString(body, "project") ?? "",
        tool,
        args as Record<string, unknown>,
      ),
    );
  }),
);
