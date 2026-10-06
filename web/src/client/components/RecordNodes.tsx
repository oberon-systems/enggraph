import { useState } from "react";
import { Link } from "react-router";

import { post, query, remove } from "../api.js";
import { Empty, ErrorBox } from "./Common.js";
import { NodePicker } from "./NodePicker.js";
import { useApi } from "../hooks/useApi.js";
import type { NodeKnowledge, RecordKind, RecordNode } from "../types.js";

const RECORD_PAGES: Record<string, string> = {
  _memory: "/memories",
  _plans: "/plans",
  _suggestions: "/suggestions",
};

function nodeHref(project: string, id: string): string {
  return (
    `/projects/${encodeURIComponent(project)}` +
    `?tab=nodes&node=${encodeURIComponent(id)}`
  );
}

/** The code a memory, plan or suggestion is about, editable in place. */
export function RecordNodes({
  kind,
  id,
  about,
}: {
  kind: RecordKind;
  id: string;
  about: string | null;
}) {
  const path = `/records/${kind}/nodes${query({ id })}`;
  const nodes = useApi<RecordNode[]>(path);
  const [project, setProject] = useState(about ?? "");
  const [nodeId, setNodeId] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function act(run: () => Promise<unknown>) {
    try {
      await run();
      setError(null);
      nodes.reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <section>
      <h2>Code</h2>
      {nodes.error !== null && <ErrorBox message={nodes.error} />}
      {error !== null && <ErrorBox message={error} />}
      {nodes.data !== null && nodes.data.length === 0 && (
        <Empty>Not tied to any node yet.</Empty>
      )}
      {nodes.data !== null && nodes.data.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>Node</th>
              <th>Type</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {nodes.data.map((node) => (
              <tr key={`${node.project}:${node.node_id}`}>
                <td>
                  <Link to={nodeHref(node.project, node.node_id)}>
                    <code>
                      {node.project}:{node.node_id}
                    </code>
                  </Link>
                  {node.missing && (
                    <span className="muted"> gone from the graph</span>
                  )}
                </td>
                <td>{node.type ?? "-"}</td>
                <td>
                  <button
                    type="button"
                    className="danger"
                    onClick={() =>
                      void act(() =>
                        remove(
                          `/records/${kind}/nodes${query({
                            id,
                            project: node.project,
                            node_id: node.node_id,
                          })}`,
                        ),
                      )
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        <input
          placeholder="project"
          value={project}
          onChange={(event) => setProject(event.target.value)}
        />
        <NodePicker project={project} value={nodeId} onChange={setNodeId} />
        <button
          type="button"
          disabled={project.trim() === "" || nodeId.trim() === ""}
          onClick={() =>
            void act(async () => {
              await post(path, {
                project: project.trim(),
                node_id: nodeId.trim(),
              });
              setNodeId("");
            })
          }
        >
          Add node
        </button>
      </div>
    </section>
  );
}

/** What memories, plans and suggestions say about a node or its directories. */
export function NodeKnowledgeList({
  project,
  id,
}: {
  project: string;
  id: string;
}) {
  const known = useApi<NodeKnowledge[]>(
    `/projects/${encodeURIComponent(project)}/knowledge${query({ id })}`,
  );
  return (
    <>
      <h3>Knowledge</h3>
      {known.error !== null && <ErrorBox message={known.error} />}
      {known.data !== null && known.data.length === 0 && (
        <Empty>No memory, plan or suggestion is about this node.</Empty>
      )}
      {known.data !== null && known.data.length > 0 && (
        <ul>
          {known.data.map((record) => (
            <li key={`${record.record_project}:${record.record_id}`}>
              <Link
                to={
                  `${RECORD_PAGES[record.record_project] ?? "/memories"}/` +
                  encodeURIComponent(record.record_id)
                }
              >
                {record.title}
              </Link>{" "}
              <span className="muted">
                {record.type}
                {record.status !== null ? `, ${record.status}` : ""}
                {record.attached_to !== id ? `, via ${record.attached_to}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
