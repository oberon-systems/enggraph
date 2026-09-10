import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { query } from "../api.js";
import { Empty, ErrorBox, Pager, Spinner } from "../components/Common.js";
import { useApi, useDebounced } from "../hooks/useApi.js";
import type { MemoryFacets, MemoryRow, Page } from "../types.js";

const PAGE = 50;
const ALL = "*";
const GLOBAL = "_global";

/** What the agents have written down, and what can still be corrected.
 *
 * A memory is what stays true after a task ends - a convention, a decision,
 * the reason something is the way it is. Nothing indexes into one, so a
 * re-index never prunes them: this page is the only way a wrong one goes.
 */
export function MemoriesPage() {
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const debounced = useDebounced(search);

  const about = params.get("about") ?? ALL;
  const tag = params.get("tag");
  const offset = Number(params.get("offset") ?? "0");

  const facets = useApi<MemoryFacets>("/memories/facets");
  const memories = useApi<Page<MemoryRow>>(
    `/memories${query({ about, tag, q: debounced, limit: PAGE, offset })}`,
  );

  function setParam(key: string, value: string | null) {
    const next = new URLSearchParams(params);
    if (value === null || value === "" || value === ALL) {
      next.delete(key);
    } else {
      next.set(key, value);
    }
    if (key !== "offset") {
      next.delete("offset");
    }
    setParams(next, { replace: key === "offset" });
  }

  return (
    <>
      <div className="row">
        <h1>Memories</h1>
      </div>
      <p className="muted">
        What an agent worked out and wrote down: a convention, a decision, the
        reason something is the way it is. Written through{" "}
        <code>save_memory</code>; corrected and retired here.
      </p>

      <div className="filters">
        <label>
          About
          <select
            value={about}
            onChange={(event) => setParam("about", event.target.value)}
          >
            <option value={ALL}>every project</option>
            <option value={GLOBAL}>
              global ({facets.data?.global_memories ?? 0})
            </option>
            {(facets.data?.abouts ?? []).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Tag
          <select
            value={tag ?? ""}
            onChange={(event) => setParam("tag", event.target.value)}
          >
            <option value="">any</option>
            {(facets.data?.tags ?? []).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <input
          className="search"
          placeholder="Search titles and text"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setParam("q", event.target.value);
          }}
        />
      </div>

      {memories.error !== null && <ErrorBox message={memories.error} />}
      {memories.loading && memories.data === null && (
        <Spinner what="memories" />
      )}
      {memories.data !== null && memories.data.items.length === 0 && (
        <Empty>
          No memory matches these filters. Agents write them with{" "}
          <code>save_memory</code>.
        </Empty>
      )}

      {memories.data !== null && memories.data.items.length > 0 && (
        <>
          <table className="grid">
            <thead>
              <tr>
                <th>Memory</th>
                <th>About</th>
                <th>Tags</th>
                <th title="characters of text">Size</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {memories.data.items.map((row) => (
                <tr key={row.id}>
                  <td>
                    <Link to={`/memories/${encodeURIComponent(row.id)}`}>
                      {row.title}
                    </Link>
                    <div className="muted id">{row.id}</div>
                  </td>
                  <td>
                    {row.about === null ? (
                      <span className="chip static">global</span>
                    ) : (
                      row.about
                    )}
                  </td>
                  <td>
                    {row.tags.length === 0 ? (
                      <span className="muted">-</span>
                    ) : (
                      row.tags.map((value) => (
                        <span key={value} className="chip static">
                          {value}
                        </span>
                      ))
                    )}
                  </td>
                  <td className="num">{row.text_length}</td>
                  <td className="muted">{row.updated_at ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager
            total={memories.data.total}
            limit={memories.data.limit}
            offset={memories.data.offset}
            onOffset={(value) => setParam("offset", String(value))}
          />
        </>
      )}
    </>
  );
}
