import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from "react";
import Problem, { StatusNote } from "./Problem";
import { api, appVersion, formatBytes, JOB_ACTIVE, needsOnline, session, wakeConsole, type Console, type ConsoleState, type Drive, type InstalledGame, type Job, type JobStatus, type Move, type Removal, type Root } from "./api";
import { Badge, Bar, Collapsible, Empty, Field, Icon, Meter, Notice, PageHead, Panel, Path, Search, Spinner, Time, utc, type Tone } from "./ui";

const JOB_LABEL: Record<JobStatus, [string, Tone]> = {
  queued: ["Waiting", "info"], preflight: ["Checking", "info"], extracting: ["Extracting", "info"], transferring: ["Copying", "info"],
  registering: ["Finishing", "info"], success: ["Installed", "success"], failed: ["Failed", "danger"],
  canceled: ["Canceled", ""], interrupted: ["Interrupted", "danger"],
};

/** Installs on this console: polls every 2 s while one is running. `onDone` re-reads the inventory. */
export function Jobs({ consoleId, name, onDone }: { consoleId: string; name?: string; onDone: () => void }) {
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
    <Panel title={name ? `Installs on ${name}` : "Installs"} desc="They run one after another. Finished ones stay here for a day." flush>
      <ul className="sp-list">
        {recent.map((j) => {
          const [label, tone] = JOB_LABEL[j.status];
          const copying = j.status === "transferring" || (j.status === "extracting" && j.bytes_transferred > 0);
          return (
            <li key={j.id} className="sp-listrow">
              <div className="sp-listrow__lead"><Badge tone={tone}>{label}</Badge></div>
              <div className="sp-listrow__main">
                <div className="sp-listrow__title"><a className="sp-link" href={`#/game/${j.game_id}`}>{j.title}</a></div>
                {copying && <Bar value={j.progress} label={`Copying ${j.title}`} />}
                <div className="sp-listrow__meta">
                  {copying && <>
                    <span className="sp-num">{formatBytes(j.bytes_transferred)} of {formatBytes(j.total_bytes)}</span><span className="sp-sep" />
                    <span className="sp-num">{formatBytes(j.transfer_rate_bps)}/s</span>
                    {j.eta_seconds != null && <><span className="sp-sep" /><span className="sp-num">{Math.ceil(j.eta_seconds / 60)} min left</span></>}
                    {j.message && <span className="sp-sep" />}
                  </>}
                  {j.message && <span>{j.message}</span>}
                </div>
              </div>
              {JOB_ACTIVE.includes(j.status) && !session.readOnly && (
                <div className="sp-listrow__aside"><button type="button" className="sp-btn sp-btn--sm sp-btn--ghost" onClick={() => cancel(j)}>Cancel</button></div>
              )}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

/** A modal Panel. Closing it (Esc, or the close button) calls `onClose`. */
function Dialog({ dref, onClose, title, badge, foot, children }: {
  dref: React.RefObject<HTMLDialogElement | null>; onClose: () => void; title: ReactNode; badge?: ReactNode; foot: ReactNode; children: ReactNode;
}) {
  return (
    <dialog ref={dref} className="sp-dialog" onClose={onClose}>
      <Panel title={title} badge={badge} foot={foot}>{children}</Panel>
    </dialog>
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
  const asking = p?.allowed && p.status === "awaiting_confirmation";

  return (
    <Dialog dref={ref} onClose={onClose} title={`Remove ${game.title}?`} badge={<span className="sp-code">{game.title_id}</span>}
      foot={<>
        <button type="button" className="sp-btn sp-btn--ghost" onClick={() => ref.current?.close()}>{ended || !p?.allowed ? "Close" : "Cancel"}</button>
        {asking && <button type="submit" form="remove-confirm" className="sp-btn sp-btn--danger-solid" disabled={typed.trim().toUpperCase() !== p.title_id}>Remove from console</button>}
      </>}>
      {!p ? (error ? <Problem error={error} /> : <p className="sp-muted">Checking…</p>) : !p.allowed ? <Notice tone="warning" title="This can't be removed" text={p.reason} /> : (
        <>{p.orphaned ? <>
          <p>Its home-screen tile points at files that are no longer on the console:</p>
          {p.source_path && <Path path={p.source_path} copy />}
          <p className="sp-muted">No space is freed. Save data and the copy in your PC library are kept.</p>
        </> : <>
          <p>This deletes the game's ShadowMountPlus files from the console:</p>
          {p.source_path && <Path path={p.source_path} copy />}
          <dl className="sp-kv">
            <dt>Space freed</dt><dd className="sp-num">{p.estimated_bytes_freed ? `About ${formatBytes(p.estimated_bytes_freed)}` : <span className="sp-muted">Unknown</span>}</dd>
            <dt>Kept</dt><dd>Save data and the copy in your PC library</dd>
          </dl>
          <p className="sp-muted">Its home-screen tile is removed too. If that fails, you'll be told to delete it on the PS5.</p>
        </>}
          {p.warnings.map((w) => <Notice key={w} tone="warning" title={w} />)}
          {asking && (
            <form id="remove-confirm" onSubmit={confirm}>
              <Field label={<>Type <span className="sp-code">{p.title_id}</span> to confirm</>}>
                <input className="sp-input sp-input--mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" spellCheck={false} />
              </Field>
            </form>
          )}
          {p.status === "removing" && (
            <div className="sp-stack sp-stack--tight" role="status">
              <Bar value={p.progress} label="Deleting" />
              <span className="sp-hint sp-num">{p.bytes_total ? `${formatBytes(p.bytes_done)} of ${formatBytes(p.bytes_total)} · ` : ""}{p.message}</span>
            </div>
          )}
          {ended && <Notice tone={p.status === "failed" ? "danger" : p.tile_left ? "warning" : "success"} title={p.status === "failed" ? "Couldn't remove it" : "Removed"} text={p.message} />}
          {error && <Problem error={error} />}
        </>
      )}
    </Dialog>
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
  const choosing = !m && (others.length > 0 || roots.length > 0);

  return (
    <Dialog dref={ref} onClose={onClose} title={`Move ${game.title}`}
      foot={m?.status === "moving"
        ? <button type="button" className="sp-btn" onClick={() => api(`/moves/${m.id}/cancel`, { method: "POST" })}>Cancel move</button>
        : <>
          <button type="button" className="sp-btn sp-btn--ghost" onClick={() => ref.current?.close()}>{ended || !choosing ? "Close" : "Cancel"}</button>
          {choosing && <button type="submit" form="move-start" className="sp-btn sp-btn--primary" disabled={!did}>Move</button>}
        </>}>
      {!m ? (choosing ? (
        <form id="move-start" className="sp-stack" onSubmit={start}>
          <p>{root ? <>Spela copies the game into {root.path} on this PC, checks the copy and adds it to your library. Then it deletes the game from the console.</>
            : <>The console copies the game to the other drive itself, checks the copy, then deletes the original.
              {game.size_bytes > 0 && ` It needs about ${formatBytes(game.size_bytes)} free there.`}</>}</p>
          {game.source_path && <Path path={game.source_path} copy />}
          <Field label="Move to" hint={root ? "Close the game on the PS5 first. Its home-screen tile is removed; save data stays on the console."
            : "Close the game on the PS5 first. Its home-screen tile is replaced by one from the new drive; save data is kept."}>
            <select className="sp-select" value={did} onChange={(e) => setDid(e.target.value)}>
              {others.map((d) => <option key={d.id} value={d.id}>{d.label}{d.total_bytes ? ` (${formatBytes(d.free_bytes)} free)` : ""}</option>)}
              {roots.map((r) => <option key={r.id} value={`root:${r.id}`}>This PC: {r.path}</option>)}
            </select>
          </Field>
        </form>
      ) : <p>This console has no other drive to move it to.</p>) : (
        <div className="sp-stack" role="status">
          {m.status === "moving" && <div className="sp-stack sp-stack--tight">
            <Bar value={m.progress} label="Copying" />
            <span className="sp-hint sp-num">{m.bytes_total ? `${formatBytes(m.bytes_done)} of ${formatBytes(m.bytes_total)} · ` : ""}{m.message}</span>
          </div>}
          {ended && <Notice tone={m.status === "failed" ? "danger" : m.tile_left ? "warning" : "success"} title={m.status === "failed" ? "Couldn't move it" : "Moved"} text={m.message} />}
        </div>
      )}
      {error && <Problem error={error} />}
    </Dialog>
  );
}

/** The portrait poster from PlayStation Store, else the console's square icon. `children` are the overlays on the art. */
export function Art({ g, children }: { g: InstalledGame; children?: ReactNode }) {
  const [src, setSrc] = useState<string | null>(g.game_id ? `/api/v1/games/${g.game_id}/poster` : `/api/v1/titles/${g.title_id}/poster`);
  if (!src) return <div className="sp-cover__art"><span className="sp-cover__ph">{g.title}</span>{children}</div>;
  const square = src === g.cover_url;
  return (
    <div className={square ? "sp-cover__art sp-cover__art--square" : "sp-cover__art"} style={square ? ({ "--art": `url("${src}")` } as CSSProperties) : undefined}>
      <img src={src} alt="" loading="lazy" onError={() => setSrc(square ? null : g.cover_url)} />
      {children}
    </div>
  );
}

export const STATE_TONE: Record<ConsoleState, Tone> = {
  online: "success", standby: "warning", degraded: "warning", offline: "danger", disabled: "", unknown: "",
};
export const STATE_LABEL: Record<ConsoleState, string> = {
  online: "Online", standby: "Rest mode", degraded: "Degraded", offline: "Offline", disabled: "Disabled", unknown: "Not checked yet",
};
const STATUS: Record<InstalledGame["status"], [string, Tone]> = {
  installed: ["Installed", "success"],
  update_available: ["Update in library", "warning"],
  partially_installed: ["Partly installed", "danger"],
  orphaned_icon: ["Icon only", "danger"],
  unknown: ["Unknown", ""],
};
const DRIVE_TYPE: Record<Drive["drive_type"], string> = { internal: "Internal", usb: "USB", network: "Network", unknown: "Unknown type" };

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

  // one cover per title: a game on several drives lists each copy on its detail page
  const titles = useMemo(() => {
    const by = new Map<string, InstalledGame[]>();
    for (const g of games) by.set(g.title_id, [...(by.get(g.title_id) ?? []), g]);
    return [...by.values()];
  }, [games]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return titles.filter(([g]) => !needle || g.title.toLowerCase().includes(needle) || g.title_id.toLowerCase().includes(needle));
  }, [titles, q]);

  if (error && !consoles) return <Problem error={error} />;
  if (!consoles) return <p className="sp-muted">Loading…</p>;
  if (!consoles.length)
    return (
      <>
        <PageHead title="Installed" />
        <div className="sp-panel">
          <Empty icon="inbox" title="No consoles yet" text="Add your PS5 in Settings to see what's installed on it.">
            <a className="sp-btn sp-btn--primary" href="#/settings">Add console</a>
          </Empty>
        </div>
      </>
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
    <>
      <PageHead title={<>{c.name}<Badge tone={STATE_TONE[c.state]} dot>{STATE_LABEL[c.state]}</Badge></>}
        desc={c.state !== "online" && c.status_message ? <StatusNote message={c.status_message} fix={c.status_fix} /> : `What's installed on ${c.name}, drive by drive.`}
        actions={<>
          {consoles.length > 1 && (
            <select className="sp-select sp-console-pick" aria-label="Console" value={c.id} onChange={(e) => setId(e.target.value)}>
              {consoles.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          )}
          {c.state === "standby" && c.has_wake_key && !session.readOnly && <button type="button" className="sp-btn sp-btn--primary" disabled={busy} onClick={wake}><Icon name="power" />{busy ? "Waking…" : "Wake"}</button>}
          {!session.readOnly && <button type="button" className="sp-btn" {...needsOnline(c)} disabled={busy || c.state !== "online"} onClick={rescan}>
            {busy ? <Spinner /> : <Icon name="refresh" />}Refresh inventory</button>}
        </>} />

      {(helperBad || consoleAppBad) && !session.readOnly && (
        <Notice tone="warning" title={helperBad ? "The helper is out of date" : "The PS5 app is out of date"}
          text={`This version of Spela needs v${version}. Some installs won't work until it's updated.`}
          action={<a className="sp-btn sp-btn--sm" href="#/settings">Open Settings</a>} />
      )}
      {leftover > 0 && !session.readOnly && (
        <Notice title={`${formatBytes(leftover)} taken by unfinished copies`} text="Left by an install, move or removal that stopped partway."
          action={<button type="button" className="sp-btn sp-btn--sm" disabled={busy || c.state !== "online"} onClick={cleanUp}>Clean up</button>} />
      )}
      {error && <Problem error={error} />}

      {drives.length > 0 && (
        <Collapsible title="Storage" meta={`${drives.length} ${drives.length === 1 ? "drive" : "drives"} · ${formatBytes(drives.reduce((n, d) => n + d.free_bytes, 0))} free`}>
          <p className="sp-muted">Turn off a drive to keep Spela from installing there.</p>
          <ul className="sp-list sp-boxed">
            {drives.map((d) => (
              <li key={d.id} className="sp-listrow">
                <div className="sp-listrow__lead">
                  <label className="sp-switch">
                    <input type="checkbox" role="switch" disabled={session.readOnly} checked={d.is_install_target} onChange={(e) => setTarget(d, e.target.checked)} />
                    <span className="sp-switch__track" />
                    <span className="sp-visually-hidden">Allow installs on {d.label}</span>
                  </label>
                </div>
                <div className="sp-listrow__main">
                  <div className="sp-listrow__title">{d.label}{!d.is_install_target && <Badge>No installs</Badge>}</div>
                  <div className="sp-listrow__meta">
                    <span>{DRIVE_TYPE[d.drive_type]}</span><span className="sp-sep" />
                    <Path path={d.path} /><span className="sp-sep" />
                    <span className="sp-num">{d.installed_count} installed</span><span className="sp-sep" />
                    <span>Seen <Time t={d.last_seen_at} /></span>
                  </div>
                </div>
                <div className="sp-listrow__aside sp-drivemeter">
                  {d.total_bytes > 0 ? <>
                    <Meter used={d.total_bytes - d.free_bytes} total={d.total_bytes} label={d.label} />
                    <span className="sp-meter__value">{formatBytes(d.free_bytes)} free of {formatBytes(d.total_bytes)}</span>
                  </> : <span className="sp-meter__value">Space unknown</span>}
                </div>
              </li>
            ))}
          </ul>
        </Collapsible>
      )}

      <Jobs consoleId={c.id} onDone={() => loadInventory(c.id)} />

      {games.length > 0 ? (
        <div className="sp-stack">
          <div className="sp-toolbar">
            <Search value={q} onChange={setQ} shown={shown.length} total={titles.length} noun="games" label="Search installed games" />
          </div>
          <div className="sp-covergrid sp-covergrid--small">
            {shown.map((copies) => {
              const g = copies.find((x) => x.removing || x.moving || x.status !== "installed") ?? copies[0];
              const single = copies.length === 1;
              const [label, tone]: [string, Tone] = g.removing ? ["Removing", "info"] : g.moving ? ["Moving", "info"] : STATUS[g.status];
              const tip = copies.map((x) => `${x.title_id} · v${x.version ?? "?"} · ${x.size_bytes ? formatBytes(x.size_bytes) : "size unknown"}\n${x.installed_path}`).join("\n");
              const act = single && g.install_route === "shadowmountplus" && !g.removing && !g.moving && !session.readOnly;
              const canMove = act && g.status !== "orphaned_icon" && (drives.length > 1 || !g.game_id);
              const why = c.state !== "online" ? "The console must be online." : undefined;
              return (
                <div key={g.title_id} className="sp-coveritem">
                  <a className="sp-cover" href={g.game_id ? `#/game/${g.game_id}` : `#/installed/${c.id}/${g.id}`} title={tip}>
                    <Art g={g}>
                      <span className="sp-cover__caption">{g.title}
                        <span className="sp-cover__sub">{copies.map((x) => (x.drive_id && driveLabel[x.drive_id]) || "Unknown drive").join(" · ")}</span>
                      </span>
                    </Art>
                    {label !== "Installed" && <span><Badge tone={tone}>{label}</Badge></span>}
                  </a>
                  {act && (
                    <div className="sp-cover__actions">
                      {canMove && <button type="button" className="sp-cover__chip" disabled={!!why} title={why ?? "Move to another drive"}
                        aria-label={`Move ${g.title} to another drive`} onClick={() => setMoving(g)}><Icon name="move" /></button>}
                      <button type="button" className="sp-cover__chip sp-cover__chip--remove" disabled={!!why} title={why ?? "Remove from console"}
                        aria-label={`Remove ${g.title} from console`} onClick={() => setRemoving(g)}><Icon name="trash" /></button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {!shown.length && <p className="sp-muted">No installed game matches “{q.trim()}”.</p>}
        </div>
      ) : (
        <div className="sp-panel">
          {c.last_inventory_scan_at
            ? <Empty icon="gamepad" title="No games on this console" text="Install one from your library, and it shows up here." >
              <a className="sp-btn sp-btn--primary" href="#/">Open your library</a>
            </Empty>
            : <Empty icon="refresh" title="Inventory not read yet" text="Spela reads what's installed once the console is online and the helper runs.">
              {!session.readOnly && <button type="button" className="sp-btn sp-btn--primary" {...needsOnline(c)} disabled={busy || c.state !== "online"} onClick={rescan}>Refresh inventory</button>}
            </Empty>}
        </div>
      )}
      {moving && <MoveDialog consoleId={c.id} game={moving} drives={drives} onClose={() => { setMoving(null); loadInventory(c.id); }} />}
      {removing && <RemoveDialog consoleId={c.id} game={removing} onClose={() => { setRemoving(null); loadInventory(c.id); }} />}
    </>
  );
}
