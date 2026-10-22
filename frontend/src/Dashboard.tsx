import { useEffect, useState } from "react";
import Problem from "./Problem";
import { SetupCard } from "./Setup";
import { api, appVersion, formatBytes, session, wakeConsole, type AuditEntry, type Console, type Drive, type Game, type InstalledGame } from "./api";
import { ACTION_LABEL, RESULT } from "./AuditLog";
import { Jobs, STATE_LABEL, STATE_TONE } from "./Installed";
import { useScan } from "./Library";
import { Badge, Empty, Icon, Meter, Notice, PageHead, Panel, Spinner, Time } from "./ui";

const WEEK = 7 * 86400e3;

/** One console: its state, helper, drives and installs, with a refresh button. Its installs are a panel of their own, below it. */
function ConsoleCard({ c, version, many, onRefreshed }: { c: Console; version: string; many: boolean; onRefreshed: () => void }) {
  const [drives, setDrives] = useState<Drive[]>([]);
  const [count, setCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>("");
  const load = () => Promise.all([
    api<Drive[]>(`/consoles/${c.id}/drives`).then(setDrives),
    api<InstalledGame[]>(`/consoles/${c.id}/installed`).then((gs) => setCount(gs.length)),
  ]).catch(setError);
  useEffect(() => { load(); }, [c.id, c.last_inventory_scan_at]);

  async function wake() {
    setBusy(true);
    setError("");
    try {
      const woken = await wakeConsole(c);
      if (woken.state === "standby") setError("It's still in rest mode. Check its Remote Play key and rest-mode settings.");
      onRefreshed();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    setBusy(true);
    setError("");
    try {
      await api<Console>(`/consoles/${c.id}/rescan`, { method: "POST" });
      await load();
      onRefreshed();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const helper = c.helper_status.helper;
  const running = !!helper && helper !== "missing" && helper !== "other_pc";
  return (
    <>
      <Panel title={c.name} badge={<Badge tone={STATE_TONE[c.state]} dot>{STATE_LABEL[c.state]}</Badge>}
        actions={!session.readOnly && <>
          {c.state === "standby" && c.has_wake_key && <button type="button" className="sp-btn" disabled={busy} onClick={wake}><Icon name="power" />{busy ? "Waking…" : "Wake"}</button>}
          <button type="button" className="sp-btn" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
            onClick={refresh}>{busy ? <Spinner /> : <Icon name="refresh" />}Refresh inventory</button>
        </>}>
        {c.status_message && c.state !== "online" && <Notice tone={STATE_TONE[c.state] || "warning"} title={c.status_message} text={c.status_fix} />}
        <dl className="sp-kv">
          <dt>Firmware</dt><dd>{c.firmware ? <span className="sp-num">{c.firmware}</span> : <span className="sp-muted">Unknown</span>}</dd>
          <dt>Helper</dt>
          <dd>
            {running ? <span className="sp-num">v{helper}</span> : <span className="sp-muted">Not running</span>}
            {version && running && helper !== version && <Badge tone="warning">Needs v{version}</Badge>}
          </dd>
          <dt>Installed</dt><dd>{count == null ? <span className="sp-muted">…</span> : <a className="sp-link sp-num" href="#/installed">{count} {count === 1 ? "game" : "games"}</a>}</dd>
          <dt>Last inventory</dt><dd><Time t={c.last_inventory_scan_at} /></dd>
        </dl>
        {drives.length > 0 && (
          <div className="sp-meters">
            {drives.map((d) => (
              <div key={d.id} className="sp-meter">
                <span>{d.label}{!d.is_install_target && <span className="sp-muted"> · no installs</span>}</span>
                {d.total_bytes > 0 ? <>
                  <Meter used={d.total_bytes - d.free_bytes} total={d.total_bytes} label={d.label} />
                  <span className="sp-meter__value">{formatBytes(d.free_bytes)} free of {formatBytes(d.total_bytes)}</span>
                </> : <><span /><span className="sp-meter__value">Space unknown</span></>}
              </div>
            ))}
          </div>
        )}
        <Problem error={error} />
      </Panel>
      <Jobs consoleId={c.id} name={many ? c.name : undefined} onDone={load} />
    </>
  );
}

/** The overview (project.md §15): library and console totals, installs, problems from the last week, quick actions. */
export default function Dashboard() {
  const [games, setGames] = useState<Game[] | null>(null);
  const [consoles, setConsoles] = useState<Console[] | null>(null);
  const [problems, setProblems] = useState<AuditEntry[]>([]);
  const [version, setVersion] = useState("");
  const [error, setError] = useState("");

  const loadGames = () => api<Game[]>("/games").then(setGames, (e) => setError(e.message));
  const loadConsoles = () => api<Console[]>("/consoles").then(setConsoles, (e) => setError(e.message));
  const loadProblems = () => session.readOnly ? Promise.resolve() : api<AuditEntry[]>("/maintenance/audit-logs").then((rows) => setProblems(
    rows.filter((r) => (r.result === "failed" || r.result === "blocked") && Date.now() - new Date(r.created_at).getTime() < WEEK).slice(0, 8)), () => {});
  const [scan, setScan] = useScan(() => (loadGames(), loadProblems()));
  useEffect(() => {
    appVersion.then(setVersion);
    loadGames();
    loadProblems();
    loadConsoles();
    const t = setInterval(() => (loadConsoles(), loadProblems()), 15000); // the backend checks every 15 s
    return () => clearInterval(t);
  }, []);

  const startScan = () => api("/library/scan", { method: "POST" }).then(() => setScan((s) => ({ ...s!, running: true })), (e) => setError(e.message));

  if (!games || !consoles) return error ? <Problem error={error} /> : <p className="sp-muted">Loading…</p>;
  const lib = games.filter((g) => g.kind === "game" && !g.missing);
  const dlc = games.filter((g) => g.kind === "dlc" && !g.missing);
  const ready = lib.filter((g) => g.compatibility_status === "ready" && !g.installed);
  const missing = games.filter((g) => g.missing);
  const attention = games.filter((g) => !g.missing && (g.compatibility_status === "needs_attention" || g.metadata_confidence === "low" || g.metadata_confidence === "unknown"));
  const some = (gs: Game[]) => gs.slice(0, 3);

  return (
    <>
      <PageHead title="Dashboard"
        desc={scan?.running ? "Scanning your folders…" : scan?.finished_at ? <>Last scan <Time t={scan.finished_at} /></> : "No scan since the app started."}
        actions={!session.readOnly && (
          <button type="button" className="sp-btn sp-btn--primary sp-btn--lg" onClick={startScan} disabled={scan?.running}>
            {scan?.running ? <><Spinner />Scanning…</> : <><Icon name="refresh" />Scan library</>}
          </button>
        )} />
      {error && <Problem error={error} />}
      {!session.readOnly && <SetupCard />}

      <div className="sp-stats">
        <a className="sp-stat" href="#/"><span className="sp-stat__value">{lib.length}</span><span className="sp-stat__label">Games in your library</span><span className="sp-stat__meta">{formatBytes(lib.reduce((n, g) => n + g.size_bytes, 0))}</span></a>
        <a className="sp-stat" href="#/installed"><span className="sp-stat__value">{lib.filter((g) => g.installed).length}</span><span className="sp-stat__label">Installed on a console</span><span className="sp-stat__meta">{ready.length} more ready to install</span></a>
        <a className="sp-stat" href="#/dlc"><span className="sp-stat__value">{dlc.length}</span><span className="sp-stat__label">DLC</span><span className="sp-stat__meta">Open the DLC page</span></a>
        <a className="sp-stat" href="#/"><span className="sp-stat__value">{games.filter((g) => g.new).length}</span><span className="sp-stat__label">New this month</span><span className="sp-stat__meta">Not opened yet</span></a>
      </div>

      {consoles.length ? consoles.map((c) => <ConsoleCard key={c.id} c={c} version={version} many={consoles.length > 1} onRefreshed={() => (loadConsoles(), loadGames())} />) : (
        <section className="sp-panel">
          <Empty icon="inbox" title="No consoles yet" text="Add your PS5 by its IP address, or let Spela find it on your network.">
            {!session.readOnly && <a className="sp-btn sp-btn--primary" href="#/settings">Add console</a>}
          </Empty>
        </section>
      )}

      {(missing.length > 0 || attention.length > 0 || problems.length > 0) && (
        <Panel title="Needs a look" flush
          foot={problems.length > 0 && <>
            <span className="sp-panel__note">Failures from the last 7 days.</span>
            <a className="sp-btn sp-btn--ghost" href="#/settings">Open the activity log</a>
          </>}>
          <ul className="sp-list">
            {missing.length > 0 && (
              <li className="sp-listrow">
                <div className="sp-listrow__lead"><Badge tone="danger">Missing</Badge></div>
                <div className="sp-listrow__main">
                  <div className="sp-listrow__title">{missing.length} {missing.length === 1 ? "game's files are" : "games' files are"} gone from disk</div>
                  <div className="sp-listrow__meta">
                    <span>{some(missing).map((g) => g.title).join(", ")}{missing.length > 3 && "…"}</span><span className="sp-sep" />
                    <span>Remove {missing.length === 1 ? "it" : "them"} from the catalog, or plug the drive back in and scan.</span>
                  </div>
                </div>
              </li>
            )}
            {attention.length > 0 && (
              <li className="sp-listrow">
                <div className="sp-listrow__lead"><Badge tone="warning">Needs check</Badge></div>
                <div className="sp-listrow__main">
                  <div className="sp-listrow__title">{attention.length} {attention.length === 1 ? "game needs" : "games need"} a title ID checked before installing</div>
                  <div className="sp-listrow__meta">
                    {some(attention).flatMap((g, i) => [i ? <span key={`s${g.id}`} className="sp-sep" /> : null, <a key={g.id} className="sp-link" href={`#/game/${g.id}`}>{g.title}</a>])}
                    {attention.length > 3 && <span>…</span>}
                  </div>
                </div>
              </li>
            )}
            {problems.map((p) => {
              const g = p.title_id ? games.find((g) => g.title_id === p.title_id && g.kind === "game") ?? games.find((g) => g.title_id === p.title_id) : undefined;
              const why = p.details.message ?? p.details.reason ?? p.details.error;
              return (
                <li key={p.id} className="sp-listrow">
                  <div className="sp-listrow__lead"><Badge tone={RESULT[p.result][1]}>{RESULT[p.result][0]}</Badge></div>
                  <div className="sp-listrow__main">
                    <div className="sp-listrow__title">
                      {ACTION_LABEL[p.action] ?? p.action}
                      {g ? <> · <a className="sp-link" href={`#/game/${g.id}`}>{g.title}</a></> : p.title_id && <span className="sp-code">{p.title_id}</span>}
                    </div>
                    <div className="sp-listrow__meta">
                      {typeof why === "string" && <><span>{why}</span><span className="sp-sep" /></>}
                      {p.console_name && <><span>{p.console_name}</span><span className="sp-sep" /></>}
                      <Time t={p.created_at} />
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}
    </>
  );
}
