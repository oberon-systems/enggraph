import { useState } from "react";
import { Link } from "react-router";

import { post, remove } from "../api.js";
import { Count, Empty, ErrorBox, Spinner } from "../components/Common.js";
import { useApi } from "../hooks/useApi.js";
import type { LinkEdge, ProjectLinks } from "../types.js";

const KINDS = [
  "image",
  "role",
  "npm",
  "composer",
  "pypi",
  "go",
  "cargo",
  "cmake",
  "vcpkg",
];
const RELATIONS = [
  "deploys_to",
  "documents",
  "depends_on",
  "calls",
  "generated_from",
  "implements",
];
const DEPTHS = [1, 2, 3];

function NodeLink({ project, id }: { project: string; id: string }) {
  return (
    <Link
      to={
        `/projects/${encodeURIComponent(project)}` +
        `?tab=nodes&node=${encodeURIComponent(id)}`
      }
    >
      <code>
        {project}:{id}
      </code>
    </Link>
  );
}

function EdgeRow({
  edge,
  onDrop,
}: {
  edge: LinkEdge;
  onDrop:
    | ((edge: LinkEdge, sample: LinkEdge["samples"][number]) => void)
    | null;
}) {
  return (
    <tr>
      <td>
        <Link to={`/projects/${encodeURIComponent(edge.from)}?tab=links`}>
          {edge.from}
        </Link>
      </td>
      <td>
        {edge.relation}
        {edge.kind !== null && <span className="kind"> {edge.kind}</span>}
      </td>
      <td>
        <Link to={`/projects/${encodeURIComponent(edge.to)}?tab=links`}>
          {edge.to}
        </Link>
      </td>
      <td>
        <details>
          <summary>
            <Count value={edge.count} /> {edge.origin}
          </summary>
          <ul>
            {edge.samples.map((sample) => (
              <li key={`${sample.source_id}>${sample.target_id}`}>
                <NodeLink project={edge.from} id={sample.source_id} /> to{" "}
                <NodeLink project={edge.to} id={sample.target_id} />
                {sample.name !== null && (
                  <span className="muted"> by {sample.name}</span>
                )}
                {edge.origin === "declared" && onDrop !== null && (
                  <button type="button" onClick={() => onDrop(edge, sample)}>
                    Remove
                  </button>
                )}
              </li>
            ))}
          </ul>
        </details>
      </td>
    </tr>
  );
}

/** Which projects this one uses and is used by, and what it provides.
 *
 * An organization reads its members together and writes nothing: a relation
 * or an export belongs to the member it is about.
 */
