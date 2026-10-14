import { useEffect, useState, type FormEvent } from "react";
import { api, formatBytes, formatLabel, SOURCE_LABEL, STATUS_LABEL, type Game } from "./api";
import { Cover } from "./Library";

const TEXT_FIELDS = [
  ["title", "Title"],
  ["title_id", "Title ID"],
  ["version", "Version"],
  ["region", "Region"],
  ["genre", "Genre"],
] as const;

export default function GameDetail({ id }: { id: string }) {
  const [game, setGame] = useState<Game | null>(null);
  const [draft, setDraft] = useState<Partial<Game>>({});
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);

  useEffect(() => {
    api<Game>(`/games/${id}`).then(setGame, (e) => setMsg({ text: e.message, error: true }));
  }, [id]);

  if (!game) return msg ? <p className="error" role="alert">{msg.text}</p> : <p className="muted">Loading…</p>;

  const val = <K extends keyof Game>(k: K) => (k in draft ? draft[k] : game[k]) as Game[K];
  const set = (k: keyof Game, v: unknown) => setDraft((d) => ({ ...d, [k]: v === "" ? null : v }));

  async function run(p: Promise<Game>, ok: string) {
    try {
      setGame(await p);
      setDraft({});
      setMsg({ text: ok });
    } catch (e) {
      setMsg({ text: (e as Error).message, error: true });
    }
  }

  const save = (e: FormEvent) => {
    e.preventDefault();
    run(api<Game>(`/games/${id}`, { method: "PATCH", json: draft }), "Saved.");
  };

  const upload = (file: File) => {
    const body = new FormData();
    body.append("file", file);
    run(api<Game>(`/games/${id}/cover`, { body }), "Cover updated.");
  };

  async function remove() {
    if (!confirm(`Remove "${game!.title}" from the catalog? Files on disk are not touched; a rescan will add it back.`)) return;
    await api(`/games/${id}`, { method: "DELETE" });
    location.hash = "#/";
  }

  return (
    <div className="detail">
      <aside>
        <Cover game={game} />
        <label className="button secondary">
          Change cover
          <input type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        </label>
        <button className="link" onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { favorite: !game.favorite } }), game.favorite ? "Removed from favorites." : "Added to favorites.")}>
          {game.favorite ? "★ Favorite" : "☆ Add to favorites"}
        </button>
      </aside>

      <section>
        <a href="#/" className="muted">← Library</a>
        <h1>{game.title}</h1>
        <div className={`panel ${game.compatibility_status}`}>
          <strong>{STATUS_LABEL[game.compatibility_status]}</strong>
          {game.compatibility_reason && <span> — {game.compatibility_reason}</span>}
        </div>

        {(game.metadata_confidence === "low" || game.metadata_confidence === "unknown") && (
          <div className="panel needs_attention" role="alert">
            <strong>Warning:</strong>{" "}
            {game.title_id
              ? `Title ID was derived from the ${game.metadata_source} because package metadata could not be read. Verify before installation.`
              : "No title ID could be identified. Enter it below."}
            {game.title_id && (
              <div>
                <button className="secondary" onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { title_id: game.title_id } }), "Title ID confirmed.")}>
                  Confirm {game.title_id} is correct
                </button>
              </div>
            )}
          </div>
        )}

        <dl className="facts">
          <dt>Metadata source</dt>
          <dd>
            <span className={`badge source-${game.metadata_confidence}`}>{SOURCE_LABEL[game.metadata_source] ?? game.metadata_source}</span>{" "}
            <span className="muted">{game.metadata_confidence} confidence</span>
          </dd>
          {game.content_id && <><dt>Content ID</dt><dd><code>{game.content_id}</code></dd></>}
          <dt>Format</dt><dd>{formatLabel(game)}</dd>
          <dt>Size</dt><dd>{formatBytes(game.size_bytes)}</dd>
          <dt>Source</dt><dd><code>{game.source_path}</code>{game.missing && " (missing)"}</dd>
        </dl>

        <form className="edit" onSubmit={save}>
          <h2>Metadata</h2>
          <div className="fields">
            {TEXT_FIELDS.map(([k, label]) => (
              <label key={k}>
                {label}
                <input value={val(k) ?? ""} onChange={(e) => set(k, e.target.value)} />
              </label>
            ))}
            <label>
              Release date
              <input type="date" value={val("release_date") ?? ""} onChange={(e) => set("release_date", e.target.value)} />
            </label>
          </div>
          <label>
            Description
            <textarea rows={3} value={val("description") ?? ""} onChange={(e) => set("description", e.target.value)} />
          </label>

          <h2>Provenance</h2>
          <div className="fields">
            <label>
              Owned copy
              <select value={val("owned_copy_type")} onChange={(e) => set("owned_copy_type", e.target.value)}>
                {["disc", "digital", "other", "unknown"].map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>
            <label>
              Backup date
              <input type="date" value={val("backup_date") ?? ""} onChange={(e) => set("backup_date", e.target.value)} />
            </label>
          </div>
          <label>
            Notes
            <textarea rows={3} value={val("provenance_notes") ?? ""} onChange={(e) => set("provenance_notes", e.target.value)} />
          </label>

          <div className="actions">
            <button type="submit" disabled={!Object.keys(draft).length}>Save</button>
            {msg && <span className={msg.error ? "error" : "muted"} role="status">{msg.text}</span>}
          </div>
        </form>

        {game.parser_warnings.length > 0 && (
          <details open>
            <summary>Parser warnings ({game.parser_warnings.length})</summary>
            <ul>{game.parser_warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          </details>
        )}

        <details>
          <summary>Detection evidence</summary>
          <pre>{JSON.stringify(game.detection_evidence, null, 2)}</pre>
        </details>

        <button className="danger" onClick={remove}>Remove from catalog</button>
      </section>
    </div>
  );
}
