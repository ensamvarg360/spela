import { useEffect, useState } from "react";
import { api, type AuditEntry } from "./api";

export const ACTION_LABEL: Record<string, string> = {
  setup: "Password set",
  login: "Log in",
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
  viewer_password: "View-only password changed",
  wake: "Woken from rest mode",
  save_backup: "Saves backed up",
};

// Internal ids, meaningless to the reader.
const HIDDEN = ["job", "removal"];

const BADGE: Record<string, string> = { success: "ready", failed: "incompatible", blocked: "incompatible", canceled: "needs_attention" };

export default function AuditLog() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  const load = (before?: string) =>
    api<AuditEntry[]>(`/maintenance/audit-logs${before ? `?before=${encodeURIComponent(before)}` : ""}`).then(
      (page) => (setRows((r) => (before ? [...r, ...page] : page)), setMore(page.length === 100)),
      (e) => setError(e.message),
    );
  useEffect(() => { load(); }, []);

  return (
    <section className="card">
      <h2>Activity log</h2>
      <p className="muted">Every install, removal, login and settings change, newest first.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="table-wrap">
        <table className="audit">
          <thead>
            <tr><th>When</th><th>What</th><th>Result</th><th>Console</th><th>Title ID</th><th>Details</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{new Date(r.created_at).toLocaleString()}</td>
                <td>{ACTION_LABEL[r.action] ?? r.action}</td>
                <td><span className={`badge ${BADGE[r.result] ?? ""}`}>{r.result}</span></td>
                <td>{r.console_name ?? (r.console_id ? <span className="muted">Removed</span> : "")}</td>
                <td>{r.title_id && <code>{r.title_id}</code>}</td>
                <td className="muted">
                  {r.target_path && <code>{r.target_path}</code>}
                  {Object.entries(r.details).filter(([k, v]) => !HIDDEN.includes(k) && !(Array.isArray(v) && !v.length)).map(([k, v]) => (
                    <div key={k}>{k}: {Array.isArray(v) ? v.join("; ") : typeof v === "object" ? JSON.stringify(v) : String(v)}</div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!rows.length && !error && <p className="muted">Nothing yet.</p>}
      {more && <button className="secondary" onClick={() => load(rows[rows.length - 1].created_at)}>Load older</button>}
    </section>
  );
}
