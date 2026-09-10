import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { patch, query, remove } from "../api.js";
import { ErrorBox, Markdown, Spinner } from "../components/Common.js";
import { ConfirmModal } from "../components/ConfirmModal.js";
import { useApi } from "../hooks/useApi.js";
import type { Memory } from "../types.js";

/** One memory, as written and as corrected.
 *
 * The scope and the slug are the node id together, so neither is edited here:
 * moving a memory between projects is a different operation from correcting
 * one, and the page says so rather than offering a field that would leave two.
 */
export function MemoryPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const memory = useApi<Memory>(`/memory${query({ id })}`);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState("");
  const [dropping, setDropping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (memory.data !== null) {
      setDraft(memory.data.text);
      setTitle(memory.data.title);
      setSummary(memory.data.summary ?? "");
      setTags(memory.data.tags.join(", "));
    }
  }, [memory.data]);

  async function apply(body: Record<string, string>) {
    try {
      await patch(`/memory${query({ id })}`, body);
      setEditing(false);
      setError(null);
      memory.reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function drop() {
    await remove(`/memory${query({ id })}`);
    void navigate("/memories");
  }

  if (memory.error !== null) {
    return <ErrorBox message={memory.error} />;
  }
  if (memory.data === null) {
    return <Spinner what="the memory" />;
  }

  const row = memory.data;

  return (
    <>
      <p>
        <Link to="/memories">Back to the memories</Link>
      </p>
      <h1>{row.title}</h1>
      <p className="muted id">{row.id}</p>
      <dl className="inline">
        <dt>About</dt>
        <dd>{row.about ?? "global"}</dd>
        <dt>Tags</dt>
        <dd>{row.tags.length === 0 ? "-" : row.tags.join(", ")}</dd>
        <dt>Written</dt>
        <dd>{row.created_at}</dd>
        <dt>Updated</dt>
        <dd>{row.updated_at ?? "-"}</dd>
      </dl>
      <p className="muted">
        The scope and the slug are this memory&apos;s identity. To file it
        against another project, delete it here and let the agent write it again
        where it belongs.
      </p>

      {error !== null && <ErrorBox message={error} />}

      <div className="row">
        <button type="button" onClick={() => setEditing(!editing)}>
          {editing ? "Preview" : "Edit"}
        </button>
        {editing && (
          <button
            type="button"
            onClick={() => void apply({ title, summary, tags, text: draft })}
          >
            Save
          </button>
        )}
        <button
          type="button"
          className="danger"
          onClick={() => setDropping(true)}
        >
          Delete
        </button>
      </div>

      {editing ? (
        <>
          <div className="filters">
            <label>
              Title
              <input
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <label>
              Gist
              <input
                type="text"
                value={summary}
                onChange={(event) => setSummary(event.target.value)}
              />
            </label>
            <label>
              Tags, comma separated
              <input
                type="text"
                value={tags}
                onChange={(event) => setTags(event.target.value)}
              />
            </label>
          </div>
          <textarea
            className="editor"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </>
      ) : (
        <Markdown text={row.text} />
      )}

      {dropping && (
        <ConfirmModal
          danger
          title={`Delete ${row.title}?`}
          confirmLabel="Delete it"
          onConfirm={drop}
          onClose={() => setDropping(false)}
        >
          <p>
            Nothing rebuilds a memory: it was written from what an agent worked
            out, and no index run will put it back.
          </p>
        </ConfirmModal>
      )}
    </>
  );
}
