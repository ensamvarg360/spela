import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, formatBytes, formatLabel, needsOnline, session, SOURCE_LABEL, STATUS_LABEL, STATUS_TONE, useConsoles, type Console, type Drive, type Game, type InstalledGame, type Job } from "./api";
import { cart, useCart } from "./Cart";
import { Cover } from "./Library";
import { Art, coverVersion, MoveDialog, RemoveDialog } from "./Installed";
import { RESULT } from "./AuditLog";
import { toast, toastError } from "./Toast";
import { Badge, Bar, Collapsible, Empty, Field, Icon, Notice, PageHead, Panel, Path, Spinner, Time, type Tone } from "./ui";

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
  const [installed, setInstalled] = useState(false);
  const carted = useCart().includes(game.id);

  useEffect(() => {
    setDid("");
    if (cid) api<Drive[]>(`/consoles/${cid}/drives`).then((ds) => setDrives(ds.filter((d) => d.is_install_target)), () => setDrives([]));
  }, [cid]);
  useEffect(() => { // already on this console? It's moved from its "On <console>" panel instead
    setInstalled(false);
    if (cid && game.kind === "game" && game.title_id)
      api<InstalledGame[]>(`/consoles/${cid}/installed`).then(
        (rows) => setInstalled(rows.some((r) => r.title_id === game.title_id && r.status !== "orphaned_icon")), () => {});
  }, [cid, game.id]);

  const [blocked, setBlocked] = useState<Record<string, string>>({}); // game id -> why this console can't take it
  const ids = (copies.length ? copies : [opened]).map((c) => c.id).join(",");
  useEffect(() => {
    setBlocked({});
    if (cid) api<Record<string, string>>(`/consoles/${cid}/blockers?game_ids=${ids}`).then(setBlocked, () => {});
  }, [cid, ids, target?.firmware]);

  if (!consoles.length || (installed && consoles.length === 1)) return null;
  const why = installed ? `Already on ${target?.name}.` : blocked[game.id];

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
      toast(`Queued ${game.title}.`, false, ["Follow it on the Installed page", "#/installed"]);
    } catch (e) {
      toastError(e);
    }
  }

  const choices = consoles.length > 1 || copies.length > 1 || game.kind === "game";
  return (
    <Panel title={game.kind === "dlc" ? "Add to the game" : "Install"} desc={game.kind === "dlc" ? "DLC goes onto its game's drive." : undefined}
      foot={<>
        <span className="sp-panel__note">{target && target.state !== "online" ? `${target.name} must be online.` : carted ? <a className="sp-link" href="#/cart">Open the cart</a> : null}</span>
        <div className="sp-btngroup">
          <button type="button" className="sp-btn" aria-pressed={carted} onClick={() => (carted ? cart.remove : cart.add)([game.id])}>
            {carted ? <><Icon name="check" />In cart</> : <><Icon name="cart" />Add to cart</>}
          </button>
          <button type="button" className="sp-btn sp-btn--primary" {...needsOnline(target)} {...(why ? { disabled: true, title: why } : {})} onClick={install}>
            {game.kind === "dlc" ? "Add to game" : "Install"}</button>
        </div>
      </>}>
      {(choices || why) && <>
        {choices && (
          <div className="sp-formgrid">
            {consoles.length > 1 && (
              <Field label="Console">
                <select className="sp-select" value={cid} onChange={(e) => setCid(e.target.value)}>
                  {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </Field>
            )}
            {copies.length > 1 && (
              <Field label="Version">
                <select className="sp-select" value={game.id} onChange={(e) => setPick(e.target.value)}>
                  {copies.map((c) => <option key={c.id} value={c.id}>{formatLabel(c)}{c.version ? ` · v${c.version}` : ""} · {formatBytes(c.size_bytes)}{blocked[c.id] ? " · not for this console" : ""}</option>)}
                </select>
              </Field>
            )}
            {game.kind === "game" && (
              <Field label="Drive">
                <select className="sp-select" value={did} onChange={(e) => setDid(e.target.value)}>
                  <option value="">Most free space</option>
                  {drives.map((d) => <option key={d.id} value={d.id}>{d.label}{d.total_bytes ? ` (${formatBytes(d.free_bytes)} free)` : ""}</option>)}
                </select>
              </Field>
            )}
          </div>
        )}
        {why && !installed && <Notice tone="danger" title="This console can't run it" text={why} />}
      </>}
    </Panel>
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
    <Collapsible title="PlayStation Store images" meta="Every image the store has">
      {!stores && <div className="sp-btngroup"><button type="button" className="sp-btn" onClick={load} disabled={busy}>
        {busy ? <><Spinner />Asking PlayStation Store…</> : "Browse PlayStation Store images"}</button></div>}
      {error && <p className="sp-error" role="alert">{error}</p>}
      {stores?.map((s) => (
        <div key={s.store} className="sp-stack">
          <h3 className="sp-subhead">{s.store} store{s.name && ` · ${s.name}`}</h3>
          {!s.sources.length && <p className="sp-muted">Not sold in this store.</p>}
          {s.sources.map((src) => (
            <details key={src.label} className="sp-disclosure" open={src.label.startsWith("Concept")}>
              <summary><Icon name="chevronRight" />{src.label} · {src.images.length} {src.images.length === 1 ? "image" : "images"}</summary>
              <div className="sp-thumbs">
                {src.images.map((i) => (
                  <a key={i.url} href={i.url} target="_blank" rel="noreferrer">
                    <img src={i.url} alt={i.type ?? "Image"} loading="lazy" />
                    <span>{i.type ?? "Unknown type"}</span>
                  </a>
                ))}
              </div>
            </details>
          ))}
        </div>
      ))}
    </Collapsible>
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
    <li className="sp-listrow">
      <div className="sp-listrow__main">
        <div className="sp-listrow__title">Convert to .exfat</div>
        {converting ? <>
          <Bar value={conv.progress} label="Conversion progress" />
          <div className="sp-listrow__meta" role="status"><span className="sp-num">{Math.floor(conv.progress * 100)}%</span><span className="sp-sep" /><span>{conv.message}</span></div>
        </> : (
          <div className="sp-listrow__meta"><span>Writes this folder dump into one disc image next to it. The folder is kept.</span></div>
        )}
        {error && <p className="sp-error" role="alert">{error}</p>}
        {conv && !converting && (
          <div className={conv.status === "success" ? "sp-listrow__meta" : "sp-error"} role="status">
            <span>{conv.message}</span>{conv.image_game_id && <a className="sp-link" href={`#/game/${conv.image_game_id}`}>Open the image</a>}
          </div>
        )}
      </div>
      <div className="sp-listrow__aside">
        {converting
          ? <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost" onClick={() => api(`/games/${game.id}/convert/cancel`, { method: "POST" })}>Cancel</button>
          : <button type="button" className="sp-btn sp-btn--sm" onClick={start}>Convert</button>}
      </div>
    </li>
  );
}

