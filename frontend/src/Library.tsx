import { useEffect, useMemo, useState } from "react";
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
      <ul className="grid">
        {shown.map((g) => (
          <li key={g.id}>
            <a className="tile" href={`#/game/${g.id}`}>
              <Cover game={g} />
              {g.installed
                ? <span className="badge installed">Installed</span>
                : <span className={`badge ${g.compatibility_status}`}>{STATUS_LABEL[g.compatibility_status]}</span>}
              <Title game={g} />
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}

/** The game's logo from PlayStation Store; its title as text when the store has none. */
function Title({ game }: { game: Game }) {
  const [noLogo, setNoLogo] = useState(!game.content_id);
  return (
    <strong className="title">
      {game.favorite && "★ "}
      {noLogo ? game.title : <img className="logo" src={`/api/v1/games/${game.id}/logo`} alt={game.title} loading="lazy" onError={() => setNoLogo(true)} />}
    </strong>
  );
}

export function Cover({ game }: { game: Game }) {
  const url = coverUrl(game);
  return url ? <img className="cover" src={url} alt="" loading="lazy" /> : <div className="cover placeholder" aria-hidden>{game.title.slice(0, 1)}</div>;
}
