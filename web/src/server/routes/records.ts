import { Router } from "express";

import {
  badRequest,
  notFound,
  readQuery,
  requireBodyString,
  requireQuery,
  route,
} from "../args.js";
import { dbPool } from "../db.js";
import * as sql from "../queries.js";
import { requireProject } from "./projects.js";

const RECORD_PROJECTS: Record<string, string> = {
  memory: "_memory",
  plan: "_plans",
  suggestion: "_suggestions",
};

const GROUPS = new Set(["kind", "lever", "about", "directory"]);
const ROOT_ID = "./";

function recordProject(kind: string): string {
  const project = RECORD_PROJECTS[kind];
  if (project === undefined) {
    throw badRequest(
      `A record is one of ${Object.keys(RECORD_PROJECTS).join(", ")}`,
    );
  }
  return project;
}

/** The node and every directory above it, up to the root. */
function aroundNode(id: string): string[] {
  const parts = id
    .split("::", 1)[0]
    .replace(/\/+$/, "")
    .split("/")
    .filter((part) => part !== "" && part !== ".");
  const ids = [id];
  for (let end = parts.length - 1; end > 0; end -= 1) {
    ids.push(`${parts.slice(0, end).join("/")}/`);
  }
  if (id !== ROOT_ID) {
    ids.push(ROOT_ID);
  }
  return [...new Set(ids)];
}

export const recordsRouter = Router();

recordsRouter.get(
  "/records/:kind/nodes",
  route(async (req, res) => {
    const rows = await dbPool.query(sql.RECORD_NODES, [
      recordProject(req.params.kind),
      requireQuery(req, "id"),
    ]);
    res.json(rows.rows);
  }),
);

recordsRouter.post(
  "/records/:kind/nodes",
  route(async (req, res) => {
    const record = recordProject(req.params.kind);
    const id = requireQuery(req, "id");
    const body = req.body as unknown;
    const project = await requireProject(requireBodyString(body, "project"));
    const nodeId = requireBodyString(body, "node_id");
    const rows = await dbPool.query(sql.ADD_RECORD_NODE, [
      record,
      id,
      project,
      nodeId,
    ]);
    if (rows.rowCount === 0) {
      const linked = await dbPool.query(sql.RECORD_NODES, [record, id]);
      const already = linked.rows.some(
        (row: { project: string; node_id: string }) =>
          row.project === project && row.node_id === nodeId,
      );
      if (!already) {
        throw notFound(`No node "${nodeId}" in ${project}, or no record ${id}`);
      }
    }
    res.json({ project, node_id: nodeId });
  }),
);

recordsRouter.delete(
  "/records/:kind/nodes",
  route(async (req, res) => {
    const rows = await dbPool.query(sql.DROP_RECORD_NODE, [
      recordProject(req.params.kind),
      requireQuery(req, "id"),
      requireQuery(req, "project"),
      requireQuery(req, "node_id"),
    ]);
    if (rows.rowCount === 0) {
      throw notFound("That node is not linked to this record");
    }
    res.json(rows.rows[0]);
  }),
);

recordsRouter.get(
  "/projects/:name/knowledge",
  route(async (req, res) => {
    const name = await requireProject(req.params.name);
    const rows = await dbPool.query(sql.NODE_KNOWLEDGE, [
      name,
      aroundNode(requireQuery(req, "id")),
    ]);
    res.json(rows.rows);
  }),
);

recordsRouter.get(
  "/suggestions/groups",
  route(async (req, res) => {
    const by = readQuery(req, "by") ?? "kind";
    if (!GROUPS.has(by)) {
      throw badRequest(`Group by one of ${[...GROUPS].join(", ")}`);
    }
    const status = readQuery(req, "status");
    const rows =
      by === "directory"
        ? await dbPool.query(sql.SUGGESTION_DIRECTORIES, [status])
        : await dbPool.query(sql.SUGGESTION_GROUPS, [by, status]);
    res.json({ by, groups: rows.rows });
  }),
);
