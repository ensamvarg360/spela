import { useEffect, useRef, type ReactNode } from "react";
import { toast } from "./Toast";

// The Spela design system's building blocks, as markup for spela.css. Each one matches its component card in the system.

// Lucide-style stroke icons on a 24px grid.
const PATHS = {
  alert: <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4" /><path d="M12 17h.01" /></>,
  arrowUpDown: <><path d="m3 8 4-4 4 4" /><path d="M7 4v16" /><path d="m21 16-4 4-4-4" /><path d="M17 20V4" /></>,
  arrowLeft: <><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></>,
  bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" /></>,
  bug: <><path d="m8 2 1.88 1.88" /><path d="M14.12 3.88 16 2" /><path d="M9 7.13v-1a3 3 0 1 1 6 0v1" /><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6" /><path d="M12 20v-9" /><path d="M6.53 9C4.6 8.8 3 7.1 3 5" /><path d="M6 13H2" /><path d="M3 21c0-2.1 1.7-3.9 3.8-4" /><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4" /><path d="M22 13h-4" /><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4" /></>,
  cart: <><circle cx="8" cy="21" r="1" /><circle cx="19" cy="21" r="1" /><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12" /></>,
  check: <path d="M20 6 9 17l-5-5" />,
  checkCircle: <><circle cx="12" cy="12" r="10" /><path d="m9 12 2 2 4-4" /></>,
  checkSquare: <><rect x="3" y="3" width="18" height="18" rx="3" /><path d="m8 12 3 3 5-6" /></>,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  chevronRight: <path d="m9 6 6 6-6 6" />,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></>,
  download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>,
  drive: <><path d="M22 12H2" /><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /><path d="M6 16h.01" /><path d="M10 16h.01" /></>,
  filter: <path d="M22 3H2l8 9.46V19l4 2v-8.54z" />,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  gamepad: <><path d="M6 11h4" /><path d="M8 9v4" /><path d="M15 12h.01" /><path d="M18 10h.01" /><path d="M17.32 5H6.68a4 4 0 0 0-3.98 3.59C2.6 9.42 2 14.46 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.41-1.41A2 2 0 0 1 9.83 16h4.34a2 2 0 0 1 1.41.59L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.55-.6-6.58-.69-7.26A4 4 0 0 0 17.32 5z" /></>,
  inbox: <><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></>,
  info: <><circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" /></>,
  logout: <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></>,
  move: <><path d="M4 9h13l-4-4" /><path d="M20 15H7l4 4" /></>,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  power: <><path d="M12 2v10" /><path d="M18.4 6.6a9 9 0 1 1-12.77.04" /></>,
  refresh: <><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 4v5h-5" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  settings: <><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></>,
  star: <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z" />,
  trash: <><path d="M4 7h16" /><path d="M10 11v6" /><path d="M14 11v6" /><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" /><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /></>,
  x: <><path d="M18 6 6 18" /><path d="m6 6 12 12" /></>,
  xCircle: <><circle cx="12" cy="12" r="10" /><path d="m15 9-6 6" /><path d="m9 9 6 6" /></>,
};
export type IconName = keyof typeof PATHS;

export function Icon({ name, className, filled }: { name: IconName; className?: string; filled?: boolean }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth={2}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>{PATHS[name]}</svg>
  );
}

/** A status word's colour: green OK, amber check this, red failed, blue in progress, "" neutral. */
export type Tone = "success" | "warning" | "danger" | "info" | "";
const tone = (base: string, t: Tone) => (t ? `${base} ${base}--${t}` : base);

export function Badge({ tone: t = "", dot, mono, title, children }: { tone?: Tone; dot?: boolean; mono?: boolean; title?: string; children: ReactNode }) {
  return <span className={tone("sp-badge", t) + (mono ? " sp-badge--mono" : "")} title={title}>{dot && <span className={tone("sp-dot", t)} />}{children}</span>;
}

