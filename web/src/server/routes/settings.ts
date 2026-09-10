import { Router } from "express";

import { readBodyString, route } from "../args.js";
import { passOn, upstream } from "../content.js";
import { dbPool } from "../db.js";
import { readFeature, redactKeys, requireFeature } from "../features.js";
import { INDEXING_KEY, readIndexing } from "../indexing.js";
import * as sql from "../queries.js";

// The built-in project the global defaults hang off, as migration 0012
// creates it and enggraph.config.SETTINGS_PROJECT names it. Its one row is
// what every project falls back to when neither it nor one of its directories
// has said what to index.
const SETTINGS_PROJECT = "_settings";

export const settingsRouter = Router();

settingsRouter.get(
  "/settings",
  route(async (_req, res) => {
    const row = await dbPool.query<{ settings: unknown }>(
      sql.PROJECT_LEVEL_SETTINGS,
      [SETTINGS_PROJECT],
    );
    const level = row.rows[0];
    res.json(
      level === undefined
        ? {
            ctxkeep: null,
            ctxignore: null,
            settings: {},
            updated_at: null,
          }
        : { ...level, settings: redactKeys(level.settings) },
    );
  }),
);

settingsRouter.put(
  "/settings",
  route(async (req, res) => {
    const body = req.body as Record<string, unknown> | undefined;
    // Empty is NULL rather than an empty string, for the same reason a
    // project's own row treats it so: a level that says nothing has to be
    // indistinguishable from one that is not there.
    const keep = readBodyString(body, "ctxkeep")?.trim();
    const ignore = readBodyString(body, "ctxignore")?.trim();
    const saved = await dbPool.query(sql.SAVE_SETTINGS, [
      SETTINGS_PROJECT,
      keep === undefined || keep === "" ? null : `${keep}\n`,
      ignore === undefined || ignore === "" ? null : `${ignore}\n`,
    ]);
    res.json(saved.rows[0]);
  }),
);

// The schedule every project falls back to. Stored beside the selection
// documents rather than in a column of its own, so the knobs that follow are
// keys of one object instead of a migration each.
settingsRouter.put(
  "/settings/indexing",
  route(async (req, res) => {
    const value = readIndexing(req.body, true);
    const saved =
      value === null
        ? await dbPool.query(sql.CLEAR_SETTINGS_KEY, [
            SETTINGS_PROJECT,
            INDEXING_KEY,
          ])
        : await dbPool.query(sql.SAVE_SETTINGS_KEY, [
            SETTINGS_PROJECT,
            JSON.stringify({ [INDEXING_KEY]: value }),
          ]);
    res.json(saved.rows[0] ?? { project: SETTINGS_PROJECT });
  }),
);

// The global switch for a background feature, and the server it talks to.
// Off here is off everywhere: enggraph.features reads this level first and
// answers from it, so a project that says otherwise is not asked. That is
// what makes it safe to shut the model down - one field, not one per project.
settingsRouter.put(
  "/settings/features/:feature",
  route(async (req, res) => {
    const feature = requireFeature(req.params.feature);
    const value = readFeature(req.body, true);
    const saved =
      value === null
        ? await dbPool.query(sql.CLEAR_SETTINGS_KEY, [
            SETTINGS_PROJECT,
            feature,
          ])
        : await dbPool.query(sql.MERGE_SETTINGS_KEY, [
            SETTINGS_PROJECT,
            feature,
            JSON.stringify(value),
          ]);
    res.json(saved.rows[0] ?? { project: SETTINGS_PROJECT });
  }),
);

// What the embedding queue amounts to, per project: how much has vectors, how
// deep the queue is, and which server answered. The API owns all three - it
// holds the mounts, the queue and the model - so this is a pass-through.
settingsRouter.get(
  "/embeddings",
  route(async (_req, res) => {
    const state = await upstream<unknown>("GET", "/embeddings").catch(passOn);
    res.json(state);
  }),
);

// Test a chat server before it is stored. Its own endpoint rather than the
// embedding one: a model that completes a sentence and a model that returns a
// vector are two servers, and dialling the wrong route proves nothing.
settingsRouter.post(
  "/summaries/probe",
  route(async (req, res) => {
    const body = req.body as Record<string, unknown> | undefined;
    const answer = await upstream<unknown>(
      "POST",
      "/summaries/probe",
      {},
      {
        url: readBodyString(body, "url") ?? "",
      },
    ).catch(passOn);
    res.json(answer);
  }),
);

// Test a server URL before it is stored. A URL typed wrong is otherwise found
// out by a queue that quietly stops, which is the failure this prevents.
settingsRouter.post(
  "/embeddings/probe",
  route(async (req, res) => {
    const body = req.body as Record<string, unknown> | undefined;
    const answer = await upstream<unknown>(
      "POST",
      "/embeddings/probe",
      {},
      {
        url: readBodyString(body, "url") ?? "",
      },
    ).catch(passOn);
    res.json(answer);
  }),
);

// What the summarizing queue amounts to, per project: how much the model has
// described, how deep the queue is, and whether anything is pushing it. The
// pair of /embeddings above, and a pass-through for the same reason.
settingsRouter.get(
  "/summaries",
  route(async (_req, res) => {
    const state = await upstream<unknown>("GET", "/summaries").catch(passOn);
    res.json(state);
  }),
);

// What the global level actually comes to, feature by feature, as the API
// resolves it. The settings page shows these as the placeholder in every
// field: a box that says "default" tells a reader nothing, and the number in
// force is the only useful thing to put there.
settingsRouter.get(
  "/settings/features",
  route(async (_req, res) => {
    const settled = await upstream<unknown>(
      "GET",
      "/projects/_settings/features",
    ).catch(passOn);
    res.json(settled);
  }),
);

// Put the files that gave up back in a queue. Asked for by hand: a task fails
// when its attempts run out, and whether that was the file or the server is
// not something the queue can know.
for (const queue of ["embeddings", "summaries"] as const) {
  settingsRouter.post(
    `/${queue}/retry`,
    route(async (req, res) => {
      const body = req.body as Record<string, unknown> | undefined;
      const answer = await upstream<unknown>(
        "POST",
        `/${queue}/retry`,
        {},
        { project: readBodyString(body, "project") ?? "" },
      ).catch(passOn);
      res.json(answer);
    }),
  );
}
