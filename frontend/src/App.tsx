import { useEffect, useRef, useState, type FormEvent } from "react";
import Problem from "./Problem";
import { api, ApiError, appVersion, session } from "./api";
import Library, { NO_VIEW, type View } from "./Library";
import Cart, { useCart } from "./Cart";
import Dashboard from "./Dashboard";
import GameDetail from "./GameDetail";
import Installed from "./Installed";
import Settings from "./Settings";
import Setup from "./Setup";
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
  useEffect(() => { appVersion.then(setVersion); }, []);

  useEffect(() => {
    api<{ user: string }>("/auth/me").then((r) => setUser(r.user), (e) => {
      setNeedsSetup(e instanceof ApiError && e.code === "setup_required");
      setUser(null);
    });
  }, []);

  if (user === undefined) return null;
  if (!user) return <Login setup={needsSetup} onLogin={setUser} />;
  session.readOnly = user === "viewer";

  const gameId = route.match(/^\/game\/([\w-]+)$/)?.[1];
  const logout = () => api("/auth/logout", { method: "POST" }).then(() => setUser(null));

  const dlc = route === "/dlc";
  if (!gameId) from.current = route; // where a game's back link returns to

  return (
    <>
      <header className="topbar">
        <a href="#/" className="brand">
          <img className="wordmark" src={wordmark} alt="Spela" />
          {version && <small>v{version}</small>}
        </a>
        <nav>
          <a href="#/dashboard" aria-current={route === "/dashboard" ? "page" : undefined}>Dashboard</a>
          <a href="#/" aria-current={route === "/" || !!gameId ? "page" : undefined}>Library</a>
          <a href="#/dlc" aria-current={dlc ? "page" : undefined}>DLC</a>
          <a href="#/installed" aria-current={route === "/installed" ? "page" : undefined}>Installed</a>
        </nav>
        <div className="topbar-end">
          {session.readOnly && <span className="badge installed" title="Logged in with the view-only password: you can look, not change anything.">View only</span>}
          {!session.readOnly && (
            <a href="#/cart" className="icon-btn" title="Cart" aria-label={`Cart${inCart.length ? `, ${inCart.length} items` : ""}`} aria-current={route === "/cart" ? "page" : undefined}>
              <svg viewBox="0 0 24 24" aria-hidden><circle cx="9" cy="20" r="1.5" /><circle cx="18" cy="20" r="1.5" /><path d="M2 3h3l2.7 12.4a1 1 0 0 0 1 .8h9.1a1 1 0 0 0 1-.8L20.5 7H6" /></svg>
              {inCart.length > 0 && <span className="count-pill">{inCart.length}</span>}
            </a>
          )}
          {!session.readOnly && (
            <a href="#/settings" className="icon-btn" title="Settings" aria-label="Settings" aria-current={route === "/settings" ? "page" : undefined}>
              <svg viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="3" style={{ fill: "none", stroke: "currentColor" }} /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></svg>
            </a>
          )}
          <button className="icon-btn" onClick={logout} title="Log out" aria-label="Log out">
            <svg viewBox="0 0 24 24" aria-hidden><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" /></svg>
          </button>
        </div>
      </header>
      <main>
        {gameId ? <GameDetail id={gameId} from={from.current} /> : route === "/dashboard" ? <Dashboard /> : route === "/settings" ? (session.readOnly
          ? <p className="muted">Settings need the admin password. <button className="link" onClick={logout}>Log out</button> and log in with it.</p> : <Settings />) : route === "/installed" ? <Installed /> : route === "/cart" && !session.readOnly ? <Cart />
          : route === "/setup" && !session.readOnly ? <Setup />
          : dlc ? <Library key="dlc" kind="dlc" view={dlcView} setView={setDlcView} /> : <Library key="game" kind="game" view={view} setView={setView} />}
      </main>
    </>
  );
}

function Login({ setup, onLogin }: { setup: boolean; onLogin: (u: string) => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (setup && password !== confirm) return setError("The passwords don't match.");
    try {
      const { user } = await api<{ user: string }>(setup ? "/auth/setup" : "/auth/login", { json: { password } });
      if (setup) location.hash = "#/setup"; // first run: straight to the checklist
      onLogin(user);
    } catch (err) {
      setError(err instanceof ApiError ? err : "Login failed.");
    }
  }

  return (
    <form className="login card" onSubmit={submit}>
      <h1><img className="wordmark" src={wordmark} alt="Spela" /></h1>
      {setup && <p className="muted">Welcome! Choose an admin password. You'll use it to log in, including from other devices.</p>}
      <label>
        {setup ? "New admin password" : "Admin password"}
        <input type="password" autoFocus required minLength={setup ? 8 : undefined} autoComplete={setup ? "new-password" : "current-password"}
          value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>
      {setup && (
        <label>
          Confirm password
          <input type="password" required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </label>
      )}
      <Problem error={error} />
      <button type="submit">{setup ? "Set password" : "Log in"}</button>
    </form>
  );
}