export function LinksTab({
  project,
  organization,
}: {
  project: string;
  organization: boolean;
}) {
  const [depth, setDepth] = useState(1);
  const path = `/projects/${encodeURIComponent(project)}`;
  const links = useApi<ProjectLinks>(`${path}/links?depth=${depth}`);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [relation, setRelation] = useState(RELATIONS[0]);
  const [note, setNote] = useState("");
  const [kind, setKind] = useState(KINDS[0]);
  const [exported, setExported] = useState("");
  const [nodeId, setNodeId] = useState("");

  async function run(action: () => Promise<{ said: string }>) {
    setError(null);
    try {
      setSaid((await action()).said);
      links.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  if (links.error !== null) {
    return <ErrorBox message={links.error} />;
  }
  if (links.data === null) {
    return <Spinner what="the links" />;
  }
  const data = links.data;

  return (
    <>
      {error !== null && <ErrorBox message={error} />}
      {said !== null && <p className="muted">{said}</p>}

      <div className="row">
        <h2>Links</h2>
        <label>
          Projects away{" "}
          <select
            value={depth}
            onChange={(event) => setDepth(Number(event.target.value))}
          >
            {DEPTHS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      {data.edges.length === 0 ? (
        <Empty>
          No other project is linked to this one. A link appears when one
          project takes an image, a role or a package another one provides, or
          when a relation is declared below.
        </Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>From</th>
              <th>Relation</th>
              <th>To</th>
              <th>Node pairs</th>
            </tr>
          </thead>
          <tbody>
            {data.edges.map((edge) => (
              <EdgeRow
                key={`${edge.from}>${edge.to}:${edge.relation}:${edge.kind ?? ""}:${edge.origin}`}
                edge={edge}
                onDrop={
                  organization
                    ? null
                    : (dropped, sample) =>
                        void run(() =>
                          remove(`${path}/links`, {
                            from: dropped.from,
                            to: dropped.to,
                            relation: dropped.relation,
                            source_id: sample.source_id,
                            target_id: sample.target_id,
                          }),
                        )
                }
              />
            ))}
          </tbody>
        </table>
      )}

      {organization ? (
        <p className="muted">
          Links between members show here too. Declare a relation or a name a
          member provides on that member's own page.
        </p>
      ) : (
        <>
          <h2>Declare a relation</h2>
          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              void run(() =>
                post(`${path}/links`, {
                  to: target.trim(),
                  relation,
                  ...(note.trim() === "" ? {} : { note: note.trim() }),
                }),
              );
            }}
          >
            <span>{project}</span>
            <select
              value={relation}
              onChange={(event) => setRelation(event.target.value)}
            >
              {RELATIONS.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
            <input
              placeholder="target project"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
            />
            <input
              placeholder="why (optional)"
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
            <button type="submit" disabled={target.trim() === ""}>
              Save
            </button>
          </form>
        </>
      )}

      <h2>Provides</h2>
      {data.provides.length === 0 ? (
        <Empty>
          No manifest of this tree names anything another project could take.
        </Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Name</th>
              <th>Node</th>
              <th>Found by</th>
            </tr>
          </thead>
          <tbody>
            {data.provides.map((row) => (
              <tr key={`${row.project}:${row.kind}:${row.name}`}>
                <td>{row.kind}</td>
                <td>
                  <code>{row.name}</code>
                </td>
                <td>
                  <NodeLink project={row.project} id={row.node_id} />
                </td>
                <td>
                  {row.origin === "auto" ? (
                    "the index run"
                  ) : organization ? (
                    "hand"
                  ) : (
                    <>
                      hand{" "}
                      <button
                        type="button"
                        onClick={() =>
                          void run(() =>
                            remove(`${path}/exports`, {
                              kind: row.kind,
                              name: row.name,
                            }),
                          )
                        }
                      >
                        Remove
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!organization && (
        <>
          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              void run(() =>
                post(`${path}/exports`, {
                  kind,
                  name: exported.trim(),
                  ...(nodeId.trim() === "" ? {} : { node_id: nodeId.trim() }),
                }),
              );
            }}
          >
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value)}
            >
              {KINDS.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
            <input
              placeholder="name, as others write it"
              value={exported}
              onChange={(event) => setExported(event.target.value)}
            />
            <input
              placeholder="node (default ./)"
              value={nodeId}
              onChange={(event) => setNodeId(event.target.value)}
            />
            <button type="submit" disabled={exported.trim() === ""}>
              Add
            </button>
          </form>
          <p className="muted">
            For what no file states, such as an image a CI pipeline builds:
            every project taking the name is linked here from then on.
          </p>
        </>
      )}

      <h2>Not linked</h2>
      {data.unprovided.length === 0 && data.ambiguous.length === 0 ? (
        <Empty>Every name this project takes is linked.</Empty>
      ) : (
        <>
          {data.unprovided.map((row) => (
            <p key={`${row.project}:${row.kind}`}>
              {organization && <>{row.project}: </>}
              <span className="kind">{row.kind}</span>{" "}
              <Count value={row.count} /> taken, provided by no indexed project:{" "}
              {row.names.map((name) => (
                <span key={name} className="chip static">
                  {name}
                </span>
              ))}
              {row.count > row.names.length && " ..."}
            </p>
          ))}
          {data.ambiguous.map((row) => (
            <p key={`${row.project}:${row.kind}:${row.name}`}>
              {organization && <>{row.project}: </>}
              <span className="kind">{row.kind}</span> <code>{row.name}</code>{" "}
              is provided by {row.candidates.join(", ")}, so it is left unlinked
              rather than guessed.
            </p>
          ))}
        </>
      )}
    </>
  );
}
