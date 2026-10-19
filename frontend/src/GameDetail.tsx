import { useEffect, useState, type FormEvent } from "react";
import { api, formatBytes, formatLabel, needsOnline, session, SOURCE_LABEL, STATUS_LABEL, useConsoles, type Drive, type Game, type InstalledGame, type Job } from "./api";
import { cart, useCart } from "./Cart";
import { Cover } from "./Library";
import { when } from "./Installed";

/** Queues an install on a console; the job shows up on the Installed page. DLC goes onto its game's drive. */
function Install({ game }: { game: Game }) {
  const { consoles, cid, setCid, console: target } = useConsoles();
  const [drives, setDrives] = useState<Drive[]>([]);
  const [did, setDid] = useState("");
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const [installed, setInstalled] = useState<InstalledGame | null>(null);
  const carted = useCart().includes(game.id);

  useEffect(() => {
    setDid("");
    setMsg(null);
    if (cid) api<Drive[]>(`/consoles/${cid}/drives`).then((ds) => setDrives(ds.filter((d) => d.is_install_target)), () => setDrives([]));
  }, [cid]);
  useEffect(() => { // already on this console? Then the button moves it to the chosen drive instead
    setInstalled(null);
    if (cid && game.kind === "game" && game.title_id)
      api<InstalledGame[]>(`/consoles/${cid}/installed`).then(
        (rows) => setInstalled(rows.find((r) => r.title_id === game.title_id && r.install_route === "shadowmountplus" && r.status !== "orphaned_icon") ?? null),
        () => {});
  }, [cid, game.id]);

  if (!consoles.length) return null;

  async function install() {
    if (game.format === "archive" && !confirm("This archive will be extracted to the private staging folder on this PC before it's installed. Make sure the PC has room for the unpacked game.")) return;
    if ((game.format === "ps5_pkg" || game.format === "ps4_pkg") && !confirm(
      `This will submit a package file to the PS5 package installer. Confirm the title ID (${game.title_id ?? "unknown"}) ` +
      `and version (${game.version ?? "unknown"}) before continuing.`)) return;
    if (game.kind === "dlc" && game.format !== "ps5_pkg" && game.format !== "ps4_pkg" && !confirm(
      "The unlocker's files are copied into the installed game's folder and replace files there with the same names. " +
      "Close the game on the PS5 first. To undo it, remove the game and install it again.")) return;
    try {
      await api<Job>(`/consoles/${cid}/install`, { json: { game_id: game.id, drive_id: did || null } });
      setMsg({ text: "Queued. Follow it on the Installed page." });
    } catch (e) {
      setMsg({ text: (e as Error).message, error: true });
    }
  }

  async function move() {
    if (!installed || !confirm(`Move ${game.title} to the chosen drive? The console copies it, checks the copy, then deletes the original. Close the game on the PS5 first.`)) return;
    try {
      await api(`/consoles/${cid}/move`, { json: { installed_game_id: installed.id, drive_id: did } });
      setMsg({ text: "Moving. Follow it on the Installed page." });
    } catch (e) {
      setMsg({ text: (e as Error).message, error: true });
    }
  }

  return (
    <div className="toolbar">
      {consoles.length > 1 && (
        <select aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
          {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      )}
      {game.kind === "game" && (
        <select aria-label="Drive" value={did} onChange={(e) => setDid(e.target.value)}>
          <option value="">Drive with the most free space</option>
          {drives.map((d) => <option key={d.id} value={d.id}>{d.label}{d.total_bytes ? ` (${formatBytes(d.free_bytes)} free)` : ""}</option>)}
        </select>
      )}
      {installed ? (
        <>
          <span className="muted">Already installed. Pick a drive to move it there.</span>
          <button {...needsOnline(target)} disabled={!did || did === installed.drive_id || target?.state !== "online"} onClick={move}>Move</button>
        </>
      ) : (
        <button {...needsOnline(target)} onClick={install}>{game.kind === "dlc" ? "Add to game" : "Install"}</button>
      )}
      <button className="secondary" aria-pressed={carted} onClick={() => (carted ? cart.remove : cart.add)([game.id])}>
        {carted ? "In cart ✓" : "Add to cart"}
      </button>
      {carted && <a href="#/cart">Open cart</a>}
      {msg && <span className={msg.error ? "error" : "muted"} role="status">{msg.text}{!msg.error && <> <a href="#/installed">Open</a></>}</span>}
    </div>
  );
}

/** Replaces the plain-text name on the game's PS5 home-screen page with its logo from PlayStation Store. */
function HomeLogo({ game }: { game: Game }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  const { consoles } = useConsoles();

  if (!game.installed) return null;

  async function apply() {
    setBusy(true);
    setMsg(null);
    try {
      const results = await api<{ console: string; ok: boolean; message: string }[]>(`/games/${game.id}/home-logo`, { method: "POST" });
      setMsg({
        text: results.length === 1 ? results[0].message : results.map((r) => `${r.console}: ${r.message}`).join(" "),
        error: results.some((r) => !r.ok),
      });
    } catch (e) {
      setMsg({ text: (e as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="toolbar">
      {/* applies on every console the game is on; ponytail: any online console enables it, the result names the ones that failed */}
      <button className="secondary" onClick={apply} {...needsOnline(consoles.find((c) => c.state === "online"))} disabled={busy || !consoles.some((c) => c.state === "online")}>{busy ? "Setting logo…" : "Show logo on PS5 home screen"}</button>
      {msg && <span className={msg.error ? "error" : "muted"} role="status">{msg.text}</span>}
    </div>
  );
}

type StoreImages = { store: string; name: string | null; sources: { label: string; images: { type: string | null; url: string }[] }[] }[];

/** Lists every image PlayStation Store has for the game, by store and edition, to see which ones exist. */
function StoreImageBrowser({ game }: { game: Game }) {
  const [stores, setStores] = useState<StoreImages | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!game.title_id) return null;

  async function load() {
    setBusy(true);
    setError("");
    try {
      setStores(await api<StoreImages>(`/games/${game.id}/store-images`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="history">
      <div className="toolbar">
        <button className="secondary" onClick={load} disabled={busy}>{busy ? "Asking PlayStation Store…" : "Browse PlayStation Store images"}</button>
        {error && <span className="error" role="alert">{error}</span>}
      </div>
      {stores?.map((s) => (
        <div key={s.store}>
          <h2>{s.store} store{s.name && ` · ${s.name}`}</h2>
          {!s.sources.length && <p className="muted">Not sold in this store.</p>}
          {s.sources.map((src) => (
            <details key={src.label} open={src.label.startsWith("Concept")}>
              <summary>{src.label} · {src.images.length} image(s)</summary>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                {src.images.map((i) => (
                  <a key={i.url} href={i.url} target="_blank" rel="noreferrer" style={{ width: 160 }}>
                    <img src={i.url} alt={i.type ?? "image"} loading="lazy" style={{ width: 160, background: "#444" }} />
                    <div className="muted">{i.type ?? "unknown"}</div>
                  </a>
                ))}
              </div>
            </details>
          ))}
        </div>
      ))}
    </section>
  );
}

type Conversion = { status: "converting" | "success" | "failed" | "canceled"; progress: number; message: string; image_path: string; image_game_id: string | null };

/** Writes a folder dump into one .exfat disc image next to it; the image then shows up in the library as its own game. */
function Convert({ game }: { game: Game }) {
  const [conv, setConv] = useState<Conversion | null>(null);
  const [error, setError] = useState("");
  const converting = conv?.status === "converting";

  useEffect(() => {
    if (game.format === "folder_dump") api<Conversion>(`/games/${game.id}/convert`).then(setConv, () => {});
  }, [game.id, game.format]);
  useEffect(() => {
    if (!converting) return;
    const t = setInterval(() => api<Conversion>(`/games/${game.id}/convert`).then(setConv, () => {}), 1000);
    return () => clearInterval(t);
  }, [converting, game.id]);

  if (game.format !== "folder_dump") return null;

  async function start() {
    if (!confirm(`Write this game into one .exfat disc image next to its folder? It needs about ${formatBytes(game.size_bytes)} free on that drive. The folder is kept.`)) return;
    setError("");
    try {
      setConv(await api<Conversion>(`/games/${game.id}/convert`, { method: "POST" }));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="toolbar">
      {converting ? (
        <>
          <progress value={conv.progress} aria-label="Conversion progress" />
          <span className="muted" role="status">{Math.floor(conv.progress * 100)}% · {conv.message}</span>
          <button className="secondary" onClick={() => api(`/games/${game.id}/convert/cancel`, { method: "POST" })}>Cancel</button>
        </>
      ) : (
        <button className="secondary" onClick={start}>Convert to .exfat</button>
      )}
      {error && <span className="error" role="alert">{error}</span>}
      {conv && !converting && (
        <span className={conv.status === "success" ? "muted" : "error"} role="status">
          {conv.message}{conv.image_game_id && <> <a href={`#/game/${conv.image_game_id}`}>Open the image</a></>}
        </span>
      )}
    </div>
  );
}

type History = {
  installed: { console: string; drive: string; path: string; version: string | null; status: string }[];
  events: { at: string; what: string; result: string; console: string; drive: string; message: string }[];
};
const WHAT: Record<string, string> = { install: "Install", uninstall: "Removed", move: "Moved" };
const RESULT_BADGE: Record<string, string> = { success: "ready", failed: "incompatible", interrupted: "incompatible", blocked: "incompatible", canceled: "unknown" };

/** Where the game is installed now, and what happened to it: installs, removals and moves. */
function InstallHistory({ game }: { game: Game }) {
  const [h, setH] = useState<History | null>(null);
  useEffect(() => { api<History>(`/games/${game.id}/history`).then(setH, () => {}); }, [game.id]);
  if (!h || (!h.installed.length && !h.events.length)) return null;
  return (
    <section className="history">
      {h.installed.length > 0 && <>
        <h2>On your consoles</h2>
        <ul>{h.installed.map((i) => (
          <li key={i.console + i.path}><strong>{i.console}</strong> · {i.drive || "Unknown drive"}{i.version && ` · v${i.version}`} <code>{i.path}</code></li>
        ))}</ul>
      </>}
      {h.events.length > 0 && <>
        <h2>History</h2>
        <ul>{h.events.map((e, n) => (
          <li key={n}>
            <span className={`badge ${RESULT_BADGE[e.result] ?? "needs_attention"}`}>{e.result}</span> {WHAT[e.what] ?? e.what}
            {e.console && ` · ${e.console}`}{e.drive && ` · ${e.drive}`} · <span className="muted">{when(e.at)}</span>
            {e.message && <div className="muted">{e.message}</div>}
          </li>
        ))}</ul>
      </>}
    </section>
  );
}

type SaveBackup = { id: string; status: "copying" | "success" | "failed"; progress: number; bytes_done: number; bytes_total: number; message: string };
type SavedBackup = { name: string; path: string; files: number; bytes: number };

/** Copies the game's save data from a console to this PC. The console is only read; restoring is by hand. */
function SaveBackups({ game }: { game: Game }) {
  const { consoles, cid, setCid, console: target } = useConsoles();
  const [list, setList] = useState<SavedBackup[]>([]);
  const [b, setB] = useState<SaveBackup | null>(null);
  const [error, setError] = useState("");
  const tid = game.title_id;
  const load = () => tid && api<SavedBackup[]>(`/save-backups?title_id=${tid}`).then(setList, () => {});
  useEffect(() => {
    load();
  }, [tid]);
  useEffect(() => {
    if (b?.status !== "copying") return;
    const t = setInterval(() => api<SaveBackup>(`/save-backups/${b.id}`).then((nb) => { setB(nb); if (nb.status !== "copying") load(); }, () => {}), 1000);
    return () => clearInterval(t);
  }, [b?.id, b?.status]);

  if (!tid || game.kind !== "game" || !consoles.length) return null;
  const start = () => (setError(""), api<SaveBackup>(`/consoles/${cid}/save-backups`, { json: { title_id: tid } }).then(setB, (e) => setError(e.message)));

  return (
    <section className="history">
      <h2>Save data</h2>
      <p className="muted">Copies this game's saves from the console to this PC, for every user. They stay encrypted and only work on the
        same console and account; to restore, copy the folder back with an FTP payload.</p>
      <div className="toolbar">
        {consoles.length > 1 && (
          <select aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
            {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <button className="secondary" {...needsOnline(target)} disabled={b?.status === "copying" || !!needsOnline(target).disabled} onClick={start}>{b?.status === "copying" ? "Backing up…" : "Back up saves"}</button>
        {b?.status === "copying" && <progress max={1} value={b.progress} aria-label="Backing up" />}
        {b && <span className={b.status === "failed" ? "error" : "muted"} role="status">{b.message}</span>}
        {error && <span className="error" role="alert">{error}</span>}
      </div>
      {list.length > 0 && <ul>{list.map((s) => (
        <li key={s.name}><strong>{s.name}</strong> · {s.files} file(s), {formatBytes(s.bytes)} <code>{s.path}</code></li>
      ))}</ul>}
    </section>
  );
}

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
    location.hash = game!.kind === "dlc" ? "#/dlc" : "#/";
  }

  return (
    <div className="detail">
      <aside>
        <Cover key={game.updated_at} game={game} />
        {!session.readOnly && <>
        <label className="button secondary">
          Change cover
          <input type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        </label>
        <button className="link" onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { favorite: !game.favorite } }), game.favorite ? "Removed from favorites." : "Added to favorites.")}>
          {game.favorite ? "★ Favorite" : "☆ Add to favorites"}
        </button>
        </>}
      </aside>

      <section>
        {game.kind === "dlc" ? <a href="#/dlc" className="muted">← DLC</a> : <a href="#/" className="muted">← Library</a>}
        <h1>{game.title}</h1>
        <div className={`panel ${game.compatibility_status}`}>
          <strong>{STATUS_LABEL[game.compatibility_status]}</strong>
          {game.compatibility_reason && <span> — {game.compatibility_reason}</span>}
        </div>
        {!session.readOnly && <>
          <Install game={game} />
          <HomeLogo game={game} />
          <Convert game={game} />
        </>}

        {(game.metadata_confidence === "low" || game.metadata_confidence === "unknown") && (
          <div className="panel needs_attention" role="alert">
            <strong>Warning:</strong>{" "}
            {game.title_id
              ? `Title ID was derived from the ${game.metadata_source} because package metadata could not be read. Verify before installation.`
              : "No title ID could be identified. Enter it below."}
            {game.title_id && !session.readOnly && (
              <div>
                <button className="secondary" onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { title_id: game.title_id } }), "Title ID confirmed.")}>
                  Confirm {game.title_id} is correct
                </button>
              </div>
            )}
          </div>
        )}

        <InstallHistory game={game} />
        <StoreImageBrowser game={game} />
        {!session.readOnly && <SaveBackups game={game} />}

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
          <fieldset disabled={session.readOnly}>
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

          </fieldset>
          {!session.readOnly && <div className="actions">
            <button type="submit" disabled={!Object.keys(draft).length}>Save</button>
            {msg && <span className={msg.error ? "error" : "muted"} role="status">{msg.text}</span>}
          </div>}
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

        {!session.readOnly && <button className="danger" onClick={remove}>Remove from catalog</button>}
      </section>
    </div>
  );
}
