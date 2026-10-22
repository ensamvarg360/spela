import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, type Console, type Root } from "./api";
import { FindConsole } from "./Consoles";
import { STATE_LABEL, STATE_TONE } from "./Installed";
import { useScan } from "./Library";
import Problem from "./Problem";
import { Folders, NothingFound } from "./Settings";
import { Badge, Bar, Collapsible, Field, Icon, Notice, PageHead, Panel, Spinner } from "./ui";

/** GET /setup: each step worked out from what's already set up (backend/app/main.py, setup_status). */
export interface SetupState {
  dismissed: boolean;
  folder: boolean;
  scan: boolean;
  console: boolean;
  helper: boolean;
  console_app: boolean;
  wake: boolean;
  dlc_folder: boolean;
  viewer: boolean;
}

const STEPS = [
  ["folder", "Add your game folder"],
  ["scan", "Scan it"],
  ["console", "Add your PS5"],
  ["helper", "Start the helper"],
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
  return <span className={done ? "sp-stepcheck sp-stepcheck--done" : "sp-stepcheck"} aria-hidden>{done && <Icon name="check" />}</span>;
}

/** The Dashboard's reminder until the required steps are done or it's hidden. */
export function SetupCard() {
  const [state, load] = useSetup();
  if (!state || state.dismissed) return null;
  const done = STEPS.filter(([k]) => state[k]).length;
  if (done === STEPS.length) return null;
  return (
    <Panel title="Finish setting up Spela" badge={<Badge>{done} of {STEPS.length} done</Badge>}
      actions={<button type="button" className="sp-btn sp-btn--ghost" onClick={() => dismiss(true).then(load)}>Hide</button>}
      foot={<>
        <span className="sp-panel__note">Spela remembers where you got to.</span>
        <a className="sp-btn sp-btn--primary" href="#/setup">Continue setup</a>
      </>}>
      <ol className="sp-steps">
        {STEPS.map(([k, label]) => <li key={k} className={state[k] ? "sp-muted" : ""}><Check done={state[k]} />{label}</li>)}
      </ol>
    </Panel>
  );
}

/** Step 3: found on the network, or typed in by name and IP, nothing else. Settings → Consoles has the rest. */
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
    <div className="sp-addconsole">
      <FindConsole onAdded={onAdded} />
      <form onSubmit={add}>
        <h3>I know its IP address</h3>
        <div className="sp-formgrid">
          <Field label="Name"><input className="sp-input" required maxLength={100} placeholder="Living Room PS5" value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="IP address"><input className="sp-input sp-input--mono" required inputMode="decimal" placeholder="192.168.1.50" value={ip} onChange={(e) => setIp(e.target.value)} /></Field>
        </div>
        <p className="sp-hint">On the PS5: Settings → Network → Connection Status → View Connection Status, under IPv4 address.
          It can change when the router restarts; most routers let you reserve one for the PS5.</p>
        <Problem error={error} />
        <div className="sp-btngroup"><button type="submit" className="sp-btn sp-btn--primary">Add console</button></div>
      </form>
    </div>
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
      <ul className="sp-list sp-boxed">
        {consoles.map((c) => (
          <li key={c.id}>
            <div className="sp-listrow">
              <div className="sp-listrow__main">
                <div className="sp-listrow__title">{c.name}<Badge tone={STATE_TONE[c.state]} dot>{STATE_LABEL[c.state]}</Badge></div>
                <div className="sp-listrow__meta">{c.state === "online" ? "The helper is running." : c.status_message || "Checking…"}</div>
              </div>
              {c.state !== "online" && (
                <div className="sp-listrow__aside">
                  <button type="button" className="sp-btn sp-btn--sm sp-btn--ghost" disabled={!!busy} onClick={() => act(c, "test-connection")}>{busy === c.id ? "Checking…" : "Check now"}</button>
                  <button type="button" className="sp-btn sp-btn--sm" disabled={!!busy || c.state === "standby"} title={c.state === "standby" ? "Wake the console first." : undefined}
                    onClick={() => act(c, "helper")}>Send helper now</button>
                </div>
              )}
            </div>
            {c.state !== "online" && c.status_fix && <div className="sp-listrow__more"><Notice tone="warning" title="What to do" text={c.status_fix} /></div>}
          </li>
        ))}
      </ul>
      <p className="sp-hint">Spela checks every 15 seconds; this page updates by itself.</p>
      <Problem error={error?.e} retry={error?.retry} />
    </>
  );
}

const NEEDS = [
  <>A PS5 that runs homebrew, with an <strong>ELF loader</strong> on port 9021 (most jailbreaks start one).</>,
  <><a className="sp-link" href="https://github.com/drakmor/ShadowMountPlus" target="_blank" rel="noreferrer">ShadowMountPlus</a> on the PS5. It's what puts installed games on the home screen.</>,
  <>This PC and the PS5 on the <strong>same home network</strong>.</>,
  <>Your <strong>own backups</strong> of games you own, in a folder or on a drive of this PC. Spela never downloads games.</>,
];

