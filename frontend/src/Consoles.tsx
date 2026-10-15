import { useEffect, useState, type FormEvent } from "react";
import { api, type Console } from "./api";
import { STATE_BADGE } from "./Installed";

const EMPTY = { name: "", ip_address: "", connection_type: "helper", port: "7735", username: "", password: "" };
type Draft = typeof EMPTY;

function ConsoleForm({ console: c, onDone }: { console?: Console; onDone: () => void }) {
  const [d, setD] = useState<Draft>(c ? {
    name: c.name, ip_address: c.ip_address, connection_type: c.connection_type, port: String(c.port), username: c.username ?? "", password: "",
  } : EMPTY);
  const [forget, setForget] = useState(false);
  const [error, setError] = useState("");
  const field = (k: keyof Draft) => ({ value: d[k], onChange: (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value }) });

  async function save(e: FormEvent) {
    e.preventDefault();
    const { password, ...rest } = d;
    const body: Record<string, unknown> = { ...rest, port: d.connection_type === "helper" ? 7735 : Number(d.port), username: d.username || null };
    if (password) body.password = password; // blank = keep the saved one
    else if (forget || !c) body.password = null;
    try {
      await (c ? api(`/consoles/${c.id}`, { method: "PATCH", json: body }) : api("/consoles", { json: body }));
      onDone();
    } catch (err) {
      setError((err as Error).message);
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
      {c?.has_password && d.connection_type !== "helper" && (
        <label className="check"><input type="checkbox" checked={forget} onChange={(e) => setForget(e.target.checked)} /> Forget saved password</label>
      )}
      <div className="actions">
        <button type="submit">{c ? "Save" : "Add console"}</button>
        <button type="button" className="link" onClick={onDone}>Cancel</button>
        {error && <span className="error" role="alert">{error}</span>}
      </div>
    </form>
  );
}

export default function Consoles() {
  const [consoles, setConsoles] = useState<Console[]>([]);
  const [editing, setEditing] = useState<string | null>(null); // console id, "new", or null
  const [error, setError] = useState("");

  const load = () => api<Console[]>("/consoles").then(setConsoles, (e) => setError(e.message));
  useEffect(() => { load(); }, []);
  const done = () => (setEditing(null), load());
  const test = (c: Console) => api<Console>(`/consoles/${c.id}/test-connection`, { method: "POST" }).then(load, (e) => setError(e.message));
  const remove = (c: Console) =>
    confirm(`Remove ${c.name}? Nothing on the console is changed.`) &&
    api(`/consoles/${c.id}`, { method: "DELETE" }).then(load, (e) => setError(e.message));

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
                <span className={`badge ${STATE_BADGE[c.state]}`}>{c.state}</span> {c.status_message || (c.has_password ? "Password saved" : "No password")}
              </span>
              <span className="inline">
                <button className="link" onClick={() => test(c)}>Test connection</button>
                <button className="link" onClick={() => setEditing(c.id)}>Edit</button>
                <button className="link" onClick={() => remove(c)}>Remove</button>
              </span>
            </li>
          ))}
      </ul>
      {editing === "new" ? <ConsoleForm onDone={done} /> : <button onClick={() => setEditing("new")}>Add console</button>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
