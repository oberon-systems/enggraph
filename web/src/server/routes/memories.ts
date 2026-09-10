import { Router } from "express";

import {
  notFound,
  readBodyString,
  readLimit,
  readOffset,
  readQuery,
  requireQuery,
  route,
} from "../args.js";
import { count, dbPool } from "../db.js";
import * as sql from "../queries.js";

// The scope is free text with no foreign key, exactly as a suggestion's is,
// so these two are selections rather than project names: every scope, and the
// memories about none in particular.
const ALL_SCOPES = "*";
const GLOBAL_ONLY = "_global";

const MIN_QUERY = 2;

function searchPattern(value: string | null): string | null {
  if (value === null || value.trim().length < MIN_QUERY) {
    return null;
  }
  return `%${value.trim()}%`;
}

export const memoriesRouter = Router();

// No POST, for the reason the suggestions router gives about suggestions: a
// memory is written by the agent that learned the thing, through save_memory,
// and what the dashboard is for is correcting and retiring them.
memoriesRouter.get(
  "/memories",
  route(async (req, res) => {
    const scope = readQuery(req, "about");
    const limit = readLimit(req);
    const offset = readOffset(req);
    const rows = await dbPool.query<{ total: string; text_length: string }>(
      sql.MEMORIES,
      [
        scope === null || scope === ALL_SCOPES || scope === GLOBAL_ONLY
          ? null
          : scope,
        scope === GLOBAL_ONLY,
        readQuery(req, "tag"),
        searchPattern(readQuery(req, "q")),
        limit,
        offset,
      ],
    );

    res.json({
      items: rows.rows.map(({ total: _total, ...memory }) => ({
        ...memory,
        text_length: count(memory.text_length),
      })),
      total: rows.rowCount === 0 ? 0 : count(rows.rows[0].total),
      limit,
      offset,
    });
  }),
);

memoriesRouter.get(
  "/memories/facets",
  route(async (_req, res) => {
    const rows = await dbPool.query<{
      abouts: string[] | null;
      tags: string[] | null;
      global_memories: string;
    }>(sql.MEMORY_FACETS);
    const row = rows.rows[0];
    res.json({
      abouts: (row.abouts ?? []).sort(),
      tags: (row.tags ?? []).sort(),
      global_memories: count(row.global_memories),
    });
  }),
);

memoriesRouter.get(
  "/memory",
  route(async (req, res) => {
    const id = requireQuery(req, "id");
    const rows = await dbPool.query(sql.MEMORY, [id]);
    if (rows.rowCount === 0) {
      throw notFound(`No memory "${id}"`);
    }
    res.json(rows.rows[0]);
  }),
);

// The scope and the slug are not patched: together they are the node id, so
// changing one is a move rather than an edit. A memory filed against the
// wrong project is dropped and written again by the agent that owns it.
memoriesRouter.patch(
  "/memory",
  route(async (req, res) => {
    const id = requireQuery(req, "id");
    const body = req.body as unknown;
    const tags = readBodyString(body, "tags");
    const rows = await dbPool.query(sql.PATCH_MEMORY, [
      id,
      readBodyString(body, "title") ?? null,
      readBodyString(body, "summary") ?? null,
      readBodyString(body, "text") ?? null,
      tags === undefined
        ? null
        : JSON.stringify(
            tags
              .split(",")
              .map((tag) => tag.trim())
              .filter((tag) => tag !== ""),
          ),
    ]);
    if (rows.rowCount === 0) {
      throw notFound(`No memory "${id}"`);
    }
    res.json(rows.rows[0]);
  }),
);

memoriesRouter.delete(
  "/memory",
  route(async (req, res) => {
    const id = requireQuery(req, "id");
    const rows = await dbPool.query(sql.DROP_MEMORY, [id]);
    if (rows.rowCount === 0) {
      throw notFound(`No memory "${id}". Nothing was deleted.`);
    }
    res.json(rows.rows[0]);
  }),
);