const OPTIONAL: [keyof SetupState, string, ReactNode][] = [
  ["console_app", "PS5 app", <>Browse and install from the couch with the controller: Install PS5 app in <a className="sp-link" href="#/settings">Settings → Consoles</a>.</>],
  ["wake", "Wake from rest mode", <>Save the PS5's Remote Play key under <a className="sp-link" href="#/settings">Settings → Consoles → Edit</a> to wake it from here.</>],
  ["dlc_folder", "DLC folder", <>If you keep DLC in its own folder, add it in <a className="sp-link" href="#/settings">Settings</a>. DLC inside game folders is found anyway.</>],
  ["viewer", "View-only password", <>Let a phone or a guest browse without being able to change anything. In <a className="sp-link" href="#/settings">Settings</a>.</>],
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

  if (!state) return <p className="sp-muted">Loading…</p>;
  const current = STEPS.findIndex(([k]) => !state[k]);
  const done = STEPS.filter(([k]) => state[k]).length;
  const startScan = () => (setScanError(""), api("/library/scan", { method: "POST" })
    .then(() => (setFinished(false), setScan((s) => ({ ...s!, running: true }))), setScanError));
  const reload = () => (loadState(), loadConsoles());

  const body: Record<(typeof STEPS)[number][0], ReactNode> = {
    folder: <>
      <p>Pick the folder that holds your PS5 backups, or a whole drive like <code className="sp-code">G:\</code>. Spela only ever reads it, subfolders included.</p>
      <div className="sp-boxed"><Folders kind="game" roots={roots} act={(p) => p.then(() => (loadRoots(), loadState()))} /></div>
    </>,
    scan: <>
      <p>Spela looks through the folder for game folders, disc images, packages and archives, and gets their art from PlayStation Store.</p>
      <div className="sp-row">
        <button type="button" className="sp-btn sp-btn--primary" onClick={startScan} disabled={scan?.running || !state.folder}>
          {scan?.running ? <><Spinner />Scanning…</> : <><Icon name="refresh" />Scan now</>}
        </button>
        <span className="sp-hint" role="status">{!state.folder ? "Add a folder first."
          : scan?.running ? "A large drive can take a few minutes. You can carry on with the next steps meanwhile."
          : finished && scan?.report ? `Done: ${scan.report.added.length} added.` : null}</span>
      </div>
      {finished && !scan?.running && scan?.report && !state.scan && <NothingFound report={scan.report} />}
      <Problem error={scanError} />
    </>,
    console: consoles.length
      ? <p>Added: {consoles.map((c) => `${c.name} (${c.ip_address})`).join(", ")}. More in <a className="sp-link" href="#/settings">Settings → Consoles</a>.</p>
      : <AddConsole onAdded={reload} />,
    helper: consoles.length ? <HelperStep consoles={consoles} reload={reload} /> : <p className="sp-muted">Add your PS5 first.</p>,
  };

  return (
    <>
      <PageHead title="Get started"
        desc={done === STEPS.length ? "You're all set. Open a game in your library and click Install." : `${done} of ${STEPS.length} done. Spela remembers where you got to.`}
        actions={done === STEPS.length && <a className="sp-btn sp-btn--primary sp-btn--lg" href="#/">Open your library</a>} />
      <Bar value={done / STEPS.length} label="Setup progress" />

      <Collapsible title="Before you start" meta="What you need" open={done === 0}>
        <ul className="sp-bullets">{NEEDS.map((n, i) => <li key={i}>{n}</li>)}</ul>
      </Collapsible>

      <div className="sp-stack">
        {STEPS.map(([k, label], i) => (
          // keyed on the current step, so the next one opens by itself when one is done
          <Collapsible key={`${k}-${current}`} open={i === current} meta={state[k] ? "Done" : undefined}
            title={<><Check done={state[k]} />{i + 1}. {label}</>}>
            {body[k]}
          </Collapsible>
        ))}
      </div>

      <Panel title="Optional" desc="Worth doing once the steps above are done." flush>
        <ul className="sp-list">
          {OPTIONAL.map(([k, label, text]) => (
            <li key={k} className="sp-listrow">
              <div className="sp-listrow__lead"><Check done={!!state[k]} /></div>
              <div className="sp-listrow__main"><div className="sp-listrow__title">{label}</div><div className="sp-listrow__meta"><span>{text}</span></div></div>
            </li>
          ))}
        </ul>
      </Panel>

      <div className="sp-row">
        {state.dismissed
          ? <span className="sp-hint sp-push">Hidden from the Dashboard. It stays here, under Settings.</span>
          : <button type="button" className="sp-btn sp-btn--ghost sp-push" onClick={() => dismiss(true).then(() => (location.hash = "#/dashboard"))}>Hide this guide</button>}
      </div>
    </>
  );
}
