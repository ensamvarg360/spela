import { useEffect, useState, useSyncExternalStore } from "react";
import { api, formatBytes, formatLabel, needsOnline, useConsoles, type Drive, type Game, type Job } from "./api";
import { Empty, Icon, Notice, PageHead, Panel } from "./ui";

// The cart: game ids kept in this browser, so it survives reloads and moving between pages.
// ponytail: per browser, not shared with the PS5 app's cart; move it to the PC's database if both should see one cart.
const KEY = "cart";
const listeners = new Set<() => void>();
let ids: string[] = (() => {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "[]");
  } catch {
    return [];
  }
})();

function save(next: string[]) {
  ids = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch { /* private window: the cart lasts until the page closes */ }
  listeners.forEach((f) => f());
}

export const cart = {
  add: (add: string[]) => save([...ids, ...add.filter((id) => !ids.includes(id))]),
  remove: (drop: string[]) => save(ids.filter((id) => !drop.includes(id))),
};

export const useCart = () => useSyncExternalStore((f) => (listeners.add(f), () => listeners.delete(f)), () => ids);

/** The warnings a batch needs before it's queued; false if the user backed out. */
export function confirmBatch(games: Game[]): boolean {
  if (games.some((g) => g.format === "archive") && !confirm("Archives are extracted to the private staging folder on this PC before they're installed, one at a time. Make sure the PC has room for the biggest unpacked game.")) return false;
  if (games.some((g) => g.format === "ps5_pkg" || g.format === "ps4_pkg") && !confirm("Package files are submitted to the PS5 package installer. Check their title IDs and versions before you continue.")) return false;
  if (games.some((g) => g.kind === "dlc" && g.format === "unlocker") && !confirm("Each unlocker's files are copied into its installed game's folder and replace files there with the same names. Close the games on the PS5 first.")) return false;
  return true;
}

/** Queues each game on one console, in order, and says which couldn't be queued and why. DLC always goes onto its game's drive. */
export async function queueAll(cid: string, games: Game[], driveId: string | null) {
  const failed: [Game, string][] = [];
  for (const g of games) {
    try {
      await api<Job>(`/consoles/${cid}/install`, { json: { game_id: g.id, drive_id: g.kind === "game" ? driveId : null } });
    } catch (e) {
      failed.push([g, (e as Error).message]);
    }
  }
  return failed;
}

