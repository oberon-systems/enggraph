import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";

import { badRequest, HttpError } from "./args.js";

export const MCP_URL = process.env.MCP_URL ?? "http://mcp-server:3000";

const CALL_TIMEOUT_MS = 180_000;

const ASK_GROUPS: [string, string[]][] = [
  ["Context", ["get_context", "search_code", "search_text", "get_overview"]],
  [
    "Graph",
    [
      "search_code_nodes",
      "get_code_graph_neighbors",
      "get_node_summary",
      "shortest_path",
    ],
  ],
  [
    "Symbols",
    [
      "find_definition",
      "find_callers",
      "find_callees",
      "find_references",
      "find_implementations",
      "find_tests",
      "impact_analysis",
    ],
  ],
  [
    "Project",
    [
      "describe_project",
      "get_project_links",
      "find_linked_name",
      "list_projects",
      "list_indexed_files",
    ],
  ],
  ["Records", ["get_plans", "get_memory", "get_suggestions"]],
];

const OFFERED = new Set(ASK_GROUPS.flatMap(([, names]) => names));

export type AskTool = {
  name: string;
  group: string;
  description: string;
  inputSchema: unknown;
};

export type AskBlock = { type: string; text: string };

export type AskResult = {
  content: AskBlock[];
  is_error: boolean;
  ms: number;
  chars: number;
};

type Session = { client: Client; transport: StreamableHTTPClientTransport };

const sessions = new Map<string, Promise<Session>>();

async function connect(project: string): Promise<Session> {
  const path = project === "" ? "/mcp" : `/mcp/${encodeURIComponent(project)}`;
  const transport = new StreamableHTTPClientTransport(new URL(path, MCP_URL));
  const client = new Client({ name: "enggraph-dashboard", version: "1.0.0" });
  await client.connect(transport);
  return { client, transport };
}

function session(project: string): Promise<Session> {
  const held = sessions.get(project);
  if (held !== undefined) {
    return held;
  }
  const opening = connect(project);
  sessions.set(project, opening);
  opening.catch(() => {
    if (sessions.get(project) === opening) {
      sessions.delete(project);
    }
  });
  return opening;
}

function passOn(error: unknown): never {
  if (error instanceof McpError) {
    const timedOut = error.code === (ErrorCode.RequestTimeout as number);
    throw new HttpError(timedOut ? 504 : 502, error.message);
  }
  const reason = error instanceof Error ? error.message : String(error);
  throw new HttpError(
    502,
    `the MCP server at ${MCP_URL} did not answer: ${reason}`,
  );
}

// One session per project, as an agent keeps one. Anything but an MCP error
// means the session is gone (a restarted server), so it is opened once more.
async function withSession<T>(
  project: string,
  run: (client: Client) => Promise<T>,
): Promise<T> {
  for (const lastTry of [false, true]) {
    const held = session(project);
    try {
      return await run((await held).client);
    } catch (error) {
      if (error instanceof McpError || lastTry) {
        passOn(error);
      }
      if (sessions.get(project) === held) {
        sessions.delete(project);
      }
    }
  }
  throw new HttpError(502, `the MCP server at ${MCP_URL} did not answer`);
}

export async function listTools(project: string): Promise<AskTool[]> {
  const { tools } = await withSession(project, (client) => client.listTools());
  const byName = new Map(tools.map((tool) => [tool.name, tool]));
  return ASK_GROUPS.flatMap(([group, names]) =>
    names.flatMap((name) => {
      const tool = byName.get(name);
      return tool === undefined
        ? []
        : [
            {
              name,
              group,
              description: tool.description ?? "",
              inputSchema: tool.inputSchema,
            },
          ];
    }),
  );
}

function blocks(content: unknown): AskBlock[] {
  if (!Array.isArray(content)) {
    return [];
  }
  return content.map((block: unknown) => {
    const { type, text } = block as { type?: unknown; text?: unknown };
    return typeof text === "string"
      ? { type: "text", text }
      : { type: String(type), text: JSON.stringify(block) };
  });
}

export async function callTool(
  project: string,
  name: string,
  args: Record<string, unknown>,
): Promise<AskResult> {
  if (!OFFERED.has(name)) {
    throw badRequest(`Tool "${name}" is not offered here`);
  }
  const started = Date.now();
  const result = await withSession(project, (client) =>
    client.callTool({ name, arguments: args }, undefined, {
      timeout: CALL_TIMEOUT_MS,
    }),
  );
  const content = blocks(result.content);
  return {
    content,
    is_error: result.isError === true,
    ms: Date.now() - started,
    chars: content.reduce((sum, block) => sum + block.text.length, 0),
  };
}

// The links tab reads and writes through the MCP server, which owns the rules
// a name is normalized and a relation is checked by.
const LINK_TOOLS = new Set([
  "get_project_links",
  "save_project_link",
  "drop_project_link",
  "save_project_export",
  "drop_project_export",
  "trace",
]);

export async function callLinkTool(
  project: string,
  name: string,
  args: Record<string, unknown>,
): Promise<string> {
  if (!LINK_TOOLS.has(name)) {
    throw badRequest(`Tool "${name}" is not a link tool`);
  }
  const result = await withSession(project, (client) =>
    client.callTool({ name, arguments: args }, undefined, {
      timeout: CALL_TIMEOUT_MS,
    }),
  );
  // The first call of a session gets the skill check appended as a later block.
  const text = blocks(result.content)[0]?.text ?? "";
  if (result.isError === true) {
    throw badRequest(text);
  }
  return text;
}

export async function closeSessions(): Promise<void> {
  const held = [...sessions.values()];
  sessions.clear();
  await Promise.allSettled(
    held.map(async (opening) => {
      const { client, transport } = await opening;
      await transport.terminateSession().catch(() => undefined);
      await client.close();
    }),
  );
}
