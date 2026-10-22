import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { api } from "./api";
import { Empty, Icon, when } from "./ui";

/** A link that follows the message, shown as a text button: [label, href]. */
type Action = [label: string, href: string];
type Toast = { id: number; title: ReactNode; text?: ReactNode; error?: boolean; action?: Action; at: number };
/** What the backend also showed as a Windows notification: an install, move or conversion that ended. */
type Notified = { id: number; title: string; message: string; ok: boolean; at: number };

let push: (t: Toast) => void = () => {};
let next = 0;

// ponytail: kept in memory only, so a reload clears it. Persist on the backend if people want yesterday's.
let history: Toast[] = [];
const listeners = new Set<() => void>();
const changed = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => (listeners.add(l), () => { listeners.delete(l); });

/** Tells the user how a button press went, in a corner pop-up that stays put while the page scrolls. */
export function toast(title: ReactNode, error = false, action?: Action) {
  const t = { id: next++, title, error, action, at: Date.now() };
  history = [t, ...history].slice(0, 30);
  changed();
  push(t);
}
export const toastError = (e: unknown) => toast(e instanceof Error ? e.message : String(e), true);

const tone = (t: Toast) => (t.error ? "danger" : "success");
const icon = (t: Toast) => (t.error ? "xCircle" : "checkCircle");

/** Mounted once, in App. Newest at the bottom, nearest the corner. */
export default function Toasts() {
  const [list, setList] = useState<Toast[]>([]);
  const drop = (id: number) => setList((l) => l.filter((t) => t.id !== id));
  useEffect(() => {
    push = (t) => setList((l) => [...l, t].slice(-3));
  }, []);
  return (
    <div className="sp-toasts" aria-live="polite">
      {list.map((t) => <ToastItem key={t.id} t={t} drop={() => drop(t.id)} />)}
    </div>
  );
}

/** Goes after 5 s, 8 with an action; errors stay until dismissed. Hovering or focusing it pauses the clock. */
function ToastItem({ t, drop }: { t: Toast; drop: () => void }) {
  const left = useRef(t.action ? 8000 : 5000);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (t.error || paused) return;
    const start = Date.now();
    const timer = setTimeout(drop, left.current);
    return () => { clearTimeout(timer); left.current -= Date.now() - start; };
  }, [paused]);
  return (
    <div className={`sp-toast sp-toast--${tone(t)}`} role={t.error ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)} onFocus={() => setPaused(true)} onBlur={() => setPaused(false)}>
      <Icon name={icon(t)} className="sp-toast__icon" />
      <div className="sp-toast__body">
        <span className="sp-toast__title">{t.title}</span>
        {t.text && <span className="sp-toast__text">{t.text}</span>}
        {t.action && <div className="sp-toast__actions"><a className="sp-toast__action" href={t.action[1]} onClick={drop}>{t.action[0]}</a></div>}
      </div>
      <button type="button" className="sp-btn sp-btn--ghost sp-btn--icon sp-btn--sm sp-toast__close" aria-label="Dismiss" onClick={drop}><Icon name="x" /></button>
    </div>
  );
}

/** "2 min ago" within the hour, then "Today, 4:41 PM", then "Oct 20, 9:12 AM". */
function ago(at: number) {
  const min = Math.floor((Date.now() - at) / 60000);
  return min < 1 ? "Just now" : min < 60 ? `${min} min ago` : when(new Date(at).toISOString());
}

/** The top bar's bell and its panel: the recent toasts again, for the one that vanished before it was read, and the
    backend's notifications, for the install that ended while the app was closed or on another page.
    ponytail: no "Needs you" section, nothing in the app waits on a decision yet. Add it with the first job that does. */
export function Bell() {
  const toasts = useSyncExternalStore(subscribe, () => history);
  const [notified, setNotified] = useState<Notified[]>([]);
  const [seen, setSeen] = useState(0); // what's newer counts as unread
  const [dots, setDots] = useState(0); // the panel keeps marking what was unread when it opened
  useEffect(() => {
    const load = () => api<Notified[]>("/notifications").then(setNotified, () => {});
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);
  const list: Toast[] = [
    ...toasts,
    ...notified.map((n) => ({ id: -1 - n.id, title: n.title, text: n.message, error: !n.ok, at: n.at * 1000 })),
  ].sort((a, b) => b.at - a.at).slice(0, 30);
  const count = list.filter((t) => t.at > seen).length;
  return (
    <>
      <button type="button" popoverTarget="sp-ncenter" className="sp-btn sp-btn--ghost sp-btn--icon sp-bell"
        title="Notifications" aria-label={`Notifications${count ? `, ${count} unread` : ""}`}>
        <Icon name="bell" />
        {count > 0 && <span className="sp-bell__count" aria-hidden>{count}</span>}
      </button>
      <section id="sp-ncenter" popover="auto" className="sp-ncenter" aria-label="Notifications"
        onToggle={(e) => { if (e.newState === "open") { setDots(seen); setSeen(Date.now()); } }}
        onClick={(e) => { if ((e.target as Element).closest("a")) e.currentTarget.hidePopover(); }}>
        <header className="sp-ncenter__head"><h2 className="sp-ncenter__title">Notifications</h2></header>
        <div className="sp-ncenter__scroll">
          {list.length ? (
            <div className="sp-ncenter__section">
              <h3 className="sp-ncenter__label">Recent</h3>
              <ul className="sp-ncenter__list">
                {list.map((t) => (
                  <li key={t.id} className={`sp-nitem sp-nitem--${tone(t)}${t.at > dots ? " sp-nitem--unread" : ""}`}>
                    <Icon name={icon(t)} className="sp-nitem__icon" />
                    <div className="sp-nitem__body">
                      <div className="sp-nitem__top">
                        <span className="sp-nitem__title">{t.title}</span>
                        <time className="sp-nitem__time" dateTime={new Date(t.at).toISOString()}>{ago(t.at)}</time>
                      </div>
                      {t.text && <span className="sp-nitem__text">{t.text}</span>}
                      {t.action && <div className="sp-nitem__actions sp-nitem__actions--text"><a className="sp-toast__action" href={t.action[1]}>{t.action[0]}</a></div>}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ) : <Empty icon="bell" title="You're all caught up" />}
        </div>
        <footer className="sp-ncenter__foot"><span>Cleared when Spela restarts</span><a className="sp-link" href="#/settings">Activity log</a></footer>
      </section>
    </>
  );
}