export function PageHead({ back, title, desc, actions }: { back?: [href: string, label: string]; title: ReactNode; desc?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="sp-pagehead">
      <div className="sp-pagehead__titles">
        {back && <a className="sp-pagehead__back" href={back[0]}><Icon name="arrowLeft" className="sp-icon" />{back[1]}</a>}
        <h1 className="sp-pagehead__title">{title}</h1>
        {desc && <p className="sp-pagehead__desc">{desc}</p>}
      </div>
      {actions && <div className="sp-pagehead__actions">{actions}</div>}
    </header>
  );
}

/** One block of a screen. `flush` for lists and tables, so their dividers run edge to edge; `foot` holds the primary action, last. */
export function Panel({ title, badge, desc, actions, flush, foot, children }: {
  title: ReactNode; badge?: ReactNode; desc?: ReactNode; actions?: ReactNode; flush?: boolean; foot?: ReactNode; children?: ReactNode;
}) {
  return (
    <section className="sp-panel">
      <div className="sp-panel__head">
        <div className="sp-panel__titles">
          <h2 className="sp-panel__title">{title}{badge}</h2>
          {desc && <p className="sp-panel__desc">{desc}</p>}
        </div>
        {actions && <div className="sp-panel__actions">{actions}</div>}
      </div>
      {children != null && children !== false && <div className={flush ? "sp-panel__body sp-panel__body--flush" : "sp-panel__body"}>{children}</div>}
      {foot && <div className="sp-panel__foot">{foot}</div>}
    </section>
  );
}

/** A folding section, for detail people open only sometimes. `meta` keeps a closed one telling you something. */
export function Collapsible({ title, meta, open, children }: { title: ReactNode; meta?: ReactNode; open?: boolean; children: ReactNode }) {
  return (
    <details className="sp-collapsible" open={open}>
      <summary>{title}{meta != null && <span className="sp-collapsible__meta">{meta}</span>}<Icon name="chevronDown" className="sp-collapsible__chev" /></summary>
      <div className="sp-collapsible__body">{children}</div>
    </details>
  );
}

export function Notice({ tone: t = "", title, text, action, alert }: { tone?: Tone; title: ReactNode; text?: ReactNode; action?: ReactNode; alert?: boolean }) {
  const icon: IconName = t === "warning" ? "alert" : t === "danger" ? "xCircle" : t === "success" ? "checkCircle" : "info";
  return (
    <div className={tone("sp-notice", t)} role={alert ? "alert" : "status"}>
      <Icon name={icon} className="sp-notice__icon" />
      <div className="sp-notice__body">
        <span className="sp-notice__title">{title}</span>
        {text && <div className="sp-notice__text">{text}</div>}
      </div>
      {action && <div className="sp-notice__action sp-btngroup">{action}</div>}
    </div>
  );
}

export function Empty({ icon, title, text, children }: { icon: IconName; title: ReactNode; text?: ReactNode; children?: ReactNode }) {
  return (
    <div className="sp-empty">
      <Icon name={icon} className="sp-empty__icon" />
      <span className="sp-empty__title">{title}</span>
      {text && <span className="sp-empty__text">{text}</span>}
      {children && <div className="sp-btngroup">{children}</div>}
    </div>
  );
}

/** A search box that holds its own count: "Search 35 games" at rest, "2 of 35" and a clear button while searching.
    "/" focuses it from anywhere on the page, unless the focus is already in a field. */
export function Search({ value, onChange, shown, total, noun, label }: {
  value: string; onChange: (q: string) => void; shown: number; total: number; noun: string; label: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || (e.target as Element).closest("input, textarea, select, [contenteditable]")) return;
      e.preventDefault();
      input.current?.focus();
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, []);
  return (
    <div className={value ? "sp-search sp-search--active" : "sp-search"}>
      <Icon name="search" className="sp-search__icon" />
      <input ref={input} className="sp-input" type="search" aria-label={label} placeholder={`Search ${total} ${noun}`} value={value} onChange={(e) => onChange(e.target.value)} />
      <span className="sp-search__end">{value ? <>
        <span className="sp-search__count" role="status">{shown} of {total}</span>
        <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon sp-btn--sm" aria-label="Clear search" onClick={() => { onChange(""); input.current?.focus(); }}><Icon name="x" /></button>
      </> : <kbd className="sp-kbd" aria-hidden>/</kbd>}</span>
    </div>
  );
}

