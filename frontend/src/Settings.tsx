import { useEffect, useState, type FormEvent } from "react";
import { api, type Kind, type Root, type ScanStatus } from "./api";
import AuditLog from "./AuditLog";
import { useScan } from "./Library";
import Consoles from "./Consoles";

interface Listing { path: string; parent: string | null; dirs: string[] }

function FolderPicker({ start, onPick, onClose }: { start: string; onPick: (path: string) => void; onClose: () => void }) {
  const [list, setList] = useState<Listing | null>(null);
  const [error, setError] = useState("");
  const open = (path: string) =>
    api<Listing>(`/library/browse?path=${encodeURIComponent(path)}`).then((l) => (setList(l), setError("")), (e) => setError(e.message));
  useEffect(() => { open(start); }, []);
  const name = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() || p;

  return (
    <div className="picker">
      <div className="inline">
        <button type="button" disabled={list?.parent == null} onClick={() => open(list!.parent!)}>Up</button>
        <code className="picker-path">{list?.path || "Drives"}</code>
        <button type="button" disabled={!list?.path} onClick={() => onPick(list!.path)}>Add this folder</button>
        <button type="button" className="link" onClick={onClose}>Cancel</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <ul className="picker-list">
        {list?.dirs.map((d) => <li key={d}><button type="button" className="link" onClick={() => open(d)}>{name(d)}</button></li>)}
        {list && !list.dirs.length && <li className="muted">No subfolders</li>}
      </ul>
      <p className="muted">The folder and every subfolder under it are scanned.</p>
    </div>
  );
}

/** One kind's library folders. Kind only sorts them into lists: what's DLC is told by content, wherever it is. */
function Folders({ kind, roots, act }: { kind: Kind; roots: Root[]; act: (p: Promise<unknown>) => Promise<void> }) {
  const [path, setPath] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const addPath = (p: string) => act(api("/library/roots", { json: { path: p, kind } }).then(() => (setPath(""), setBrowsing(false))));
  const add = (e: FormEvent) => {
    e.preventDefault();
    addPath(path);
  };
  const label = kind === "dlc" ? "DLC folder" : "library folder";

  return (
    <>
      <ul className="roots">
        {roots.filter((r) => r.kind === kind).map((r) => (
          <li key={r.id}>
            <label className="check">
              <input type="checkbox" checked={r.enabled} onChange={(e) => act(api(`/library/roots/${r.id}`, { method: "PATCH", json: { enabled: e.target.checked } }))} />
              <code>{r.path}</code>
            </label>
            {!r.reachable && <span className="badge incompatible">Unreachable</span>}
            <input
              aria-label={`Exclude patterns for ${r.path}`}
              placeholder="Exclude patterns, comma-separated (e.g. *.tmp, old/*)"
              defaultValue={r.exclude_patterns.join(", ")}
              onBlur={(e) => {
                const pats = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                if (pats.join() !== r.exclude_patterns.join()) act(api(`/library/roots/${r.id}`, { method: "PATCH", json: { exclude_patterns: pats } }));
              }}
            />
            <button className="link" onClick={() => confirm(`Stop scanning ${r.path} and remove what it holds from the library? Files on disk are not touched.`) && act(api(`/library/roots/${r.id}`, { method: "DELETE" }))}>Remove</button>
          </li>
        ))}
      </ul>
      <form className="inline" onSubmit={add}>
        <input required placeholder={kind === "dlc" ? "G:\\DLC" : "G:\\"} aria-label={`New ${label}`} value={path} onChange={(e) => setPath(e.target.value)} />
        <button type="submit">Add folder</button>
        <button type="button" onClick={() => setBrowsing(true)}>Browse…</button>
      </form>
      {browsing && <FolderPicker start={path} onPick={addPath} onClose={() => setBrowsing(false)} />}
    </>
  );
}

