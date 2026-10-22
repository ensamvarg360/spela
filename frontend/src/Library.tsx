import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api, appVersion, coverUrl, formatBytes, formatLabel, groupCopies, needsOnline, session, STATUS_LABEL, useConsoles, type Console, type Game, type Kind, type ScanStatus, type StoreInfo } from "./api";
import { cart, confirmBatch, queueAll } from "./Cart";
import { STATE_LABEL, STATE_TONE } from "./Installed";
import Problem from "./Problem";
import { toastError } from "./Toast";
import { Empty, Icon, Notice, PageHead, type IconName, type Tone } from "./ui";

export type Sort = "title" | "recent" | "rating" | "released" | "size";
/** What the library shows: the header's search, the sidebar's ticked values per filter, and the order. */
export interface View { q: string; picked: Record<string, string[]>; sort: Sort }
export const NO_VIEW: View = { q: "", picked: {}, sort: "title" };

const year = (g: Game) => Number((g.store?.release_date ?? g.release_date ?? "").slice(0, 4)) || null;

/** The sidebar's filters, each listing the values its games have. Ticked values of one filter are OR'ed, filters AND'ed. */
const FILTERS: { key: string; label: string; values: (g: Game) => (string | null | undefined)[]; order?: string[] }[] = [
  { key: "collection", label: "Collection", order: ["New", "Favorites", "Installed", "Not installed"],
    values: (g) => [g.new ? "New" : null, g.favorite ? "Favorites" : null, g.installed ? "Installed" : "Not installed"] },
  { key: "genre", label: "Genre", values: (g) => (g.store?.genres.length ? g.store.genres : [g.genre]) },
  { key: "year", label: "Released", order: ["2020 – now", "2015 – 2019", "2010 – 2014", "Before 2010"], values: (g) => {
    const y = year(g);
    return [y ? (y >= 2020 ? "2020 – now" : y >= 2015 ? "2015 – 2019" : y >= 2010 ? "2010 – 2014" : "Before 2010") : null];
  } },
  { key: "rating", label: "Rating", order: ["4.5 and up", "4.0 and up", "3.0 and up"], values: (g) => {
    const r = g.store?.rating ?? 0;
    return [r >= 4.5 ? "4.5 and up" : null, r >= 4 ? "4.0 and up" : null, r >= 3 ? "3.0 and up" : null];
  } },
  { key: "status", label: "Status", values: (g) => [STATUS_LABEL[g.compatibility_status]] },
  { key: "format", label: "Format", values: (g) => [formatLabel(g)] },
  { key: "region", label: "Region", values: (g) => [g.region] },
];

const COMPARE: Record<Sort, (a: Game, b: Game) => number> = {
  title: (a, b) => a.title.localeCompare(b.title),
  recent: (a, b) => b.created_at.localeCompare(a.created_at),
  rating: (a, b) => (b.store?.rating ?? 0) - (a.store?.rating ?? 0),
  released: (a, b) => (year(b) ?? 0) - (year(a) ?? 0),
  size: (a, b) => b.size_bytes - a.size_bytes,
};

const matches = (g: Game, picked: View["picked"], skip?: string) =>
  FILTERS.every((f) => f.key === skip || !picked[f.key]?.length || f.values(g).some((v) => v && picked[f.key].includes(v)));

/** The library scan's status, polled while one runs. `onDone` fires when a scan this page saw running ends. */
export function useScan(onDone?: (s: ScanStatus) => void) {
  const [scan, setScan] = useState<ScanStatus | null>(null);
  const done = useRef(onDone);
  done.current = onDone;
  useEffect(() => { api<ScanStatus>("/library/scan-status").then(setScan, () => {}); }, []);
  useEffect(() => {
    if (!scan?.running) return;
    const t = setTimeout(() => api<ScanStatus>("/library/scan-status").then((s) => {
      setScan(s);
      if (!s.running) done.current?.(s);
    }, () => {}), 1500);
    return () => clearTimeout(t);
  }, [scan]);
  return [scan, setScan] as const;
}

