import { useEffect, useMemo, useState } from "react";
import { api, appVersion, formatBytes, JOB_ACTIVE, type Console, type ConsoleState, type Drive, type InstalledGame, type Job, type JobStatus } from "./api";

const JOB_LABEL: Record<JobStatus, [string, string]> = {
  queued: ["Waiting", "unknown"], preflight: ["Checking", "unknown"], transferring: ["Copying", "needs_attention"],
  registering: ["Finishing", "needs_attention"], success: ["Installed", "ready"], failed: ["Failed", "incompatible"],
  canceled: ["Canceled", "unknown"], interrupted: ["Interrupted", "incompatible"],
};

/** Installs on this console: polls every 2 s while one is running. `onDone` re-reads the inventory. */
function Jobs({ consoleId, onDone }: { consoleId: string; onDone: () => void }) {
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
              {j.status === "transferring" && (
                <>
                  <progress max={1} value={j.progress} />
                  <span className="muted">
                    {formatBytes(j.bytes_transferred)} of {formatBytes(j.total_bytes)} · {formatBytes(j.transfer_rate_bps)}/s
                    {j.eta_seconds != null && ` · ${Math.ceil(j.eta_seconds / 60)} min left`}
                  </span>
                </>
              )}
              <span className="muted">{j.message}</span>
              {JOB_ACTIVE.includes(j.status) && <button className="small secondary" onClick={() => cancel(j)}>Cancel</button>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// Reuses the library badge colours.
export const STATE_BADGE: Record<ConsoleState, string> = {
  online: "ready", standby: "unknown", degraded: "needs_attention", offline: "incompatible", disabled: "unknown", unknown: "unknown",
};
const STATE_LABEL: Record<ConsoleState, string> = {
  online: "Online", standby: "Rest mode", degraded: "Degraded", offline: "Offline", disabled: "Disabled", unknown: "Not checked yet",
};
const ROUTE_LABEL = { package_installer: "Package", shadowmountplus: "ShadowMountPlus", unknown: "Unknown" };
const STATUS = {
  installed: ["Installed", "ready"],
  update_available: ["Update in library", "needs_attention"],
  partially_installed: ["Partly installed", "incompatible"],
  orphaned_icon: ["Icon only", "incompatible"],
  unknown: ["Unknown", "unknown"],
} as const;

// SQLite drops the zone from stored UTC times; without a "Z" the browser would read them as local time.
const utc = (t: string) => new Date(/(Z|[+-]\d\d:\d\d)$/.test(t) ? t : t + "Z");
const when = (t: string | null) => (t ? utc(t).toLocaleString() : "Never");
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
  const updates = games.filter((g) => g.status === "update_available").length;
  const helper = c.helper_status.helper; // "missing", a version, or absent before the first check
  const helperOk = !!version && helper === version;
  const consoleApp = c.helper_status.console_app; // same: "missing", a version, or absent
  const consoleAppOk = !!version && consoleApp === version;

  async function act(path: string) {
    setBusy(true);
    setError("");
    try {
      const updated = await api<Console>(`/consoles/${c.id}/${path}`, { method: "POST" });
      setConsoles((cs) => cs!.map((x) => (x.id === updated.id ? updated : x)));
      await loadInventory(c.id);
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
      <section className="card">
        <div className="toolbar">
          {consoles.length > 1 ? (
            <select aria-label="Console" value={c.id} onChange={(e) => setId(e.target.value)}>
              {consoles.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          ) : <h2 className="inline-title">{c.name}</h2>}
          <span className={`badge ${STATE_BADGE[c.state]}`}>{STATE_LABEL[c.state]}</span>
          {c.status_message && <span className="muted">{c.status_message}</span>}
          <span className="spacer" />
          <button className="secondary" disabled={busy} onClick={() => act("test-connection")}>Check connection</button>
          <button disabled={busy} onClick={() => act("rescan")}>{busy ? "Working…" : "Refresh inventory"}</button>
        </div>
        <dl className="facts">
          <dt>Firmware</dt><dd>{c.firmware ?? "Unknown"}</dd>
          <dt>Helper payload</dt>
          <dd>
            {!helper || helper === "missing" ? "Not running" : `v${helper}`}
            {helper && helper !== "missing" && !helperOk && version && ` (this app needs v${version})`}
            {!helperOk && (
              <button className="small" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
                onClick={() => act("helper")}>{!helper || helper === "missing" ? "Install helper" : "Update helper"}</button>
            )}
          </dd>
          <dt>PS5 app</dt>
          <dd>
            {!consoleApp || consoleApp === "missing" ? "Not installed" : `v${consoleApp}`}
            {!consoleAppOk && (
              <button className="small" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
                onClick={() => act("console-app")}>{!consoleApp || consoleApp === "missing" ? "Install PS5 app" : "Update PS5 app"}</button>
            )}
          </dd>
          <dt>Connections</dt><dd>{Object.entries(c.helper_status).filter(([k]) => !["banner", "helper", "console_app"].includes(k)).map(([k, v]) => `${k}: ${v}`).join(", ") || "Unknown"}</dd>
          <dt>Last seen</dt><dd>{when(c.last_seen_at)}</dd>
          <dt>Last inventory</dt><dd>{when(c.last_inventory_scan_at)}</dd>
          <dt>Installed</dt><dd>{games.length} game{games.length === 1 ? "" : "s"}{updates ? ` · ${updates} with a newer version in your library` : ""}</dd>
        </dl>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <Jobs consoleId={c.id} onDone={() => loadInventory(c.id)} />

      {drives.length > 0 && (
        <ul className="drives">
          {drives.map((d) => (
            <li key={d.id} className="card">
              <strong>{d.label}</strong>
              <span className="muted">{d.drive_type} · <code>{d.path}</code></span>
              {d.total_bytes > 0 && <meter min={0} max={d.total_bytes} value={d.total_bytes - d.free_bytes} />}
              <span>
                {bytes(d.free_bytes)} free
                {d.total_bytes > 0 && ` of ${formatBytes(d.total_bytes)} · ${formatBytes(d.total_bytes - d.free_bytes)} used`}
              </span>
              <span className="muted">{d.installed_count} installed · last seen {when(d.last_seen_at)}</span>
              <label className="check">
                <input type="checkbox" checked={d.is_install_target} onChange={(e) => setTarget(d, e.target.checked)} /> Allow installs here
              </label>
            </li>
          ))}
        </ul>
      )}

      {games.length > 0 ? (
        <>
          <div className="toolbar">
            <input type="search" placeholder="Search title or title ID" aria-label="Search installed games" value={q} onChange={(e) => setQ(e.target.value)} />
            <span className="muted">{shown.length} of {games.length}</span>
          </div>
          <ul className="grid">
            {shown.map((g) => {
              const [label, cls] = STATUS[g.status];
              return (
                <li key={g.id} className="tile">
                  {g.has_cover
                    ? <img className="cover" src={`/api/v1/games/${g.game_id}/cover`} alt="" loading="lazy" />
                    : <div className="cover placeholder" aria-hidden>{g.title.slice(0, 1)}</div>}
                  <span className={`badge ${cls}`}>{label}</span>
                  <strong>{g.game_id ? <a href={`#/game/${g.game_id}`}>{g.title}</a> : g.title}</strong>
                  <span className="muted">
                    {g.title_id} · v{g.version ?? "?"}{g.status === "update_available" && ` → ${g.library_version}`} · {ROUTE_LABEL[g.install_route]}
                  </span>
                  <span className="muted">{(g.drive_id && driveLabel[g.drive_id]) || "Unknown drive"} · {bytes(g.size_bytes)}</span>
                  <code className="muted" title={`Verified ${when(g.last_verified_at)}`}>{g.installed_path}</code>
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p className="muted">{c.last_inventory_scan_at ? "No games found on this console." : "Inventory not read yet. Click Refresh inventory."}</p>
      )}
    </div>
  );
}
