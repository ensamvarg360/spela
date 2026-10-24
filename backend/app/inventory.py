"""Console inventory sync and health monitor (project.md §9, §11). Copies what an adapter
reports into drives/installed_games; those tables are a cache of the console, not a source of truth."""
import asyncio
import hmac
import re
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import __version__, config, credentials, relay
from .consoles import CONSOLE_APP_DIR, CONSOLE_APP_LOGO, CONSOLE_APP_TITLE_ID, TITLE_ID, AdapterError, ConsoleAdapter, ConsoleStatus, HelperConsoleAdapter, get_adapter, ours
from .database import SessionLocal
from .models import AuditLog, Console, Drive, Game, InstalledGame, now

BACKOFF = (2, 5, 10, 30, 60)  # seconds between checks after 1, 2, 3... failures in a row
status: dict[str, dict] = {}  # console id -> {state, message, checked_at, failures}; live, so in memory
AUTO_HELPER_SECONDS = 60  # between tries to send the helper (or an update of the PS5 app) to a console
_auto_helper_at: dict[str, float] = {}
_auto_app_at: dict[str, float] = {}
app_logo_set: set[str] = set()  # consoles whose PS5 app tile has the wordmark as its logo


def adapter_for(db: Session, console: Console) -> ConsoleAdapter:
    return get_adapter(console, credentials.get_password(db, console))


def console_app_token(console_id: str) -> str:
    """The PS5 app's key: names its console and proves the PC issued it. Nothing is stored; deleting the console revokes it."""
    sig = hmac.new(config.SECRET_KEY.encode(), f"console-app:{console_id}".encode(), "sha256").hexdigest()
    return f"{console_id}.{sig}"


def console_app_files(console: Console) -> dict[str, bytes]:
    """The PS5 app as copied to the console, paired with this PC. The app asks the helper on its own console, which
    passes its requests on to this PC (relay.py), so it needs no address of the PC."""
    files = {p.relative_to(CONSOLE_APP_DIR).as_posix(): p.read_bytes() for p in CONSOLE_APP_DIR.rglob("*") if p.is_file()}
    files["assets/server.txt"] = (f"url=http://127.0.0.1:{HelperConsoleAdapter.HELPER_PORT}\n"
                                  f"token={console_app_token(console.id)}\n").encode()
    files["assets/version.txt"] = __version__.encode()
    return files


def _ver(v: str | None) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v or ""))


async def refresh(db: Session, console: Console):
    """Reads info, drives and installed games from the console and stores them. Raises AdapterError."""
    adapter = adapter_for(db, console)
    info = await adapter.get_console_info()
    drives = await adapter.list_drives()
    games = await adapter.scan_installed_games()

    t = now()
    console.firmware = info.firmware or console.firmware
    console.helper_status = info.helper_status
    console.last_seen_at = console.last_inventory_scan_at = t

    # Drives that aren't seen stay (an unplugged USB drive keeps its install-target setting).
    rows = {d.external_id: d for d in db.scalars(select(Drive).where(Drive.console_id == console.id))}
    for d in drives:
        row = rows.get(d.external_id)
        if not row:
            row = rows[d.external_id] = Drive(console_id=console.id, external_id=d.external_id,
                                              is_install_target=d.drive_type != "network")
            db.add(row)
        row.label, row.path, row.drive_type = d.label, d.path, d.drive_type
        row.total_bytes, row.free_bytes, row.last_seen_at = d.total_bytes, d.free_bytes, t
    db.flush()

    library: dict[str, Game] = {}  # title ID -> newest library copy
    for g in db.scalars(select(Game).where(Game.title_id.is_not(None), Game.kind == "game")):
        if g.title_id not in library or _ver(g.version) > _ver(library[g.title_id].version):
            library[g.title_id] = g

    installed = {g.installed_path: g for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == console.id))}
    for g in games:
        row = installed.pop(g.installed_path, None)
        if not row:
            row = InstalledGame(console_id=console.id, installed_path=g.installed_path)
            db.add(row)
        match = library.get(g.title_id)
        row.title_id, row.title, row.version, row.install_route = g.title_id, g.title, g.version, g.install_route
        row.source_path, row.size_bytes, row.last_verified_at = g.source_path, g.size_bytes, t
        row.drive_id = rows[g.drive_external_id].id if g.drive_external_id in rows else None
        row.game_id = match.id if match else None
        newer = match and g.version and match.version and _ver(match.version) > _ver(g.version)
        row.status = "orphaned_icon" if g.orphaned else "update_available" if newer else "installed"
    for gone in installed.values():  # no longer on the console
        db.delete(gone)
    db.commit()

    # Icons for titles we have no picture of yet; kept forever, since a title's icon doesn't change.
    need = [g for g in games if TITLE_ID.fullmatch(g.title_id) and not icon_path(g.title_id).is_file()
            and not ((m := library.get(g.title_id)) and m.cover_path)]
    if need:
        try:
            icons = await adapter.get_icons(need)
        except AdapterError:
            return  # the inventory is stored; icons are tried again next scan
        icon_path("x").parent.mkdir(parents=True, exist_ok=True)
        for tid, data in icons.items():
            icon_path(tid).write_bytes(data)


def icon_path(title_id: str):
    return config.COVERS_DIR / "console" / f"{title_id}.png"


def forget(db: Session, console_id: str):
    """Drops the cached inventory of a console that's being deleted."""
    db.execute(delete(InstalledGame).where(InstalledGame.console_id == console_id))
    db.execute(delete(Drive).where(Drive.console_id == console_id))
    status.pop(console_id, None)
    relay.forget(console_id)