/** What's in the cart, how big it is, and one button that queues it all. */
export default function Cart() {
  const inCart = useCart();
  const [games, setGames] = useState<Game[] | null>(null);
  const { consoles, cid, setCid, console: target } = useConsoles();
  const [drives, setDrives] = useState<Drive[]>([]);
  const [did, setDid] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ queued: number; failed: [Game, string][] } | null>(null);

  useEffect(() => { api<Game[]>("/games").then(setGames, () => setGames([])); }, []);
  useEffect(() => {
    setDid("");
    if (cid) api<Drive[]>(`/consoles/${cid}/drives`).then((ds) => setDrives(ds.filter((d) => d.is_install_target)), () => setDrives([]));
  }, [cid]);

  if (!games) return <p className="sp-muted">Loading…</p>;
  const byId = new Map(games.map((g) => [g.id, g]));
  const items = inCart.map((id) => byId.get(id)).filter((g): g is Game => !!g); // a game since deleted from the library drops out
  const gameBytes = items.filter((g) => g.kind === "game").reduce((n, g) => n + g.size_bytes, 0);
  const total = items.reduce((n, g) => n + g.size_bytes, 0);
  const drive = drives.find((d) => d.id === did);
  const tooBig = drive && drive.total_bytes > 0 && gameBytes > drive.free_bytes;

  async function installAll() {
    if (!confirmBatch(items)) return;
    setBusy(true);
    const failed = await queueAll(cid, items, did || null);
    setBusy(false);
    setResult({ queued: items.length - failed.length, failed });
    cart.remove(items.filter((g) => !failed.some(([f]) => f.id === g.id)).map((g) => g.id)); // what failed stays, to fix and retry
  }

  return (
    <>
      <PageHead title="Cart" desc="Add the games you want, then install them all at once. They're queued in this order and copied one after another." />
      {result && result.queued > 0 && (
        <Notice tone="success" title={`Queued ${result.queued}`}
          text={<>They install one after another; follow them on the <a className="sp-link" href="#/installed">Installed</a> page.</>} />
      )}
      {result && result.failed.length > 0 && (
        <Notice tone="danger" title={`Couldn't queue ${result.failed.length}`}
          text={<ul className="sp-bullets">{result.failed.map(([g, why]) => <li key={g.id}><strong>{g.title}</strong>: {why}</li>)}</ul>} />
      )}
      <Panel title="In your cart" badge={items.length > 0 && <span className="sp-badge sp-num">{items.length}</span>} flush
        actions={items.length > 0 && <button type="button" className="sp-btn sp-btn--ghost" disabled={busy} onClick={() => cart.remove(items.map((g) => g.id))}>Empty cart</button>}
        foot={items.length > 0 && <>
          <span className="sp-panel__note sp-num">{items.length} {items.length === 1 ? "item" : "items"} · {formatBytes(total)}</span>
          {!consoles.length ? <a className="sp-btn sp-btn--primary" href="#/settings">Add a PS5 to install</a> : (
            <div className="sp-btngroup">
              {consoles.length > 1 && (
                <select className="sp-select sp-console-pick" aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
                  {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              )}
              {gameBytes > 0 && (
                <select className="sp-select sp-console-pick" aria-label="Drive" value={did} onChange={(e) => setDid(e.target.value)}>
                  <option value="">Drive with the most free space, for each game</option>
                  {drives.map((d) => <option key={d.id} value={d.id}>{d.label}{d.total_bytes ? ` (${formatBytes(d.free_bytes)} free)` : ""}</option>)}
                </select>
              )}
              <button type="button" className="sp-btn sp-btn--primary" {...needsOnline(target)} disabled={busy || !!needsOnline(target).disabled} onClick={installAll}>
                {busy ? "Queuing…" : `Install all ${items.length}`}
              </button>
            </div>
          )}
        </>}>
        {!items.length ? (
          <Empty icon="cart" title="Your cart is empty" text="In the Library, press Select, pick some games, then Add to cart. Or use Add to cart on a game's page.">
            <a className="sp-btn" href="#/">Open your library</a>
          </Empty>
        ) : <>
          {tooBig && <div className="sp-listrow__more sp-listrow__more--top">
            <Notice tone="warning" title={`${drive.label} has ${formatBytes(drive.free_bytes)} free`}
              text={`These games need ${formatBytes(gameBytes)}. The ones that don't fit will fail when their turn comes.`} />
          </div>}
          <div className="sp-tablewrap">
            <table className="sp-table">
              <thead><tr><th>Game</th><th>Format</th><th className="sp-num">Size</th><th><span className="sp-visually-hidden">Remove</span></th></tr></thead>
              <tbody>
                {items.map((g) => (
                  <tr key={g.id}>
                    <td>
                      <a className="sp-link" href={`#/game/${g.id}`}>{g.title}</a>
                      {g.kind === "dlc" && <span className="sp-muted"> · DLC</span>}
                      {g.title_id && <> <span className="sp-code">{g.title_id}</span></>}
                    </td>
                    <td className="sp-nowrap">{formatLabel(g)}</td>
                    <td className="sp-nowrap sp-num">{formatBytes(g.size_bytes)}</td>
                    <td className="sp-nowrap">
                      <button type="button" className="sp-btn sp-btn--ghost sp-btn--danger sp-btn--icon sp-btn--sm" onClick={() => cart.remove([g.id])}
                        aria-label={`Remove ${g.title} from the cart`} title="Remove from cart"><Icon name="trash" /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>}
      </Panel>
    </>
  );
}
