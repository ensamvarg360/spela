import { useEffect, useMemo, useState } from "react";
import { api, formatBytes, type Console, type ConsoleState, type Drive, type InstalledGame } from "./api";

// Reuses the library badge colours.
export const STATE_BADGE: Record<ConsoleState, string> = {
  online: "ready", degraded: "needs_attention", offline: "incompatible", disabled: "unknown", unknown: "unknown",
};
const STATE_LABEL: Record<ConsoleState, string> = {
  online: "Online", degraded: "Degraded", offline: "Offline", disabled: "Disabled", unknown: "Not checked yet",
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
const when = (t: string | null) => (t ? new Date(/(Z|[+-]\d\d:\d\d)$/.test(t) ? t : t + "Z").toLocaleString() : "Never");
const bytes = (n: number) => (n ? formatBytes(n) : "Unknown");

export default function Installed() {
  const [consoles, setConsoles] = useState<Console[] | null>(null);
  const [id, setId] = useState("");
  const [drives, setDrives] = useState<Drive[]>([]);
  const [games, setGames] = useState<InstalledGame[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
          <dt>Helpers</dt><dd>{Object.entries(c.helper_status).filter(([k]) => k !== "banner").map(([k, v]) => `${k}: ${v}`).join(", ") || "Unknown"}</dd>
          <dt>Last seen</dt><dd>{when(c.last_seen_at)}</dd>
          <dt>Last inventory</dt><dd>{when(c.last_inventory_scan_at)}</dd>
          <dt>Installed</dt><dd>{games.length} game{games.length === 1 ? "" : "s"}{updates ? ` · ${updates} with a newer version in your library` : ""}</dd>
        </dl>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

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
