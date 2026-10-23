import { Fragment, useEffect, useState, type FormEvent } from "react";
import Problem from "./Problem";
import { api, type Game, type Kind, type Root, type ScanStatus } from "./api";
import AuditLog from "./AuditLog";
import { useScan } from "./Library";
import { toast, toastError } from "./Toast";
import Consoles from "./Consoles";
import { Badge, Field, Icon, Notice, PageHead, Panel, Path, Spinner, Time } from "./ui";

interface Listing { path: string; parent: string | null; dirs: string[] }

function FolderPicker({ start, onPick, onClose }: { start: string; onPick: (path: string) => void; onClose: () => void }) {
  const [list, setList] = useState<Listing | null>(null);
  const [error, setError] = useState<unknown>("");
  const open = (path: string) =>
    api<Listing>(`/library/browse?path=${encodeURIComponent(path)}`).then((l) => (setList(l), setError("")), setError);
  useEffect(() => { open(start); }, []);
  const name = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() || p;

  return (
    <div className="sp-stack sp-grow">
      <div className="sp-row">
        <button type="button" className="sp-btn sp-btn--sm" disabled={list?.parent == null} onClick={() => open(list!.parent!)}>Up</button>
        {list?.path ? <Path path={list.path} /> : <span className="sp-muted">Drives</span>}
        <div className="sp-btngroup sp-push">
          <button type="button" className="sp-btn sp-btn--ghost sp-btn--sm" onClick={onClose}>Cancel</button>
          <button type="button" className="sp-btn sp-btn--primary sp-btn--sm" disabled={!list?.path} onClick={() => onPick(list!.path)}>Add this folder</button>
        </div>
      </div>
      <Problem error={error} />
      <ul className="sp-boxed sp-boxed--scroll">
        {list?.dirs.map((d) => (
          <li key={d}><button type="button" className="sp-btn sp-btn--ghost sp-btn--sm sp-picker__dir" onClick={() => open(d)}><Icon name="folder" />{name(d)}</button></li>
        ))}
        {list && !list.dirs.length && <li className="sp-picker__none sp-muted">No subfolders</li>}
      </ul>
      <span className="sp-hint">The folder and every subfolder under it are scanned.</span>
    </div>
  );
}

/** One kind's library folders, as list rows ending in an add row. Kind only sorts them into lists: what's DLC is told
 *  by content, wherever it is. `act` runs a change and reloads the roots; its errors show under this list. */
export function Folders({ kind, roots, act }: { kind: Kind; roots: Root[]; act: (p: Promise<unknown>) => Promise<void> }) {
  const [path, setPath] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [error, setError] = useState<unknown>("");
  const actHere = (p: Promise<unknown>) => (setError(""), act(p).catch(setError));
  const addPath = (p: string) => actHere(api("/library/roots", { json: { path: p, kind } }).then(() => (setPath(""), setBrowsing(false))));
  const add = (e: FormEvent) => {
    e.preventDefault();
    addPath(path);
  };
  const label = kind === "dlc" ? "DLC folder" : "game folder";

  return (
    <ul className="sp-list">
      {roots.filter((r) => r.kind === kind).map((r) => (
        <li key={r.id} className="sp-listrow">
          <div className="sp-listrow__lead">
            <label className="sp-switch">
              <input type="checkbox" role="switch" checked={r.enabled}
                onChange={(e) => actHere(api(`/library/roots/${r.id}`, { method: "PATCH", json: { enabled: e.target.checked } }))} />
              <span className="sp-switch__track" />
              <span className="sp-visually-hidden">Include {r.path} in scans</span>
            </label>
            <Icon name="folder" className="sp-icon" />
          </div>
          <div className="sp-listrow__main">
            <div className="sp-listrow__title">
              <Path path={r.path} />
              {!r.reachable && <Badge tone="danger">Unreachable</Badge>}
              {!r.enabled && <Badge>Not scanned</Badge>}
            </div>
          </div>
          <div className="sp-listrow__aside">
            <input className="sp-input sp-input--sm sp-input--mono sp-exclude"
              aria-label={`Exclude patterns for ${r.path}`} title="Comma-separated patterns to skip"
              placeholder="Exclude, e.g. *.tmp, old/*"
              defaultValue={r.exclude_patterns.join(", ")}
              onBlur={(e) => {
                const pats = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                if (pats.join() !== r.exclude_patterns.join()) actHere(api(`/library/roots/${r.id}`, { method: "PATCH", json: { exclude_patterns: pats } }));
              }}
            />
            <button type="button" className="sp-btn sp-btn--ghost sp-btn--danger sp-btn--icon sp-btn--sm" aria-label={`Remove ${r.path}`} title="Remove folder"
              onClick={() => confirm(`Stop scanning ${r.path} and remove what it holds from the library? Files on disk are not touched.`) && actHere(api(`/library/roots/${r.id}`, { method: "DELETE" }))}>
              <Icon name="trash" />
            </button>
          </div>
        </li>
      ))}
      <li className="sp-listrow">
        {browsing ? <FolderPicker start={path} onPick={addPath} onClose={() => setBrowsing(false)} /> : (
          <form className="sp-inputgroup sp-grow" onSubmit={add}>
            <input className="sp-input sp-input--mono" required placeholder={kind === "dlc" ? "G:\\DLC" : "G:\\Games"} aria-label={`New ${label}`} value={path} onChange={(e) => setPath(e.target.value)} />
            <button type="button" className="sp-btn" onClick={() => setBrowsing(true)}>Browse…</button>
            <button type="submit" className="sp-btn"><Icon name="plus" />Add folder</button>
          </form>
        )}
        {error ? <div className="sp-grow"><Problem error={error} /></div> : null}
      </li>
    </ul>
  );
}

