"""Console inventory sync and health monitor (project.md §9, §11). Copies what an adapter
reports into drives/installed_games; those tables are a cache of the console, not a source of truth."""
import asyncio
import re
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import __version__, config, credentials
from .consoles import TITLE_ID, AdapterError, ConsoleAdapter, ConsoleStatus, get_adapter
from .database import SessionLocal
from .models import AuditLog, Console, Drive, Game, InstalledGame, now

BACKOFF = (2, 5, 10, 30, 60)  # seconds between checks after 1, 2, 3... failures in a row
status: dict[str, dict] = {}  # console id -> {state, message, checked_at, failures}; live, so in memory
AUTO_HELPER_SECONDS = 60  # between tries to send the helper to a console that's on without it
_auto_helper_at: dict[str, float] = {}


def adapter_for(db: Session, console: Console) -> ConsoleAdapter:
    return get_adapter(console, credentials.get_password(db, console))


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


def record(console_id: str, state: str, message: str = "", fix: str = "") -> dict:
    prev = status.get(console_id, {})
    failures = 0 if state == "online" else prev.get("failures", 0) + 1
    status[console_id] = {"state": state, "message": message, "fix": fix, "checked_at": now(), "failures": failures}
    return prev


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
        prev = record(console_id, result.state, result.message, result.fix)
        if result.state == "online":
            console.last_seen_at, console.helper_status = now(), result.helper_status
            db.commit()
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
    if not console.auto_helper or console.connection_type not in ("helper", "ftp") or result.state not in ("online", "degraded")             or result.helper_status.get("helper") == __version__:
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
