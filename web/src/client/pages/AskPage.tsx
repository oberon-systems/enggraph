import { useState } from "react";
import { useSearchParams } from "react-router";

import { post, query } from "../api.js";
import { CopyButton, ErrorBox, Spinner } from "../components/Common.js";
import { Picker, projectEntries } from "../components/Picker.js";
import { useApi } from "../hooks/useApi.js";
import type { AskProperty, AskResult, AskTool, PlanFacets } from "../types.js";

const DEFAULT_TOOL = "get_context";
const CHARS_PER_TOKEN = 4;

type Answer = {
  sent: { name: string; arguments: Record<string, unknown> };
  result: AskResult;
};

function isNumber(property: AskProperty): boolean {
  return property.type === "number" || property.type === "integer";
}

function isList(property: AskProperty): boolean {
  return property.type === "array" && property.items?.type === "string";
}

function isJson(property: AskProperty): boolean {
  return (
    property.type === "object" ||
    (property.type === "array" && !isList(property))
  );
}

function readValue(name: string, property: AskProperty, raw: string): unknown {
  if (isNumber(property)) {
    const value = Number(raw);
    if (Number.isNaN(value)) {
      throw new Error(`"${name}" must be a number`);
    }
    return value;
  }
  if (property.type === "boolean") {
    return raw === "true";
  }
  if (isList(property)) {
    return raw
      .split(",")
      .map((entry) => entry.trim())
      .filter((entry) => entry !== "");
  }
  if (isJson(property)) {
    try {
      return JSON.parse(raw) as unknown;
    } catch {
      throw new Error(`"${name}" must be valid JSON`);
    }
  }
  return raw;
}

function readArguments(
  tool: AskTool,
  values: Record<string, string>,
): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  for (const [name, property] of Object.entries(
    tool.inputSchema.properties ?? {},
  )) {
    const raw = values[name] ?? "";
    if (raw.trim() !== "") {
      args[name] = readValue(name, property, raw);
    }
  }
  return args;
}

function pretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function AskPage() {
  const [params, setParams] = useSearchParams();
  const [values, setValues] = useState<Record<string, string>>({});
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const project = params.get("project") ?? "";
  const picked = params.get("tool") ?? DEFAULT_TOOL;

  const facets = useApi<PlanFacets>("/plans/facets");
  const listing = useApi<{ tools: AskTool[] }>(
    `/ask/tools${query({ project })}`,
  );
  const tools = listing.data?.tools ?? [];
  const tool = tools.find((entry) => entry.name === picked) ?? null;
  const properties = Object.entries(tool?.inputSchema.properties ?? {});
  const required = tool?.inputSchema.required ?? [];
  const ready =
    tool !== null &&
    !running &&
    required.every((name) => (values[name] ?? "").trim() !== "");

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value === "") {
      next.delete(key);
    } else {
      next.set(key, value);
    }
    setParams(next, { replace: true });
  }

  function setValue(name: string, value: string) {
    setValues((held) => ({ ...held, [name]: value }));
  }

  async function run() {
    if (tool === null) {
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const sent = { name: tool.name, arguments: readArguments(tool, values) };
      const result = await post<AskResult>("/ask", {
        project,
        tool: sent.name,
        arguments: sent.arguments,
      });
      setAnswer({ sent, result });
    } catch (reason: unknown) {
      setAnswer(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRunning(false);
    }
  }

  return (
    <>
      <h1>Ask</h1>
      <p className="muted">
        Call a tool of the MCP server the way an agent does, and read exactly
        what it answers.
      </p>

      <div className="filters">
        <label>
          Session project
          <Picker
            value={project}
            onChange={(value) => setParam("project", value)}
            entries={[
              { value: "", label: "none, the bare /mcp address" },
              ...projectEntries(facets.data?.targets ?? []),
            ]}
          />
        </label>
        <label>
          Tool
          <Picker
            value={picked}
            onChange={(value) => setParam("tool", value)}
            entries={tools.map((entry) => ({
              value: entry.name,
              label: entry.name,
              group: entry.group,
            }))}
          />
        </label>
      </div>

      {listing.error !== null && (
        <ErrorBox message={listing.error} what="the tool list" />
      )}
      {listing.loading && listing.data === null && <Spinner what="tools" />}

      {tool !== null && (
        <div
          className="ask-form"
          onKeyDown={(event) => {
            if (event.key === "Enter" && event.ctrlKey && ready) {
              void run();
            }
          }}
        >
          <p className="ask-description">{tool.description}</p>
          {properties.map(([name, property]) => (
            <label key={name} className="ask-field">
              <span>
                <code>{name}</code>
                {required.includes(name) && " (required)"}
              </span>
              <Field
                name={name}
                property={property}
                value={values[name] ?? ""}
                onChange={(value) => setValue(name, value)}
              />
              <span className="muted">{property.description}</span>
            </label>
          ))}
          <div className="row">
            <button type="button" disabled={!ready} onClick={() => void run()}>
              {running ? "Running..." : "Run"}
            </button>
            <span className="muted">Ctrl+Enter</span>
          </div>
        </div>
      )}

      {error !== null && <ErrorBox message={error} what="the tool call" />}
      {answer !== null && <AnswerView answer={answer} />}
    </>
  );
}

function Field({
  name,
  property,
  value,
  onChange,
}: {
  name: string;
  property: AskProperty;
  value: string;
  onChange: (value: string) => void;
}) {
  const options =
    property.type === "boolean" ? ["true", "false"] : property.enum;
  if (options !== undefined) {
    return (
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">default</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }
  if (name === "query" || isJson(property)) {
    return (
      <textarea
        rows={3}
        placeholder={isJson(property) ? "JSON" : ""}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <input
      type={isNumber(property) ? "number" : "text"}
      placeholder={isList(property) ? "comma separated" : ""}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function AnswerView({ answer }: { answer: Answer }) {
  const { sent, result } = answer;
  const tokens = Math.ceil(result.chars / CHARS_PER_TOKEN);
  return (
    <>
      <h2>Answer</h2>
      <div className="row">
        {result.is_error && <span className="bad">tool error</span>}
        <span className="muted">
          {result.ms.toLocaleString("en-US")} ms,{" "}
          {result.chars.toLocaleString("en-US")} chars, about{" "}
          {tokens.toLocaleString("en-US")} tokens, {result.content.length}{" "}
          block(s)
        </span>
      </div>
      <h3>Request</h3>
      <pre className="source ask-block">{JSON.stringify(sent, null, 2)}</pre>
      {result.content.map((block, index) => (
        <div key={index}>
          <div className="row">
            <h3>
              Block {index + 1} of {result.content.length}, {block.type}
            </h3>
            <CopyButton text={block.text} />
          </div>
          <pre className="source ask-block">{pretty(block.text)}</pre>
        </div>
      ))}
    </>
  );
}
