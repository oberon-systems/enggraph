import { useState } from "react";

import { put } from "../api.js";
import { ErrorBox, Spinner } from "../components/Common.js";
import { FeatureEditor, UnsavedNote } from "../components/FeatureFields.js";
import { IndexingEditor } from "../components/IndexingFields.js";
import { LAMP_REFRESH_MS, ServerLines } from "../components/StatusLamps.js";
import { useApi } from "../hooks/useApi.js";
import { unsaved, useDraft } from "../hooks/useDraft.js";
import type {
  EmbeddingsView,
  ProjectFeatures,
  SettingsLevel,
  SummariesView,
} from "../types.js";

/** The selection every project falls back to.
 *
 * Read last, after the directory and the project have both declined to say
 * what to index - and after a `.enggraph-keep` in the tree, which beats all three.
 * Left empty, the fallback is the built-in set of file types the parsers know.
 */
export function SettingsPage() {
  const { data, error, reload } = useApi<SettingsLevel>("/settings");
  // What the global level comes to once the built-in defaults are folded in.
  // Every field below shows it as its placeholder.
  const settled = useApi<ProjectFeatures>("/settings/features");
  const embeddings = useApi<EmbeddingsView>("/embeddings", LAMP_REFRESH_MS);
  const summaries = useApi<SummariesView>("/summaries", LAMP_REFRESH_MS);
  const selection = useDraft("/settings/selection", {
    ctxkeep: data?.ctxkeep ?? "",
    ctxignore: data?.ctxignore ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  if (error !== null) {
    return <ErrorBox message={error} />;
  }
  // Only the first load: a reload keeps the page, and what is typed on it.
  if (data === null) {
    return <Spinner what="the defaults" />;
  }

  function reloadAll() {
    reload();
    settled.reload();
    embeddings.reload();
    summaries.reload();
  }

  async function save() {
    setSaving(true);
    setFailure(null);
    try {
      await put("/settings", {
        ctxkeep: selection.value.ctxkeep,
        ctxignore: selection.value.ctxignore,
      });
      selection.commit();
      reload();
    } catch (reason: unknown) {
      setFailure(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <h1>Global defaults</h1>
      <p className="muted">
        What every project falls back to when neither it nor one of its
        directories has said otherwise.
      </p>

      {failure !== null && <ErrorBox message={failure} />}

      <h2>Indexing</h2>
      <p className="muted">
        Whether a project is indexed without anyone asking, and how often.{" "}
        <code>auto</code> watches the mounted directories and starts a run once
        they have been quiet for the throttle, and never runs on a tree nothing
        changed. The switch stops all of that for every project at once.
      </p>
      <IndexingEditor
        root
        path="/settings/indexing"
        indexing={data.settings?.indexing}
        feature={data.settings?.indexing}
        settled={settled.data?.features.indexing}
        onSaved={reloadAll}
      />

      <h2>Summarizing</h2>
      <p className="muted">
        Whether a file may be described by a model, and the llama.cpp server
        that answers. With a server set, the worker API pushes the queue at it
        on its own; with none, `make summarize` and a remote worker are what
        drain it. Off here stops all three.
      </p>
      <ServerLines rows={summaries.data?.summaries} />
      <FeatureEditor
        root
        path="/settings/features/summarize"
        settled={settled.data?.features.summarize}
        probePath="/summaries/probe"
        feature={data.settings?.summarize}
        onSaved={reloadAll}
      />

      <h2>Embedding</h2>
      <p className="muted">
        Whether files are queued for vectors, which is what{" "}
        <code>search_code</code> searches by meaning with, and the server that
        writes them. Empty uses the <code>embedder</code> container - start it
        with <code>make up EMBED=1</code> - or <code>EMBED_SERVER_URL</code>
        when that is set.
      </p>
      <ServerLines rows={embeddings.data?.embeddings} />
      <FeatureEditor
        root
        path="/settings/features/embedding"
        settled={settled.data?.features.embedding}
        probePath="/embeddings/probe"
        feature={data.settings?.embedding}
        onSaved={reloadAll}
      />

      <h2>Selection</h2>
      <p className="muted">
        What a project indexes when neither it nor one of its directories has
        said. A <code>.enggraph-keep</code> or <code>.enggraph-ignore</code> in
        a tree beats this and everything else; leaving both empty falls back to
        the built-in set of file types the parsers know.
      </p>

      <div className="editors">
        <label>
          ctxkeep - what becomes a node
          <textarea
            className={unsaved(undefined, selection.isDirty("ctxkeep"))}
            value={selection.value.ctxkeep}
            rows={20}
            onChange={(event) =>
              selection.update({ ctxkeep: event.target.value })
            }
          />
        </label>
        <label>
          ctxignore - what is pruned, on top of the built-in skip list
          <textarea
            className={unsaved(undefined, selection.isDirty("ctxignore"))}
            value={selection.value.ctxignore}
            rows={20}
            onChange={(event) =>
              selection.update({ ctxignore: event.target.value })
            }
          />
        </label>
      </div>

      <div className="row">
        {data.updated_at !== null && (
          <span className="muted">
            Last saved {new Date(data.updated_at).toLocaleString("en-GB")}
          </span>
        )}
        <button type="button" disabled={saving} onClick={() => void save()}>
          Save
        </button>
        <UnsavedNote
          count={selection.dirtyCount}
          disabled={saving}
          onDiscard={selection.discard}
        />
      </div>
    </>
  );
}
