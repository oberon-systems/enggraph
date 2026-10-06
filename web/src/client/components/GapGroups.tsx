import { useState } from "react";
import { Link } from "react-router";

import { query } from "../api.js";
import { Empty, ErrorBox } from "./Common.js";
import { useApi } from "../hooks/useApi.js";
import type { GapGroup } from "../types.js";

const GROUPS = ["kind", "lever", "about", "directory"];

/** Where the gaps pile up: by kind, lever, scope, or the code they name. */
export function GapGroups({ status }: { status: string | null }) {
  const [by, setBy] = useState("kind");
  const groups = useApi<{ by: string; groups: GapGroup[] }>(
    `/suggestions/groups${query({ by, status })}`,
  );

  return (
    <section>
      <div className="row">
        <h2>Where the gaps are</h2>
        <label>
          Group by
          <select value={by} onChange={(event) => setBy(event.target.value)}>
            {GROUPS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      {groups.error !== null && <ErrorBox message={groups.error} />}
      {groups.data !== null && groups.data.groups.length === 0 && (
        <Empty>
          {by === "directory"
            ? "No suggestion names a node yet."
            : "No suggestions match."}
        </Empty>
      )}
      {groups.data !== null && groups.data.groups.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>{by}</th>
              <th>Records</th>
              <th>Hits</th>
              <th>Most hit</th>
            </tr>
          </thead>
          <tbody>
            {groups.data.groups.map((group) => (
              <tr key={`${group.project ?? ""}:${group.key}`}>
                <td>
                  <code>
                    {group.project !== null ? `${group.project}:` : ""}
                    {group.key}
                  </code>
                </td>
                <td>{group.records}</td>
                <td>{group.hits}</td>
                <td>
                  {group.top.map((gap, index) => (
                    <span key={gap.id}>
                      {index > 0 ? ", " : ""}
                      <Link to={`/suggestions/${encodeURIComponent(gap.id)}`}>
                        {gap.title}
                      </Link>{" "}
                      <span className="muted">({gap.hits})</span>
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