type History = {
  installed: { id: string; console_id: string; console: string; drive: string; path: string; version: string | null; status: string }[];
  events: { at: string; what: string; result: string; console: string; drive: string; message: string }[];
};
const WHAT: Record<string, string> = { install: "Installed", uninstall: "Removed", move: "Moved" };
const EVENT_RESULT: Record<string, [string, Tone]> = { ...RESULT, interrupted: ["Interrupted", "danger"] };

/** What happened to it: installs, removals and moves. */
function InstallHistory({ h }: { h: History | null }) {
  if (!h?.events.length) return null;
  return (
    <>
      {h.events.length > 0 && (
        <Collapsible title="History" meta={`${h.events.length} ${h.events.length === 1 ? "event" : "events"}`}>
          <ul className="sp-list sp-boxed">{h.events.map((e, n) => {
            const [label, tone] = EVENT_RESULT[e.result] ?? [e.result, "warning"];
            return (
              <li key={n} className="sp-listrow sp-listrow--dense">
                <div className="sp-listrow__lead"><Badge tone={tone}>{label}</Badge></div>
                <div className="sp-listrow__main">
                  <div className="sp-listrow__title">{WHAT[e.what] ?? e.what}</div>
                  <div className="sp-listrow__meta">
                    {e.console && <><span>{e.console}</span><span className="sp-sep" /></>}
                    {e.drive && <><span>{e.drive}</span><span className="sp-sep" /></>}
                    <Time t={e.at} />
                    {e.message && <><span className="sp-sep" /><span>{e.message}</span></>}
                  </div>
                </div>
              </li>
            );
          })}</ul>
        </Collapsible>
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
    let done = false;  // confirm() blocks: without this, a tick queued behind it would ask again after Cancel
    const t = setInterval(() => api<SaveJob>(`/save-restores/${r.id}`).then((nr) => {
      if (done) return;
      if (nr.status !== "copying") (done = true), clearInterval(t);
      setR(nr);
      if (nr.status === "confirm" && confirm(`${nr.message}\n\nReplace them with the backup "${nr.backup}"? Spela first backs up ` +
        "the saves on the console to this PC. Close the game on the PS5 first.")) restore(nr.backup!, true);
      else if (nr.status !== "copying") load();
    }, () => {}), 1000);
    return () => ((done = true), clearInterval(t));
  }, [r?.id, r?.status]);

  if (!consoles.length) return null;
  const start = () => (setError(""), api<SaveJob>(`/consoles/${cid}/save-backups`, { json: { title_id: tid } }).then(setB, (e) => setError(e.message)));
  const busy = b?.status === "copying" || r?.status === "copying";
  const status = (j: SaveJob | null, label: string) => j && (j.status === "copying"
    ? <div className="sp-stack sp-stack--tight"><Bar value={j.progress} label={label} /><span className="sp-hint">{j.message}</span></div>
    : <p className={j.status === "failed" ? "sp-error" : "sp-hint"} role="status">{j.message}</p>);

  return (
    <Collapsible title="Save data" meta={list.length ? `${list.length} ${list.length === 1 ? "backup" : "backups"}` : "No backups yet"}>
      <p className="sp-muted">Copies this game's saves from the console to this PC, for every user, and back. They stay encrypted and only
        work on the same console and account. Restoring over saves already on the console asks first.</p>
      <div className="sp-row">
        {consoles.length > 1 && !consoleId && (
          <select className="sp-select sp-console-pick" aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
            {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <button type="button" className="sp-btn" {...needsOnline(target)} disabled={busy || !!needsOnline(target).disabled} onClick={start}>
          {b?.status === "copying" ? <><Spinner />Backing up…</> : "Back up saves"}</button>
      </div>
      {status(b, "Backing up")}
      {status(r, "Restoring")}
      {error && <p className="sp-error" role="alert">{error}</p>}
      {list.length > 0 && <ul className="sp-list sp-boxed">{list.map((s) => (
        <li key={s.name} className="sp-listrow sp-listrow--dense">
          <div className="sp-listrow__main">
            <div className="sp-listrow__title">{s.name}</div>
            <div className="sp-listrow__meta"><span className="sp-num">{s.files} {s.files === 1 ? "file" : "files"} · {formatBytes(s.bytes)}</span><span className="sp-sep" /><Path path={s.path} /></div>
          </div>
          <div className="sp-listrow__aside">
            <button type="button" className="sp-btn sp-btn--sm" {...needsOnline(target)} disabled={busy || !!needsOnline(target).disabled}
              onClick={() => restore(s.name)}>{r?.status === "copying" && r.backup === s.name ? "Restoring…" : "Restore"}</button>
          </div>
        </li>
      ))}</ul>}
    </Collapsible>
  );
}

/** Under the cover: puts the game's PlayStation Store logo on the home screen of each console it's on (`consoleIds`), now.
 * Spela also does this by itself each time a console comes online; this is for a game that missed it, or to see why. */
function HomeLogo({ tid, consoleIds }: { tid: string; consoleIds: string[] }) {
  const { consoles } = useConsoles();
  const on = consoles.filter((c) => consoleIds.includes(c.id));
  const [busy, setBusy] = useState(false);
  if (!on.length || session.readOnly) return null;
  const online = on.filter((c) => c.state === "online");
  async function push() {
    setBusy(true);
    try {
      for (const c of online) await api(`/consoles/${c.id}/home-logo`, { json: { title_id: tid } });
      toast(`Logo is on the home screen of ${online.map((c) => c.name).join(" and ")}.`);
    } catch (e) {
      toastError(e);
    } finally {
      setBusy(false);
    }
  }
  return <>
    <button type="button" className="sp-btn" disabled={busy || !online.length} onClick={push}
      title={online.length ? "Shows the game's official logo from PlayStation Store on the PS5 home screen instead of its name" : "The console must be online."}>
      {busy ? <><Spinner />Putting it there…</> : "Put logo on home screen"}</button>
  </>;
}

const TEXT_FIELDS = [
  ["title", "Title"],
  ["title_id", "Title ID"],
  ["version", "Version"],
  ["region", "Region"],
  ["genre", "Genre"],
] as const;
const MONO = ["title_id", "version"];

const BACK: Record<string, string> = { "/": "Library", "/dlc": "DLC", "/installed": "Installed", "/dashboard": "Dashboard", "/cart": "Cart" };
const CONFIDENCE: Record<Game["metadata_confidence"], [string, Tone]> = {
  high: ["High confidence", "success"], medium: ["Medium confidence", "info"], low: ["Low confidence", "warning"], unknown: ["Unknown confidence", "warning"],
};
const COPY_TYPE: Record<Game["owned_copy_type"], string> = { disc: "Disc", digital: "Digital", other: "Other", unknown: "Unknown" };

/** The tools panel's rows, when there's any. */
const Tools = ({ children }: { children: ReactNode[] }) =>
  children.some(Boolean) ? <Panel title="More actions" flush><ul className="sp-list">{children}</ul></Panel> : null;

/** `from`: the page the user came from, so ← goes back there; null (opened by link) falls back to the game's list. */
export default function GameDetail({ id, from }: { id: string; from: string | null }) {
  const [game, setGame] = useState<Game | null>(null);
  const [draft, setDraft] = useState<Partial<Game>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    api<Game>(`/games/${id}`).then(setGame, (e) => setError(e.message));
  }, [id]);
  const [history, setHistory] = useState<History | null>(null);
  useEffect(() => { setHistory(null); api<History>(`/games/${id}/history`).then(setHistory, () => {}); }, [id]);
  const { consoles } = useConsoles();
  const onConsoles = [...new Set(history?.installed.map((i) => i.console_id))];

  if (!game) return error ? <p className="sp-error" role="alert">{error}</p> : <p className="sp-muted">Loading…</p>;

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

  const [confidence, confidenceTone] = CONFIDENCE[game.metadata_confidence];
  const unsure = game.metadata_confidence === "low" || game.metadata_confidence === "unknown";

  return (
    <div className="sp-detail">
      <aside className="sp-detail__aside">
        <Cover key={game.updated_at} game={game} />
        {!session.readOnly && <>
          <button type="button" className="sp-btn sp-btn--ghost" aria-pressed={game.favorite}
            onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { favorite: !game.favorite } }), game.favorite ? "Removed from favorites." : "Added to favorites.")}>
            <Icon name="star" filled={game.favorite} />{game.favorite ? "Favorite" : "Add to favorites"}
          </button>
          {game.kind === "game" && game.title_id && <HomeLogo tid={game.title_id} consoleIds={onConsoles} />}
          <label className="sp-btn">
            Change cover
            <input type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          </label>
        </>}
      </aside>

      <div className="sp-detail__main">
        <PageHead back={[`#${back}`, BACK[back]]}
          title={<>{game.title}{game.installed
            ? <Badge tone="success" dot>Installed</Badge>
            : <Badge tone={STATUS_TONE[game.compatibility_status]}>{STATUS_LABEL[game.compatibility_status]}</Badge>}</>}
          desc={<>{game.title_id && <><span className="sp-code">{game.title_id}</span> · </>}{formatLabel(game)} · <span className="sp-num">{formatBytes(game.size_bytes)}</span>
            {game.version && <> · <span className="sp-num">v{game.version}</span></>}{game.region && ` · ${game.region}`}</>} />

        {game.compatibility_status !== "ready" && game.compatibility_reason && (
          <Notice tone={STATUS_TONE[game.compatibility_status] || "warning"} title={STATUS_LABEL[game.compatibility_status]} text={game.compatibility_reason} />
        )}
        {unsure && (
          <Notice tone="warning" title={game.title_id ? "Check the title ID before installing" : "No title ID found"}
            text={game.title_id
              ? `It was worked out from the ${SOURCE_LABEL[game.metadata_source]?.toLowerCase() ?? game.metadata_source}, because the package's own metadata couldn't be read.`
              : "Enter it under Metadata below."}
            action={game.title_id && !session.readOnly && (
              <button type="button" className="sp-btn sp-btn--sm" onClick={() => run(api<Game>(`/games/${id}`, { method: "PATCH", json: { title_id: game.title_id } }), "Title ID confirmed.")}>
                {game.title_id} is correct
              </button>
            )} />
        )}

        {game.title_id && onConsoles.map((cid) =>
          <OnConsole key={cid} consoleId={cid} console={consoles.find((c) => c.id === cid)} tid={game.title_id!} />)}
        {!session.readOnly && <Install game={game} />}
        {!session.readOnly && game.title_id && game.kind === "game" && <SaveBackups tid={game.title_id} />}
        <Collapsible title="File details" meta={`${formatLabel(game)} · ${formatBytes(game.size_bytes)}`} open>
          <dl className="sp-kv">
            <dt>Metadata source</dt>
            <dd><Badge tone={confidenceTone} mono>{SOURCE_LABEL[game.metadata_source] ?? game.metadata_source}</Badge><span className="sp-muted">{confidence}</span></dd>
            <dt>Content ID</dt><dd>{game.content_id ? <Path path={game.content_id} /> : <span className="sp-muted">—</span>}</dd>
            <dt>Format</dt><dd>{formatLabel(game)}</dd>
            <dt>Size</dt><dd className="sp-num">{formatBytes(game.size_bytes)}</dd>
            <dt>Source</dt><dd><Path path={game.source_path} copy />{game.missing && <Badge tone="danger">Missing</Badge>}</dd>
          </dl>
        </Collapsible>
        <InstallHistory h={history} />
        {!session.readOnly && <Tools>{[
          game.format === "folder_dump" && <Convert key="convert" game={game} />,
        ]}</Tools>}

        <form className="sp-stack" onSubmit={save}>
          {game.title_id && <StoreImageBrowser url={`/games/${game.id}/store-images`} />}

          <fieldset className="sp-stack sp-fieldset" disabled={session.readOnly}>
            <Collapsible title="Metadata" meta="Title, IDs, genre and description" open={unsure && !game.title_id}>
              <div className="sp-formgrid">
                {TEXT_FIELDS.map(([k, label]) => (
                  <Field key={k} label={label}>
                    <input className={MONO.includes(k) ? "sp-input sp-input--mono" : "sp-input"} value={val(k) ?? ""} onChange={(e) => set(k, e.target.value)} />
                  </Field>
                ))}
                <Field label="Release date">
                  <input className="sp-input" type="date" value={val("release_date") ?? ""} onChange={(e) => set("release_date", e.target.value)} />
                </Field>
                <Field wide label="Description">
                  <textarea className="sp-textarea" rows={3} value={val("description") ?? ""} onChange={(e) => set("description", e.target.value)} />
                </Field>
              </div>
            </Collapsible>

            <Collapsible title="Provenance" meta={`${COPY_TYPE[game.owned_copy_type]} copy`}>
              <div className="sp-formgrid">
                <Field label="Owned copy">
                  <select className="sp-select" value={val("owned_copy_type")} onChange={(e) => set("owned_copy_type", e.target.value)}>
                    {(Object.keys(COPY_TYPE) as Game["owned_copy_type"][]).map((t) => <option key={t} value={t}>{COPY_TYPE[t]}</option>)}
                  </select>
                </Field>
                <Field label="Backup date">
                  <input className="sp-input" type="date" value={val("backup_date") ?? ""} onChange={(e) => set("backup_date", e.target.value)} />
                </Field>
                <Field wide label="Notes">
                  <textarea className="sp-textarea" rows={3} value={val("provenance_notes") ?? ""} onChange={(e) => set("provenance_notes", e.target.value)} />
                </Field>
              </div>
            </Collapsible>
          </fieldset>
          {!session.readOnly && Object.keys(draft).length > 0 && (
            <div className="sp-row">
              <span className="sp-hint">Unsaved changes</span>
              <div className="sp-btngroup sp-push">
                <button type="button" className="sp-btn sp-btn--ghost" onClick={() => setDraft({})}>Discard</button>
                <button type="submit" className="sp-btn sp-btn--primary">Save changes</button>
              </div>
            </div>
          )}

          {game.parser_warnings.length > 0 && (
            <Collapsible title="Parser warnings" meta={`${game.parser_warnings.length}`}>
              <ul className="sp-bullets">{game.parser_warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </Collapsible>
          )}
          <Collapsible title="Detection evidence" meta="What the scanner saw">
            <pre className="sp-pre">{JSON.stringify(game.detection_evidence, null, 2)}</pre>
          </Collapsible>
        </form>

        {!session.readOnly && <div className="sp-row"><button type="button" className="sp-btn sp-btn--danger" onClick={remove}>Remove from catalog</button></div>}
      </div>
    </div>
  );
}

