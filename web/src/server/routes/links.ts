import { Router } from "express";

import {
  badRequest,
  readBodyString,
  readQuery,
  requireBodyString,
  route,
} from "../args.js";
import { callLinkTool } from "../mcp.js";

export const linksRouter = Router();

linksRouter.get(
  "/projects/:name/links",
  route(async (req, res) => {
    const depth = Number(readQuery(req, "depth") ?? "1");
    const text = await callLinkTool(req.params.name, "get_project_links", {
      depth: Number.isFinite(depth) ? depth : 1,
      direction: readQuery(req, "direction") ?? "both",
    });
    res.json(JSON.parse(text) as unknown);
  }),
);

// A declared relation is written under the project it leaves, so the page of
// either end can add or remove it.
function relationArgs(page: string, body: unknown): Record<string, unknown> {
  const from = readBodyString(body, "from") ?? page;
  const to = requireBodyString(body, "to");
  if (from !== page && to !== page) {
    throw badRequest(`A relation shown here starts or ends at ${page}`);
  }
  return {
    project: from,
    target_project: to,
    relation: requireBodyString(body, "relation"),
    source_id: readBodyString(body, "source_id") ?? "./",
    target_id: readBodyString(body, "target_id") ?? "./",
  };
}

linksRouter.post(
  "/projects/:name/links",
  route(async (req, res) => {
    const args = relationArgs(req.params.name, req.body);
    const note = readBodyString(req.body, "note");
    const said = await callLinkTool(String(args.project), "save_project_link", {
      ...args,
      ...(note === undefined ? {} : { note }),
    });
    res.status(201).json({ said });
  }),
);

linksRouter.delete(
  "/projects/:name/links",
  route(async (req, res) => {
    const args = relationArgs(req.params.name, req.body);
    const said = await callLinkTool(
      String(args.project),
      "drop_project_link",
      args,
    );
    res.json({ said });
  }),
);

linksRouter.post(
  "/projects/:name/exports",
  route(async (req, res) => {
    const said = await callLinkTool(req.params.name, "save_project_export", {
      project: req.params.name,
      kind: requireBodyString(req.body, "kind"),
      name: requireBodyString(req.body, "name"),
      node_id: readBodyString(req.body, "node_id") ?? "./",
    });
    res.status(201).json({ said });
  }),
);

linksRouter.delete(
  "/projects/:name/exports",
  route(async (req, res) => {
    const said = await callLinkTool(req.params.name, "drop_project_export", {
      project: req.params.name,
      kind: requireBodyString(req.body, "kind"),
      name: requireBodyString(req.body, "name"),
    });
    res.json({ said });
  }),
);
