import { useEffect, useState } from "react";

import { put } from "../api.js";
import { ErrorBox, Spinner } from "../components/Common.js";
import { FeatureEditor } from "../components/FeatureFields.js";
import { IndexingEditor } from "../components/IndexingFields.js";
import { useApi } from "../hooks/useApi.js";
import type { ProjectFeatures, SettingsLevel } from "../types.js";

/** The selection every project falls back to.
 *
 * Read last, after the directory and the project have both declined to say
 * what to index - and after a `.enggraph-keep` in the tree, which beats all three.
 * Left empty, the fallback is the built-in set of file types the parsers know.
 */
export function SettingsPage() {
  const { data, error, loading, reload } = useApi<SettingsLevel>("/settings");
  // What the global level comes to once the built-in defaults are folded in.
  // Every field below shows it as its placeholder.
  const settled = useApi<ProjectFeatures>("/settings/features");
  const [keep, setKeep] = useState<string | null>(null);
  const [ignore, setIgnore] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  // The editors start from what is stored, and stop tracking it once typed in.
  useEffect(() => {
    if (data !== null) {
      setKeep((current) => current ?? data.ctxkeep ?? "");
      setIgnore((current) => current ?? data.ctxignore ?? "");
    }
  }, [data]);

  if (error !== null) {
    return <ErrorBox message={error} />;
  }
  if (loading || data === null || keep === null || ignore === null) {
    return <Spinner what="the defaults" />;
  }

  async function save() {
    setSaving(true);
    setFailure(null);
    try {
      await put("/settings", { ctxkeep: keep, ctxignore: ignore });
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
        they have been quiet for the throttle - and still sweeps on the
        interval, because a watch is blind on a network filesystem and where the
        host has run out of inotify watches. The switch stops all of that for
        every project at once.
      </p>
      <IndexingEditor
        root
        path="/settings/indexing"
        indexing={data.settings?.indexing}
        feature={data.settings?.indexing}
        settled={settled.data?.features.indexing}
        onSaved={reload}
      />

      <h2>Summarizing</h2>
      <p className="muted">
        Whether a file may be described by a model, and the llama.cpp server
        that answers. With a server set, the worker API pushes the queue at it
        on its own; with none, `make summarize` and a remote worker are what
        drain it. Off here stops all three.
      </p>
      <FeatureEditor
        root
        path="/settings/features/summarize"
        settled={settled.data?.features.summarize}
        probePath="/summaries/probe"
        feature={data.settings?.summarize}
        onSaved={reload}
      />

      <h2>Embedding</h2>
      <p className="muted">
        Whether files are queued for vectors, which is what{" "}
        <code>search_code</code> searches by meaning with, and the server that
        writes them. Empty uses the <code>embedder</code> container - start it
        with <code>make up EMBED=1</code> - or <code>EMBED_SERVER_URL</code>
        when that is set.
      </p>
      <FeatureEditor
        root
        path="/settings/features/embedding"
        settled={settled.data?.features.embedding}
        probePath="/embeddings/probe"
        feature={data.settings?.embedding}
        onSaved={reload}
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
            value={keep}
            rows={20}
            onChange={(event) => setKeep(event.target.value)}
          />
        </label>
        <label>
          ctxignore - what is pruned, on top of the built-in skip list
          <textarea
            value={ignore}
            rows={20}
            onChange={(event) => setIgnore(event.target.value)}
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
      </div>
    </>
  );
}
