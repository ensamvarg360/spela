import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { api, coverUrl, formatLabel, STATUS_LABEL, type Game, type Status } from "./api";

type Sort = "title" | "recent" | "size" | "version";

export default function Library() {
  const [games, setGames] = useState<Game[] | null>(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<Status | "">("");
  const [format, setFormat] = useState("");
  const [region, setRegion] = useState("");
  const [favorites, setFavorites] = useState(false);
  const [sort, setSort] = useState<Sort>("title");

  useEffect(() => {
    api<Game[]>("/games").then(setGames, (e) => setError(e.message));
  }, []);

  const formats = useMemo(() => [...new Set(games?.map(formatLabel))].sort(), [games]);
  const regions = useMemo(() => [...new Set(games?.map((g) => g.region).filter(Boolean) as string[])].sort(), [games]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = (games ?? []).filter(
      (g) =>
        (!needle || g.title.toLowerCase().includes(needle) || g.title_id?.toLowerCase().includes(needle)) &&
        (!status || g.compatibility_status === status) &&
        (!format || formatLabel(g) === format) &&
        (!region || g.region === region) &&
        (!favorites || g.favorite),
    );
    const cmp: Record<Sort, (a: Game, b: Game) => number> = {
      title: (a, b) => a.title.localeCompare(b.title),
      recent: (a, b) => b.created_at.localeCompare(a.created_at),
      size: (a, b) => b.size_bytes - a.size_bytes,
      version: (a, b) => (b.version ?? "").localeCompare(a.version ?? "", undefined, { numeric: true }),
    };
    return list.sort(cmp[sort]);
  }, [games, q, status, format, region, favorites, sort]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!games) return <p className="muted">Loading…</p>;
  if (!games.length)
    return (
      <div className="empty card">
        <h2>Your library is empty</h2>
        <p>Add a library folder in <a href="#/settings">Settings</a>, then run a scan.</p>
      </div>
    );

  return (
    <>
      <div className="toolbar">
        <input type="search" placeholder="Search title or title ID" aria-label="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value as Status | "")}>
          <option value="">All statuses</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select aria-label="Format" value={format} onChange={(e) => setFormat(e.target.value)}>
          <option value="">All formats</option>
          {formats.map((f) => <option key={f}>{f}</option>)}
        </select>
        {regions.length > 0 && (
          <select aria-label="Region" value={region} onChange={(e) => setRegion(e.target.value)}>
            <option value="">All regions</option>
            {regions.map((r) => <option key={r}>{r}</option>)}
          </select>
        )}
        <label className="check"><input type="checkbox" checked={favorites} onChange={(e) => setFavorites(e.target.checked)} /> Favorites</label>
        <select aria-label="Sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
          <option value="title">Title</option>
          <option value="recent">Recently added</option>
          <option value="size">Size</option>
          <option value="version">Version</option>
        </select>
        <span className="muted">{shown.length} of {games.length}</span>
      </div>
      <ul className="games">
        {shown.map((g) => <li key={g.id}><Tile game={g} /></li>)}
      </ul>
    </>
  );
}

interface StoreInfo {
  publisher: string | null;
  release_date: string | null;
  genres: string[];
  rating: number | null;
  rating_count: number;
  age_rating: string | null;
}

function Tile({ game }: { game: Game }) {
  const [colors, setColors] = useState<CSSProperties>();
  const [info, setInfo] = useState<StoreInfo | null>(null);
  const loaded = (img: HTMLImageElement) => {
    setColors(sampleColors(img));
    // asked once the art loads, so only tiles that have scrolled into view ask the store
    if (game.title_id && !info) api<StoreInfo | null>(`/games/${game.id}/store`).then(setInfo, () => {});
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