def record(console_id: str, state: str, message: str = "", fix: str = "") -> dict:
    prev = status.get(console_id, {})
    failures = 0 if state == "online" else prev.get("failures", 0) + 1
    status[console_id] = {"state": state, "message": message, "fix": fix, "checked_at": now(), "failures": failures}
    if state == "online":  # home-screen logos: right as it's back on (often after a restart, which drops them), then regularly
        from . import jobs, logos  # they import this module
        if prev.get("state") == "online" and time.monotonic() - _logos_at.get(console_id, float("-inf")) < logos.SYNC_SECONDS:
            return prev
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return prev  # not in the app's event loop (tests)
        _logos_at[console_id] = time.monotonic()
        jobs._spawn(logos.restore(console_id))
    return prev


_logos_at: dict[str, float] = {}  # when logos.restore last started per console


async def check(console_id: str) -> dict:
    """One health check. After an offline -> online change it also refreshes the inventory."""
    with SessionLocal() as db:
        console = db.get(Console, console_id)
        if not console:
            return {"state": "unknown"}
        try:
            adapter = adapter_for(db, console)
            result = await adapter.connect()
            if await _auto_helper(db, console, adapter, result):
                result = await adapter.connect()
        except AdapterError as e:  # unsupported connection type, unreadable password
            record(console_id, "degraded", e.message, e.fix)
            return status[console_id]
        if not console.auto_helper and result.state == "degraded" and result.helper_status.get("helper") == "missing":
            result.message, result.fix = "The helper is removed.", ("Spela won't send it again until you click "
                                                                  "Install helper in Settings → Consoles.")
        await _auto_console_app(db, console, adapter, result)
        prev = record(console_id, result.state, result.message, result.fix)
        if result.state == "online":
            console.last_seen_at, console.helper_status = now(), result.helper_status
            db.commit()
            if console.connection_type in ("helper", "ftp"):  # the helper runs: the PS5 app reaches the PC through it
                if ours(result.helper_status):
                    relay.ensure(console.id, console.ip_address)
                else:  # another install that shares our key sent it, and its PS5 app pairing
                    relay.stop(console.id)
            if prev.get("state") != "online":
                try:
                    await refresh(db, console)
                except AdapterError as e:
                    record(console_id, "degraded", e.message, e.fix)
        return status[console_id]


async def _auto_helper(db: Session, console: Console, adapter: ConsoleAdapter, result: ConsoleStatus) -> bool:
    """The helper stops when the PS5 restarts. Once the console is on again and the jailbreak's ELF loader is up,
    this sends the helper (or replaces an outdated one) without the user clicking Install helper. True when it did.
    The console is on when it's online or degraded; offline and rest mode can't run payloads."""
    if (not console.auto_helper or console.connection_type not in ("helper", "ftp") or result.state not in ("online", "degraded")
            or result.helper_status.get("helper") in (__version__, "other_pc") or not ours(result.helper_status)):
        return False
    if time.monotonic() - _auto_helper_at.get(console.id, float("-inf")) < AUTO_HELPER_SECONDS:
        return False
    _auto_helper_at[console.id] = time.monotonic()
    try:
        version = await adapter.install_helper()
    except AdapterError:  # usually the loader isn't running yet; tried again in a minute
        return False
    # Successes only: a failure every minute until the jailbreak runs would bury the audit log.
    db.add(AuditLog(action="helper_install", console_id=console.id, result="success", details={"version": version, "automatic": True}))
    db.commit()
    return True


async def _auto_console_app(db: Session, console: Console, adapter: ConsoleAdapter, result: ConsoleStatus):
    """Brings an installed PS5 app up to this version, as _auto_helper does for the helper, then puts the wordmark
    on its home-screen page (once per run of this PC app: the home screen keeps it). Never installs it on a console
    without it, so Remove PS5 app keeps it removed."""
    app = result.helper_status.get("console_app")
    if (console.connection_type not in ("helper", "ftp") or result.state != "online" or not ours(result.helper_status)
            or app in (None, "missing") or (app == __version__ and console.id in app_logo_set) or not CONSOLE_APP_DIR.is_dir()):
        return
    if time.monotonic() - _auto_app_at.get(console.id, float("-inf")) < AUTO_HELPER_SECONDS:
        return
    _auto_app_at[console.id] = time.monotonic()
    if app != __version__:
        try:
            await adapter.install_console_app(console_app_files(console))
        except AdapterError:  # tried again in a minute; Update PS5 app in Settings shows why
            return
        result.helper_status["console_app"] = __version__
        db.add(AuditLog(action="console_app_install", console_id=console.id, result="success",
                        details={"version": __version__, "from": app, "automatic": True}))
        db.commit()
    try:
        await adapter.set_home_logo(CONSOLE_APP_TITLE_ID, CONSOLE_APP_LOGO.read_bytes())
        app_logo_set.add(console.id)
    except AdapterError:  # its tile isn't on the home screen yet, or the helper is too old: tried again in a minute
        pass


async def monitor():
    """Checks every enabled console every CONSOLE_CHECK_SECONDS, backing off while it's unreachable."""
    due: dict[str, float] = {}
    while True:
        with SessionLocal() as db:
            ids = list(db.scalars(select(Console.id).where(Console.enabled)))
        todo = [i for i in ids if due.get(i, 0) <= time.monotonic()]
        # ponytail: checks run together, so one slow console delays the next round. Fine for a handful of consoles.
        await asyncio.gather(*(check(i) for i in todo), return_exceptions=True)
        for i in todo:
            failures = status.get(i, {}).get("failures", 0)
            due[i] = time.monotonic() + (BACKOFF[min(failures, len(BACKOFF)) - 1] if failures else config.CONSOLE_CHECK_SECONDS)
        await asyncio.sleep(1)
