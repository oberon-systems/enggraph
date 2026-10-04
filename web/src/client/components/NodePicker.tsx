import { useId } from "react";

import { query } from "../api.js";
import { useApi, useDebounced } from "../hooks/useApi.js";
import type { NodeRow, Page } from "../types.js";

const ROOT_ID = "./";
const MATCHES = 20;
const SUMMARY_CHARS = 60;

/** A node id typed by hand or picked from the nodes of a project matching it. */
export function NodePicker({
  project,
  value,
  onChange,
}: {
  project: string;
  value: string;
  onChange: (id: string) => void;
}) {
  const listId = useId();
  const typed = useDebounced(value.trim());
  const path =
    project === "" || typed === "" || typed === ROOT_ID
      ? null
      : `/projects/${encodeURIComponent(project)}/nodes${query({
          q: typed,
          limit: MATCHES,
        })}`;
  const matches = useApi<Page<NodeRow>>(path);

  return (
    <>
      <input
        list={listId}
        placeholder="node (default ./, the whole tree)"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id={listId}>
        <option value={ROOT_ID}>the whole tree</option>
        {(matches.data?.items ?? []).map((node) => (
          <option key={node.id} value={node.id}>
            {node.type}
            {node.summary ? `: ${node.summary.slice(0, SUMMARY_CHARS)}` : ""}
          </option>
        ))}
      </datalist>
    </>
  );
}
