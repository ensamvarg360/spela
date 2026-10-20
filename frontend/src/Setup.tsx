import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, type Console, type Root } from "./api";
import { STATE_BADGE, STATE_LABEL } from "./Installed";
import { useScan } from "./Library";
import Problem from "./Problem";
import { Folders, NothingFound } from "./Settings";

/** GET /setup: each step worked out from what's already set up (backend/app/main.py, setup_status). */
export interface SetupState {
  dismissed: boolean;
  folder: boolean;
  scan: boolean;
  console: boolean;
  helper: boolean;
  install: boolean;
  console_app: boolean;
  wake: boolean;
  dlc_folder: boolean;
  viewer: boolean;
  suggest: { id: string; title: string } | null;
}

const STEPS = [
  ["folder", "Add your game folder"],
  ["scan", "Scan it"],
  ["console", "Add your PS5"],
  ["helper", "Start the helper"],
  ["install", "Install a game"],
] as const;

/** The setup state, re-read every 5 s: the console steps change on their own as the PS5 comes up. */
export function useSetup() {
  const [state, setState] = useState<SetupState | null>(null);
  const load = () => api<SetupState>("/setup").then(setState, () => {});
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);
  return [state, load] as const;
}

const dismiss = (dismissed: boolean) => api("/setup", { json: { dismissed } });

function Check({ done }: { done: boolean }) {
  return (
    <span className={`step-check${done ? " done" : ""}`} aria-hidden>
      {done && <svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>}
    </span>
  );
}

/** The Dashboard's reminder until the required steps are done or it's hidden. */
export function SetupCard() {
  const [state, load] = useSetup();
  if (!state || state.dismissed) return null;
  const done = STEPS.filter(([k]) => state[k]).length;
  if (done === STEPS.length) return null;
  return (
    <section className="card setup-card">
      <div className="toolbar">
        <h2 className="inline-title">Finish setting up Spela</h2>
        <span className="muted">{done} of {STEPS.length} done</span>
        <span className="spacer" />
        <button className="link" onClick={() => dismiss(true).then(load)}>Hide</button>
      </div>
      <ol className="setup-mini">
        {STEPS.map(([k, label]) => <li key={k} className={state[k] ? "done" : ""}><Check done={state[k]} />{label}</li>)}
      </ol>
      <a className="button" href="#/setup">Continue setup</a>
    </section>
  );
}

/** Step 3: the two things a console needs to be found, nothing else. Settings → Consoles has the rest. */
function AddConsole({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [ip, setIp] = useState("");
  const [error, setError] = useState<unknown>("");

  async function add(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await api("/consoles", { json: { name, ip_address: ip.trim(), connection_type: "helper", port: 7735 } });
      onAdded();
    } catch (err) {
      setError(err instanceof ApiError && err.status === 422 && /ip/i.test(err.message)
        ? new ApiError(422, "invalid_ip", `${ip} isn't an IP address.`, "It's four numbers with dots between them, like 192.168.1.50.")
        : err);
    }
  }

  return (
    <form className="setup-form" onSubmit={add}>
      <div className="fields">
        <label>Name<input required maxLength={100} placeholder="Living Room PS5" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>IP address<input required inputMode="decimal" placeholder="192.168.1.50" value={ip} onChange={(e) => setIp(e.target.value)} /></label>
      </div>
      <p className="muted">On the PS5: <em>Settings → Network → Connection Status → View Connection Status</em>, under <em>IPv4 address</em>.
        It can change when the router restarts; most routers let you reserve one for the PS5.</p>
      <div className="actions"><button type="submit">Add console</button></div>
      <Problem error={error} />
    </form>
  );
}

