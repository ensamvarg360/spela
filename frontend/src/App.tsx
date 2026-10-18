import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, appVersion, session } from "./api";
import Library, { NO_VIEW, type View } from "./Library";
import Cart, { useCart } from "./Cart";
import Dashboard from "./Dashboard";
import GameDetail from "./GameDetail";
import Installed from "./Installed";
import Settings from "./Settings";

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
  const search = (q: string) => {
    if (dlc) return setDlcView({ ...dlcView, q });
    setView({ ...view, q });
    if (route !== "/") location.hash = "#/";
  };

  return (
    <>
      <header className="topbar">
        <a href="#/" className="brand">
          <span className="mark" aria-hidden>▶</span>
          <span>PS5 Local Storefront{version && <small>v{version}</small>}</span>
        </a>
        <nav>
          <a href="#/dashboard" aria-current={route === "/dashboard" ? "page" : undefined}>Dashboard</a>
          <a href="#/" aria-current={route === "/" || !!gameId ? "page" : undefined}>Library</a>
          <a href="#/dlc" aria-current={dlc ? "page" : undefined}>DLC</a>
          <a href="#/installed" aria-current={route === "/installed" ? "page" : undefined}>Installed</a>
          {!session.readOnly && <a href="#/cart" aria-current={route === "/cart" ? "page" : undefined}>Cart{inCart.length > 0 && <span className="count-pill">{inCart.length}</span>}</a>}
          {!session.readOnly && <a href="#/settings" aria-current={route === "/settings" ? "page" : undefined}>Settings</a>}
        </nav>
        {session.readOnly && <span className="badge installed" title="Logged in with the view-only password: you can look, not change anything.">View only</span>}
        <input className="search" type="search" placeholder={dlc ? "Search your DLC" : "Search your library"} aria-label="Search title or title ID"
          value={dlc ? dlcView.q : view.q} onChange={(e) => search(e.target.value)} />
        <button className="link" onClick={logout}>Log out</button>
      </header>
      <main>
        {gameId ? <GameDetail id={gameId} /> : route === "/dashboard" ? <Dashboard /> : route === "/settings" ? (session.readOnly
          ? <p className="muted">Settings need the admin password. <button className="link" onClick={logout}>Log out</button> and log in with it.</p> : <Settings />) : route === "/installed" ? <Installed /> : route === "/cart" && !session.readOnly ? <Cart />
          : dlc ? <Library key="dlc" kind="dlc" view={dlcView} setView={setDlcView} /> : <Library key="game" kind="game" view={view} setView={setView} />}
      </main>
    </>
  );
}

function Login({ setup, onLogin }: { setup: boolean; onLogin: (u: string) => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (setup && password !== confirm) return setError("The passwords don't match.");
    try {
      onLogin((await api<{ user: string }>(setup ? "/auth/setup" : "/auth/login", { json: { password } })).user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed.");
    }
  }

  return (
    <form className="login card" onSubmit={submit}>
      <h1>PS5 Local Storefront</h1>
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
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit">{setup ? "Set password" : "Log in"}</button>
    </form>
  );
}