/** A second password that can look at everything but change nothing: for a phone in the living room, or a guest. */
function ViewOnly() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  useEffect(() => { api<{ enabled: boolean }>("/auth/viewer").then((r) => setEnabled(r.enabled), () => {}); }, []);

  async function save(pw: string | null) {
    try {
      setEnabled((await api<{ enabled: boolean }>("/auth/viewer", { json: { password: pw } })).enabled);
      setPassword("");
      setMsg({ text: pw ? "Saved. Devices using an older view-only password were logged out." : "View-only access is off. Devices using it were logged out." });
    } catch (e) {
      setMsg({ text: (e as Error).message, error: true });
    }
  }

  return (
    <section className="card">
      <h2>View-only access</h2>
      <p className="muted">A second password that can browse the library, the dashboard and what's installed, but can't install,
        remove or change anything, and can't open Settings. {enabled ? "It's on." : enabled === false ? "It's off." : ""}</p>
      <form className="inline" onSubmit={(e: FormEvent) => (e.preventDefault(), save(password))}>
        <input type="password" required minLength={8} autoComplete="new-password" aria-label="View-only password"
          placeholder={enabled ? "New view-only password" : "View-only password (8 or more characters)"} value={password} onChange={(e) => setPassword(e.target.value)} />
        <button type="submit">{enabled ? "Change" : "Turn on"}</button>
        {enabled && <button type="button" className="link" onClick={() => save(null)}>Turn off</button>}
      </form>
      {msg && <p className={msg.error ? "error" : "muted"} role="status">{msg.text}</p>}
    </section>
  );
}

export default function Settings() {
  const [roots, setRoots] = useState<Root[]>([]);
  const [error, setError] = useState("");
  const [finished, setFinished] = useState(false); // a scan started or seen running here has ended
  const [scan, setScan] = useScan(() => setFinished(true));

  const load = () => api<Root[]>("/library/roots").then(setRoots, (e) => setError(e.message));

  useEffect(() => {
    load();
  }, []);


  async function act(p: Promise<unknown>) {
    setError("");
    try {
      await p;
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const startScan = () => act(api("/library/scan", { method: "POST" }).then(() => (setFinished(false), setScan((s) => ({ ...s!, running: true })))));
  const counts = (r: NonNullable<ScanStatus["report"]>) =>
    `${r.added.length} added, ${r.updated.length} updated, ${r.missing.length} missing${r.errors.length ? `, ${r.errors.length} errors` : ""}`;

  return (
    <div className="settings">
      <section className="card">
        <h2>Game folders</h2>
        <p className="muted">A folder or a whole drive, e.g. <code>G:\</code>. Every subfolder is scanned too. Folders are only ever read.</p>
        <Folders kind="game" roots={roots} act={act} />
        <h2>DLC folders</h2>
        <p className="muted">Where you keep DLC packages and DLC unlocker folders. DLC inside game folders is found too, and
          shows on the DLC page either way.</p>
        <Folders kind="dlc" roots={roots} act={act} />
        {error && <p className="error" role="alert">{error}</p>}
        <div className="toolbar scan-bar">
          <button onClick={startScan} disabled={scan?.running || !roots.length}>
            {scan?.running ? <><span className="spinner" aria-hidden />Scanning…</> : "Scan now"}
          </button>
          <span role="status" className={finished && scan?.report?.errors.length ? "error" : "muted"}>
            {scan?.running ? "Scanning every folder in both lists. A large drive can take a few minutes; you can leave this page."
              : finished && scan?.report ? <>Scan finished: {counts(scan.report)}. See them in the <a href="#/">Library</a> or on the <a href="#/dlc">DLC</a> page.</>
              : null}
          </span>
        </div>
        {scan?.report && !scan.running && (
          <>
            <p className="muted">Last scan finished {new Date(scan.finished_at!).toLocaleString()}</p>
            <ul className="report">
              {Object.entries(scan.report).map(([k, v]) => (
                <li key={k}>
                  <details>
                    <summary>{k}: {v.length}</summary>
                    <ul>{v.map((p) => <li key={p}><code>{p}</code></li>)}</ul>
                  </details>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <Consoles />
      <ViewOnly />
      <AuditLog />
    </div>
  );
}
