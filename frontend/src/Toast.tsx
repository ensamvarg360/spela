import { useEffect, useState, type ReactNode } from "react";

type Toast = { id: number; text: ReactNode; error?: boolean };

let push: (t: Toast) => void = () => {};
let next = 0;

/** Tells the user how a button press went, in a corner pop-up that stays put while the page scrolls. */
export function toast(text: ReactNode, error = false) {
  push({ id: next++, text, error });
}
export const toastError = (e: unknown) => toast(e instanceof Error ? e.message : String(e), true);

/** Mounted once, in App. Errors stay longer: they're the ones to read twice. */
export default function Toasts() {
  const [list, setList] = useState<Toast[]>([]);
  const drop = (id: number) => setList((l) => l.filter((t) => t.id !== id));
  useEffect(() => {
    push = (t) => {
      setList((l) => [...l, t].slice(-4));
      setTimeout(() => drop(t.id), t.error ? 12000 : 6000);
    };
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {list.map((t) => (
        <div key={t.id} className={`toast${t.error ? " bad" : ""}`}>
          <span>{t.text}</span>
          <button className="link" aria-label="Dismiss" onClick={() => drop(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}
