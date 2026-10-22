import { Fragment, useEffect, useMemo, useState } from "react";
import { api, formatBytes, type AuditEntry, type Game } from "./api";
import { Badge, Empty, Icon, Panel, Path, Time, type Tone } from "./ui";

export const ACTION_LABEL: Record<string, string> = {
  setup: "Password set",
  login: "Signed in",
  scan: "Library scan",
  install_queued: "Install queued",
  install: "Install",
  uninstall: "Removal",
  convert: "Converted to .exfat",
  move: "Moved to another drive",
  console_create: "Console added",
  console_update: "Console changed",
  console_delete: "Console removed",
  credential_update: "Console password changed",
  helper_install: "Helper installed",
  helper_remove: "Helper removed",
  console_app_install: "PS5 app installed",
  console_app_remove: "PS5 app removed",
  leftovers_clean: "Unfinished copies cleaned up",
  viewer_password: "View-only password changed",
  wake: "Woken from rest mode",
  save_backup: "Saves backed up",
  save_restore: "Saves restored",
  home_logo: "Home-screen logo",
};

export const RESULT: Record<AuditEntry["result"], [string, Tone]> = {
  success: ["Success", "success"], failed: ["Failed", "danger"], blocked: ["Blocked", "danger"], canceled: ["Canceled", ""],
};

// Internal ids, meaningless to the reader.
const HIDDEN = ["job", "removal"];

/** A detail key the way people read it: "replaced_saves_backup" → "Replaced saves backup". */
const label = (k: string) => (k.charAt(0).toUpperCase() + k.slice(1)).replace(/_/g, " ");
function value(k: string, v: unknown) {
  if (typeof v === "number" && /bytes/.test(k)) return formatBytes(v);
  if (Array.isArray(v)) return v.join("; ");
  if (typeof v === "string" && /^([A-Za-z]:\\|\/)/.test(v)) return <Path path={v} />;
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

export default function AuditLog() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [games, setGames] = useState<Game[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  const load = (before?: string) =>
    api<AuditEntry[]>(`/maintenance/audit-logs${before ? `?before=${encodeURIComponent(before)}` : ""}`).then(
      (page) => (setRows((r) => (before ? [...r, ...page] : page)), setMore(page.length === 100)),
      (e) => setError(e.message),
    );
  useEffect(() => {
    load();
    api<Game[]>("/games").then(setGames, () => {});
    // New rows on top, older pages already loaded kept below them.
    const t = setInterval(() => api<AuditEntry[]>("/maintenance/audit-logs").then(
      (page) => setRows((r) => [...page, ...r.filter((x) => x.created_at < page[page.length - 1]?.created_at)]), () => {}), 3000);
    return () => clearInterval(t);
  }, []);
  // name the game, not just its title ID
  const byTid = useMemo(() => new Map([...games.filter((g) => g.kind === "dlc"), ...games.filter((g) => g.kind === "game")] // the game wins over its DLC
    .filter((g) => g.title_id).map((g) => [g.title_id!, g])), [games]);

  return (
    <Panel title="Activity log" desc="Every install, removal, sign-in and settings change, newest first." flush
      foot={more && <button type="button" className="sp-btn" onClick={() => load(rows[rows.length - 1].created_at)}>Load older</button>}>
      {error ? <div className="sp-listrow__more sp-listrow__more--top"><p className="sp-error" role="alert">{error}</p></div>
        : !rows.length ? <Empty icon="inbox" title="Nothing yet" text="Installs, removals, scans and sign-ins show up here as they happen." /> : (
        <div className="sp-tablewrap">
          <table className="sp-table">
            <thead>
              <tr><th>When</th><th>Event</th><th>Game</th><th>Console</th><th>Result</th><th><span className="sp-visually-hidden">Details</span></th></tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const g = r.title_id ? byTid.get(r.title_id) : undefined;
                const details = Object.entries(r.details).filter(([k, v]) => !HIDDEN.includes(k) && v != null && v !== "" && !(Array.isArray(v) && !v.length));
                const [result, tone] = RESULT[r.result] ?? [r.result, ""];
                return (
                  <tr key={r.id}>
                    <td className="sp-nowrap"><Time t={r.created_at} /></td>
                    <td>{ACTION_LABEL[r.action] ?? label(r.action)}</td>
                    <td>
                      {g ? <a className="sp-link" href={`#/game/${g.id}`}>{g.title}</a> : !r.title_id && <span className="sp-muted">—</span>}
                      {r.title_id && <> <span className="sp-code">{r.title_id}</span></>}
                    </td>
                    <td className="sp-nowrap">{r.console_name ?? (r.console_id ? <span className="sp-muted">Removed</span> : <span className="sp-muted">—</span>)}</td>
                    <td><Badge tone={tone}>{result}</Badge></td>
                    <td>
                      {(r.target_path || details.length > 0) && (
                        <details className="sp-disclosure">
                          <summary><Icon name="chevronRight" />Details</summary>
                          <dl className="sp-kv sp-kv--compact">
                            {r.target_path && <><dt>Path</dt><dd><Path path={r.target_path} /></dd></>}
                            {details.map(([k, v]) => <Fragment key={k}><dt>{label(k)}</dt><dd>{value(k, v)}</dd></Fragment>)}
                          </dl>
                        </details>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