/** A second password that can look at everything but change nothing: for a phone in the living room, or a guest. */
function ViewOnly() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  useEffect(() => { api<{ enabled: boolean }>("/auth/viewer").then((r) => setEnabled(r.enabled), () => {}); }, []);

  async function save(pw: string | null) {
    try {
      setEnabled((await api<{ enabled: boolean }>("/auth/viewer", { json: { password: pw } })).enabled);
      setPassword("");
      toast(pw ? "Saved. Devices using an older view-only password were signed out." : "View-only access is off. Devices using it were signed out.");
    } catch (e) {
      toastError(e);
    }
  }

  return (
    <form onSubmit={(e: FormEvent) => (e.preventDefault(), save(password))}>
      <Panel title="View-only access" badge={enabled != null && (enabled ? <Badge tone="success">On</Badge> : <Badge>Off</Badge>)}
        desc="A second password that can browse the library, the dashboard and what's installed, but can't install, remove or open Settings."
        foot={<>
          <span className="sp-panel__note">Anyone with this password can see your library.</span>
          <div className="sp-btngroup">
            {enabled && <button type="button" className="sp-btn sp-btn--ghost" onClick={() => save(null)}>Turn off</button>}
            <button type="submit" className="sp-btn sp-btn--primary">{enabled ? "Change password" : "Turn on"}</button>
          </div>
        </>}>
        <Field label={enabled ? "New view-only password" : "View-only password"}>
          <input className="sp-input" type="password" required minLength={8} autoComplete="new-password"
            placeholder="At least 8 characters" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
      </Panel>
    </form>
  );
}

/** The key in the helpers this install sends. Pasting another Spela's lets both use one helper: a PC and a NAS, or a dev build. */
function HelperKey() {
  const [state, setState] = useState<{ key: string; source: "own" | "pasted" | "env" } | null>(null);
  const [key, setKey] = useState("");
  useEffect(() => { api<NonNullable<typeof state>>("/helper-key").then(setState, () => {}); }, []);

  async function save(k: string | null) {
    try {
      setState(await api<NonNullable<typeof state>>("/helper-key", { json: { key: k } }));
      setKey("");
      toast(k ? "Saved. This Spela now uses the other one's helper." : "This Spela uses its own key again. It sends its own helper within a minute.");
    } catch (e) {
      toastError(e);
    }
  }

  return (
    <form onSubmit={(e: FormEvent) => (e.preventDefault(), save(key))}>
      <Panel title="Helper key" badge={state && state.source !== "own" && <Badge tone="info">{state.source === "env" ? "From SPELA_HELPER_KEY" : "Shared"}</Badge>}
        desc="Running Spela in two places, like on this PC and a NAS? Copy this key into the other one's Settings and both use the same helper on your PS5. The one that sent it keeps it up to date."
        foot={<>
          <span className="sp-panel__note">Anyone with this key can control the helper on your PS5.</span>
          {state?.source !== "env" && <div className="sp-btngroup">
            {state?.source === "pasted" && <button type="button" className="sp-btn sp-btn--ghost" onClick={() => save(null)}>Use own key</button>}
            <button type="submit" className="sp-btn sp-btn--primary">Use pasted key</button>
          </div>}
        </>}>
        <Field label="This Spela's key">
          <div className="sp-inputgroup">
            <input className="sp-input sp-input--mono" readOnly value={state?.key ?? ""} onFocus={(e) => e.target.select()} />
            <button type="button" className="sp-btn" onClick={() => navigator.clipboard.writeText(state!.key).then(() => toast("Copied."), toastError)}>Copy</button>
          </div>
        </Field>
        {state?.source !== "env" && <Field label="Key from another Spela">
          <input className="sp-input sp-input--mono" required pattern="\s*[0-9a-fA-F]{64}\s*" autoComplete="off" spellCheck={false}
            placeholder="Paste it here" value={key} onChange={(e) => setKey(e.target.value)} />
        </Field>}
      </Panel>
    </form>
  );
}

