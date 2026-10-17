import { useEffect, useState } from "react";
import { api, appVersion, formatBytes, session, wakeConsole, type AuditEntry, type Console, type Drive, type Game, type InstalledGame } from "./api";
import { ACTION_LABEL } from "./AuditLog";
import { Jobs, STATE_BADGE, STATE_LABEL, when } from "./Installed";
import { useScan } from "./Library";

const WEEK = 7 * 86400e3;

/** One console: its state, helper, drives and installs, with a refresh button. */
function ConsoleCard({ c, version, onRefreshed }: { c: Console; version: string; onRefreshed: () => void }) {
  const [drives, setDrives] = useState<Drive[]>([]);
  const [count, setCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => Promise.all([
    api<Drive[]>(`/consoles/${c.id}/drives`).then(setDrives),
    api<InstalledGame[]>(`/consoles/${c.id}/installed`).then((gs) => setCount(gs.length)),
  ]).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [c.id, c.last_inventory_scan_at]);

  async function wake() {
    setBusy(true);
    setError("");
    try {
      const woken = await wakeConsole(c);
      if (woken.state === "standby") setError("It's still in rest mode. Check its Remote Play key and rest-mode settings.");
      onRefreshed();
    } catch (e) {
      setError((e as Error).message);
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
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const helper = c.helper_status.helper;
  return (
    <section className="card console-card">
      <div className="toolbar">
        <h2 className="inline-title">{c.name}</h2>
        <span className={`badge ${STATE_BADGE[c.state]}`}>{STATE_LABEL[c.state]}</span>
        <span className="spacer" />
        {!session.readOnly && c.state === "standby" && c.has_wake_key && <button className="small" disabled={busy} onClick={wake}>{busy ? "Waking…" : "Wake"}</button>}
        {!session.readOnly && <button className="secondary small" disabled={busy || c.state !== "online"} onClick={refresh}>{busy ? "Reading…" : "Refresh inventory"}</button>}
      </div>
      {c.status_message && <p className="muted">{c.status_message}</p>}
      <dl className="facts">
        <dt>Firmware</dt><dd>{c.firmware ?? "Unknown"}</dd>
        <dt>Helper</dt><dd>{!helper || helper === "missing" ? "Not running" : `v${helper}`}{version && helper && helper !== "missing" && helper !== version && ` (needs v${version})`}</dd>
        <dt>Installed</dt><dd>{count ?? "…"} {count === 1 ? "game" : "games"} · <a href="#/installed">Open</a></dd>
        <dt>Last inventory</dt><dd>{when(c.last_inventory_scan_at)}</dd>
      </dl>
      {drives.length > 0 && (
        <ul className="dash-drives">
          {drives.map((d) => (
            <li key={d.id}>
              <span>{d.label}{!d.is_install_target && <span className="muted"> · no installs</span>}</span>
              {d.total_bytes > 0 ? <>
                <meter min={0} max={d.total_bytes} value={d.total_bytes - d.free_bytes} aria-label={`${d.label} used`} />
                <span className="muted">{formatBytes(d.free_bytes)} free of {formatBytes(d.total_bytes)}</span>
              </> : <span className="muted">Space unknown</span>}
            </li>
          ))}
        </ul>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      <Jobs consoleId={c.id} onDone={load} />
    </section>
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
    const t = setInterval(loadConsoles, 15000); // the backend checks every 15 s
    return () => clearInterval(t);
  }, []);

  const startScan = () => api("/library/scan", { method: "POST" }).then(() => setScan((s) => ({ ...s!, running: true })), (e) => setError(e.message));

  if (!games || !consoles) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">Loading…</p>;
  const lib = games.filter((g) => g.kind === "game" && !g.missing);
  const dlc = games.filter((g) => g.kind === "dlc" && !g.missing);
  const ready = lib.filter((g) => g.compatibility_status === "ready" && !g.installed);
  const missing = games.filter((g) => g.missing);
  const attention = games.filter((g) => !g.missing && (g.compatibility_status === "needs_attention" || g.metadata_confidence === "low" || g.metadata_confidence === "unknown"));

  return (
    <div className="dashboard">
      <ul className="stats">
        <li className="card"><strong>{lib.length}</strong><span>games in your library</span><span className="muted">{formatBytes(lib.reduce((n, g) => n + g.size_bytes, 0))}</span></li>
        <li className="card"><strong>{lib.filter((g) => g.installed).length}</strong><span>installed on a console</span><span className="muted">{ready.length} more ready to install</span></li>
        <li className="card"><strong>{dlc.length}</strong><span>DLC</span><span className="muted"><a href="#/dlc">Open the DLC page</a></span></li>
        <li className="card"><strong>{games.filter((g) => g.new).length}</strong><span>new this month</span><span className="muted">Not opened yet</span></li>
      </ul>

      <div className="toolbar">
        {!session.readOnly && <button onClick={startScan} disabled={scan?.running}>{scan?.running ? <><span className="spinner" aria-hidden />Scanning…</> : "Scan library"}</button>}
        <span className="muted">{scan?.finished_at ? `Last scan ${new Date(scan.finished_at).toLocaleString()}` : "No scan since the app started."}</span>
        {error && <span className="error" role="alert">{error}</span>}
      </div>

      {consoles.length ? consoles.map((c) => <ConsoleCard key={c.id} c={c} version={version} onRefreshed={() => (loadConsoles(), loadGames())} />) : (
        <div className="empty card"><h2>No consoles yet</h2><p>Add your PS5 under <a href="#/settings">Settings → Consoles</a>.</p></div>
      )}

      {(missing.length > 0 || attention.length > 0 || problems.length > 0) && (
        <section className="card">
          <h2>Needs a look</h2>
          <ul className="problems">
            {missing.length > 0 && <li><span className="badge incompatible">Missing</span> {missing.length} {missing.length === 1 ? "game's files are" : "games' files are"} gone from disk: {missing.slice(0, 3).map((g) => g.title).join(", ")}{missing.length > 3 && "…"}. Remove {missing.length === 1 ? "it" : "them"} from the catalog, or plug the drive back in and scan.</li>}
            {attention.length > 0 && <li><span className="badge needs_attention">Check</span> {attention.length} {attention.length === 1 ? "game needs" : "games need"} a title ID checked before installing: {attention.slice(0, 3).flatMap((g, i) => [i ? ", " : "", <a key={g.id} href={`#/game/${g.id}`}>{g.title}</a>])}{attention.length > 3 && "…"}</li>}
            {problems.map((p) => (
              <li key={p.id}>
                <span className="badge incompatible">{p.result}</span> {ACTION_LABEL[p.action] ?? p.action}
                {p.title_id && <> · <code>{p.title_id}</code></>}
                {p.console_name && <> · {p.console_name}</>}
                {" · "}<span className="muted">{new Date(p.created_at).toLocaleString()}</span>
                {typeof (p.details.message ?? p.details.reason ?? p.details.error) === "string" && <div className="muted">{String(p.details.message ?? p.details.reason ?? p.details.error)}</div>}
              </li>
            ))}
          </ul>
          {problems.length > 0 && <p className="muted">From the last 7 days. The full list is in <a href="#/settings">Settings → Activity log</a>.</p>}
        </section>
      )}
    </div>
  );
}
