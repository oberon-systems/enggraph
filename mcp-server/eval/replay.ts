import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const RESULTS_DIR = resolve(import.meta.dirname, "../../eval/results");
const SEARCH_LIMIT = 10;
const CONTEXT_BUDGET = 4000;
const SUGGESTIONS_READ = 50;

interface Node {
  project: string;
  node_id: string;
}

interface Gap {
  id: string;
  title: string;
  status: string;
  queries: string[];
  nodes: (Node & { missing: boolean })[];
}

interface Hit {
  project?: string;
  id: string;
  file_path: string | null;
}

interface Replayed {
  suggestion: string;
  status: string;
  query: string;
  expect: string[];
  rank: number | null;
  in_context: boolean;
}

// The first block is the answer; a session's first call appends a skill check.
function answer<T>(result: unknown): T {
  const content =
    (result as { content?: { type: string; text?: string }[] }).content ?? [];
  return JSON.parse(content[0]?.text ?? "null") as T;
}

/** Whether a result is the expected node, its file, or inside its directory. */
function reaches(hit: Hit, node: Node, single: boolean): boolean {
  if (!single && hit.project !== undefined && hit.project !== node.project) {
    return false;
  }
  const file = node.node_id.split("::", 1)[0];
  if (hit.id === node.node_id || hit.file_path === file) {
    return true;
  }
  return (
    node.node_id.endsWith("/") &&
    (hit.id.startsWith(node.node_id) ||
      (hit.file_path ?? "").startsWith(node.node_id))
  );
}

async function replay(
  client: Client,
  gap: Gap,
  query: string,
): Promise<Replayed> {
  const nodes = gap.nodes.filter((node) => !node.missing);
  const projects = [...new Set(nodes.map((node) => node.project))];
  const single = projects.length === 1;
  const project = single ? projects[0] : "*";
  const found = answer<Hit[]>(
    await client.callTool({
      name: "search_code",
      arguments: { query, project, limit: SEARCH_LIMIT },
    }),
  );
  const index = found.findIndex((hit) =>
    nodes.some((node) => reaches(hit, node, single)),
  );
  const packet = answer<{ entries: Hit[] }>(
    await client.callTool({
      name: "get_context",
      arguments: {
        query,
        project,
        token_budget: CONTEXT_BUDGET,
        include_chunks: false,
      },
    }),
  );
  return {
    suggestion: gap.id,
    status: gap.status,
    query,
    expect: nodes.map((node) => `${node.project}:${node.node_id}`),
    rank: index === -1 ? null : index + 1,
    in_context: packet.entries.some((entry) =>
      nodes.some((node) => reaches(entry, node, single)),
    ),
  };
}

function rate(rows: Replayed[], test: (row: Replayed) => boolean): number {
  return rows.length === 0
    ? 0
    : Number((rows.filter(test).length / rows.length).toFixed(4));
}

async function main(): Promise<number> {
  const url = process.env.REPLAY_MCP_URL;
  if (url === undefined || url === "") {
    console.error("REPLAY_MCP_URL is not set; run it through `make replay`");
    return 2;
  }
  const client = new Client({ name: "enggraph-replay", version: "1.0.0" });
  await client.connect(
    new StreamableHTTPClientTransport(new URL(`${url}/mcp`)),
  );
  const rows: Replayed[] = [];
  let read = 0;
  try {
    const gaps = answer<Gap[]>(
      await client.callTool({
        name: "get_suggestions",
        arguments: { about: "*", status: "*", limit: SUGGESTIONS_READ },
      }),
    );
    read = gaps.length;
    for (const gap of gaps) {
      if (gap.queries.length === 0 || gap.nodes.every((one) => one.missing)) {
        continue;
      }
      for (const query of gap.queries) {
        rows.push(await replay(client, gap, query));
      }
    }
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 1;
  } finally {
    await client.close();
  }

  const resolved = rows.filter((row) => row.status === "resolved");
  const summary = {
    suggestions_read: read,
    queries: rows.length,
    hit_at_5: rate(rows, (row) => row.rank !== null && row.rank <= 5),
    hit_at_10: rate(rows, (row) => row.rank !== null),
    in_context: rate(rows, (row) => row.in_context),
    resolved_queries: resolved.length,
    resolved_hit_at_5: rate(
      resolved,
      (row) => row.rank !== null && row.rank <= 5,
    ),
  };
  mkdirSync(RESULTS_DIR, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const out = resolve(RESULTS_DIR, `replay-${stamp}.json`);
  writeFileSync(out, JSON.stringify({ summary, rows }, null, 2) + "\n");

  console.log(JSON.stringify(summary, null, 2));
  for (const row of rows.filter((one) => one.rank === null)) {
    console.log(`miss  ${row.suggestion}: ${row.query}`);
  }
  console.log(`written to ${out}`);
  if (read === SUGGESTIONS_READ) {
    console.log(`only the ${SUGGESTIONS_READ} most hit suggestions were read`);
  }
  return 0;
}

main().then(
  (code) => process.exit(code),
  (error: unknown) => {
    console.error(error);
    process.exit(2);
  },
);
