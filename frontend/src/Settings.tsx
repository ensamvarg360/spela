import { useEffect, useState, type FormEvent } from "react";
import { api, type Root, type ScanStatus } from "./api";
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

export default function Settings() {
  const [roots, setRoots] = useState<Root[]>([]);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [scan, setScan] = useState<ScanStatus | null>(null);
  const [browsing, setBrowsing] = useState(false);

  const load = () => api<Root[]>("/library/roots").then(setRoots, (e) => setError(e.message));
  const pollScan = () => api<ScanStatus>("/library/scan-status").then(setScan);

  useEffect(() => {
    load();
    pollScan();
  }, []);

  useEffect(() => {
    if (!scan?.running) return;
    const t = setTimeout(pollScan, 1500);
    return () => clearTimeout(t);
  }, [scan]);

  async function act(p: Promise<unknown>) {
    setError("");
    try {
      await p;
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const addPath = (p: string) => act(api("/library/roots", { json: { path: p } }).then(() => (setPath(""), setBrowsing(false))));
  const add = (e: FormEvent) => {
    e.preventDefault();
    addPath(path);
  };

  const startScan = () => act(api("/library/scan", { method: "POST" }).then(() => setScan((s) => ({ ...s!, running: true }))));

  return (
    <div className="settings">
      <section className="card">
        <h2>Library folders</h2>
        <p className="muted">A folder or a whole drive, e.g. <code>G:\</code>. Every subfolder is scanned too. Folders are only ever read.</p>
        <ul className="roots">
          {roots.map((r) => (
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
              <button className="link" onClick={() => confirm(`Stop scanning ${r.path} and remove its games from the library? Files on disk are not touched.`) && act(api(`/library/roots/${r.id}`, { method: "DELETE" }))}>Remove</button>
            </li>
          ))}
        </ul>
        <form className="inline" onSubmit={add}>
          <input required placeholder="G:\" aria-label="New library folder" value={path} onChange={(e) => setPath(e.target.value)} />
          <button type="submit">Add folder</button>
          <button type="button" onClick={() => setBrowsing(true)}>Browse…</button>
        </form>
        {browsing && <FolderPicker start={path} onPick={addPath} onClose={() => setBrowsing(false)} />}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="card">
        <h2>Scan</h2>
        <button onClick={startScan} disabled={scan?.running || !roots.length}>{scan?.running ? "Scanning…" : "Scan now"}</button>
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
    </div>
  );
}
