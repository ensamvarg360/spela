import { useEffect, useState, type FormEvent } from "react";
import Problem from "./Problem";
import { api, appVersion, needsOnline, type Console } from "./api";
import { STATE_LABEL, STATE_TONE } from "./Installed";
import { toast } from "./Toast";
import { Badge, Empty, Field, Icon, Notice, Panel, Spinner, Time } from "./ui";

/** Saves the console's RP-RegistKey right where it's missing, so waking doesn't need a trip through Edit. */
function WakeKey({ c, onSaved }: { c: Console; onSaved: () => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<unknown>("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    setError("");
    api(`/consoles/${c.id}`, { method: "PATCH", json: { wake_key: key } }).then(onSaved, setError);
  };
  return (
    <form className="sp-stack sp-stack--tight sp-grow" onSubmit={save}>
      <div className="sp-row">
        <input className="sp-input sp-input--sm sp-input--mono sp-exclude" required autoComplete="off" spellCheck={false} aria-label="Remote Play key"
          placeholder="e.g. 1a2b3c4d" value={key} onChange={(e) => setKey(e.target.value)} />
        <button className="sp-btn sp-btn--sm" type="submit">Save key</button>
      </div>
      <span className="sp-hint">The console's RP-RegistKey from Chiaki-ng. Edit shows how to get it.</span>
      <Problem error={error} />
    </form>
  );
}

/** "v0.7.3" and whether it matches this app, or that it isn't there. */
function Version({ v, want, missing }: { v: string | undefined; want: string; missing: string }) {
  if (!v || v === "missing") return <span className="sp-muted">{missing}</span>;
  return <>
    <span className="sp-num">v{v}</span>
    {want && (v === want ? <Badge tone="success">Up to date</Badge> : <Badge tone="warning">Needs v{want}</Badge>)}
  </>;
}

function Facts({ c, version, busy, act, onSaved }: { c: Console; version: string; busy: boolean; act: (path: string) => void; onSaved: () => void }) {
  const helper = c.helper_status.helper; // "missing", a version, or absent before the first check
  const hasHelper = !!helper && helper !== "missing";
  const helperOk = !!version && helper === version;
  const consoleApp = c.helper_status.console_app; // same: "missing", a version, or absent
  const hasApp = !!consoleApp && consoleApp !== "missing";
  const consoleAppOk = !!version && consoleApp === version;
  const on = c.state === "online" || c.state === "degraded";
  const connections = Object.entries(c.helper_status).filter(([k]) => !["banner", "helper", "console_app"].includes(k));
  return (
    <dl className="sp-kv">
      <dt>Firmware</dt><dd>{c.firmware ? <span className="sp-num">{c.firmware}</span> : <span className="sp-muted">Unknown</span>}</dd>
      <dt>Helper payload</dt>
      <dd>
        <Version v={helper} want={version} missing="Not running" />
        <button type="button" className="sp-btn sp-btn--sm" disabled={busy || c.state === "standby"} title={c.state === "standby" ? "Wake the console first." : undefined}
          onClick={() => act("helper")}>{!hasHelper ? "Install helper" : helperOk ? "Reinstall helper" : "Update helper"}</button>
        {(c.auto_helper || hasHelper) && (
          <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost sp-btn--danger" disabled={busy || !on}
            title={!on ? "The console must be on." : "Stop the helper, and stop sending it by itself."}
            onClick={() => act("remove-helper")}>Remove helper</button>
        )}
        {!c.auto_helper ? <span className="sp-hint">Not sent by itself until you click Install helper.</span>
          : !helperOk && c.connection_type !== "mock" && <span className="sp-hint">Sent by itself once the console's ELF loader is running.</span>}
      </dd>
      <dt>PS5 app</dt>
      <dd>
        <Version v={consoleApp} want={hasApp ? version : ""} missing="Not installed" />
        <button type="button" className="sp-btn sp-btn--sm" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
          onClick={() => act("console-app")}>{!hasApp ? "Install PS5 app" : consoleAppOk ? "Reinstall PS5 app" : "Update PS5 app"}</button>
        {hasApp && (
          <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost sp-btn--danger" disabled={busy || c.state !== "online"} title={c.state !== "online" ? "The console must be online." : undefined}
            onClick={() => act("remove-console-app")}>Remove PS5 app</button>
        )}
      </dd>
      <dt>Wake from rest mode</dt>
      <dd>
        {c.has_wake_key ? "Remote Play key saved" : <WakeKey c={c} onSaved={onSaved} />}
        {c.has_wake_key && c.state === "standby" && <button type="button" className="sp-btn sp-btn--sm" disabled={busy} onClick={() => act("wake")}><Icon name="power" />Wake</button>}
      </dd>
      <dt>Connection</dt><dd>{connections.length ? connections.map(([k, v]) => `${k}: ${v}`).join(", ") : c.connection_type}</dd>
      <dt>Last seen</dt><dd><Time t={c.last_seen_at} /></dd>
      <dt>Last inventory</dt><dd><Time t={c.last_inventory_scan_at} /></dd>
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
    <div>
      <h3>Find it on my network</h3>
      <p className="sp-hint">Finds a PS5 that's switched on, or in rest mode with Stay connected to the internet on.</p>
      {found && found.length > 0 && (
        <ul className="sp-boxed">
          {found.map((p) => (
            <li key={p.ip} className="sp-listrow sp-listrow--dense">
              <div className="sp-listrow__main">
                <div className="sp-listrow__title">{p.name}{p.state === "online" ? <Badge tone="success" dot>On</Badge> : <Badge tone="warning" dot>Rest mode</Badge>}</div>
                <div className="sp-listrow__meta"><span className="sp-path">{p.ip}</span></div>
              </div>
              <div className="sp-listrow__aside">
                {p.added ? <Badge tone="success">Added</Badge> : <button type="button" className="sp-btn sp-btn--sm" onClick={() => add(p)}>Add</button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {found && !found.length && !looking && (
        <Problem error="No PS5 answered." fix={"It may be switched off, on another network, or the router may keep devices " +
          "from seeing each other (client or AP isolation, common on guest Wi-Fi). Enter its IP address instead."} />
      )}
      <Problem error={error} />
      <div className="sp-btngroup"><button type="button" className="sp-btn" disabled={looking} onClick={look}>
        {looking ? <><Spinner />Looking…</> : found ? "Look again" : "Look for it"}
      </button></div>
    </div>
  );
}

const EMPTY = { name: "", ip_address: "", connection_type: "helper", port: "7735", username: "", password: "", wake_key: "" };
type Draft = typeof EMPTY;

function ConsoleForm({ console: c, onDone, onRemove }: { console?: Console; onDone: () => void; onRemove?: () => void }) {
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
    <form className="sp-stack" onSubmit={save}>
      <div className="sp-formgrid">
        <Field label="Name"><input className="sp-input" required maxLength={100} placeholder="Living Room PS5" {...field("name")} /></Field>
        <Field label="IP address"><input className="sp-input sp-input--mono" required placeholder="192.168.1.50" {...field("ip_address")} /></Field>
        <Field label="Connection">
          <select className="sp-select" {...field("connection_type")}>
            <option value="helper">Helper payload</option>
            <option value="ftp">FTP (fallback)</option>
            <option value="http">HTTP</option>
            <option value="custom">Custom</option>
            <option value="mock">Mock (testing)</option>
          </select>
        </Field>
        {d.connection_type !== "helper" && <>
          <Field label="Port"><input className="sp-input sp-input--mono" required type="number" min={1} max={65535} {...field("port")} /></Field>
          <Field label="Username"><input className="sp-input" autoComplete="off" placeholder="Optional" {...field("username")} /></Field>
          <Field label="Password">
            <input className="sp-input" type="password" autoComplete="new-password" disabled={forget}
              placeholder={c?.has_password ? "Saved. Leave blank to keep it" : "Optional"} {...field("password")} />
          </Field>
        </>}
        <Field wide label="Remote Play key" hint="Optional. Lets Spela wake the console from rest mode.">
          <input className="sp-input sp-input--mono" autoComplete="off" spellCheck={false} disabled={forgetWake}
            placeholder={c?.has_wake_key ? "Saved. Leave blank to keep it" : "e.g. 1a2b3c4d"} {...field("wake_key")} />
        </Field>
      </div>
      <Notice tone="info" title="Get the wake key from Chiaki-ng"
        text={<>Register this PC's Remote Play once with Chiaki-ng (PS5 Settings → System → Remote Play → Link Device), then copy the console's
          RP-RegistKey here. In its rest-mode settings the PS5 needs Stay connected to the internet and Enable turning on PS5 from network.</>} />
      {c?.has_wake_key && (
        <label className="sp-check"><input type="checkbox" checked={forgetWake} onChange={(e) => setForgetWake(e.target.checked)} /> Forget saved Remote Play key</label>
      )}
      {c?.has_password && d.connection_type !== "helper" && (
        <label className="sp-check"><input type="checkbox" checked={forget} onChange={(e) => setForget(e.target.checked)} /> Forget saved password</label>
      )}
      <Problem error={error} />
      <div className="sp-row">
        {onRemove && <button type="button" className="sp-btn sp-btn--danger" onClick={onRemove}>Remove {c!.name}</button>}
        <div className="sp-btngroup sp-push">
          <button type="button" className="sp-btn sp-btn--ghost" onClick={onDone}>Cancel</button>
          <button type="submit" className="sp-btn sp-btn--primary">{c ? "Save" : "Add console"}</button>
        </div>
      </div>
    </form>
  );
}

export default function Consoles() {
  const [consoles, setConsoles] = useState<Console[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null); // console id, "new", or null
  const [error, setError] = useState<{ e: unknown; retry?: () => void } | null>(null);
  const [busy, setBusy] = useState(false);
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
    if (path === "remove-console-app" && !confirm(`Remove the PS5 app from ${c.name}?\n\nIts folder and its home-screen tile ` +
      "are deleted. If it's open on the PS5, close it first. Install PS5 app puts it back.")) return;
    setBusy(true);
    setError(null);
    try {
      if (path === "remove-console-app") {
        toast((await api<{ message: string }>(`/consoles/${c.id}/console-app`, { method: "DELETE" })).message);
        await load();
        return;
      }
      const after = await api<Console>(`/consoles/${c.id}/${removing ? "helper" : path}`, { method: removing ? "DELETE" : "POST" });
      if (path === "test-connection") toast(`${c.name}: ${STATE_LABEL[after.state]}. ${after.status_message}`, after.state !== "online");
      if (path === "rescan") toast(`Refreshed what's installed on ${c.name}.`);
      if (removing) toast(`The helper is stopped on ${c.name}, and Spela won't send it again until you click Install helper.`);
      if (path === "helper") toast(`Helper v${after.helper_status.helper} is running on ${c.name}.`);
      if (path === "console-app")
        toast(`PS5 app v${after.helper_status.console_app} copied to ${c.name} and checked file by file. If it was open on the PS5, close and reopen it. ` +
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
    api(`/consoles/${c.id}`, { method: "DELETE" }).then(done, (e) => setError({ e }));

  const adding = editing === "new";
  const none = consoles && !consoles.length && !adding;

  return (
    <Panel title="Consoles" desc="The PS5s Spela installs to. Spela checks on them every 15 seconds."
      actions={!adding && !none && <button type="button" className="sp-btn" onClick={() => setEditing("new")}><Icon name="plus" />Add console</button>} flush>
      {error?.e ? <div className="sp-listrow__more sp-listrow__more--top"><Problem error={error.e} retry={error.retry} /></div> : null}
      {none ? (
        <Empty icon="inbox" title="No consoles yet" text="Add your PS5 by its IP address, or let Spela find it on your network.">
          <button type="button" className="sp-btn sp-btn--primary" onClick={() => setEditing("new")}>Add console</button>
        </Empty>
      ) : (
        <ul className="sp-list">
          {consoles?.map((c) => (
            <li key={c.id}>
              {editing === c.id ? (
                <div className="sp-listrow__more sp-listrow__more--top"><ConsoleForm console={c} onDone={done} onRemove={() => remove(c)} /></div>
              ) : <>
                <div className="sp-listrow">
                  <div className="sp-listrow__main">
                    <div className="sp-listrow__title">{c.name}<Badge tone={STATE_TONE[c.state]} dot>{STATE_LABEL[c.state]}</Badge></div>
                    <div className="sp-listrow__meta">
                      <span className="sp-path">{c.ip_address}</span><span className="sp-sep" />
                      <span>{c.connection_type === "helper" ? "Helper payload" : c.connection_type.toUpperCase()}</span>
                      {c.connection_type !== "helper" && <><span className="sp-sep" /><span>{c.has_password ? "Password saved" : "No password"}</span></>}
                    </div>
                  </div>
                  <div className="sp-listrow__aside">
                    <button type="button" className="sp-btn sp-btn--sm" disabled={busy} onClick={() => act(c, "test-connection")}>Check connection</button>
                    <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost" {...needsOnline(c)} disabled={busy || c.state !== "online"} onClick={() => act(c, "rescan")}>Refresh inventory</button>
                    <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost" onClick={() => setEditing(c.id)}>Edit</button>
                  </div>
                </div>
                <div className="sp-listrow__more">
                  {c.status_message && c.state !== "online" && <Notice tone={STATE_TONE[c.state] || "warning"} title={c.status_message} text={c.status_fix} />}
                  <Facts c={c} version={version} busy={busy} act={(path) => act(c, path)} onSaved={load} />
                </div>
              </>}
            </li>
          ))}
          {adding && (
            <li className="sp-listrow__more sp-listrow__more--top">
              <div className="sp-addconsole">
                <FindConsole onAdded={done} />
                <div>
                  <h3>I know its IP address</h3>
                  <ConsoleForm onDone={done} />
                </div>
              </div>
            </li>
          )}
        </ul>
      )}
    </Panel>
  );
}