/** After a scan that left the library without games: what Spela looks for, and what it couldn't identify. */
export function NothingFound({ report }: { report: NonNullable<ScanStatus["report"]> }) {
  return <Problem error="Nothing in these folders looks like a PS5 game." fix={"Spela recognizes game folders with sce_sys/param.json " +
    "inside, .exfat, .ffpkg, .ffpfs and .pkg files, and .zip, .7z and .rar archives that hold one of them. " +
    (report.unknown.length ? `${report.unknown.length} files couldn't be identified; they're listed under "Not identified" in Settings. ` : "") +
    "Check that the folder holds your backups, or add the one that does."} />;
}

const REPORT_LABEL: Record<keyof NonNullable<ScanStatus["report"]>, string> = {
  added: "Added", updated: "Updated", missing: "Missing", unknown: "Not identified", invalid: "Invalid", errors: "Errors",
};

export default function Settings() {
  const [roots, setRoots] = useState<Root[]>([]);
  const [error, setError] = useState<unknown>("");
  const [finished, setFinished] = useState(false); // a scan started or seen running here has ended
  const [empty, setEmpty] = useState(false); // the library has no games after the scan
  const [scan, setScan] = useScan(() => {
    setFinished(true);
    api<Game[]>("/games").then((gs) => setEmpty(!gs.some((g) => g.kind === "game" && !g.missing)), () => {});
  });

  const load = () => api<Root[]>("/library/roots").then(setRoots, setError);

  useEffect(() => {
    load();
  }, []);

  const act = (p: Promise<unknown>) => p.then(load);

  const startScan = () => (setError(""), api("/library/scan", { method: "POST" })
    .then(() => (setFinished(false), setScan((s) => ({ ...s!, running: true }))), setError));
  const report = !scan?.running && scan?.report;
  const counts = (r: NonNullable<ScanStatus["report"]>) =>
    `${r.added.length} added, ${r.updated.length} updated, ${r.missing.length} missing${r.errors.length ? `, ${r.errors.length} errors` : ""}.`;

  return (
    <>
      <PageHead title="Settings" desc="Spela only ever reads your folders. Console passwords are stored encrypted and never shown again."
        actions={<a className="sp-btn" href="#/setup">Setup guide</a>} />

      <Panel title="Game folders" desc="A folder or a whole drive. Every subfolder is scanned too." flush>
        <Folders kind="game" roots={roots} act={act} />
      </Panel>
      <Panel title="DLC folders" desc="Where you keep DLC packages and unlocker folders. DLC inside game folders is found too." flush>
        <Folders kind="dlc" roots={roots} act={act} />
      </Panel>

      <Panel title="Library scan" badge={scan?.running && <Badge tone="info"><Spinner />Scanning</Badge>}
        desc="Reads every folder in both lists. A large drive can take a few minutes, and you can leave this page meanwhile."
        foot={<>
          <span className="sp-panel__note">{scan?.finished_at ? <>Last scan <Time t={scan.finished_at} /></> : "No scan since the app started."}</span>
          <button type="button" className="sp-btn sp-btn--primary" onClick={startScan} disabled={scan?.running || !roots.length}
            title={!roots.length ? "Add a folder first." : undefined}><Icon name="refresh" />Scan now</button>
        </>}>
        {(error || report) ? <>
          <Problem error={error} />
          {finished && report && (empty ? <NothingFound report={report} /> : (
            <Notice tone={report.errors.length ? "warning" : "success"} title="Scan finished"
              text={<>{counts(report)} See them in the <a className="sp-link" href="#/">Library</a> or on the <a className="sp-link" href="#/dlc">DLC</a> page.</>} />
          ))}
          {report && (
            <dl className="sp-kv">
              {(Object.keys(REPORT_LABEL) as (keyof typeof REPORT_LABEL)[]).map((k) => (
                <Fragment key={k}>
                  <dt>{REPORT_LABEL[k]}</dt>
                  <dd>{report[k].length ? (
                    <details className="sp-disclosure">
                      <summary><Icon name="chevronRight" /><span className="sp-num">{report[k].length}</span> {report[k].length === 1 ? "item" : "items"}</summary>
                      <div className="sp-stack sp-stack--tight">{report[k].map((p) => <Path key={p} path={p} />)}</div>
                    </details>
                  ) : <span className="sp-muted sp-num">0</span>}</dd>
                </Fragment>
              ))}
            </dl>
          )}
        </> : null}
      </Panel>

      <Consoles />
      <HelperKey />
      <ViewOnly />
      <AuditLog />
    </>
  );
}
