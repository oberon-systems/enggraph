import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { patch, query, remove } from "../api.js";
import { ErrorBox, Markdown, Spinner } from "../components/Common.js";
import { ConfirmModal } from "../components/ConfirmModal.js";
import { RecordNodes } from "../components/RecordNodes.js";
import { Picker, projectEntries } from "../components/Picker.js";
import { useApi } from "../hooks/useApi.js";
import type { Plan, PlanFacets } from "../types.js";

const GLOBAL_LABEL = "global, listed under every project";

export function PlanPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const plan = useApi<Plan>(`/plan${query({ id })}`);
  const facets = useApi<PlanFacets>("/plans/facets");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [moving, setMoving] = useState<string | null>(null);

  useEffect(() => {
    if (plan.data !== null) {
      setDraft(plan.data.content);
    }
  }, [plan.data]);

  async function save() {
    try {
      await patch(`/plan${query({ id })}`, { content: draft });
      setEditing(false);
      setError(null);
      plan.reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function drop() {
    try {
      await remove(`/plan${query({ id })}`);
      void navigate("/plans");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  if (plan.error !== null) {
    return <ErrorBox message={plan.error} />;
  }
  if (plan.data === null) {
    return <Spinner what="the plan" />;
  }

  const current = plan.data.project;
  const from = current ?? "global";
  const to = moving === "" ? "global" : moving;

  return (
    <>
      <p>
        <Link to="/plans">Back to the plans</Link>
      </p>
      <h1>{plan.data.title}</h1>
      <p className="muted id">{plan.data.id}</p>
      <dl className="inline">
        <dt>Project</dt>
        <dd>
          <Picker
            value={current ?? ""}
            onChange={setMoving}
            entries={[
              { value: "", label: GLOBAL_LABEL },
              ...projectEntries(
                facets.data?.targets ?? [],
                current === null ? [] : [current],
              ),
            ]}
          />
        </dd>
        <dt>Type</dt>
        <dd>{plan.data.type}</dd>
        <dt>Status</dt>
        <dd>{plan.data.status}</dd>
        <dt>Updated</dt>
        <dd>{new Date(plan.data.updated_at).toLocaleString("en-GB")}</dd>
      </dl>

      {error !== null && <ErrorBox message={error} />}

      <div className="row">
        <button type="button" onClick={() => setEditing(!editing)}>
          {editing ? "Preview" : "Edit"}
        </button>
        {editing && (
          <button type="button" onClick={() => void save()}>
            Save
          </button>
        )}
        <button type="button" className="danger" onClick={() => void drop()}>
          Delete
        </button>
      </div>

      {editing ? (
        <textarea
          className="editor"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
      ) : (
        <Markdown text={plan.data.content} />
      )}

      <RecordNodes kind="plan" id={plan.data.id} about={plan.data.project} />

      {moving !== null && (
        <ConfirmModal
          title={`Move ${plan.data.id} to ${to}`}
          confirmLabel="Move it"
          onClose={() => setMoving(null)}
          onConfirm={async () => {
            await patch(`/plan${query({ id })}`, { project: moving });
            setMoving(null);
            plan.reload();
          }}
        >
          <p>
            The plan moves from {from} to {to}. Its id, title, content, type and
            status stay as they are.
          </p>
          <ul>
            <li>
              {current === null
                ? "It stops being listed under every project."
                : `get_plans for ${current} stops listing it.`}
            </li>
            <li>
              {moving === ""
                ? "It is listed under every project, as a global plan."
                : `get_plans for ${moving} starts listing it.`}
            </li>
          </ul>
        </ConfirmModal>
      )}
    </>
  );
}
