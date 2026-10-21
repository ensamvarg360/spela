import { useEffect, useState, type FormEvent } from "react";
import { api, formatBytes, formatLabel, needsOnline, session, SOURCE_LABEL, STATUS_LABEL, useConsoles, type Drive, type Game, type InstalledGame, type Job } from "./api";
import { cart, useCart } from "./Cart";
import { Cover } from "./Library";
import { Art, MoveDialog, RemoveDialog, when } from "./Installed";
import { toast, toastError } from "./Toast";

/** A titled card on the detail page; collapsible unless `open` is omitted-and-fixed via `fixed`. */
function Section({ title, open = true, fixed, children }: { title?: string; open?: boolean; fixed?: boolean; children: React.ReactNode }) {
  if (fixed) return <div className="card section">{title && <h2>{title}</h2>}{children}</div>;
  return (
    <details className="card section" open={open}>
      <summary>{title}</summary>
      {children}
    </details>
  );
}

/** Queues an install on a console; the job shows up on the Installed page. DLC goes onto its game's drive. */
function Install({ game: opened }: { game: Game }) {
  const [copies, setCopies] = useState<Game[]>([]); // every format of this title, so the install can ask which
  const [pick, setPick] = useState("");
  const game = copies.find((c) => c.id === pick) ?? opened;
  useEffect(() => {
    setPick(opened.id);
    setCopies([]);
    if (opened.kind === "game" && opened.title_id)
      api<Game[]>("/games").then((gs) => setCopies(gs.filter((g) => g.kind === "game" && g.title_id === opened.title_id)), () => {});
  }, [opened.id]);
  const { consoles, cid, setCid, console: target } = useConsoles();
  const [drives, setDrives] = useState<Drive[]>([]);
  const [did, setDid] = useState("");
  const [installed, setInstalled] = useState<InstalledGame | null>(null);
  const carted = useCart().includes(game.id);

  useEffect(() => {
    setDid("");
    if (cid) api<Drive[]>(`/consoles/${cid}/drives`).then((ds) => setDrives(ds.filter((d) => d.is_install_target)), () => setDrives([]));
  }, [cid]);
  useEffect(() => { // already on this console? Then the button moves it to the chosen drive instead
    setInstalled(null);
    if (cid && game.kind === "game" && game.title_id)
      api<InstalledGame[]>(`/consoles/${cid}/installed`).then(
        (rows) => setInstalled(rows.find((r) => r.title_id === game.title_id && r.install_route === "shadowmountplus" && r.status !== "orphaned_icon") ?? null),
        () => {});
  }, [cid, game.id]);

  const [blocked, setBlocked] = useState<Record<string, string>>({}); // game id -> why this console can't take it
  const ids = (copies.length ? copies : [opened]).map((c) => c.id).join(",");
  useEffect(() => {
    setBlocked({});
    if (cid) api<Record<string, string>>(`/consoles/${cid}/blockers?game_ids=${ids}`).then(setBlocked, () => {});
  }, [cid, ids, target?.firmware]);

  if (!consoles.length) return null;
  const why = blocked[game.id];

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
      toast(<>Queued {game.title}. <a href="#/installed">Follow it on the Installed page</a></>);
    } catch (e) {
      toastError(e);
    }
  }

  async function move() {
    if (!installed || !confirm(`Move ${game.title} to the chosen drive? The console copies it, checks the copy, then deletes the original. Close the game on the PS5 first.`)) return;
    try {
      await api(`/consoles/${cid}/move`, { json: { installed_game_id: installed.id, drive_id: did } });
      toast(<>Moving {game.title}. <a href="#/installed">Follow it on the Installed page</a></>);
    } catch (e) {
      toastError(e);
    }
  }

  return (
    <div className="toolbar">
      {consoles.length > 1 && (
        <select aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
          {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      )}
      {copies.length > 1 && (
        <select aria-label="Version" value={game.id} onChange={(e) => setPick(e.target.value)}>
          {copies.map((c) => <option key={c.id} value={c.id}>{formatLabel(c)}{c.version ? ` · v${c.version}` : ""} · {formatBytes(c.size_bytes)}{blocked[c.id] && !installed ? " · not for this console" : ""}</option>)}
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
        <>
          <button {...needsOnline(target)} {...(why ? { disabled: true, title: why } : {})} onClick={install}>{game.kind === "dlc" ? "Add to game" : "Install"}</button>
          {why && <span className="error" role="status">{why}</span>}
        </>
      )}
      <button className="secondary" aria-pressed={carted} onClick={() => (carted ? cart.remove : cart.add)([game.id])}>
        {carted ? "In cart ✓" : "Add to cart"}
      </button>
      {carted && <a href="#/cart">Open cart</a>}
    </div>
  );
}

