import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { StatusNote } from "./Problem";
import { api, appVersion, formatBytes, JOB_ACTIVE, needsOnline, session, wakeConsole, type Console, type ConsoleState, type Drive, type InstalledGame, type Job, type JobStatus, type Move, type Removal, type Root } from "./api";

const JOB_LABEL: Record<JobStatus, [string, string]> = {
  queued: ["Waiting", "unknown"], preflight: ["Checking", "unknown"], extracting: ["Extracting", "needs_attention"], transferring: ["Copying", "needs_attention"],
  registering: ["Finishing", "needs_attention"], success: ["Installed", "ready"], failed: ["Failed", "incompatible"],
  canceled: ["Canceled", "unknown"], interrupted: ["Interrupted", "incompatible"],
};

/** Installs on this console: polls every 2 s while one is running. `onDone` re-reads the inventory. */
export function Jobs({ consoleId, onDone }: { consoleId: string; onDone: () => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const active = jobs.some((j) => JOB_ACTIVE.includes(j.status));

  useEffect(() => {
    let wasActive = false;
    const load = () => api<Job[]>(`/consoles/${consoleId}/jobs`).then((js) => {
      const now = js.some((j) => JOB_ACTIVE.includes(j.status));
      if (wasActive && !now) onDone();
      wasActive = now;
      setJobs(js);
    }, () => {});
    load();
    const t = setInterval(load, active ? 2000 : 15000);
    return () => clearInterval(t);
  }, [consoleId, active]);

  const recent = jobs.filter((j) => JOB_ACTIVE.includes(j.status) || Date.now() - utc(j.created_at).getTime() < 86400e3);
  if (!recent.length) return null;
  const cancel = (j: Job) => api<Job>(`/jobs/${j.id}/cancel`, { method: "POST" }).then((nj) => setJobs((js) => js.map((x) => (x.id === nj.id ? nj : x))), () => {});

  return (
    <section className="card">
      <h2>Installs</h2>
      <ul className="jobs">
        {recent.map((j) => {
          const [label, cls] = JOB_LABEL[j.status];
          return (
            <li key={j.id}>
              <span className={`badge ${cls}`}>{label}</span>
              <strong><a href={`#/game/${j.game_id}`}>{j.title}</a></strong>
              {(j.status === "transferring" || (j.status === "extracting" && j.bytes_transferred > 0)) && (
                <>
                  <progress max={1} value={j.progress} />
                  <span className="muted">
                    {formatBytes(j.bytes_transferred)} of {formatBytes(j.total_bytes)} · {formatBytes(j.transfer_rate_bps)}/s
                    {j.eta_seconds != null && ` · ${Math.ceil(j.eta_seconds / 60)} min left`}
                  </span>
                </>
              )}
              <span className="muted">{j.message}</span>
              {JOB_ACTIVE.includes(j.status) && !session.readOnly && <button className="small secondary" onClick={() => cancel(j)}>Cancel</button>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** Preview, typed title ID, then follows the removal until it ends. Closing early doesn't stop it. */
export function RemoveDialog({ consoleId, game, onClose }: { consoleId: string; game: InstalledGame; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [p, setP] = useState<Removal | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    ref.current?.showModal();
    api<Removal>(`/consoles/${consoleId}/uninstall/preview`, { json: { installed_game_id: game.id } }).then(setP, (e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (p?.status !== "removing") return;
    const t = setInterval(() => api<Removal>(`/removal-jobs/${p.id}`).then(setP, () => {}), 1000);
    return () => clearInterval(t);
  }, [p?.id, p?.status]);

  function confirm(e: FormEvent) {
    e.preventDefault();
    setError("");
    api<Removal>(`/consoles/${consoleId}/uninstall`, { json: { removal_id: p!.id, confirm_title_id: typed } }).then(setP, (e) => setError(e.message));
  }
  const ended = p?.status === "success" || p?.status === "failed";

  return (
    <dialog ref={ref} className="card remove" onClose={onClose} aria-labelledby="remove-title">
      <h2 id="remove-title">Remove {game.title} ({game.title_id})?</h2>
      {!p ? <p className="muted">{error || "Checking…"}</p> : !p.allowed ? <p>{p.reason}</p> : (
        <>{p.orphaned ? <>
          <p>Its home-screen tile points at files that are no longer on the console:</p>
          <code>{p.source_path}</code>
          <p>No space is freed. Save data and the copy in your PC library are kept.</p>
        </> : <>
          <p>This deletes the game's ShadowMountPlus files from the console:</p>
          <code>{p.source_path}</code>
          <p>Space freed: {p.estimated_bytes_freed ? `about ${formatBytes(p.estimated_bytes_freed)}` : "unknown"}. Save data and the copy in your PC library are kept.</p>
          <p className="muted">Its home-screen tile is removed too. If that fails, you'll be told to delete it on the PS5.</p>
        </>}
          {p.warnings.map((w) => <p className="muted" key={w}>{w}</p>)}
          {p.status === "awaiting_confirmation" && (
            <form onSubmit={confirm}>
              <label>Type {p.title_id} to confirm
                <input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" spellCheck={false} />
              </label>
              <button className="danger" disabled={typed.trim().toUpperCase() !== p.title_id}>Remove from console</button>
            </form>
          )}
          {p.status === "removing" && (
            <div role="status">
              <progress max={1} value={p.progress} aria-label="Deleting" />
              <p className="muted">{p.bytes_total ? `${formatBytes(p.bytes_done)} of ${formatBytes(p.bytes_total)} · ` : ""}{p.message}</p>
            </div>
          )}
          {ended && <p className={p.status === "failed" ? "error" : p.tile_left ? "callout" : ""} role="status">{p.message}</p>}
        </>
      )}
      {error && p && <p className="error" role="alert">{error}</p>}
      <button className="secondary" onClick={() => ref.current?.close()}>{ended || !p?.allowed ? "Close" : "Cancel"}</button>
    </dialog>
  );
}

/** Picks another drive, or a library folder on this PC for a game that isn't in the library, then follows the move.
 * Closing early doesn't stop it; Cancel does. */
export function MoveDialog({ consoleId, game, drives, onClose }: { consoleId: string; game: InstalledGame; drives: Drive[]; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const others = drives.filter((d) => d.id !== game.drive_id);
  const [roots, setRoots] = useState<Root[]>([]);
  const [did, setDid] = useState(others[0]?.id ?? "");
  const [m, setM] = useState<Move | null>(null);
  const [error, setError] = useState("");
  const root = roots.find((r) => `root:${r.id}` === did);
  useEffect(() => {
    ref.current?.showModal();
    if (!game.game_id) api<Root[]>("/library/roots").then((rs) => {
      const ok = rs.filter((r) => r.kind === "game" && r.enabled && r.reachable);
      setRoots(ok);
      setDid((cur) => cur || (ok[0] ? `root:${ok[0].id}` : ""));
    }, () => {});
  }, []);
  useEffect(() => {
    if (m?.status !== "moving") return;
    const t = setInterval(() => api<Move>(`/moves/${m.id}`).then(setM, () => {}), 1000);
    return () => clearInterval(t);
  }, [m?.id, m?.status]);

  function start(e: FormEvent) {
    e.preventDefault();
    setError("");
    const to = root ? { root_id: root.id } : { drive_id: did };
    api<Move>(`/consoles/${consoleId}/move`, { json: { installed_game_id: game.id, ...to } }).then(setM, (e) => setError(e.message));
  }
  const ended = m && m.status !== "moving";

  return (
    <dialog ref={ref} className="card remove" onClose={onClose} aria-labelledby="move-title">
      <h2 id="move-title">Move {game.title}</h2>
      {!m ? (others.length || roots.length ? (
        <form onSubmit={start}>
          {root ? <p>Spela copies <code>{game.source_path}</code> into <code>{root.path}</code> on this PC, checks the copy and adds it to
            your library. Then it deletes the game from the console.</p>
            : <p>The console copies <code>{game.source_path}</code> to the other drive itself, checks the copy, then deletes the original.
              {game.size_bytes > 0 && ` It needs about ${formatBytes(game.size_bytes)} free there.`}</p>}
          <label>Move to
            <select value={did} onChange={(e) => setDid(e.target.value)}>
              {others.map((d) => <option key={d.id} value={d.id}>{d.label}{d.total_bytes ? ` (${formatBytes(d.free_bytes)} free)` : ""}</option>)}
              {roots.map((r) => <option key={r.id} value={`root:${r.id}`}>This PC: {r.path}</option>)}
            </select>
          </label>
          <p className="muted">{root ? "Close the game on the PS5 first. Its home-screen tile is removed; save data stays on the console."
            : "Close the game on the PS5 first. Its home-screen tile is replaced by a new one from the new drive; save data is kept."}</p>
          <button disabled={!did}>Move</button>
        </form>
      ) : <p>This console has no other drive to move it to.</p>) : (
        <div role="status">
          {m.status === "moving" && <>
            <progress max={1} value={m.progress} aria-label="Copying" />
            <p className="muted">{m.bytes_total ? `${formatBytes(m.bytes_done)} of ${formatBytes(m.bytes_total)} · ` : ""}{m.message}</p>
          </>}
          {ended && <p className={m.status === "failed" ? "error" : m.tile_left ? "callout" : ""}>{m.message}</p>}
        </div>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {m?.status === "moving"
        ? <button className="secondary" onClick={() => api(`/moves/${m.id}/cancel`, { method: "POST" })}>Cancel move</button>
        : <button className="secondary" onClick={() => ref.current?.close()}>{ended ? "Close" : "Cancel"}</button>}
    </dialog>
  );
}

/** The portrait poster from PlayStation Store, else the console's square icon. */
export function Art({ g }: { g: InstalledGame }) {
  const [src, setSrc] = useState<string | null>(g.game_id ? `/api/v1/games/${g.game_id}/poster` : `/api/v1/titles/${g.title_id}/poster`);
  if (!src) return <div className="art placeholder" aria-hidden>{g.title.slice(0, 1)}</div>;
  const square = src === g.cover_url;
  return (
    <div className={square ? "art square" : "art"} style={square ? ({ "--art": `url("${src}")` } as CSSProperties) : undefined}>
      <img src={src} alt="" loading="lazy" onError={() => setSrc(square ? null : g.cover_url)} />
    </div>
  );
}

// Reuses the library badge colours.
export const STATE_BADGE: Record<ConsoleState, string> = {
  online: "ready", standby: "needs_attention", degraded: "needs_attention", offline: "incompatible", disabled: "unknown", unknown: "unknown",
};
export const STATE_LABEL: Record<ConsoleState, string> = {
  online: "Online", standby: "Rest mode", degraded: "Degraded", offline: "Offline", disabled: "Disabled", unknown: "Not checked yet",
};
const STATUS = {
  installed: ["Installed", "ready"],
  update_available: ["Update in library", "needs_attention"],
  partially_installed: ["Partly installed", "incompatible"],
  orphaned_icon: ["Icon only", "incompatible"],
  unknown: ["Unknown", "unknown"],
} as const;

// SQLite drops the zone from stored UTC times; without a "Z" the browser would read them as local time.
const utc = (t: string) => new Date(/(Z|[+-]\d\d:\d\d)$/.test(t) ? t : t + "Z");
export const when = (t: string | null) => (t ? utc(t).toLocaleString() : "Never");
const bytes = (n: number) => (n ? formatBytes(n) : "Unknown");

export default function Installed() {
  const [consoles, setConsoles] = useState<Console[] | null>(null);
  const [id, setId] = useState("");
  const [drives, setDrives] = useState<Drive[]>([]);
  const [games, setGames] = useState<InstalledGame[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [version, setVersion] = useState("");
  const [removing, setRemoving] = useState<InstalledGame | null>(null);
  const [moving, setMoving] = useState<InstalledGame | null>(null);
  useEffect(() => { appVersion.then(setVersion); }, []);

  const loadConsoles = () => api<Console[]>("/consoles").then((cs) => {
    setConsoles(cs);
    setId((cur) => cur || (cs.find((c) => c.enabled) ?? cs[0])?.id || "");
  }, (e) => setError(e.message));
  const loadInventory = (cid: string) => Promise.all([
    api<Drive[]>(`/consoles/${cid}/drives`).then(setDrives),
    api<InstalledGame[]>(`/consoles/${cid}/installed`).then(setGames),
  ]).catch((e) => setError(e.message));

  useEffect(() => {
    loadConsoles();
    const t = setInterval(loadConsoles, 15000); // the backend checks every 15 s
    return () => clearInterval(t);
  }, []);
  useEffect(() => { if (id) loadInventory(id); }, [id]);
  const [leftover, setLeftover] = useState(0); // bytes of hidden copies left by installs, moves and removals that stopped
  const loadLeftovers = (cid: string) =>
    api<{ bytes: number }>(`/consoles/${cid}/leftovers`).then((r) => setLeftover(r.bytes), () => setLeftover(0));
  const online = consoles?.find((x) => x.id === id)?.state === "online";
  useEffect(() => { setLeftover(0); if (id && online && !session.readOnly) loadLeftovers(id); }, [id, online]);
  useEffect(() => { // a removal or move still running after its dialog closed
    if (!id || !games.some((g) => g.removing || g.moving)) return;
    const t = setInterval(() => loadInventory(id), 3000);
    return () => clearInterval(t);
  }, [id, games]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return games.filter((g) => !needle || g.title.toLowerCase().includes(needle) || g.title_id.toLowerCase().includes(needle));
  }, [games, q]);

  if (error && !consoles) return <p className="error" role="alert">{error}</p>;
  if (!consoles) return <p className="muted">Loading…</p>;
  if (!consoles.length)
    return (
      <div className="empty card">
        <h2>No consoles yet</h2>
        <p>Add your PS5 under <a href="#/settings">Settings → Consoles</a>.</p>
      </div>
    );

  const c = consoles.find((x) => x.id === id) ?? consoles[0];
  const driveLabel = Object.fromEntries(drives.map((d) => [d.id, d.label]));
  const helperBad = !!version && c.helper_status.helper !== version;
  const app = c.helper_status.console_app; // not installed is a choice, nothing to update
  const consoleAppBad = !!version && !!app && app !== "missing" && app !== version;

  async function rescan() {
    setBusy(true);
    setError("");
    try {
      const updated = await api<Console>(`/consoles/${c.id}/rescan`, { method: "POST" });
      setConsoles((cs) => cs!.map((x) => (x.id === updated.id ? updated : x)));
      await loadInventory(c.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cleanUp() {
    if (!confirm(`Delete ${formatBytes(leftover)} of unfinished copies from ${c.name}?\n\nThey're hidden copies left by ` +
      "installs, moves or removals that stopped partway. Installed games, save data and everything else stay as they are.")) return;
    setBusy(true);
    setError("");
    try {
      await api(`/consoles/${c.id}/leftovers/clean`, { method: "POST" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
    await loadLeftovers(c.id);
    await rescan(); // the drives' free space
  }

  async function wake() {
    setBusy(true);
    setError("");
    try {
      const woken = await wakeConsole(c);
      setConsoles((cs) => cs!.map((x) => (x.id === woken.id ? woken : x)));
      if (woken.state === "standby") setError("It's still in rest mode. Check its Remote Play key and rest-mode settings.");
      else await loadInventory(c.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const setTarget = (d: Drive, on: boolean) =>
    api<Drive>(`/consoles/${c.id}/drives/${d.id}`, { method: "PATCH", json: { is_install_target: on } })
      .then((nd) => setDrives((ds) => ds.map((x) => (x.id === nd.id ? { ...nd, installed_count: x.installed_count } : x))), (e) => setError(e.message));

  return (
    <div className="installed">
      <div className="toolbar">
        {consoles.length > 1 ? (
          <select aria-label="Console" value={c.id} onChange={(e) => setId(e.target.value)}>
            {consoles.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        ) : <h2 className="inline-title">{c.name}</h2>}
        <span className={`badge ${STATE_BADGE[c.state]}`}>{STATE_LABEL[c.state]}</span>
        <StatusNote message={c.status_message} fix={c.status_fix} />
        {c.state === "standby" && c.has_wake_key && !session.readOnly && <button className="small" disabled={busy} onClick={wake}>{busy ? "Waking…" : "Wake"}</button>}
        {(helperBad || consoleAppBad) && !session.readOnly && <a href="#/settings">Update needed in Settings</a>}
        <span className="spacer" />
        {drives.map((d) => (
          <details key={d.id} className="drive" name="drive">{/* same name: the browser keeps only one open */}
            <summary>
              <strong>{d.label}</strong>
              {d.total_bytes > 0 && <meter min={0} max={d.total_bytes} value={d.total_bytes - d.free_bytes} />}
              <span className="muted">{bytes(d.free_bytes)} free</span>
            </summary>
            <div className="card">
              <span className="muted">{d.drive_type} · <code>{d.path}</code></span>
              {d.total_bytes > 0 && <span>{formatBytes(d.total_bytes - d.free_bytes)} used of {formatBytes(d.total_bytes)}</span>}
              <span className="muted">{d.installed_count} installed · last seen {when(d.last_seen_at)}</span>
              <label className="check">
                <input type="checkbox" disabled={session.readOnly} checked={d.is_install_target} onChange={(e) => setTarget(d, e.target.checked)} /> Allow installs here
              </label>
            </div>
          </details>
        ))}
        {!session.readOnly && <button {...needsOnline(c)} disabled={busy || c.state !== "online"} onClick={rescan}>{busy ? "Working…" : "Refresh inventory"}</button>}
      </div>
      {leftover > 0 && !session.readOnly && (
        <p className="muted">
          {formatBytes(leftover)} on this console is taken by unfinished copies, left by an install, move or removal that stopped partway.{" "}
          <button className="small secondary" disabled={busy || c.state !== "online"} onClick={cleanUp}>Clean up</button>
        </p>
      )}
      {error && <p className="error" role="alert">{error}</p>}

      <Jobs consoleId={c.id} onDone={() => loadInventory(c.id)} />

      {games.length > 0 ? (
        <>
          <div className="toolbar">
            <input type="search" placeholder="Search title or title ID" aria-label="Search installed games" value={q} onChange={(e) => setQ(e.target.value)} />
            <span className="muted">{shown.length} of {games.length}</span>
          </div>
          <ul className="games compact">
            {shown.map((g) => {
              const [label, cls] = g.removing ? ["Removing", "needs_attention"] : g.moving ? ["Moving", "needs_attention"] : STATUS[g.status];
              const tip = `${g.title_id} · v${g.version ?? "?"} · ${bytes(g.size_bytes)}\n${g.installed_path}`;
              const body = <>
                <Art g={g} />
                <div className="name">
                  <strong>{g.title}</strong>
                  <span className="genre">{(g.drive_id && driveLabel[g.drive_id]) || "Unknown drive"}</span>
                  {(g.removing || g.moving || g.status !== "installed") && <span className={`badge ${cls}`}>{label}</span>}
                </div>
              </>;
              return (
                <li key={g.id}>
                  <a className="game" href={g.game_id ? `#/game/${g.game_id}` : `#/installed/${c.id}/${g.id}`} title={tip}>{body}</a>
                  {g.install_route === "shadowmountplus" && !g.removing && !g.moving && g.status !== "orphaned_icon" && (drives.length > 1 || !g.game_id) && !session.readOnly && (
                    <button className="fav move" disabled={c.state !== "online"} title={c.state !== "online" ? "The console must be online." : "Move to another drive"}
                      aria-label={`Move ${g.title} to another drive`} onClick={() => setMoving(g)}>
                      <svg viewBox="0 0 24 24" aria-hidden><path d="M4 9h13l-4-4M20 15H7l4 4" /></svg>
                    </button>
                  )}
                  {g.install_route === "shadowmountplus" && !g.removing && !g.moving && !session.readOnly && (
                    <button className="fav" disabled={c.state !== "online"} title={c.state !== "online" ? "The console must be online." : "Remove from console"}
                      aria-label={`Remove ${g.title} from console`} onClick={() => setRemoving(g)}>
                      <svg viewBox="0 0 24 24" aria-hidden><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg>
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p className="muted">{c.last_inventory_scan_at ? "No games found on this console." : "Inventory not read yet. Click Refresh inventory."}</p>
      )}
      {moving && <MoveDialog consoleId={c.id} game={moving} drives={drives} onClose={() => { setMoving(null); loadInventory(c.id); }} />}
      {removing && <RemoveDialog consoleId={c.id} game={removing} onClose={() => { setRemoving(null); loadInventory(c.id); }} />}
    </div>
  );
}
