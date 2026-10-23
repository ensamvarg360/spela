import { useEffect, useRef, useState, type FormEvent } from "react";
import Problem from "./Problem";
import { api, ApiError, appVersion, session } from "./api";
import Library, { NO_VIEW, type View } from "./Library";
import Cart, { useCart } from "./Cart";
import Dashboard from "./Dashboard";
import GameDetail, { ConsoleGameDetail } from "./GameDetail";
import Installed from "./Installed";
import Settings from "./Settings";
import Setup from "./Setup";
import Toasts, { Bell, toast } from "./Toast";
import { Field, Icon, Notice, Panel } from "./ui";
import wordmark from "./wordmark.png";

// ponytail: hash routing, seven routes. Swap in react-router when nested routes show up.
function useHash() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return hash.replace(/^#/, "") || "/";
}

export default function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined);
  const [needsSetup, setNeedsSetup] = useState(false);
  const route = useHash();
  const from = useRef<string | null>(null);
  const inCart = useCart();
  const [version, setVersion] = useState("");
  const [view, setView] = useState<View>(NO_VIEW); // here, so filters survive opening a game and coming back
  const [dlcView, setDlcView] = useState<View>(NO_VIEW);
  const [newer, setNewer] = useState<Newer | null>(null);
  useEffect(() => { appVersion.then(setVersion); }, []);

  useEffect(() => {
    api<{ user: string }>("/auth/me").then((r) => setUser(r.user), (e) => {
      setNeedsSetup(e instanceof ApiError && e.code === "setup_required");
      setUser(null);
    });
  }, []);
  useEffect(() => {
    if (user) api<{ latest: Newer | null; installable: boolean }>("/update")
      .then((r) => setNewer(r.latest && { ...r.latest, installable: r.installable }), () => {});
  }, [user]);

  if (user === undefined) return null;
  if (!user) return <Login setup={needsSetup} onLogin={setUser} />;
  session.readOnly = user === "viewer";

  const gameId = route.match(/^\/game\/([\w-]+)$/)?.[1];
  const onConsole = route.match(/^\/installed\/([\w-]+)\/([\w-]+)$/); // a console's game that isn't in the library
  const logout = () => api("/auth/logout", { method: "POST" }).then(() => setUser(null));

  const dlc = route === "/dlc";
  if (!gameId) from.current = route; // where a game's back link returns to
  const narrow = ["/settings", "/setup", "/cart"].includes(route); // settings and forms

  return (
    <>
      <nav className="sp-topnav" aria-label="Main">
        <a href="#/" className="sp-topnav__brand">
          <img src={wordmark} alt="Spela" />
          {version && <span className="sp-topnav__version">v{version}</span>}
        </a>
        <div className="sp-topnav__links">
          <a className="sp-topnav__link" href="#/dashboard" aria-current={route === "/dashboard" ? "page" : undefined}>Dashboard</a>
          <a className="sp-topnav__link" href="#/" aria-current={route === "/" || !!gameId ? "page" : undefined}>Library</a>
          <a className="sp-topnav__link" href="#/dlc" aria-current={dlc ? "page" : undefined}>DLC</a>
          <a className="sp-topnav__link" href="#/installed" aria-current={route === "/installed" || !!onConsole ? "page" : undefined}>Installed</a>
        </div>
        <div className="sp-topnav__end">
          {newer && !session.readOnly && <UpdateBadge newer={newer} />}
          {session.readOnly && <span className="sp-badge" title="Logged in with the view-only password: you can look, not change anything.">View only</span>}
          <Bell />
          <a href={`https://github.com/ensamvarg360/spela/issues/new?template=bug_report.yml&version=${encodeURIComponent(version)}`} target="_blank" rel="noreferrer"
            className="sp-btn sp-btn--ghost sp-btn--icon" title="Report a bug" aria-label="Report a bug (opens GitHub)"><Icon name="bug" /></a>
          {!session.readOnly && (
            <a href="#/cart" className={inCart.length ? "sp-btn sp-btn--ghost" : "sp-btn sp-btn--ghost sp-btn--icon"} title="Cart"
              aria-label={`Cart${inCart.length ? `, ${inCart.length} items` : ""}`} aria-current={route === "/cart" ? "page" : undefined}>
              <Icon name="cart" />
              {inCart.length > 0 && <span className="sp-badge sp-num">{inCart.length}</span>}
            </a>
          )}
          {!session.readOnly && (
            <a href="#/settings" className="sp-btn sp-btn--ghost sp-btn--icon" title="Settings" aria-label="Settings" aria-current={route === "/settings" ? "page" : undefined}>
              <Icon name="settings" />
            </a>
          )}
          <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon" onClick={logout} title="Sign out" aria-label="Sign out"><Icon name="logout" /></button>
        </div>
      </nav>
      <main className={narrow ? "sp-page sp-page--narrow" : "sp-page"}>
        {gameId ? <GameDetail id={gameId} from={from.current} /> : onConsole ? <ConsoleGameDetail key={route} consoleId={onConsole[1]} id={onConsole[2]} /> : route === "/dashboard" ? <Dashboard /> : route === "/settings" ? (session.readOnly
          ? <Notice title="Settings need the admin password" text="Sign out, then sign in with the admin password." action={<button type="button" className="sp-btn sp-btn--sm" onClick={logout}>Sign out</button>} />
          : <Settings />) : route === "/installed" ? <Installed /> : route === "/cart" && !session.readOnly ? <Cart />
          : route === "/setup" && !session.readOnly ? <Setup />
          : dlc ? <Library key="dlc" kind="dlc" view={dlcView} setView={setDlcView} /> : <Library key="game" kind="game" view={view} setView={setView} />}
      </main>
      <Toasts />
    </>
  );
}

