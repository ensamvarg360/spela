import { ApiError } from "./api";
import { Notice } from "./ui";

/** An error the way the user needs it: what happened, what to do about it, and a retry when there's one.
 *  Takes an ApiError (whose `fix` the backend fills in), any Error, or plain text. The error code stays under Details. */
export default function Problem({ error, fix, retry }: { error: unknown; fix?: string; retry?: () => void }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const api = error instanceof ApiError ? error : null;
  const code = api && api.code !== "http_error" ? api.code : null;
  return (
    <Notice tone="danger" alert title={message}
      text={(fix || api?.fix || code) && <>
        {fix || api?.fix}
        {code && <details className="sp-disclosure"><summary>Details</summary><code className="sp-code">{code}</code></details>}
      </>}
      action={retry && <button type="button" className="sp-btn sp-btn--sm" onClick={retry}>Try again</button>} />
  );
}

/** A console's state message with what to do about it, for lists and cards. */
export function StatusNote({ message, fix }: { message: string; fix: string }) {
  if (!message) return null;
  return <span>{message}{fix && <span className="sp-muted"> {fix}</span>}</span>;
}
