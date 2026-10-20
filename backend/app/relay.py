"""The PS5 app's way to the PC, through the helper. The PS5 app asks the helper on its own console (127.0.0.1:7735);
the helper holds the request until the PC takes it (GET /relay), and the PC answers it from its own console_api over
127.0.0.1 (POST /relay). The PC only ever connects out to the console, so Windows' firewall and whether it calls the
network Public or Private don't matter. Workers run while the console's helper is online (inventory.check)."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .consoles import HELPER_KEY_HEADER, HelperConsoleAdapter, helper_key

PREFIX = "/api/v1/console-app/"  # the only part of the PC's API the PS5 app may reach this way
WORKERS = 2  # the PS5 app asks one thing at a time; a second worker keeps a cover from waiting behind the library
POLL_TIMEOUT = 30  # the helper answers an empty GET /relay after 20 s
RETRY_SECONDS = 5  # after a failed poll: the helper isn't running yet, or is an older one without /relay

_wanted: dict[str, str] = {}  # console id -> its IP address, while its workers should run
_lock = threading.Lock()


def _forward(method: str, path: str, auth: str) -> tuple[int, bytes]:
    """Runs one PS5 app request on this PC's own API, the way the app would have asked directly."""
    if not path.startswith(PREFIX):
        return 403, b"Only the PS5 app's part of Spela can be reached through the helper."
    req = urllib.request.Request(f"http://127.0.0.1:{config.PORT}{path}", method=method,
                                 headers={"Authorization": auth} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError:
        return 502, b"Spela on your PC couldn't answer. Restart it from its tray icon."


def serve_one(host: str, key: str, port: int = HelperConsoleAdapter.HELPER_PORT) -> bool:
    """Takes one request from the helper at `host` and answers it. False when there was none to take.
    Raises OSError when the helper can't be reached or doesn't relay (an older helper answers 404)."""
    base = f"http://{host}:{port}/relay"
    with urllib.request.urlopen(urllib.request.Request(base, headers={HELPER_KEY_HEADER: key}), timeout=POLL_TIMEOUT) as r:
        if r.status == 204:
            return False
        job = json.loads(r.read())
    status, body = _forward(job["method"], job["path"], job.get("auth", ""))
    query = urllib.parse.urlencode({"id": job["id"], "status": status})
    req = urllib.request.Request(f"{base}?{query}", data=body, method="POST", headers={HELPER_KEY_HEADER: key})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404: the PS5 app stopped waiting; nothing to do
            raise
    return True


def _worker(console_id: str, host: str):
    key = helper_key(console_id)
    while _wanted.get(console_id) == host:  # stops when the console is removed, or its address changes
        try:
            serve_one(host, key)
        except Exception:  # whatever a request holds, the worker keeps going (a control character raised InvalidURL)
            time.sleep(RETRY_SECONDS)


def ensure(console_id: str, host: str):
    """Keeps WORKERS workers relaying for this console's helper. Cheap to call on every health check."""
    with _lock:
        if _wanted.get(console_id) == host:
            return
        _wanted[console_id] = host  # a different address: the old workers see it and stop
        for _ in range(WORKERS):
            threading.Thread(target=_worker, args=(console_id, host), daemon=True, name=f"relay-{host}").start()


def forget(console_id: str):
    with _lock:
        _wanted.pop(console_id, None)