type Newer = { version: string; url: string; installable?: boolean };

/** The newer version on GitHub: installed in place when Spela can do that itself, else a link to download it. */
function UpdateBadge({ newer }: { newer: Newer }) {
  const [busy, setBusy] = useState("");
  const link = <a className="sp-badge sp-badge--info" href={newer.url} target="_blank" rel="noreferrer" title="Download the new version from GitHub">v{newer.version} available</a>;
  if (!newer.installable) return link;

  async function install() {
    if (!confirm(`Update Spela to v${newer.version}?\n\nIt downloads the update, then restarts. Your library and settings are kept.`)) return;
    setBusy("Downloading…");
    try {
      await api("/update/install", { method: "POST" });
    } catch (e) {
      setBusy("");
      return toast(e instanceof ApiError ? `${e.message} ${e.fix}` : "Couldn't update.", true, ["What's new", newer.url]);
    }
    setBusy("Restarting…");
    // In Spela's own window the window restarts too; a browser elsewhere waits for the new version and reloads.
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const v = await fetch("/api/v1/health").then((r) => r.json()).then((h) => h.version, () => "");
      if (v === newer.version) break;
    }
    location.reload(); // after a minute anyway: the old version is back if the swap failed, and still offers the update
  }
  return (
    <button className="sp-badge sp-badge--info" onClick={install} disabled={!!busy} title={`Install v${newer.version} and restart Spela`}>
      {busy || `Update to v${newer.version}`}
    </button>
  );
}

function Login({ setup, onLogin }: { setup: boolean; onLogin: (u: string) => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [code, setCode] = useState("");
  // Setting it up from another device may need the code Spela writes to its log; the field shows once Spela asks for it.
  const [askCode, setAskCode] = useState(false);
  const [error, setError] = useState<unknown>("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (setup && password !== confirm) return setError("The passwords don't match.");
    try {
      const { user } = await api<{ user: string }>(setup ? "/auth/setup" : "/auth/login", { json: setup ? { password, code } : { password } });
      if (setup) location.hash = "#/setup"; // first run: straight to the checklist
      onLogin(user);
    } catch (err) {
      if (err instanceof ApiError && err.code === "setup_code") setAskCode(true);
      setError(err instanceof ApiError ? err : "Login failed.");
    }
  }

  return (
    <form className="sp-login" onSubmit={submit}>
      <Panel title={<img src={wordmark} alt="Spela" />}
        desc={setup ? "Welcome! Choose an admin password. You'll use it to sign in, from other devices too." : undefined}
        foot={<button type="submit" className="sp-btn sp-btn--primary">{setup ? "Set password" : "Sign in"}</button>}>
        {askCode && (
          <Field label="Setup code" hint="It's in Spela's log. On Docker: docker logs spela.">
            <input className="sp-input" autoFocus required autoComplete="off" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} />
          </Field>
        )}
        <Field label={setup ? "New admin password" : "Admin password"} hint={setup ? "At least 8 characters." : undefined}>
          <input className="sp-input" type="password" autoFocus required minLength={setup ? 8 : undefined} autoComplete={setup ? "new-password" : "current-password"}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {setup && (
          <Field label="Confirm password">
            <input className="sp-input" type="password" required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </Field>
        )}
        <Problem error={error} />
      </Panel>
    </form>
  );
}