/** A label above its control, with an optional hint or error under it. The label wraps the control, so no ids are needed. */
export function Field({ label, hint, error, wide, children }: { label: ReactNode; hint?: ReactNode; error?: ReactNode; wide?: boolean; children: ReactNode }) {
  return (
    <label className={wide ? "sp-field sp-field--wide" : "sp-field"}>
      <span className="sp-label">{label}</span>
      {children}
      {error ? <span className="sp-error">{error}</span> : hint && <span className="sp-hint">{hint}</span>}
    </label>
  );
}

/** Progress of a copy or a removal, 0 to 1, drawn like a storage meter. */
export function Bar({ value, label }: { value: number; label: string }) {
  return (
    <div className="sp-meter__bar sp-progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value * 100)}>
      <div className="sp-meter__fill" style={{ width: `${Math.min(1, Math.max(0, value)) * 100}%` }} />
    </div>
  );
}

/** A drive's used space. Calm blue by default, amber above 85% used, red above 95%. */
export function Meter({ used, total, label }: { used: number; total: number; label: string }) {
  const share = total > 0 ? used / total : 0;
  return (
    <div className="sp-meter__bar" role="meter" aria-label={`${label} used`} aria-valuemin={0} aria-valuemax={total} aria-valuenow={used}>
      <div className={`sp-meter__fill${share > 0.95 ? " sp-meter__fill--danger" : share > 0.85 ? " sp-meter__fill--warning" : ""}`} style={{ width: `${share * 100}%` }} />
    </div>
  );
}

export const Spinner = () => <span className="sp-spinner" aria-hidden />;

/** Keeps the drive and the last folders of a long path, so the part that tells paths apart stays visible. */
export function shorten(p: string, max = 64) {
  const sep = p.includes("\\") ? "\\" : "/";
  const rest = p.split(sep);
  if (p.length <= max || rest.length < 4) return p;
  const head = rest.shift()!;
  const tail = [rest.pop()!];
  while (rest.length > 1 && [head, "…", rest[rest.length - 1], ...tail].join(sep).length <= max) tail.unshift(rest.pop()!);
  return [head, "…", ...tail].join(sep);
}

/** A path or ID: mono, on one line, the full value in the tooltip. `copy` adds the copy button in a sunken box. */
export function Path({ path, copy }: { path: string; copy?: boolean }) {
  const text = <span className="sp-path" title={path}>{shorten(path)}</span>;
  if (!copy) return text;
  const doCopy = () => Promise.resolve().then(() => navigator.clipboard.writeText(path))
    .then(() => toast("Copied."), () => toast("Couldn't copy. Select the path and copy it instead.", true));
  return (
    <div className="sp-pathbox">
      {text}
      <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon sp-btn--sm" aria-label="Copy path" title="Copy" onClick={doCopy}><Icon name="copy" /></button>
    </div>
  );
}

/** A missing value: an em dash in muted text, never an empty cell. */
export const None = () => <span className="sp-muted">—</span>;

// SQLite drops the zone from stored UTC times; without a "Z" the browser would read them as local time.
export const utc = (t: string) => new Date(/(Z|[+-]\d\d:\d\d)$/.test(t) ? t : t + "Z");

/** "Today, 4:41 PM", else "Oct 21, 4:41 PM", with the year only when it isn't this one. */
export function when(t: string | null, never = "Never") {
  if (!t) return never;
  const d = utc(t), now = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today, ${time}`;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" })}, ${time}`;
}

/** A date the short way, the full timestamp in its tooltip. */
export function Time({ t, never = "Never" }: { t: string | null; never?: string }) {
  if (!t) return <span className="sp-muted">{never}</span>;
  const d = utc(t);
  return <time className="sp-num" dateTime={d.toISOString()} title={d.toLocaleString()}>{when(t)}</time>;
}
