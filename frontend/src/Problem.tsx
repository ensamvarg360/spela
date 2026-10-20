import { ApiError } from "./api";

/** An error the way the user needs it: what happened, what to do about it, and a retry when there's one.
 *  Takes an ApiError (whose `fix` the backend fills in), any Error, or plain text. */
export default function Problem({ error, fix, retry }: { error: unknown; fix?: string; retry?: () => void }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="problem" role="alert">
      <strong>{message}</strong>
      {(fix || api?.fix) && <p>{fix || api!.fix}</p>}
      {(retry || (api && api.code !== "http_error")) && (
        <div className="inline">
          {retry && <button type="button" className="small secondary" onClick={retry}>Try again</button>}
          {api && api.code !== "http_error" && <span className="muted">Code: <code>{api.code}</code></span>}
        </div>
      )}
    </div>
  );
}

/** A console's state message with what to do about it, for lists and cards. */
export function StatusNote({ message, fix }: { message: string; fix: string }) {
  if (!message) return null;
  return <span className="status-note">{message}{fix && <span className="muted"> {fix}</span>}</span>;
}
