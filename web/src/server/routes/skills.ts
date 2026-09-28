import { createHash } from "node:crypto";

import { Router } from "express";

import {
  badRequest,
  notFound,
  readBodyString,
  readQuery,
  requireBodyString,
  requireQuery,
  route,
} from "../args.js";
import { count, dbPool } from "../db.js";
import * as sql from "../queries.js";
import { requireProject } from "./projects.js";

const ALL_SCOPES = "*";
const GLOBAL_ONLY = "_global";
const SKILL_NAME = /^[a-z0-9][a-z0-9-]{0,63}$/;

/** The `name:` of a SKILL.md frontmatter, which is what the skill is called. */
export function frontmatterName(content: string): string | null {
  const block = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content);
  const name = block === null ? null : /^name:\s*(\S+)\s*$/m.exec(block[1]);
  return name === null ? null : name[1];
}

function readId(value: string): number {
  const id = Number(value);
  if (!Number.isInteger(id) || id <= 0) {
    throw badRequest(`"${value}" is not a skill id`);
  }
  return id;
}

export const skillsRouter = Router();

skillsRouter.get(
  "/skills",
  route(async (req, res) => {
    const scope = readQuery(req, "owner");
    const rows = await dbPool.query<{ length: string }>(sql.SKILLS, [
      scope === null || scope === ALL_SCOPES || scope === GLOBAL_ONLY
        ? null
        : scope,
      scope === GLOBAL_ONLY,
    ]);
    res.json(rows.rows.map((row) => ({ ...row, length: count(row.length) })));
  }),
);

skillsRouter.get(
  "/skill",
  route(async (req, res) => {
    const id = readId(requireQuery(req, "id"));
    const rows = await dbPool.query(sql.SKILL, [id]);
    if (rows.rowCount === 0) {
      throw notFound(`No skill ${id}`);
    }
    res.json(rows.rows[0]);
  }),
);

skillsRouter.post(
  "/skills",
  route(async (req, res) => {
    const body = req.body as unknown;
    const content = requireBodyString(body, "content");
    const owner = readBodyString(body, "owner");
    const project =
      owner === undefined || owner === "" || owner === GLOBAL_ONLY
        ? null
        : await requireProject(owner);
    const name = frontmatterName(content);
    if (name === null || !SKILL_NAME.test(name)) {
      throw badRequest(
        "The text needs a frontmatter `name:` of lowercase letters, digits " +
          "and dashes",
      );
    }
    const sha256 = createHash("sha256").update(content, "utf8").digest("hex");
    const rows = await dbPool.query(sql.IMPORT_SKILL, [
      project,
      name,
      content,
      sha256,
    ]);
    if (rows.rowCount === 0) {
      throw badRequest(`"${name}" is a built-in skill and cannot be replaced`);
    }
    res.json(rows.rows[0]);
  }),
);

skillsRouter.delete(
  "/skill",
  route(async (req, res) => {
    const id = readId(requireQuery(req, "id"));
    const rows = await dbPool.query(sql.DROP_SKILL, [id]);
    if (rows.rowCount === 0) {
      throw notFound(`No imported skill ${id}. Built-in skills stay.`);
    }
    res.json(rows.rows[0]);
  }),
);

skillsRouter.get(
  "/projects/:name/skills",
  route(async (req, res) => {
    const name = await requireProject(req.params.name);
    const rows = await dbPool.query(sql.PROJECT_SKILLS, [name]);
    res.json(rows.rows);
  }),
);

skillsRouter.put(
  "/projects/:name/skills/:id",
  route(async (req, res) => {
    const name = await requireProject(req.params.name);
    const enabled = (req.body as { enabled?: unknown } | null)?.enabled;
    if (typeof enabled !== "boolean") {
      throw badRequest('Field "enabled" must be a boolean');
    }
    const rows = await dbPool.query(sql.SET_SKILL_ENABLED, [
      name,
      readId(req.params.id),
      enabled,
    ]);
    if (rows.rowCount === 0) {
      throw badRequest("That skill cannot be switched here");
    }
    res.json(rows.rows[0]);
  }),
);