/** Replaces the plain-text name on the game's PS5 home-screen page with its logo from PlayStation Store.
 * `consoleId`: on that console only; else on every console the game is on. */
function HomeLogo({ url, consoleId }: { url: string; consoleId?: string }) {
  const [busy, setBusy] = useState(false);
  const { consoles } = useConsoles();
  const target = consoles.find((c) => (consoleId ? c.id === consoleId : c.state === "online"));

  async function apply() {
    setBusy(true);
    try {
      const results = await api<{ console: string; ok: boolean; message: string }[]>(url, { method: "POST" });
      toast(results.length === 1 ? results[0].message : results.map((r) => `${r.console}: ${r.message}`).join(" "), results.some((r) => !r.ok));
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="toolbar">
      {/* applies on every console the game is on; ponytail: any online console enables it, the result names the ones that failed */}
      <button className="secondary" onClick={apply} {...needsOnline(target)} disabled={busy || target?.state !== "online"}>{busy ? "Setting logo…" : "Show logo on PS5 home screen"}</button>
    </div>
  );
}

type StoreImages = { store: string; name: string | null; sources: { label: string; images: { type: string | null; url: string }[] }[] }[];

/** Lists every image PlayStation Store has for the game, by store and edition, to see which ones exist. */
function StoreImageBrowser({ url }: { url: string }) {
  const [stores, setStores] = useState<StoreImages | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setBusy(true);
    setError("");
    try {
      setStores(await api<StoreImages>(url));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="PlayStation Store images" open={false}>
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
    </Section>
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
  installed: { id: string; console_id: string; console: string; drive: string; path: string; version: string | null; status: string }[];
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
    <>
      {h.installed.length > 0 && (
        <Section title="On your consoles">
          <ul>{h.installed.map((i) => (
            <li key={i.id}>
              <strong>{i.console}</strong> · {i.drive || "Unknown drive"}{i.version && ` · v${i.version}`} <code>{i.path}</code>{" "}
              <a href={`#/installed/${i.console_id}/${i.id}`}>{session.readOnly ? "Details" : "Move or remove"}</a>
            </li>
          ))}</ul>
        </Section>
      )}
      {h.events.length > 0 && (
        <Section title={`History (${h.events.length})`} open={false}>
          <ul>{h.events.map((e, n) => (
            <li key={n}>
              <span className={`badge ${RESULT_BADGE[e.result] ?? "needs_attention"}`}>{e.result}</span> {WHAT[e.what] ?? e.what}
              {e.console && ` · ${e.console}`}{e.drive && ` · ${e.drive}`} · <span className="muted">{when(e.at)}</span>
              {e.message && <div className="muted">{e.message}</div>}
            </li>
          ))}</ul>
        </Section>
      )}
    </>
  );
}

type SaveJob = { id: string; status: "copying" | "confirm" | "success" | "failed"; progress: number; bytes_done: number; bytes_total: number; message: string; backup?: string };
type SavedBackup = { name: string; path: string; files: number; bytes: number };

/** Copies the game's save data from a console to this PC, and back. Replacing saves on the console asks first. */
function SaveBackups({ tid, consoleId }: { tid: string; consoleId?: string }) {
  const { consoles, cid: picked, setCid, console: chosen } = useConsoles();
  const cid = consoleId ?? picked;
  const target = consoleId ? consoles.find((c) => c.id === consoleId) : chosen;
  const [list, setList] = useState<SavedBackup[]>([]);
  const [b, setB] = useState<SaveJob | null>(null);
  const [r, setR] = useState<SaveJob | null>(null);
  const [error, setError] = useState("");
  const load = () => tid && api<SavedBackup[]>(`/save-backups?title_id=${tid}`).then(setList, () => {});
  useEffect(() => {
    load();
  }, [tid]);
  useEffect(() => {
    if (b?.status !== "copying") return;
    const t = setInterval(() => api<SaveJob>(`/save-backups/${b.id}`).then((nb) => { setB(nb); if (nb.status !== "copying") load(); }, () => {}), 1000);
    return () => clearInterval(t);
  }, [b?.id, b?.status]);
  const restore = (backup: string, overwrite = false) => (setError(""),
    api<SaveJob>(`/consoles/${cid}/save-restores`, { json: { title_id: tid, backup, overwrite } }).then(setR, (e) => setError(e.message)));
  useEffect(() => {
    if (r?.status !== "copying") return;
    const t = setInterval(() => api<SaveJob>(`/save-restores/${r.id}`).then((nr) => {
      setR(nr);
      if (nr.status === "confirm" && confirm(`${nr.message}\n\nReplace them with the backup "${nr.backup}"? Spela first backs up ` +
        "the saves on the console to this PC. Close the game on the PS5 first.")) restore(nr.backup!, true);
      else if (nr.status !== "copying") load();
    }, () => {}), 1000);
    return () => clearInterval(t);
  }, [r?.id, r?.status]);

  if (!consoles.length) return null;
  const start = () => (setError(""), api<SaveJob>(`/consoles/${cid}/save-backups`, { json: { title_id: tid } }).then(setB, (e) => setError(e.message)));
  const busy = b?.status === "copying" || r?.status === "copying";

  return (
    <Section title="Save data" open={false}>
      <p className="muted">Copies this game's saves from the console to this PC, for every user, and back. They stay encrypted and only
        work on the same console and account. Restoring over saves already on the console asks first.</p>
      <div className="toolbar">
        {consoles.length > 1 && !consoleId && (
          <select aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
            {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <button className="secondary" {...needsOnline(target)} disabled={busy || !!needsOnline(target).disabled} onClick={start}>{b?.status === "copying" ? "Backing up…" : "Back up saves"}</button>
        {b?.status === "copying" && <progress max={1} value={b.progress} aria-label="Backing up" />}
        {r?.status === "copying" && <progress max={1} value={r.progress} aria-label="Restoring" />}
        {b && <span className={b.status === "failed" ? "error" : "muted"} role="status">{b.message}</span>}
        {r && <span className={r.status === "failed" ? "error" : "muted"} role="status">{r.message}</span>}
        {error && <span className="error" role="alert">{error}</span>}
      </div>
      {list.length > 0 && <ul>{list.map((s) => (
        <li key={s.name}><strong>{s.name}</strong> · {s.files} file(s), {formatBytes(s.bytes)} <code>{s.path}</code>{" "}
          <button className="secondary" {...needsOnline(target)} disabled={busy || !!needsOnline(target).disabled}
            onClick={() => restore(s.name)}>{r?.status === "copying" && r.backup === s.name ? "Restoring…" : "Restore"}</button></li>
      ))}</ul>}
    </Section>
  );
}

const TEXT_FIELDS = [
  ["title", "Title"],
  ["title_id", "Title ID"],
  ["version", "Version"],
  ["region", "Region"],
  ["genre", "Genre"],
] as const;

const BACK: Record<string, string> = { "/": "Library", "/dlc": "DLC", "/installed": "Installed", "/dashboard": "Dashboard", "/cart": "Cart" };

/** `from`: the page the user came from, so ← goes back there; null (opened by link) falls back to the game's list. */
export default function GameDetail({ id, from }: { id: string; from: string | null }) {
  const [game, setGame] = useState<Game | null>(null);
  const [draft, setDraft] = useState<Partial<Game>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    api<Game>(`/games/${id}`).then(setGame, (e) => setError(e.message));
  }, [id]);

  if (!game) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">Loading…</p>;

  const val = <K extends keyof Game>(k: K) => (k in draft ? draft[k] : game[k]) as Game[K];
  const set = (k: keyof Game, v: unknown) => setDraft((d) => ({ ...d, [k]: v === "" ? null : v }));

  async function run(p: Promise<Game>, ok: string) {
    try {
      setGame(await p);
      setDraft({});
      toast(ok);
    } catch (e) {
      toastError(e);
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

  const back = from && BACK[from] ? from : game.kind === "dlc" ? "/dlc" : "/";

  async function remove() {
    if (!confirm(`Remove "${game!.title}" from the catalog? Files on disk are not touched; a rescan will add it back.`)) return;
    await api(`/games/${id}`, { method: "DELETE" });
    location.hash = `#${back}`;
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
        <a href={`#${back}`} className="muted">← {BACK[back]}</a>
        <h1>{game.title}</h1>
        <div className={`panel ${game.compatibility_status}`}>
          <strong>{STATUS_LABEL[game.compatibility_status]}</strong>
          {game.compatibility_reason && <span> — {game.compatibility_reason}</span>}
        </div>
        {!session.readOnly && (
          <Section fixed>
            <Install game={game} />
            {game.installed && <HomeLogo url={`/games/${game.id}/home-logo`} />}
            <Convert game={game} />
          </Section>
        )}

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
        {game.title_id && <StoreImageBrowser url={`/games/${game.id}/store-images`} />}
        {!session.readOnly && game.title_id && game.kind === "game" && <SaveBackups tid={game.title_id} />}

        <Section title="File details">
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
        </Section>

        <form className="edit" onSubmit={save}>
          <fieldset disabled={session.readOnly}>
          <Section title="Metadata">
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
          </Section>

          <Section title="Provenance" open={false}>
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
          </Section>

          </fieldset>
          {!session.readOnly && <div className="actions">
            <button type="submit" disabled={!Object.keys(draft).length}>Save</button>
          </div>}
        </form>

        {game.parser_warnings.length > 0 && (
          <Section title={`Parser warnings (${game.parser_warnings.length})`}>
            <ul>{game.parser_warnings.map((w) => <li key={w}>{w}</li>)}</ul>
          </Section>
        )}

        <Section title="Detection evidence" open={false}>
          <pre>{JSON.stringify(game.detection_evidence, null, 2)}</pre>
        </Section>

        {!session.readOnly && <button className="danger" onClick={remove}>Remove from catalog</button>}
      </section>
    </div>
  );
}

/** A game on a console: every copy of it there (one per drive), what the console says about each, and what can be done with it. */
export function ConsoleGameDetail({ consoleId, id }: { consoleId: string; id: string }) {
  const { consoles } = useConsoles();
  const c = consoles.find((x) => x.id === consoleId);
  const [tid, setTid] = useState<string | null | undefined>(undefined);
  const [rows, setRows] = useState<InstalledGame[]>([]);
  const [drives, setDrives] = useState<Drive[]>([]);
  const [dialog, setDialog] = useState<{ kind: "move" | "remove"; game: InstalledGame } | null>(null);
  const load = () => Promise.all([
    api<Drive[]>(`/consoles/${consoleId}/drives`).then(setDrives),
    // a move to another drive gives the copy a new id, so the page follows the title ID once it's known
    api<InstalledGame[]>(`/consoles/${consoleId}/installed`).then((rs) => {
      setRows(rs);
      setTid((cur) => cur ?? rs.find((r) => r.id === id)?.title_id ?? null);
    }),
  ]).catch(() => setTid((cur) => cur ?? null));
  useEffect(() => { setTid(undefined); load(); }, [consoleId, id]);
  const copies = rows.filter((r) => r.title_id === tid);
  const busy = copies.some((r) => r.removing || r.moving);
  useEffect(() => { // a removal or move still running after its dialog closed
    if (!busy) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [busy]);

  if (tid === undefined) return <p className="muted">Loading…</p>;
  const back = <a href="#/installed" className="muted">← Installed</a>;
  const g = copies[0];
  if (!g) return <section>{back}<p>This game isn't on the console any more. If you moved it to this PC, it's in the <a href="#/">Library</a>.</p></section>;
  const online = c?.state === "online";

  return (
    <div className="detail">
      <aside><Art key={g.id} g={g} /></aside>
      <section>
        {back}
        <h1>{g.title}</h1>
        <p className="muted">{g.game_id ? <a href={`#/game/${g.game_id}`}>Open in your PC library</a> : "Not in your PC library."}</p>
        {!session.readOnly && (
          <Section fixed>
            <HomeLogo url={`/consoles/${consoleId}/home-logo/${g.title_id}`} consoleId={consoleId} />
          </Section>
        )}
        {copies.map((r) => {
          const smp = r.install_route === "shadowmountplus" && !r.removing && !r.moving && !session.readOnly;
          const drive = drives.find((d) => d.id === r.drive_id)?.label ?? "Unknown drive";
          return (
            <Section key={r.id} title={copies.length > 1 ? `On ${drive}` : "On the console"}>
              <dl className="facts">
                <dt>Console</dt><dd>{c?.name ?? "…"}</dd>
                <dt>Drive</dt><dd>{drive}</dd>
                <dt>Title ID</dt><dd><code>{r.title_id}</code></dd>
                <dt>Version</dt><dd>{r.version ?? "Unknown"}</dd>
                <dt>Size</dt><dd>{r.size_bytes ? formatBytes(r.size_bytes) : "Unknown"}</dd>
                <dt>Path</dt><dd><code>{r.source_path ?? r.installed_path}</code></dd>
                <dt>Last checked</dt><dd>{when(r.last_verified_at)}</dd>
              </dl>
              {(r.removing || r.moving) && <p className="muted">{r.removing ? "Being removed…" : "Being moved…"}</p>}
              {smp && (
                <div className="toolbar">
                  {r.status !== "orphaned_icon" && <button {...needsOnline(c)} disabled={!online} onClick={() => setDialog({ kind: "move", game: r })}>Move…</button>}
                  <button className="secondary" {...needsOnline(c)} disabled={!online} onClick={() => setDialog({ kind: "remove", game: r })}>Remove from console…</button>
                </div>
              )}
            </Section>
          );
        })}
        <StoreImageBrowser url={`/titles/${g.title_id}/store-images`} />
        {!session.readOnly && <SaveBackups tid={g.title_id} consoleId={consoleId} />}
      </section>
      {dialog?.kind === "move" && <MoveDialog consoleId={consoleId} game={dialog.game} drives={drives} onClose={() => { setDialog(null); load(); }} />}
      {dialog?.kind === "remove" && <RemoveDialog consoleId={consoleId} game={dialog.game} onClose={() => { setDialog(null); load(); }} />}
    </div>
  );
}