/** A console's inventory: its drives and installed copies. Re-read every 3 s while a removal or move runs. */
function useConsoleInventory(consoleId: string) {
  const [rows, setRows] = useState<InstalledGame[] | null>(null);
  const [drives, setDrives] = useState<Drive[]>([]);
  const load = () => Promise.all([
    api<Drive[]>(`/consoles/${consoleId}/drives`).then(setDrives),
    api<InstalledGame[]>(`/consoles/${consoleId}/installed`).then(setRows),
  ]).catch(() => setRows((cur) => cur ?? []));
  useEffect(() => { setRows(null); load(); }, [consoleId]);
  const busy = !!rows?.some((r) => r.removing || r.moving);
  useEffect(() => { // still running after its dialog closed
    if (!busy) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [busy]);
  return { rows, drives, load };
}

/** One panel per copy of the game on a console (one per drive): what the console says about it, Move and Remove. */
function ConsoleCopies({ console: c, copies, drives, reload }: { console?: Console; copies: InstalledGame[]; drives: Drive[]; reload: () => void }) {
  const [dialog, setDialog] = useState<{ kind: "move" | "remove"; game: InstalledGame } | null>(null);
  const online = c?.state === "online";
  const name = c?.name ?? "the console";
  return <>
    {copies.map((r) => {
      const smp = r.install_route === "shadowmountplus" && !r.removing && !r.moving && !session.readOnly;
      const drive = drives.find((d) => d.id === r.drive_id)?.label ?? "Unknown drive";
      return (
        <Panel key={r.id} title={copies.length > 1 ? `On ${name} · ${drive}` : `On ${name}`}
          badge={(r.removing || r.moving) && <Badge tone="info"><Spinner />{r.removing ? "Removing" : "Moving"}</Badge>}
          foot={smp && <>
            <span className="sp-panel__note">{!online ? `${c?.name ?? "The console"} must be online.` : "Close the game on the PS5 first."}</span>
            <div className="sp-btngroup">
              {r.status !== "orphaned_icon" && <button type="button" className="sp-btn" {...needsOnline(c)} disabled={!online} onClick={() => setDialog({ kind: "move", game: r })}><Icon name="move" />Move…</button>}
              <button type="button" className="sp-btn sp-btn--danger" {...needsOnline(c)} disabled={!online} onClick={() => setDialog({ kind: "remove", game: r })}>Remove from console…</button>
            </div>
          </>}>
          <dl className="sp-kv">
            <dt>Drive</dt><dd>{drive}</dd>
            <dt>Version</dt><dd>{r.version ? <span className="sp-num">{r.version}</span> : <span className="sp-muted">Unknown</span>}</dd>
            <dt>Size</dt><dd>{r.size_bytes ? <span className="sp-num">{formatBytes(r.size_bytes)}</span> : <span className="sp-muted">Unknown</span>}</dd>
            <dt>Path</dt><dd><Path path={r.source_path ?? r.installed_path} copy /></dd>
            <dt>Last checked</dt><dd><Time t={r.last_verified_at} /></dd>
          </dl>
        </Panel>
      );
    })}
    {dialog?.kind === "move" && c && <MoveDialog consoleId={c.id} game={dialog.game} drives={drives} onClose={() => { setDialog(null); reload(); }} />}
    {dialog?.kind === "remove" && c && <RemoveDialog consoleId={c.id} game={dialog.game} onClose={() => { setDialog(null); reload(); }} />}
  </>;
}

/** A library game's copies on one console, for its page. */
function OnConsole({ consoleId, console: c, tid }: { consoleId: string; console?: Console; tid: string }) {
  const { rows, drives, load } = useConsoleInventory(consoleId);
  return <ConsoleCopies console={c} copies={(rows ?? []).filter((r) => r.title_id === tid)} drives={drives} reload={load} />;
}

/** A game on a console that isn't in the PC library: the same page as a library game's, without what needs its files on
 * this PC. A library game opens its own page instead (the Installed page links there). */
export function ConsoleGameDetail({ consoleId, id }: { consoleId: string; id: string }) {
  const { consoles } = useConsoles();
  const c = consoles.find((x) => x.id === consoleId);
  const { rows, drives, load } = useConsoleInventory(consoleId);
  const [tid, setTid] = useState<string | null>(null);
  const [coverV, setCoverV] = useState(0);
  // a move to another drive gives the copy a new id, so the page follows the title ID once it's known
  useEffect(() => { if (rows && !tid) setTid(rows.find((r) => r.id === id)?.title_id ?? null); }, [rows]);
  if (!rows) return <p className="sp-muted">Loading…</p>;
  const copies = rows.filter((r) => r.title_id === (tid ?? rows.find((r) => r.id === id)?.title_id));
  const g = copies[0];
  if (!g) return <>
    <PageHead back={["#/installed", "Installed"]} title="Not on the console" />
    <div className="sp-panel">
      <Empty icon="inbox" title="This game isn't on the console any more" text="If you moved it to this PC, it's in your library.">
        <a className="sp-btn" href="#/">Open your library</a>
      </Empty>
    </div>
  </>;

  return (
    <div className="sp-detail">
      <aside className="sp-detail__aside">
        <Art key={`${g.id}-${coverV}`} g={g} />
        <HomeLogo tid={g.title_id} consoleIds={[consoleId]} />
        {!session.readOnly && !g.game_id && <label className="sp-btn">
          Change cover
          <input type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            const body = new FormData();
            body.append("file", file);
            api(`/titles/${g.title_id}/cover`, { body }).then(() => (setCoverV(coverVersion[g.title_id] = Date.now()), toast("Cover updated.")), toastError);
          }} />
        </label>}
      </aside>
      <div className="sp-detail__main">
        <PageHead back={["#/installed", "Installed"]} title={<>{g.title}<Badge tone="success" dot>Installed</Badge></>}
          desc={<span className="sp-code">{g.title_id}</span>}
          actions={g.game_id && <a className="sp-btn" href={`#/game/${g.game_id}`}>Open in your PC library</a>} />
        {!g.game_id && <Notice title="Not in your PC library" text="Move it to this PC to keep a copy there." />}
        <ConsoleCopies console={c} copies={copies} drives={drives} reload={load} />
        {!session.readOnly && <SaveBackups tid={g.title_id} consoleId={consoleId} />}
        <StoreImageBrowser url={`/titles/${g.title_id}/store-images`} />
      </div>
    </div>
  );
}
