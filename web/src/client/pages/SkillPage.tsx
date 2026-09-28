import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { query, remove } from "../api.js";
import { ErrorBox, Spinner } from "../components/Common.js";
import { ConfirmModal } from "../components/ConfirmModal.js";
import { useApi } from "../hooks/useApi.js";
import type { Skill } from "../types.js";

export function SkillPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const skill = useApi<Skill>(`/skill${query({ id })}`);
  const [dropping, setDropping] = useState(false);

  if (skill.error !== null) {
    return <ErrorBox message={skill.error} />;
  }
  if (skill.data === null) {
    return <Spinner what="the skill" />;
  }

  const row = skill.data;

  return (
    <>
      <p>
        <Link to="/skills">Back to the skills</Link>
      </p>
      <h1>{row.name}</h1>
      <dl className="inline">
        <dt>Owner</dt>
        <dd>{row.owner ?? "global"}</dd>
        <dt>Source</dt>
        <dd>{row.source === "repo" ? "built-in, read-only" : "imported"}</dd>
        <dt>Version</dt>
        <dd className="id">{row.sha256}</dd>
        <dt>Updated</dt>
        <dd>{row.updated_at}</dd>
      </dl>

      {row.source === "import" && (
        <div className="row">
          <button
            type="button"
            className="danger"
            onClick={() => setDropping(true)}
          >
            Delete
          </button>
        </div>
      )}

      <pre className="source">{row.content}</pre>

      {dropping && (
        <ConfirmModal
          danger
          title={`Delete ${row.name}?`}
          confirmLabel="Delete it"
          onConfirm={async () => {
            await remove(`/skill${query({ id })}`);
            void navigate("/skills");
          }}
          onClose={() => setDropping(false)}
        >
          <p>
            Agents stop being offered it. Copies already installed stay where
            they are.
          </p>
        </ConfirmModal>
      )}
    </>
  );
}