/** Status dots for the first enabled PS5 and its helper payload, refreshed every 15 s. */
function Connection() {
  const [c, setC] = useState<Console | null | undefined>();
  const [version, setVersion] = useState("");
  useEffect(() => {
    appVersion.then(setVersion);
    const load = () => api<Console[]>("/consoles").then((cs) => setC(cs.find((x) => x.enabled) ?? null), () => setC(undefined));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);
  if (c === null) return <div className="sp-facets__foot"><span><a className="sp-link" href="#/settings">Add a PS5</a> to connect</span></div>;
  if (!c) return null;
  const online = c.state === "online";
  const helper = c.helper_status.helper; // "missing", a version, or absent before the first check
  const [tone, label]: [Tone, string] = !online ? ["", "Unknown"]
    : !helper || helper === "missing" ? ["danger", "Not running"]
    : version && helper !== version ? ["warning", `v${helper}, needs v${version}`]
    : ["success", `v${helper}`];
  const dot = (t: Tone) => (t ? `sp-dot sp-dot--${t}` : "sp-dot");
  return (
    <div className="sp-facets__foot" aria-label="Connection">
      <div className="sp-facets__status" title={c.status_message}><span className={dot(STATE_TONE[c.state])} />{c.name}<span>{STATE_LABEL[c.state]}</span></div>
      <div className="sp-facets__status"><span className={dot(tone)} />Helper<span className="sp-num">{label}</span></div>
    </div>
  );
}

/** Queues every picked game on one console, or puts them in the cart, and says which couldn't be queued and why. */
function InstallBar({ kind, picked, shown, setPicked }: { kind: Kind; picked: Set<string>; shown: Game[]; setPicked: (p: Set<string> | null) => void }) {
  const { consoles, cid, setCid, console: target } = useConsoles();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ queued: number; carted?: number; failed: [string, string][] } | null>(null);

  const games = shown.filter((g) => picked.has(g.id));
  async function install() {
    if (!confirmBatch(games)) return;
    setBusy(true);
    const failed = await queueAll(cid, games, null);
    setBusy(false);
    setResult({ queued: games.length - failed.length, failed: failed.map(([g, why]) => [g.title, why]) });
    if (!failed.length) setPicked(new Set());
  }
  const addToCart = () => {
    cart.add(games.map((g) => g.id));
    setResult({ queued: 0, carted: games.length, failed: [] });
    setPicked(new Set());
  };

  return (
    <div className="sp-panel sp-pickbar" role="region" aria-label="Selection">
      <div className="sp-row">
        <strong className="sp-num">{picked.size} selected</strong>
        <button type="button" className="sp-btn sp-btn--ghost sp-btn--sm" onClick={() => setPicked(new Set(shown.map((g) => g.id)))}>Select all {shown.length}</button>
        {picked.size > 0 && <button type="button" className="sp-btn sp-btn--ghost sp-btn--sm" onClick={() => setPicked(new Set())}>Clear</button>}
        <div className="sp-btngroup sp-push">
          <button type="button" className="sp-btn sp-btn--ghost" onClick={() => setPicked(null)}>Done</button>
          {!consoles.length ? <a className="sp-btn" href="#/settings">Add a PS5 to install</a> : <>
            {consoles.length > 1 && (
              <select className="sp-select sp-console-pick" aria-label="Console" value={cid} onChange={(e) => setCid(e.target.value)}>
                {consoles.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            )}
            <button type="button" className="sp-btn" disabled={busy || !games.length} onClick={addToCart}>Add to cart</button>
            <button type="button" className="sp-btn sp-btn--primary" {...needsOnline(target)} disabled={busy || !games.length || !!needsOnline(target).disabled} onClick={install}>
              {busy ? "Queuing…" : `${kind === "dlc" ? "Add" : "Install"} ${games.length || ""}`.trim()}
            </button>
          </>}
        </div>
      </div>
      {result && <>
        {!!result.carted && <Notice tone="success" title={`Added ${result.carted} to the cart`} action={<a className="sp-btn sp-btn--sm" href="#/cart">Open cart</a>} />}
        {result.queued > 0 && <Notice tone="success" title={`Queued ${result.queued}`}
          text={<>They install one at a time; follow them on the <a className="sp-link" href="#/installed">Installed</a> page.</>} />}
        {result.failed.length > 0 && <Notice tone="danger" title={`Couldn't queue ${result.failed.length}`}
          text={<ul className="sp-bullets">{result.failed.map(([t, why]) => <li key={t}><strong>{t}</strong>: {why}</li>)}</ul>} />}
      </>}
    </div>
  );
}

// card width in px from the size slider; from TILE_LARGE up the large game card shows, below it the poster-only one
const TILE_MIN = 80, TILE_MAX = 190, TILE_LARGE = 140;
// the grids' column gaps in px: .sp-covergrid's --space-4, .sp-gamegrid's --space-5
const gapFor = (px: number) => (px < TILE_LARGE ? 16 : 20);
/** How many cards of at least `px` fit in a row `width` wide; cards then stretch to fill it. */
const colsFor = (width: number, px: number) => Math.max(1, Math.floor((width + gapFor(px)) / (px + gapFor(px))));
/** The width cards get with `n` to a row, so each size step adds or removes one card per row. */
const tileFor = (width: number, n: number) => {
  const px = Math.floor((width + 20) / n - 20);
  return px < TILE_LARGE ? Math.min(TILE_LARGE - 1, Math.floor((width + 16) / n - 16)) : px;
};

/** The grid of games, or of DLC: DLC shares its game's title ID, so its tiles show the game's art. */
export default function Library({ kind, view, setView }: { kind: Kind; view: View; setView: (v: View) => void }) {
  const [games, setGames] = useState<Game[] | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({}); // filters showing all their values
  const [filtersOpen, setFiltersOpen] = useState(false); // phones only: the sidebar sits behind a button
  const [picked, setPicked] = useState<Set<string> | null>(null); // null = not selecting; tiles open their game
  const [tile, setTile] = useState(() => { try { return Number(localStorage.getItem("tileSize")) || TILE_MAX; } catch { return TILE_MAX; } });
  const [grid, setGrid] = useState<HTMLElement | null>(null);
  const [width, setWidth] = useState(0); // the grid's, which decides how many cards fit a row
  useEffect(() => {
    if (!grid) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(grid);
    return () => ro.disconnect();
  }, [grid]);
  const cols = colsFor(width, tile);
  const resize = (px: number) => { px = Math.min(TILE_MAX, Math.max(TILE_MIN, px)); setTile(px); try { localStorage.setItem("tileSize", String(px)); } catch { /* not remembered */ } };
  const { cid, console: target } = useConsoles();
  const [tooNew, setTooNew] = useState<Set<string>>(new Set()); // packages the console's firmware can't run
  useEffect(() => {
    if (cid) api<string[]>(`/consoles/${cid}/firmware-blocked`).then((ids) => setTooNew(new Set(ids)), () => {});
  }, [cid, target?.firmware, games?.length]);

  const load = () => api<Game[]>("/games").then((gs) => setGames(groupCopies(gs.filter((g) => g.kind === kind))), (e) => setError(e.message));
  useEffect(() => { load(); }, [kind]);
  const [scan] = useScan(load); // a scan started in Settings: show it, and its finds once it ends
  // a tile's store lookup, so the sidebar's genres, years and ratings fill in as tiles come into view
  const changed = (id: string, changes: Partial<Game>) => setGames((gs) => gs && gs.map((g) => (g.id === id ? { ...g, ...changes } : g)));

  const searched = useMemo(() => {
    const needle = view.q.trim().toLowerCase();
    return (games ?? []).filter((g) => !needle || g.title.toLowerCase().includes(needle) || g.title_id?.toLowerCase().includes(needle));
  }, [games, view.q]);

  /** Each filter's values, with how many games ticking it would show given the other filters. */
  const facets = useMemo(() => FILTERS.map((f) => {
    const counts = new Map<string, number>();
    for (const g of searched)
      if (matches(g, view.picked, f.key)) for (const v of new Set(f.values(g))) if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
    for (const v of view.picked[f.key] ?? []) if (!counts.has(v)) counts.set(v, 0); // a ticked value stays visible to untick
    const values = [...counts].sort(f.order
      ? (a, b) => f.order!.indexOf(a[0]) - f.order!.indexOf(b[0])
      : (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    return { ...f, values };
  }), [searched, view.picked]);

  const shown = useMemo(() => searched.filter((g) => matches(g, view.picked)).sort(COMPARE[view.sort]), [searched, view.picked, view.sort]);

  const noun = kind === "dlc" ? "DLC" : "games";
  if (error) return <Problem error={error} />;
  if (!games) return <p className="sp-muted">Loading…</p>;
  const scanning = scan?.running && (
    <Notice tone="info" title="Scanning your folders" text={`New ${noun} show up here when it's done.`} />
  );
  const head = (desc?: string) => (
    <PageHead title={kind === "dlc" ? "DLC" : "Library"} desc={desc}
      actions={!picked && !session.readOnly && games.length > 0 && <button type="button" className="sp-btn" onClick={() => setPicked(new Set())}>Select</button>} />
  );
  if (!games.length)
    return <>
      {head()}
      {scanning || <div className="sp-panel">{kind === "dlc" ? (
        <Empty icon="folder" title="No DLC yet" text="Add your DLC folder in Settings, then scan. DLC packages and unlocker folders (with a dlc_emu.ini) are found in game folders too.">
          {!session.readOnly && <a className="sp-btn sp-btn--primary" href="#/settings">Open Settings</a>}
        </Empty>
      ) : (
        <Empty icon="gamepad" title="Your library is empty" text={session.readOnly ? "Nothing has been added yet."
          : "The setup guide takes you through adding your games folder and your PS5, step by step."}>
          {!session.readOnly && <><a className="sp-btn" href="#/settings">Open Settings</a><a className="sp-btn sp-btn--primary" href="#/setup">Open the setup guide</a></>}
        </Empty>
      )}</div>}
    </>;

  const toggle = (key: string, v: string) => {
    const now = view.picked[key] ?? [];
    setView({ ...view, picked: { ...view.picked, [key]: now.includes(v) ? now.filter((x) => x !== v) : [...now, v] } });
  };
  const ticked = Object.values(view.picked).reduce((n, vs) => n + vs.length, 0);
  const clearFilters = () => setView({ ...view, picked: {} });
  const clear = () => setView({ ...view, q: "", picked: {} });

  return (
    <>
      {head(shown.length === games.length ? `${games.length} ${noun}` : `${shown.length} of ${games.length} ${noun}`)}
      {scanning}
      <div className="sp-browse">
        <button type="button" className="sp-btn sp-filters-toggle" aria-expanded={filtersOpen} onClick={() => setFiltersOpen(!filtersOpen)}>
          <Icon name="filter" />Filters{ticked > 0 && <span className="sp-badge sp-num">{ticked}</span>}
        </button>
        <aside className={filtersOpen ? "sp-facets sp-facets--open" : "sp-facets"} aria-label="Filters">
          {ticked > 0 && <div><button type="button" className="sp-btn sp-btn--ghost sp-btn--sm" onClick={clearFilters}><Icon name="x" />Clear filters</button></div>}
          {facets.filter((f) => f.values.length > 1 || view.picked[f.key]?.length).map((f) => (
            <fieldset key={f.key} className="sp-facet">
              <legend className="sp-facet__title">{f.label}</legend>
              {(expanded[f.key] || f.values.length <= 8 ? f.values : f.values.slice(0, 5)).map(([v, n]) => (
                <label key={v} className="sp-check">
                  <input type="checkbox" checked={!!view.picked[f.key]?.includes(v)} onChange={() => toggle(f.key, v)} />
                  <span className="sp-check__label">{v}</span>
                  <span className="sp-check__count">{n}</span>
                </label>
              ))}
              {f.values.length > 8 && (
                <div><button type="button" className="sp-btn sp-btn--ghost sp-btn--sm" onClick={() => setExpanded({ ...expanded, [f.key]: !expanded[f.key] })}>
                  {expanded[f.key] ? "Show fewer" : `Show all ${f.values.length}`}
                </button></div>
              )}
            </fieldset>
          ))}
          <Connection />
        </aside>
        <div className="sp-browse__main">
          <div className="sp-row">
            <input className="sp-input sp-search sp-grow-field" type="search" placeholder={`Search your ${kind === "dlc" ? "DLC" : "library"}`} aria-label="Search title or title ID"
              value={view.q} onChange={(e) => setView({ ...view, q: e.target.value })} />
            <label className="sp-sort">
              <span className="sp-label">Sort by</span>
              <select className="sp-select sp-console-pick" value={view.sort} onChange={(e) => setView({ ...view, sort: e.target.value as Sort })}>
                <option value="title">Title</option>
                <option value="recent">Recently added</option>
                <option value="rating">Rating</option>
                <option value="released">Release date</option>
                <option value="size">Size</option>
              </select>
            </label>
            <span className="sp-tilesize">
              <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon sp-btn--sm" aria-label="Smaller covers" title="Smaller covers" onClick={() => resize(tileFor(width, cols + 1))}>
                <svg viewBox="0 0 16 16" aria-hidden><path d="M1 1h4v4H1zM6 1h4v4H6zM11 1h4v4h-4zM1 6h4v4H1zM6 6h4v4H6zM11 6h4v4h-4zM1 11h4v4H1zM6 11h4v4H6zM11 11h4v4h-4z" /></svg>
              </button>
              {/* negative so dragging right means fewer, bigger covers */}
              <input type="range" min={-colsFor(width, TILE_MIN)} max={-colsFor(width, TILE_MAX)} value={-cols} aria-label="Cover size"
                aria-valuetext={`${cols} per row`} onChange={(e) => resize(tileFor(width, -Number(e.target.value)))} />
              <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon sp-btn--sm" aria-label="Larger covers" title="Larger covers" onClick={() => resize(tileFor(width, cols - 1))}>
                <svg viewBox="0 0 16 16" aria-hidden><path d="M1 1h6v6H1zM9 1h6v6H9zM1 9h6v6H1zM9 9h6v6H9z" /></svg>
              </button>
            </span>
          </div>
          {picked && <InstallBar kind={kind} picked={picked} shown={shown} setPicked={setPicked} />}
          {shown.length ? (() => {
            const props = (g: Game) => ({ game: g, tooNew: tooNew.has(g.id), onChange: changed, peek: !!view.q.trim(), picked: picked && picked.has(g.id), onPick: () => {
              const next = new Set(picked);
              if (!next.delete(g.id)) next.add(g.id);
              setPicked(next);
            } });
            return tile >= TILE_LARGE
              ? <ul ref={setGrid} className="sp-gamegrid" style={{ "--gamecard-min": `${tile}px` } as CSSProperties}>{shown.map((g) => <LargeTile key={g.id} {...props(g)} />)}</ul>
              : <div ref={setGrid} className="sp-covergrid" style={{ "--cover-min": `${tile}px` } as CSSProperties}>{shown.map((g) => <Tile key={g.id} {...props(g)} />)}</div>;
          })()
            : <div className="sp-panel">
              <Empty icon="filter" title={`No ${noun} match`} text="Try other filters, or another search.">
                <button type="button" className="sp-btn" onClick={clear}>Clear filters</button>
              </Empty>
            </div>}
        </div>
      </div>
    </>
  );
}

/** The one state chip a cover shows, bottom right: what's wrong first, then installed. */
function chip(game: Game, tooNew: boolean): [string, Tone, IconName] | null {
  if (tooNew) return ["Doesn't run on this console's firmware", "danger", "alert"];
  if (game.compatibility_status === "incompatible") return [STATUS_LABEL.incompatible, "danger", "alert"];
  if (game.installed) return ["Installed on a console", "success", "drive"];
  if (game.compatibility_status === "needs_attention") return [STATUS_LABEL.needs_attention, "warning", "alert"];
  return null;
}

/** `picked`: null when not selecting; otherwise a click ticks the card instead of opening the game. */
/** `peek`: a search is on, so every card shown matched it and the small card shows its title without a hover. */
type TileProps = { game: Game; tooNew: boolean; onChange: (id: string, changes: Partial<Game>) => void; peek: boolean; picked: boolean | null; onPick: () => void };

// asked once the art loads, so only cards that have scrolled into view ask the store
const askStore = ({ game, onChange }: TileProps) => {
  if (game.title_id && !game.store) api<StoreInfo | null>(`/games/${game.id}/store`).then((i) => i && onChange(game.id, { store: i }), () => {});
};
const toggleFavorite = ({ game, onChange }: TileProps) => api<Game>(`/games/${game.id}`, { method: "PATCH", json: { favorite: !game.favorite } })
  .then((g) => onChange(game.id, { favorite: g.favorite, new: false }), toastError);
const selecting = ({ picked, onPick }: TileProps) => ({
  onClick: picked !== null ? (e: React.MouseEvent) => { e.preventDefault(); onPick(); } : undefined,
  "aria-pressed": picked ?? undefined, role: picked !== null ? "button" : undefined,
});

/** The large card's one status pill. */
const PILL: Record<Game["compatibility_status"], [string, Tone]> = {
  ready: ["Ready to install", "info"], needs_attention: ["Needs check", "warning"], incompatible: ["Incompatible", "danger"], unknown: ["Unknown", ""],
};
function pill(game: Game, tooNew: boolean): [string, Tone] {
  if (game.installed) return ["Installed", "success"];
  if (tooNew) return ["Firmware too old", "danger"];
  return PILL[game.compatibility_status];
}

/** "842", "3.1k", "21.5k" */
const abbreviate = (n: number) => (n < 1000 ? String(n) : `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`);

/** The PlayStation Store score as a ring: mint from 4.0, amber from 3.0, red below. */
function Score({ rating, count }: { rating: number; count: number }) {
  const label = `${rating.toFixed(1)} out of 5 from ${count.toLocaleString()} ratings on PlayStation Store`;
  return (
    <div className={rating >= 4 ? "sp-score" : rating >= 3 ? "sp-score sp-score--mid" : "sp-score sp-score--low"} title={label}>
      <div className="sp-score__ring">
        <svg viewBox="0 0 38 38" role="img" aria-label={label}>
          <circle className="sp-score__track" cx="19" cy="19" r="16" />
          <circle className="sp-score__arc" cx="19" cy="19" r="16" strokeDasharray={`${((rating / 5) * 100.5).toFixed(1)} 100.5`} />
        </svg>
        <span className="sp-score__value" aria-hidden>{rating.toFixed(1)}</span>
      </div>
      <span className="sp-score__count" aria-hidden>{abbreviate(count)}</span>
    </div>
  );
}

/** The large card: art fading into a name box tinted from it, with genre, publisher, store score, status and size. */
function LargeTile(props: TileProps) {
  const { game, tooNew, picked } = props;
  const [colors, setColors] = useState<CSSProperties>();
  const info = game.store;
  const genre = [info?.genres[0] ?? game.genre, year(game)].filter(Boolean).join(" · ");
  const [status, tone] = pill(game, tooNew);

  return (
    <li>
      <a className={picked ? "sp-gamecard sp-gamecard--picked" : "sp-gamecard"} href={`#/game/${game.id}`} style={colors} {...selecting(props)}>
        <Cover game={game} className="sp-gamecard__art" onLoad={(img) => (setColors(sampleColors(img)), askStore(props))}>
          {picked !== null ? <span className="sp-cover__pick sp-gamecard__pick" aria-hidden>{picked && <Icon name="check" />}</span>
            : game.new && <span className="sp-badge sp-badge--new">New</span>}
        </Cover>
        <div className="sp-gamecard__body">
          <div className="sp-gamecard__head">
            <div className="sp-gamecard__titles">
              <strong className="sp-gamecard__title">{game.title}</strong>
              {genre && <span className="sp-gamecard__genre">{genre}</span>}
              {info?.publisher && <span className="sp-gamecard__publisher">{info.publisher}</span>}
            </div>
            {!!info?.rating && <Score rating={info.rating} count={info.rating_count} />}
          </div>
          <div className="sp-gamecard__foot">
            <span className={tone ? `sp-statuspill sp-statuspill--${tone}` : "sp-statuspill"}><span className="sp-dot" />{status}</span>
            <span className="sp-gamecard__size">{game.copies ? `${game.copies.length} versions` : formatBytes(game.size_bytes)}</span>
          </div>
        </div>
      </a>
      {/* a sibling, not inside the link: a button can't be nested in a link */}
      {picked === null && !session.readOnly && (
        <button type="button" className="sp-gamecard__fav" aria-pressed={game.favorite}
          aria-label={game.favorite ? `Remove ${game.title} from favorites` : `Add ${game.title} to favorites`} onClick={() => toggleFavorite(props)}>
          <Icon name="star" />
        </button>
      )}
    </li>
  );
}

/** The name box's colors, as the design system's GameCardLarge sets them: the art drawn at 8×8, its bottom two rows
 *  averaged (edge) and its most saturated pixel (vivid), each mixed into near-black. */
function sampleColors(img: HTMLImageElement): CSSProperties | undefined {
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (!ctx) return;
  ctx.drawImage(img, 0, 0, 8, 8);
  const px = ctx.getImageData(0, 0, 8, 8).data;
  const edge = [0, 0, 0];
  let vivid = [0, 0, 0], best = -1;
  for (let i = 0; i < 64; i++) {
    const rgb = [px[i * 4], px[i * 4 + 1], px[i * 4 + 2]];
    if (i >= 48) rgb.forEach((v, k) => (edge[k] += v / 16)); // bottom two rows
    const saturation = Math.max(...rgb) - Math.min(...rgb);
    if (saturation > best) [best, vivid] = [saturation, rgb];
  }
  const NEAR_BLACK = [7, 9, 13]; // #07090d
  const mix = (c: number[], share: number) => `rgb(${c.map((v, k) => Math.round(v * share + NEAR_BLACK[k] * (1 - share))).join(" ")})`;
  const alpha = (a: number) => `rgb(${vivid.join(" ")} / ${a})`;
  return { "--gc-top": mix(edge, 0.5), "--gc-bottom": mix(vivid, 0.42), "--gc-glow": alpha(0.65), "--gc-ring": alpha(0.45) } as CSSProperties;
}

/** The small, poster-only card. */
function Tile(props: TileProps) {
  const { game, tooNew, picked } = props;
  const state = chip(game, tooNew);

  return (
    <div className="sp-coveritem">
      <a className={`sp-cover${picked ? " sp-cover--picked" : ""}${props.peek ? " is-peek" : ""}`} href={`#/game/${game.id}`} {...selecting(props)}>
        <Cover game={game} onLoad={() => askStore(props)}>
          <div className="sp-cover__top">
            {picked !== null ? <span className="sp-cover__pick" aria-hidden>{picked && <Icon name="check" />}</span>
              : game.new && <span className="sp-badge sp-badge--new">New</span>}
          </div>
          {state && <div className="sp-cover__bottom">
            <span className={`sp-cover__chip sp-cover__chip--${state[1]}`} role="img" aria-label={state[0]} title={state[0]}><Icon name={state[2]} /></span>
          </div>}
          <span className="sp-cover__caption">{game.title}{game.copies && <span className="sp-cover__sub">{game.copies.length} versions</span>}</span>
        </Cover>
      </a>
      {/* a sibling, not inside the link: a button can't be nested in a link */}
      {picked === null && !session.readOnly && (
        <button type="button" className="sp-cover__chip sp-cover__fav" aria-pressed={game.favorite} title={game.favorite ? "Remove from favorites" : "Add to favorites"}
          aria-label={`Favorite ${game.title}`} onClick={() => toggleFavorite(props)}><Icon name="star" filled={game.favorite} /></button>
      )}
    </div>
  );
}

/** The store's portrait key art, else the game's own cover: filling the art box when it's a portrait too (an uploaded
 *  poster), else (a square icon) whole, on a blurred copy of itself. `children` are the overlays on the art. */
export function Cover({ game, className = "sp-cover__art", onLoad, children }: { game: Game; className?: string; onLoad?: (img: HTMLImageElement) => void; children?: ReactNode }) {
  const cover = coverUrl(game);
  const [src, setSrc] = useState(game.title_id ? `/api/v1/games/${game.id}/poster?v=${encodeURIComponent(game.updated_at)}` : cover);
  const [portrait, setPortrait] = useState(false);
  if (!src) return <div className={className}><span className="sp-cover__ph">{game.title}</span>{children}</div>;
  const square = src === cover && !portrait;
  const loaded = (img: HTMLImageElement) => {
    setPortrait(img.naturalHeight >= img.naturalWidth * 1.2);
    onLoad?.(img);
  };
  return (
    <div className={square ? `${className} sp-cover__art--square` : className} style={square ? ({ "--art": `url("${src}")` } as CSSProperties) : undefined}>
      <img src={src} alt="" loading="lazy" onLoad={(e) => loaded(e.currentTarget)} onError={() => setSrc(src === cover ? null : cover)} />
      {children}
    </div>
  );
}
