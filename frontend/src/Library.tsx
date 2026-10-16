import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { api, coverUrl, formatLabel, STATUS_LABEL, type Game, type StoreInfo } from "./api";

export type Sort = "title" | "recent" | "rating" | "released" | "size";
/** What the library shows: the header's search, the sidebar's ticked values per filter, and the order. */
export interface View { q: string; picked: Record<string, string[]>; sort: Sort }
export const NO_VIEW: View = { q: "", picked: {}, sort: "title" };

const year = (g: Game) => Number((g.store?.release_date ?? g.release_date ?? "").slice(0, 4)) || null;

/** The sidebar's filters, each listing the values its games have. Ticked values of one filter are OR'ed, filters AND'ed. */
const FILTERS: { key: string; label: string; values: (g: Game) => (string | null | undefined)[]; order?: string[] }[] = [
  { key: "collection", label: "Collection", order: ["Favorites", "Installed", "Not installed"],
    values: (g) => [g.favorite ? "Favorites" : null, g.installed ? "Installed" : "Not installed"] },
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

export default function Library({ view, setView }: { view: View; setView: (v: View) => void }) {
  const [games, setGames] = useState<Game[] | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({}); // filters showing all their values

  useEffect(() => {
    api<Game[]>("/games").then(setGames, (e) => setError(e.message));
  }, []);
  // a tile's store lookup, so the sidebar's genres, years and ratings fill in as tiles come into view
  const gotStore = (id: string, store: StoreInfo) => setGames((gs) => gs && gs.map((g) => (g.id === id ? { ...g, store } : g)));

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

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!games) return <p className="muted">Loading…</p>;
  if (!games.length)
    return (
      <div className="empty card">
        <h2>Your library is empty</h2>
        <p>Add a library folder in <a href="#/settings">Settings</a>, then run a scan.</p>
      </div>
    );

  const toggle = (key: string, v: string) => {
    const now = view.picked[key] ?? [];
    setView({ ...view, picked: { ...view.picked, [key]: now.includes(v) ? now.filter((x) => x !== v) : [...now, v] } });
  };
  const clear = () => setView({ ...view, q: "", picked: {} });
  const chips = Object.entries(view.picked).flatMap(([key, vs]) => vs.map((v) => [key, v]));
  const q = view.q.trim();

  return (
    <div className="browse">
      <aside className="filters" aria-label="Filters">
        {facets.filter((f) => f.values.length > 1 || view.picked[f.key]?.length).map((f) => (
          <details key={f.key} open>
            <summary>{f.label}</summary>
            <ul>
              {(expanded[f.key] ? f.values : f.values.slice(0, 8)).map(([v, n]) => (
                <li key={v}>
                  <label className="facet">
                    <input type="checkbox" checked={!!view.picked[f.key]?.includes(v)} onChange={() => toggle(f.key, v)} />
                    <span>{v}</span>
                    <span className="count">{n}</span>
                  </label>
                </li>
              ))}
            </ul>
            {f.values.length > 8 && (
              <button className="link more" onClick={() => setExpanded({ ...expanded, [f.key]: !expanded[f.key] })}>
                {expanded[f.key] ? "Show fewer" : `Show all ${f.values.length}`}
              </button>
            )}
          </details>
        ))}
      </aside>
      <section className="results">
        <div className="results-bar">
          <ul className="chips">
            {q && <li><button className="chip" onClick={() => setView({ ...view, q: "" })} aria-label={`Clear search ${q}`}>“{q}” <span aria-hidden>×</span></button></li>}
            {chips.map(([key, v]) => (
              <li key={key + v}><button className="chip" onClick={() => toggle(key, v)} aria-label={`Remove filter ${v}`}>{v} <span aria-hidden>×</span></button></li>
            ))}
            {(chips.length > 0 || q) && <li><button className="link" onClick={clear}>Clear all</button></li>}
          </ul>
          <span className="muted">{shown.length === games.length ? `${games.length} games` : `${shown.length} of ${games.length} games`}</span>
          <label className="sort">
            Sort by
            <select value={view.sort} onChange={(e) => setView({ ...view, sort: e.target.value as Sort })}>
              <option value="title">Title</option>
              <option value="recent">Recently added</option>
              <option value="rating">Rating</option>
              <option value="released">Release date</option>
              <option value="size">Size</option>
            </select>
          </label>
        </div>
        {shown.length
          ? <ul className="games">{shown.map((g) => <li key={g.id}><Tile game={g} onStore={gotStore} /></li>)}</ul>
          : <p className="muted nothing">No games match. <button className="link" onClick={clear}>Clear the filters</button></p>}
      </section>
    </div>
  );
}

function Tile({ game, onStore }: { game: Game; onStore: (id: string, info: StoreInfo) => void }) {
  const [colors, setColors] = useState<CSSProperties>();
  const info = game.store;
  const loaded = (img: HTMLImageElement) => {
    setColors(sampleColors(img));
    // asked once the art loads, so only tiles that have scrolled into view ask the store
    if (game.title_id && !info) api<StoreInfo | null>(`/games/${game.id}/store`).then((i) => i && onStore(game.id, i), () => {});
  };
  const genre = info?.genres[0] ?? game.genre;
  const byline = [info?.publisher, (info?.release_date ?? game.release_date)?.slice(0, 4)].filter(Boolean).join(", ");

  return (
    <a className="game" href={`#/game/${game.id}`} style={colors}>
      <Cover game={game} onLoad={loaded} />
      <div className="name">
        <strong>{game.favorite && "★ "}{game.title}</strong>
        {info?.rating != null && (
          <span className="rating" title={`${info.rating_count.toLocaleString()} ratings on PlayStation Store`}>{info.rating.toFixed(1)}</span>
        )}
        {genre && <span className="genre">{genre}</span>}
        {byline && <span className="byline">{byline}</span>}
        {game.installed
          ? <span className="badge installed">Installed</span>
          : <span className={`badge ${game.compatibility_status}`}>{STATUS_LABEL[game.compatibility_status]}</span>}
      </div>
    </a>
  );
}

/** The name box's gradient: the art's bottom edge, which it continues, into the art's most vivid patch. */
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
  return { "--edge": `rgb(${edge.map(Math.round).join(" ")})`, "--vivid": `rgb(${vivid.join(" ")})` } as CSSProperties;
}

/** The store's portrait key art, else the game's own (square) cover on a blurred copy of itself. */
export function Cover({ game, onLoad }: { game: Game; onLoad?: (img: HTMLImageElement) => void }) {
  const cover = coverUrl(game);
  const [src, setSrc] = useState(game.title_id ? `/api/v1/games/${game.id}/poster?v=${encodeURIComponent(game.updated_at)}` : cover);
  if (!src) return <div className="art placeholder" aria-hidden>{game.title.slice(0, 1)}</div>;
  const square = src === cover;
  return (
    <div className={square ? "art square" : "art"} style={square ? ({ "--art": `url("${src}")` } as CSSProperties) : undefined}>
      <img src={src} alt="" loading="lazy" onLoad={(e) => onLoad?.(e.currentTarget)} onError={() => setSrc(square ? null : cover)} />
    </div>
  );
}
