import { useEffect, useState, type FormEvent } from "react";
import Problem from "./Problem";
import { api, appVersion, needsOnline, type Console } from "./api";
import { STATE_BADGE, STATE_LABEL, when } from "./Installed";

function Facts({ c, version, busy, act }: { c: Console; version: string; busy: boolean; act: (path: string) => void }) {
  const helper = c.helper_status.helper; // "missing", a version, or absent before the first check
  const helperOk = !!version && helper === version;
  const consoleApp = c.helper_status.console_app; // same: "missing", a version, or absent
  const consoleAppOk = !!version && consoleApp === version;
  return (
    <dl className="facts">
      <dt>Firmware</dt><dd>{c.firmware ?? "Unknown"}</dd>
      <dt>Helper payload</dt>
      <dd>
        {!helper || helper === "missing" ? "Not running" : `v${helper}`}
        {helper && helper !== "missing" && !helperOk && version && ` (this app needs v${version})`}
        <button className="small" disabled={busy || c.state === "standby"} title={c.state === "standby" ? "Wake the console first." : undefined}
          onClick={() => act("helper")}>{!helper || helper === "missing" ? "Install helper" : helperOk ? "Reinstall helper" : "Update helper"}</button>
        {(c.auto_helper || (helper && helper !== "missing")) && (
          <button className="small secondary" disabled={busy || (c.state !== "online" && c.state !== "degraded")}
            title={c.state !== "online" && c.state !== "degraded" ? "The console must be on." : "Stop the helper, and stop sending it by itself."}
            onClick={() => act("remove-helper")}>Remove helper</button>
        )}
        {!c.auto_helper ? <span className="muted"> Removed: not sent by itself until you click Install helper.</span>
          : !helperOk && c.connection_type !== "mock" && <span className="muted"> Sent by itself once the console's ELF loader is running.</span>}
      </dd>
      <dt>PS5 app</dt>
      <dd>
        {!consoleApp || consoleApp === "missing" ? "Not installed" : `v${consoleApp}`}
        <button className="small" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
          onClick={() => act("console-app")}>{!consoleApp || consoleApp === "missing" ? "Install PS5 app" : consoleAppOk ? "Reinstall PS5 app" : "Update PS5 app"}</button>
      </dd>
      <dt>Wake from rest mode</dt>
      <dd>
        {c.has_wake_key ? "Remote Play key saved" : "Add its Remote Play key (Edit) to wake it from here"}
        {c.has_wake_key && c.state === "standby" && <button className="small" disabled={busy} onClick={() => act("wake")}>Wake</button>}
      </dd>
      <dt>Connections</dt><dd>{Object.entries(c.helper_status).filter(([k]) => !["banner", "helper", "console_app"].includes(k)).map(([k, v]) => `${k}: ${v}`).join(", ") || c.connection_type}</dd>
      <dt>Last seen</dt><dd>{when(c.last_seen_at)}</dd>
      <dt>Last inventory</dt><dd>{when(c.last_inventory_scan_at)}</dd>
    </dl>
  );
}

interface Found { ip: string; name: string; state: "online" | "standby"; added: boolean }

/** Looks for PS5s on this network (POST /consoles/discover, about 2 s) as soon as it shows, and adds one with a click.
 *  Added consoles use the helper payload; Edit switches that. */
export function FindConsole({ onAdded }: { onAdded: () => void }) {
  const [found, setFound] = useState<Found[] | null>(null);
  const [looking, setLooking] = useState(false);
  const [error, setError] = useState<unknown>("");

  const look = () => {
    setLooking(true);
    setError("");
    api<Found[]>("/consoles/discover", { method: "POST" }).then(setFound, setError).finally(() => setLooking(false));
  };
  useEffect(look, []);

  const add = (p: Found) => api("/consoles", { json: { name: p.name, ip_address: p.ip, connection_type: "helper", port: 7735 } })
    .then(() => (setFound((f) => f!.map((x) => (x.ip === p.ip ? { ...x, added: true } : x))), onAdded()), setError);

  return (
    <div className="add-option">
      <h3>Find it on my network</h3>
      <p className="muted">Finds a PS5 that's switched on, or in rest mode with <em>Stay connected to the internet</em> on.</p>
      {found && found.length > 0 && (
        <ul className="found">
          {found.map((p) => (
            <li key={p.ip}>
              <span><strong>{p.name}</strong> <code>{p.ip}</code></span>
              <span className={`badge ${p.state === "online" ? "ready" : "needs_attention"}`}>{p.state === "online" ? "On" : "Rest mode"}</span>
              {p.added ? <span className="muted">Added</span> : <button className="small" onClick={() => add(p)}>Add</button>}
            </li>
          ))}
        </ul>
      )}
      {found && !found.length && !looking && (
        <Problem error="No PS5 answered." fix={"It may be switched off, on another network, or the router may keep devices " +
          "from seeing each other (client or AP isolation, common on guest Wi-Fi). Enter its IP address instead."} />
      )}
      <Problem error={error} />
      <div><button type="button" className="secondary" disabled={looking} onClick={look}>
        {looking ? <><span className="spinner" aria-hidden />Looking…</> : found ? "Look again" : "Look for it"}
      </button></div>
    </div>
  );
}

const EMPTY = { name: "", ip_address: "", connection_type: "helper", port: "7735", username: "", password: "", wake_key: "" };
type Draft = typeof EMPTY;

function ConsoleForm({ console: c, onDone }: { console?: Console; onDone: () => void }) {
  const [d, setD] = useState<Draft>(c ? {
    name: c.name, ip_address: c.ip_address, connection_type: c.connection_type, port: String(c.port), username: c.username ?? "", password: "", wake_key: "",
  } : EMPTY);
  const [forget, setForget] = useState(false);
  const [forgetWake, setForgetWake] = useState(false);
  const [error, setError] = useState<unknown>("");
  const field = (k: keyof Draft) => ({ value: d[k], onChange: (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value }) });

  async function save(e: FormEvent) {
    e.preventDefault();
    const { password, wake_key, ...rest } = d;
    const body: Record<string, unknown> = { ...rest, port: d.connection_type === "helper" ? 7735 : Number(d.port), username: d.username || null };
    if (password) body.password = password; // blank = keep the saved one
    else if (forget || !c) body.password = null;
    if (wake_key) body.wake_key = wake_key; // same
    else if (forgetWake || !c) body.wake_key = null;
    try {
      await (c ? api(`/consoles/${c.id}`, { method: "PATCH", json: body }) : api("/consoles", { json: body }));
      onDone();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <form className="edit picker" onSubmit={save}>
      <div className="fields">
        <label>Name<input required maxLength={100} placeholder="Living Room PS5" {...field("name")} /></label>
        <label>IP address<input required placeholder="192.168.1.50" {...field("ip_address")} /></label>
        <label>
          Connection
          <select {...field("connection_type")}>
            <option value="helper">Helper payload</option>
            <option value="ftp">FTP (fallback)</option>
            <option value="http">HTTP</option>
            <option value="custom">Custom</option>
            <option value="mock">Mock (testing)</option>
          </select>
        </label>
        {d.connection_type !== "helper" && <>
          <label>Port<input required type="number" min={1} max={65535} {...field("port")} /></label>
          <label>Username<input autoComplete="off" placeholder="Optional" {...field("username")} /></label>
          <label>
            Password
            <input type="password" autoComplete="new-password" disabled={forget}
              placeholder={c?.has_password ? "Saved. Leave blank to keep it" : "Optional"} {...field("password")} />
          </label>
        </>}
      </div>
      <label>
        Remote Play key <span className="muted">(optional: wakes the console from rest mode)</span>
        <input autoComplete="off" spellCheck={false} disabled={forgetWake} placeholder={c?.has_wake_key ? "Saved. Leave blank to keep it" : "RP-RegistKey, e.g. 1a2b3c4d"} {...field("wake_key")} />
      </label>
      <p className="muted">Register this PC's Remote Play once with Chiaki-ng (PS5 Settings → System → Remote Play → Link Device), then copy the
        console's <em>RP-RegistKey</em> from it. The PS5 must have <em>Stay connected to the internet</em> and <em>Enable turning on PS5 from
        network</em> on in its rest-mode settings.</p>
      {c?.has_wake_key && (
        <label className="check"><input type="checkbox" checked={forgetWake} onChange={(e) => setForgetWake(e.target.checked)} /> Forget saved Remote Play key</label>
      )}
      {c?.has_password && d.connection_type !== "helper" && (
        <label className="check"><input type="checkbox" checked={forget} onChange={(e) => setForget(e.target.checked)} /> Forget saved password</label>
      )}
      <div className="actions">
        <button type="submit">{c ? "Save" : "Add console"}</button>
        <button type="button" className="link" onClick={onDone}>Cancel</button>
      </div>
      <Problem error={error} />
    </form>
  );
}

export default function Consoles() {
  const [consoles, setConsoles] = useState<Console[]>([]);
  const [editing, setEditing] = useState<string | null>(null); // console id, "new", or null
  const [error, setError] = useState<{ e: unknown; retry?: () => void } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [version, setVersion] = useState("");
  useEffect(() => { appVersion.then(setVersion); }, []);

  const load = () => api<Console[]>("/consoles").then(setConsoles, (e) => setError({ e, retry: load }));
  useEffect(() => {
    load();
    const t = setInterval(load, 15000); // the backend checks every 15 s
    return () => clearInterval(t);
  }, []);
  const done = () => (setEditing(null), load());
  async function act(c: Console, path: string) {
    const removing = path === "remove-helper";
    if (removing && !confirm(`Remove the helper from ${c.name}?\n\nIt stops now, and Spela stops sending it by itself, so the ` +
      "PS5 is free for other tools. Nothing of it stays on the PS5. Installed games, the PS5 app and logos are kept.\n\n" +
      "Spela can't read or install to this console until you click Install helper again.")) return;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      const after = await api<Console>(`/consoles/${c.id}/${removing ? "helper" : path}`, { method: removing ? "DELETE" : "POST" });
      if (removing) setNotice(`The helper is stopped on ${c.name}, and Spela won't send it again until you click Install helper.`);
      if (path === "helper") setNotice(`Helper v${after.helper_status.helper} is running on ${c.name}.`);
      if (path === "console-app")
        setNotice(`PS5 app v${after.helper_status.console_app} copied to ${c.name} and checked file by file. If it was open on the PS5, close and reopen it. ` +
          "If the PS5 says it can't start it, remove it in ShadowMount+, restart ShadowMount+ (or the PS5), then install again.");
      await load();
    } catch (e) {
      setError({ e, retry: () => act(c, path) });
    } finally {
      setBusy(false);
    }
  }
  const remove = (c: Console) =>
    confirm(`Remove ${c.name}? Nothing on the console is changed.`) &&
    api(`/consoles/${c.id}`, { method: "DELETE" }).then(load, (e) => setError({ e }));

  return (
    <section className="card">
      <h2>Consoles</h2>
      <p className="muted">Passwords are stored encrypted and are never shown again after saving.</p>
      <ul className="roots">
        {consoles.map((c) =>
          editing === c.id ? <li key={c.id} className="full"><ConsoleForm console={c} onDone={done} /></li> : (
            <li key={c.id}>
              <strong>{c.name}</strong>
              <code>{c.connection_type}://{c.ip_address}:{c.port}</code>
              <span className="muted">
                <span className={`badge ${STATE_BADGE[c.state]}`}>{STATE_LABEL[c.state]}</span> {c.status_message || (c.has_password ? "Password saved" : "No password")}
              </span>
              <span className="inline">
                <button className="link" disabled={busy} onClick={() => act(c, "test-connection")}>Check connection</button>
                <button className="link" {...needsOnline(c)} disabled={busy || c.state !== "online"} onClick={() => act(c, "rescan")}>Refresh inventory</button>
                <button className="link" onClick={() => setEditing(c.id)}>Edit</button>
                <button className="link" onClick={() => remove(c)}>Remove</button>
              </span>
              {c.status_fix && <p className="callout status-fix">{c.status_fix}</p>}
              <Facts c={c} version={version} busy={busy} act={(path) => act(c, path)} />
            </li>
          ))}
      </ul>
      {editing === "new" ? (
        <div className="add-console">
          <FindConsole onAdded={done} />
          <div className="add-option">
            <h3>I know its IP address</h3>
            <ConsoleForm onDone={done} />
          </div>
        </div>
      ) : <button onClick={() => setEditing("new")}>Add console</button>}
      {notice && <p role="status">{notice}</p>}
      <Problem error={error?.e} retry={error?.retry} />
    </section>
  );
}