/** Step 4: each console's state, what to do about it, and the button for it. */
function HelperStep({ consoles, reload }: { consoles: Console[]; reload: () => void }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<{ e: unknown; retry: () => void } | null>(null);

  async function act(c: Console, path: "helper" | "test-connection") {
    setBusy(c.id);
    setError(null);
    try {
      await api(`/consoles/${c.id}/${path}`, { method: "POST" });
      reload();
    } catch (e) {
      setError({ e, retry: () => act(c, path) });
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <p>The helper is a small program Spela sends to the PS5. It reads the drives and copies games, and stops whenever
        the PS5 restarts. Run your jailbreak's ELF loader and Spela sends it by itself; you don't need to click anything.</p>
      <ul className="setup-consoles">
        {consoles.map((c) => (
          <li key={c.id}>
            <div className="toolbar">
              <strong>{c.name}</strong>
              <span className={`badge ${STATE_BADGE[c.state]}`}>{STATE_LABEL[c.state]}</span>
              <span className="muted">{c.state === "online" ? "The helper is running." : c.status_message || "Checking…"}</span>
              <span className="spacer" />
              {c.state !== "online" && <>
                <button className="small secondary" disabled={!!busy} onClick={() => act(c, "test-connection")}>{busy === c.id ? "Checking…" : "Check now"}</button>
                <button className="small" disabled={!!busy || c.state === "standby"} onClick={() => act(c, "helper")}>Send helper now</button>
              </>}
            </div>
            {c.state !== "online" && c.status_fix && <p className="callout">{c.status_fix}</p>}
          </li>
        ))}
      </ul>
      <p className="muted">Spela checks every 15 seconds; this page updates by itself.</p>
      <Problem error={error?.e} retry={error?.retry} />
    </>
  );
}

const NEEDS = [
  <>A PS5 that runs homebrew, with an <strong>ELF loader</strong> on port 9021 (most jailbreaks start one).</>,
  <><a href="https://github.com/drakmor/ShadowMountPlus" target="_blank" rel="noreferrer">ShadowMountPlus</a> on the PS5. It's what puts installed games on the home screen.</>,
  <>This PC and the PS5 on the <strong>same home network</strong>.</>,
  <>Your <strong>own backups</strong> of games you own, in a folder or on a drive of this PC. Spela never downloads games.</>,
];

const OPTIONAL: [keyof SetupState, string, ReactNode][] = [
  ["console_app", "PS5 app", <>Browse and install from the couch with the controller. Tick <em>Allow other devices on my network</em> in Spela's tray menu, then <em>Install PS5 app</em> in <a href="#/settings">Settings → Consoles</a>.</>],
  ["wake", "Wake from rest mode", <>Save the PS5's Remote Play key under <a href="#/settings">Settings → Consoles → Edit</a> to wake it from here.</>],
  ["dlc_folder", "DLC folder", <>If you keep DLC in its own folder, add it in <a href="#/settings">Settings</a>. DLC inside game folders is found anyway.</>],
  ["viewer", "View-only password", <>Let a phone or a guest browse without being able to change anything. In <a href="#/settings">Settings</a>.</>],
];

/** The Get started page (#/setup): the required steps in order, each opening when it's next, then the optional ones. */
export default function Setup() {
  const [state, loadState] = useSetup();
  const [roots, setRoots] = useState<Root[]>([]);
  const [consoles, setConsoles] = useState<Console[]>([]);
  const [finished, setFinished] = useState(false);
  const [scanError, setScanError] = useState<unknown>("");
  const loadRoots = () => api<Root[]>("/library/roots").then(setRoots, () => {});
  const loadConsoles = () => api<Console[]>("/consoles").then(setConsoles, () => {});
  const [scan, setScan] = useScan(() => (setFinished(true), loadState()));
  useEffect(() => {
    loadRoots();
    loadConsoles();
    const t = setInterval(loadConsoles, 5000);
    return () => clearInterval(t);
  }, []);

  if (!state) return <p className="muted">Loading…</p>;
  const current = STEPS.findIndex(([k]) => !state[k]);
  const done = STEPS.filter(([k]) => state[k]).length;
  const startScan = () => (setScanError(""), api("/library/scan", { method: "POST" })
    .then(() => (setFinished(false), setScan((s) => ({ ...s!, running: true }))), setScanError));
  const reload = () => (loadState(), loadConsoles());

  const body: Record<(typeof STEPS)[number][0], ReactNode> = {
    folder: <>
      <p>Pick the folder that holds your PS5 backups, or a whole drive like <code>G:\</code>. Spela only ever reads it, subfolders included.</p>
      <Folders kind="game" roots={roots} act={(p) => p.then(() => (loadRoots(), loadState()))} />
    </>,
    scan: <>
      <p>Spela looks through the folder for game folders, disc images, packages and archives, and gets their art from PlayStation Store.</p>
      <div className="toolbar">
        <button onClick={startScan} disabled={scan?.running || !state.folder}>{scan?.running ? <><span className="spinner" aria-hidden />Scanning…</> : "Scan now"}</button>
        <span className="muted" role="status">{!state.folder ? "Add a folder first."
          : scan?.running ? "A large drive can take a few minutes. You can carry on with the next steps meanwhile."
          : finished && scan?.report ? `Done: ${scan.report.added.length} added.` : null}</span>
      </div>
      {finished && !scan?.running && scan?.report && !state.scan && <NothingFound report={scan.report} />}
      <Problem error={scanError} />
    </>,
    console: consoles.length
      ? <p>Added: {consoles.map((c) => `${c.name} (${c.ip_address})`).join(", ")}. More in <a href="#/settings">Settings → Consoles</a>.</p>
      : <AddConsole onAdded={reload} />,
    helper: consoles.length ? <HelperStep consoles={consoles} reload={reload} /> : <p className="muted">Add your PS5 first.</p>,
    install: <>
      <p>Open a game, click <strong>Install</strong>, and follow the copy on the <a href="#/installed">Installed</a> page.
        ShadowMountPlus puts it on the PS5's home screen when it's done.</p>
      {state.suggest && <p>Try <a href={`#/game/${state.suggest.id}`}>{state.suggest.title}</a>, or pick one in the <a href="#/">Library</a>.</p>}
    </>,
  };

  return (
    <div className="setup">
      <header className="setup-head">
        <h1>Get started</h1>
        <p className="muted">{done === STEPS.length ? "You're all set." : `${done} of ${STEPS.length} done. Spela remembers where you got to.`}</p>
        <meter min={0} max={STEPS.length} value={done} aria-label="Setup progress" />
      </header>

      <details className="card" open={done === 0}>
        <summary><h2 className="inline-title">Before you start</h2></summary>
        <ul className="needs">{NEEDS.map((n, i) => <li key={i}>{n}</li>)}</ul>
      </details>

      <ol className="setup-steps">
        {STEPS.map(([k, label], i) => (
          <li key={k} className={`card${state[k] ? " done" : ""}${i === current ? " current" : ""}`}>
            {/* keyed on the current step, so the next one opens by itself when one is done */}
            <details key={current} open={i === current}>
              <summary>
                <Check done={state[k]} />
                <span className="step-title">{i + 1}. {label}</span>
                {state[k] && <span className="muted">Done</span>}
              </summary>
              <div className="step-body">{body[k]}</div>
            </details>
          </li>
        ))}
      </ol>

      <section className="card">
        <h2>Optional</h2>
        <ul className="optional">
          {OPTIONAL.map(([k, label, text]) => (
            <li key={k}><Check done={!!state[k]} /><div><strong>{label}</strong><p className="muted">{text}</p></div></li>
          ))}
        </ul>
      </section>

      <div className="toolbar">
        {done === STEPS.length && <a className="button" href="#/">Open your library</a>}
        <span className="spacer" />
        {state.dismissed
          ? <span className="muted">Hidden from the Dashboard. It stays here, under Settings.</span>
          : <button className="link" onClick={() => dismiss(true).then(() => (location.hash = "#/dashboard"))}>Hide this guide</button>}
      </div>
    </div>
  );
}
