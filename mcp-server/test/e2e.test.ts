import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const MCP_URL = process.env.EVAL_MCP_URL;
const PROJECT = "alpha";
const SCRATCH = "e2e-scratch";
// A module docstring gives this file a summary to read back and restore.
const FILE = "worker/reports/daily.py";
// Only the native parsers record a hash; graphifyy keeps a cache of its own.
const HASHED = "deploy/docker-compose.yml";

interface Case {
  tool: string;
  args: (state: Map<string, string>) => Record<string, unknown>;
  keep?: string;
}

// Ordered: each write is read back and undone, so the benchmark sees the same
// graph before and after this suite.
const CASES: Case[] = [
  { tool: "list_projects", args: () => ({}) },
  { tool: "describe_project", args: () => ({ project: PROJECT }) },
  { tool: "drop_project", args: () => ({ name: PROJECT }) },
  { tool: "list_indexed_files", args: () => ({}) },
  { tool: "search_code_nodes", args: () => ({ query: "AuthService" }) },
  { tool: "search_code", args: () => ({ query: "how are tokens signed" }) },
  {
    tool: "get_context",
    args: () => ({ query: "refund flow", token_budget: 2000 }),
  },
  { tool: "get_code_graph_neighbors", args: () => ({ node_id: FILE }) },
  {
    tool: "shortest_path",
    args: () => ({ source_id: "worker/reports/export.py", target_id: FILE }),
  },
  {
    tool: "get_node_summary",
    args: () => ({ node_id: FILE }),
    keep: "summary",
  },
  {
    tool: "save_node_summary",
    args: (state) => ({ node_id: FILE, summary: state.get("summary") ?? "" }),
  },
  {
    tool: "save_plan",
    args: () => ({ plan_id: SCRATCH, title: "e2e", content: "e2e" }),
  },
  { tool: "get_plans", args: () => ({}) },
  { tool: "drop_plan", args: () => ({ plan_id: SCRATCH }) },
  {
    tool: "save_memory",
    args: () => ({ memory_id: SCRATCH, title: "e2e", text: "e2e" }),
  },
  { tool: "get_memory", args: () => ({ memory_id: SCRATCH }) },
  { tool: "drop_memory", args: () => ({ memory_id: SCRATCH }) },
  {
    tool: "save_suggestion",
    args: () => ({ suggestion_id: SCRATCH, title: "e2e", detail: "e2e" }),
  },
  { tool: "get_suggestions", args: () => ({}) },
  { tool: "drop_suggestion", args: () => ({ suggestion_id: SCRATCH }) },
  { tool: "get_file_hash", args: () => ({ rel_path: HASHED }), keep: "hash" },
  { tool: "clear_file_hash", args: () => ({ rel_path: HASHED }) },
  {
    tool: "set_file_hash",
    args: (state) => ({ rel_path: HASHED, hash: state.get("hash") ?? "" }),
  },
];

function text(result: unknown): string {
  const content =
    (result as { content?: { type: string; text?: string }[] }).content ?? [];
  return content.map((part) => part.text ?? "").join("\n");
}

function extract(body: string, key: string): string {
  const rows = JSON.parse(body) as Record<string, unknown>[];
  const value = rows[0]?.[key];
  if (typeof value !== "string" || value === "") {
    throw new Error(`no ${key} to restore in ${body}`);
  }
  return value;
}

describe.skipIf(MCP_URL === undefined)(
  "every MCP tool against a live stack",
  () => {
    const client = new Client({ name: "enggraph-e2e", version: "1.0.0" });
    const state = new Map<string, string>();

    beforeAll(async () => {
      await client.connect(
        new StreamableHTTPClientTransport(new URL(`${MCP_URL}/mcp/${PROJECT}`)),
      );
    });

    afterAll(async () => {
      await client.close();
    });

    it("has a case for every listed tool", async () => {
      const { tools } = await client.listTools();
      const listed = tools.map((tool) => tool.name).sort();
      expect([...new Set(CASES.map((c) => c.tool))].sort()).toEqual(listed);
    });

    it.each(CASES.map((c) => [c.tool, c]))("%s", async (_tool, c) => {
      const result = await client.callTool({
        name: c.tool,
        arguments: c.args(state),
      });
      const body = text(result);
      expect(result.isError, body).not.toBe(true);
      expect(body.length).toBeGreaterThan(0);
      if (c.keep !== undefined) {
        state.set(c.keep, extract(body, c.keep));
      }
    });
  },
);
