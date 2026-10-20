"""Install jobs (project.md §10): a queue in the install_jobs table and one worker that runs them.
Folder dumps and disc images are copied into a drive's homebrew folder (ShadowMountPlus). Packages are copied there too,
then the helper starts the console's installer on them (consoles.py _install_pkg; untested on a console).
Archives are extracted on this PC first (archives.py) and the dump or image inside is copied the same way.
Removals (project.md §13) delete that copy again, after a preview and the title ID typed back.
DLC unlockers (a folder with dlc_emu.ini) are copied over the installed game's folder, as their release notes say.
DLC packages need a package installer, like game packages.
Conversions turn a folder dump into an .exfat disc image next to it, on this PC (exfat.py).
Moves copy an installed game to another drive on the console itself, then delete the original."""
import asyncio
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import archives, config, exfat, inventory, logos, scanner
from .consoles import IMAGE_EXTS, AdapterError, DriveInfo, InstalledGameInfo, RemovalResult
from .database import SessionLocal
from .models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob, _id, now

ACTIVE = ("queued", "preflight", "extracting", "transferring", "registering")
RUNNING = ACTIVE[1:]
_cancel: dict[str, threading.Event] = {}  # job id -> set to stop its transfer
log = logging.getLogger(__name__)
notify: Callable[[str, str], None] | None = None  # (title, message); the launcher shows it as a Windows notification


def _notify(title: str, message: str):
    if notify:
        try:
            notify(title, message)
        except Exception:  # a notification must never fail the job
            log.exception("notification")
# Highest firmware where fake packages launch (Kstuff Lite 1.11 + ArkSama's 11.60 fix, Oct 2026).
# Raise a row when a payload release confirms more; 12.x-13.60 install but PS5 packages don't launch yet.
PKG_MAX_FIRMWARE = {"ps4_pkg": "11.60", "ps5_pkg": "11.60"}
PKG_FORMATS = tuple(PKG_MAX_FIRMWARE)


def _fw(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def pkg_blocker(console: Console, game: Game) -> str:
    """Why a package can't be installed, or "". Firmware first, so a 13.xx console hears the real reason."""
    kind, top = ("PS4" if game.format == "ps4_pkg" else "PS5"), PKG_MAX_FIRMWARE[game.format]
    if not console.firmware:
        return f"{kind} packages need firmware {top} or lower, and this console's firmware is unknown. Install the helper."
    try:
        too_new = _fw(console.firmware) > _fw(top)
    except ValueError:
        return f"Can't read this console's firmware ({console.firmware})."
    if too_new:
        return f"{kind} packages don't run on firmware {console.firmware} yet (works up to {top}). Use a folder dump or disc image."
    return ""


def unlocker_target(db: Session, console: Console, title_id: str | None) -> tuple[InstalledGame | None, str]:
    """The installed game folder a DLC unlocker for `title_id` is copied into, or (None, why not)."""
    if not title_id:
        return None, "Can't tell which game this DLC is for. Enter its title ID."
    rows = db.scalars(select(InstalledGame).where(InstalledGame.console_id == console.id, InstalledGame.title_id == title_id,
                                                  InstalledGame.status != "orphaned_icon")).all()
    if row := next((r for r in rows if r.install_route == "shadowmountplus" and r.source_path
                    and not r.source_path.lower().endswith(IMAGE_EXTS)), None):
        return row, ""
    if rows:
        return None, (f"{title_id} is installed as a {'disc image' if rows[0].install_route == 'shadowmountplus' else 'package'}. "
                      f"A DLC unlocker is copied into the game's folder, so the game must be installed as a folder dump.")
    return None, f"Install the game ({title_id}) on this console first. The DLC unlocker is copied into its folder."


def blocker(db: Session, console: Console, game: Game) -> str | None:
    """Why this game can't be queued for this console, or None. Console-side checks (online, space) run at preflight."""
    if game.format in PKG_FORMATS and (reason := "DLC packages can't be installed yet." if game.kind == "dlc" else pkg_blocker(console, game)):
        return reason
    if game.format == "archive" and (reason := archives.blocker(game.source_path)):
        return reason
    if game.format not in ("folder_dump", "image", "archive", "unlocker", *PKG_FORMATS):
        return "Format not recognized."
    if game.compatibility_status == "incompatible":
        return game.compatibility_reason
    if game.format == "unlocker" and not (target := unlocker_target(db, console, game.title_id))[0]:
        return target[1]
    if game.kind == "game" and game.title_id and db.scalar(select(InstalledGame.id).where(
            InstalledGame.console_id == console.id, InstalledGame.title_id == game.title_id)):
        return "Already installed on this console."
    if db.scalar(select(InstallJob.id).where(InstallJob.console_id == console.id, InstallJob.game_id == game.id,
                                             InstallJob.status.in_(ACTIVE))):
        return "Already queued."
    return None


def queue(db: Session, console: Console, game: Game, drive_id: str | None = None) -> InstallJob:
    """Adds a job. Raises ValueError with the reason when the game can't be installed."""
    if reason := blocker(db, console, game):
        raise ValueError(reason)
    if drive_id and not db.scalar(select(Drive.id).where(Drive.id == drive_id, Drive.console_id == console.id)):
        raise ValueError("That drive isn't on this console.")
    job = InstallJob(console_id=console.id, game_id=game.id, target_drive_id=drive_id,
                     total_bytes=game.size_bytes, message="Waiting.")
    db.add(job)
    db.add(AuditLog(action="install_queued", console_id=console.id, title_id=game.title_id,
                    target_path=game.source_path, result="success", details={"job": job.id}))
    db.commit()
    return job


def cancel(db: Session, job: InstallJob) -> bool:
    """A queued job ends now; a running one stops at its next chunk. False if it had already ended."""
    if job.status == "queued":
        _finish(db, job, "canceled", "Canceled before it started.")
        return True
    if job.status in RUNNING:
        _cancel.setdefault(job.id, threading.Event()).set()
        return True
    return False


def _finish(db: Session, job: InstallJob, status: str, message: str, error: dict | None = None):
    job.status, job.message, job.error_detail, job.finished_at = status, message, error, now()
    game = db.get(Game, job.game_id)
    db.add(AuditLog(action="install", console_id=job.console_id, title_id=game and game.title_id,
                    target_path=game and game.source_path, result="success" if status == "success" else
                    "canceled" if status == "canceled" else "failed", details={"job": job.id, "status": status, **(error or {})}))
    db.commit()
    if status != "canceled":  # the user canceled it, so they know
        title = game.title if game else "Install"
        _notify(f"{title}: {'installed' if status == 'success' else status}", message)


def _pick_drive(db: Session, console: Console, job: InstallJob, drives: list[DriveInfo], needed: int) -> DriveInfo:
    live = {d.external_id: d for d in drives}
    if job.target_drive_id:
        row = db.get(Drive, job.target_drive_id)
        candidates = [live[row.external_id]] if row and row.external_id in live else []
        if not candidates:
            raise AdapterError("drive_not_found", "The chosen drive isn't connected to the console.")
    else:
        targets = {d.external_id for d in db.scalars(select(Drive).where(Drive.console_id == console.id, Drive.is_install_target))}
        candidates = [d for d in drives if d.external_id in targets]
        if not candidates:
            raise AdapterError("no_target_drive", "No drive on this console is set as an install target.")
    if all(d.total_bytes == 0 for d in candidates):
        raise AdapterError("space_unknown", "The app can't see free space without the helper. Install the helper, then try again.")
    fits = [d for d in candidates if d.free_bytes >= needed]
    if not fits:
        raise AdapterError("no_space", f"Not enough free space: needs {needed / 1024**3:.1f} GiB.")
    return max(fits, key=lambda d: d.free_bytes)


def _progress(job_id: str, started: float):
    """Callback for the transfer thread: writes progress to the job row about once a second.
    The rate is the current speed: each second's, smoothed so it doesn't jump with every chunk."""
    last, last_done, rate = started, 0, 0.0

    def report(done: int, total: int):
        nonlocal last, last_done, rate
        t = time.monotonic()
        if t - last < 1 and done < total:
            return
        now_rate = (done - last_done) / max(t - last, 0.001)
        rate = now_rate if not rate else 0.3 * now_rate + 0.7 * rate
        last, last_done = t, done
        rate = int(rate)
        with SessionLocal() as db:
            job = db.get(InstallJob, job_id)
            job.bytes_transferred, job.total_bytes = done, total
            job.progress = done / total if total else 1.0
            job.transfer_rate_bps = rate
            job.eta_seconds = int((total - done) / rate) if rate else None
            db.commit()
    return report


async def run(job_id: str):
    with SessionLocal() as db:
        job = db.get(InstallJob, job_id)
        console, game = db.get(Console, job.console_id), db.get(Game, job.game_id)
        stop = _cancel.setdefault(job.id, threading.Event())
        try:
            job.status, job.started_at, job.message = "preflight", now(), "Checking the console."
            db.commit()
            if not console or not game:
                raise AdapterError("not_found", "The console or game was deleted.")
            if not os.path.exists(game.source_path):
                raise AdapterError("source_missing", f"Can't read {game.source_path}.")
            if inventory.status.get(console.id, {}).get("state") != "online":
                raise AdapterError("console_offline", "The console isn't online.")
            adapter = inventory.adapter_for(db, console)
            source, needed = game.source_path, game.size_bytes
            pkg = game.format in PKG_FORMATS
            if pkg:
                needed *= 2  # the staged package and the installed game; ponytail: the console installs to its own drive
            if game.format == "archive":
                job.status, job.message = "extracting", "Reading the archive."
                db.commit()
                archive, needed = await asyncio.to_thread(archives.prepare, source, job.id, stop)
            dlc = game.kind == "dlc"  # goes into the installed game's folder, on that game's drive
            if not dlc:
                drive = _pick_drive(db, console, job, await adapter.list_drives(), needed)
                job.target_drive_id = db.scalar(select(Drive.id).where(Drive.console_id == console.id,
                                                                       Drive.external_id == drive.external_id))
            if game.format == "archive":
                job.message = "Extracting on this PC."
                db.commit()
                name = scanner.VOLUME_KEY_RE.sub("", os.path.basename(source)) or "game"
                item = await asyncio.to_thread(archives.extract, archive, needed, name, job.id,
                                               _progress(job.id, time.monotonic()), stop)
                db.refresh(job)
                _check_contents(db, console, game, item)
                source = item.source_path
                if dlc and item.detected_format != "unlocker":
                    raise AdapterError("archive_contents", "This archive doesn't hold a DLC unlocker folder.")
            if dlc:
                await _add_unlocker(db, adapter, console, job, game, source)
                return
            job.status, job.message = "transferring", (f"Copying to {drive.label}, then installing it." if pkg
                                                        else f"Copying to {drive.label}.")
            db.commit()

            progress = _progress(job.id, time.monotonic())
            staged = await (adapter.install_package(source, progress, drive, stop) if pkg
                            else adapter.stage_shadowmount_content(source, drive, progress, stop))

            db.refresh(job)
            job.status, job.message = "registering", "Reading the console's games."
            db.commit()
            try:
                await inventory.refresh(db, console)
            except AdapterError:
                pass  # the copy is done; the next inventory read will show it
            checked = f" All {staged.checked_files} files checked." if staged and staged.checked_files else ""
            _finish(db, job, "success", f"Installed on {drive.label}.{checked}")
            if game.title_id:
                _spawn(_add_logo(job.id, console.id, game.title_id))
        except AdapterError as e:
            db.rollback()
            db.refresh(job)
            status = {"canceled": "canceled", "connection_lost": "interrupted"}.get(e.code, "failed")
            _finish(db, job, status, e.message, {"error": e.code})
        except Exception as e:  # a bug must fail the job, not leave it "running" or stop the worker
            log.exception("install job %s", job_id)
            db.rollback()
            _finish(db, db.get(InstallJob, job_id), "failed", f"Unexpected error: {e}", {"error": "internal"})
        finally:
            _cancel.pop(job_id, None)
            await asyncio.to_thread(archives.clean, job_id)


LOGO_WAIT_SECONDS = 180  # how long ShadowMountPlus gets to add a new game to the home screen
LOGO_RETRY_SECONDS = 10


async def _logo_once_listed(db: Session, console: Console, title_id: str) -> str:
    """Once ShadowMountPlus has added the game to the home screen, puts its PlayStation Store logo there too
    (logos.apply). Returns a note for the install's or move's message."""
    deadline = time.monotonic() + LOGO_WAIT_SECONDS
    while True:
        try:
            await logos.apply(db, console, title_id)
            ok, note = True, "Its logo is on the home screen."
        except AdapterError as e:
            if e.code == "not_found" and time.monotonic() < deadline:  # not on the home screen yet
                await asyncio.sleep(LOGO_RETRY_SECONDS)
                continue
            ok, note = False, f"No home-screen logo: {e.message}"
        except Exception as e:  # a bug must not leave the task crashing silently
            log.exception("home-screen logo for %s", title_id)
            ok, note = False, f"No home-screen logo: unexpected error: {e}"
        logos.audit(db, console, title_id, ok, note)
        db.commit()
        return note


async def _add_logo(job_id: str, console_id: str, title_id: str):
    """After an install. The install is already done, so this only adds a note to the job's message."""
    with SessionLocal() as db:
        note = await _logo_once_listed(db, db.get(Console, console_id), title_id)
        job = db.get(InstallJob, job_id)
        if job:
            job.message = f"{job.message} {note}"
        db.commit()


async def _add_unlocker(db: Session, adapter, console: Console, job: InstallJob, game: Game, source: str):
    """Copies a DLC unlocker over the installed game's folder. Its files replace the game's own ones of the same name."""
    row, why = unlocker_target(db, console, game.title_id)
    if not row:
        raise AdapterError("game_not_installed", why)
    job.target_drive_id = row.drive_id
    job.status, job.message = "transferring", f"Copying into {row.source_path}."
    db.commit()
    target = InstalledGameInfo(row.title_id, row.title, None, "shadowmountplus", None, row.source_path, row.source_path, 0)
    await adapter.add_to_game_folder(source, target, _progress(job.id, time.monotonic()), _cancel[job.id])
    db.refresh(job)
    _finish(db, job, "success", f"Added to {row.title}. Start the game to use the DLC.")


def _check_contents(db: Session, console: Console, game: Game, item: scanner.Item):
    """What the archive really holds: its param.json title ID replaces one guessed from the file name."""
    tid = item.title_id
    if game.metadata_source == "manual" and game.title_id and tid and tid != game.title_id:
        raise AdapterError("title_mismatch", f"The archive holds {tid}, but this game is set to {game.title_id}.")
    if tid and item.metadata_confidence == "high" and game.metadata_source != "manual":
        game.title_id, game.metadata_source, game.metadata_confidence = tid, item.metadata_source, "high"
        game.version = item.version or game.version
    if game.kind == "game" and tid and db.scalar(select(InstalledGame.id).where(InstalledGame.console_id == console.id,
                                                                                InstalledGame.title_id == tid)):
        raise AdapterError("already_installed", f"{tid} is already installed on this console.")


def recover():
    """Jobs that were running when the app stopped can't be resumed: mark them interrupted, and drop their staging."""
    archives.clean()
    with SessionLocal() as db:
        for job in db.scalars(select(InstallJob).where(InstallJob.status.in_(RUNNING))):
            _finish(db, job, "interrupted", "The app was closed during the install. Queue it again to retry.",
                    {"error": "app_restarted"})


async def worker():
    recover()
    while True:
        # ponytail: one job at a time across all consoles (spec default is one per console); fine for one PS5.
        with SessionLocal() as db:
            job_id = db.scalar(select(InstallJob.id).where(InstallJob.status == "queued").order_by(InstallJob.created_at))
        if job_id:
            await run(job_id)
        else:
            await asyncio.sleep(1)


# ---------------------------------------------------------------- removal (project.md §13)

# ponytail: removals live in memory, with the audit log as the lasting record. The removal_jobs table's foreign key
# would block dropping the installed_games row once the game is gone. A restart forgets previews and progress.
removals: dict[str, dict] = {}  # removal id -> preview, then status: awaiting_confirmation|removing|success|failed
_tasks: set[asyncio.Task] = set()  # keeps running removals and logo tasks from being garbage collected
PREVIEW_SECONDS = 600  # a preview older than this must be made again


def active_removal(installed_game_id: str) -> dict | None:
    return next((r for r in removals.values() if r["installed_game_id"] == installed_game_id and r["status"] == "removing"), None)


def removing(installed_game_id: str) -> bool:
    return active_removal(installed_game_id) is not None


def leftovers_blocker(db: Session, console_id: str) -> str | None:
    """Why the console's hidden .partial copies can't be cleaned up now: an install, move or removal may be writing one."""
    if db.scalar(select(InstallJob.id).where(InstallJob.console_id == console_id, InstallJob.status.in_(ACTIVE))):
        return "An install to this console is queued or running. Clean up once it's done."
    if any(x["console_id"] == console_id and x["status"] in ("moving", "removing") for x in (*moves.values(), *removals.values())):
        return "A game on this console is being moved or removed. Clean up once that's done."
    return None


def removal_blocker(db: Session, row: InstalledGame) -> str | None:
    if row.install_route != "shadowmountplus" or not row.source_path:
        return "Only games installed through ShadowMountPlus can be removed from here. Delete this one on the PS5."
    if moving(row.id):
        return "It's being moved to another drive. Wait for that to finish."
    if db.scalar(select(InstallJob.id).join(Game, Game.id == InstallJob.game_id).where(
            InstallJob.console_id == row.console_id, InstallJob.status.in_(ACTIVE), Game.title_id == row.title_id)):
        return "An install of this game is queued or running. Cancel it first."
    if removing(row.id):
        return "It's already being removed."
    return None


def preview(db: Session, row: InstalledGame) -> dict:
    """Exactly what removing would delete. Removing then needs this preview's id and the title ID typed back."""
    lib = db.get(Game, row.game_id) if row.game_id else None
    reason = removal_blocker(db, row)
    orphaned = row.status == "orphaned_icon"
    warnings = ["If the game is running on the PS5, close it first."]
    if orphaned:
        warnings = ["Its files are already gone from the console, so this only removes its home-screen tile."]
    elif (row.source_path or "").lower().endswith(IMAGE_EXTS):
        warnings.append("If ShadowMountPlus has the disc image mounted, the space may only come back after the PS5 restarts.")
    p = {"id": _id(), "console_id": row.console_id, "installed_game_id": row.id, "title_id": row.title_id,
         "title": row.title, "install_route": row.install_route, "source_path": row.source_path,
         "icon_removal_required": True, "orphaned": orphaned,
         "estimated_bytes_freed": 0 if orphaned else row.size_bytes or (lib.size_bytes if lib else 0),
         "save_data_retained": True, "library_backup_retained": True, "warnings": warnings,
         "allowed": reason is None, "reason": reason or "",
         "status": "awaiting_confirmation" if reason is None else "blocked", "message": "", "created": time.monotonic(),
         "progress": 0.0, "bytes_done": 0, "bytes_total": 0, "tile_left": False}
    if reason is None:
        removals[p["id"]] = p
    return p


def start_removal(db: Session, console: Console, removal_id: str, confirm_title_id: str) -> dict:
    """Checks the preview still holds, then deletes in the background. Raises ValueError with the reason (audited)."""
    r = removals.get(removal_id)
    try:
        if not r or r["console_id"] != console.id or r["status"] != "awaiting_confirmation" \
                or time.monotonic() - r["created"] > PREVIEW_SECONDS:
            raise ValueError("This preview has expired. Choose Remove again.")
        if confirm_title_id.strip().upper() != r["title_id"]:
            raise ValueError(f"Type {r['title_id']} to confirm.")
        row = db.get(InstalledGame, r["installed_game_id"])
        if not row or (row.source_path, row.title_id) != (r["source_path"], r["title_id"]):
            raise ValueError("The game changed on the console since the preview. Choose Remove again.")
        if reason := removal_blocker(db, row):
            raise ValueError(reason)
    except ValueError as e:
        db.add(AuditLog(action="uninstall", console_id=console.id, title_id=r and r["title_id"],
                        target_path=r and r["source_path"], result="blocked", details={"removal": removal_id, "reason": str(e)}))
        db.commit()
        raise
    r["status"], r["message"] = "removing", "Deleting the game's files from the console."
    _spawn(_remove(removal_id))
    return r


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _remove(removal_id: str):
    r = removals[removal_id]
    with SessionLocal() as db:
        console = db.get(Console, r["console_id"])
        game = InstalledGameInfo(r["title_id"], r["title"], None, "shadowmountplus", None,
                                 r["source_path"], r["source_path"], 0)
        def progress(done: int, total: int):  # called from the transfer thread; plain assignments
            r["bytes_done"], r["bytes_total"], r["progress"] = done, total, done / total if total else 1.0

        try:
            if not console:
                raise AdapterError("not_found", "The console was deleted.")
            adapter = inventory.adapter_for(db, console)
            if r["orphaned"]:  # files already gone; the helper re-checks that before removing the tile
                result = RemovalResult(True, "")
                progress(0, 0)
            else:
                result = await adapter.remove_shadowmount_source(game, progress)
            if not result.ok:
                raise AdapterError("failed", result.message)
            r["message"] = "Removing its tile from the home screen."
            tile = await adapter.remove_home_screen_icon(r["title_id"])
            try:
                await inventory.refresh(db, console)
            except AdapterError:
                pass  # the files are gone; the next inventory read drops the game
            freed = f"Freed {result.bytes_freed / 1e9:.1f} GB. " if result.bytes_freed else ""
            r["status"], r["tile_left"] = "success", not tile.ok
            r["message"] = (f"Removed. {freed}Its tile is gone from the home screen too." if tile.ok else
                            f"Removed. {freed}Its tile is still on the PS5 home screen: delete it there (Options, then "
                            f"Delete). It won't come back. ({tile.message})")
        except AdapterError as e:
            r["status"], r["message"] = "failed", e.message
        except Exception as e:  # a bug must fail the removal, not leave it "removing"
            log.exception("removal %s", removal_id)
            r["status"], r["message"] = "failed", f"Unexpected error: {e}"
        db.add(AuditLog(action="uninstall", console_id=r["console_id"], title_id=r["title_id"], target_path=r["source_path"],
                        result="success" if r["status"] == "success" else "failed",
                        details={"removal": removal_id, "message": r["message"], "bytes_freed": r["bytes_total"],
                                 "tile_left": r["tile_left"]}))
        db.commit()


# ---------------------------------------------------------------- moving a game to another drive on the console

# ponytail: in memory like removals; the audit log is the lasting record. One at a time per console: the helper copies one.
moves: dict[str, dict] = {}  # move id -> status: moving|success|failed|canceled, progress, message
_move_stop: dict[str, threading.Event] = {}


def moving(installed_game_id: str) -> bool:
    return any(m["installed_game_id"] == installed_game_id and m["status"] == "moving" for m in moves.values())


def move_blocker(db: Session, row: InstalledGame, drive: Drive | None) -> str | None:
    if row.install_route != "shadowmountplus" or not row.source_path:
        return "Only games installed through ShadowMountPlus can be moved."
    if row.status == "orphaned_icon":
        return "Its files are already gone from the console, so there's nothing to move."
    if not drive or drive.console_id != row.console_id:
        return "That drive isn't on this console."
    if drive.id == row.drive_id:
        return "It's already on that drive."
    if reason := removal_blocker(db, row):
        return reason
    if any(m["console_id"] == row.console_id and m["status"] == "moving" for m in moves.values()):
        return "Another game is being moved on this console. Wait for it to finish."
    lib = db.get(Game, row.game_id) if row.game_id else None
    needed = row.size_bytes or (lib.size_bytes if lib else 0)
    if needed and drive.total_bytes and drive.free_bytes < needed:
        return f"Not enough free space on {drive.label}: it needs {needed / 1024**3:.1f} GiB."
    return None


def start_move(db: Session, console: Console, installed_game_id: str, drive_id: str) -> dict:
    """Raises ValueError with the reason when the game can't be moved there."""
    row, drive = db.get(InstalledGame, installed_game_id), db.get(Drive, drive_id)
    if not row or row.console_id != console.id:
        raise ValueError("That game isn't on this console.")
    if reason := move_blocker(db, row, drive):
        raise ValueError(reason)
    if inventory.status.get(console.id, {}).get("state") != "online":
        raise ValueError("The console isn't online.")
    move_id = _id()
    m = moves[move_id] = {"id": move_id, "console_id": console.id, "installed_game_id": row.id, "title_id": row.title_id, "title": row.title,
                        "source_path": row.source_path, "drive_external_id": drive.external_id, "drive_label": drive.label,
                        "status": "moving", "progress": 0.0, "bytes_done": 0, "bytes_total": 0, "tile_left": False,
                        "message": "Copying on the console. Then the copy is checked and the original deleted."}
    _move_stop[move_id] = threading.Event()
    _spawn(_move(move_id))
    return m


def cancel_move(move_id: str) -> bool:
    if (m := moves.get(move_id)) and m["status"] == "moving":
        _move_stop[move_id].set()
        return True
    return False


async def _move(move_id: str):
    m = moves[move_id]

    def progress(done: int, total: int):  # called from the adapter's thread; plain assignments
        m["bytes_done"], m["bytes_total"], m["progress"] = done, total, done / total if total else 0.0

    with SessionLocal() as db:
        console = db.get(Console, m["console_id"])
        game = InstalledGameInfo(m["title_id"], m["title"], None, "shadowmountplus", None, m["source_path"], m["source_path"], 0)
        dest = None
        try:
            if not console:
                raise AdapterError("not_found", "The console was deleted.")
            dest, tile = await inventory.adapter_for(db, console).move_shadowmount_source(
                game, m["drive_external_id"], progress, _move_stop[move_id])
            m["message"] = "Reading the console's games."
            try:
                await inventory.refresh(db, console)
            except AdapterError:
                pass  # the move is done; the next inventory read shows it
            m["status"], m["tile_left"] = "success", not tile.ok
            m["message"] = (f"Moved to {m['drive_label']}." if tile.ok else
                            f"Moved to {m['drive_label']}, but its old tile is still on the PS5 home screen: delete it there "
                            f"(Options, then Delete), and ShadowMountPlus adds the game again from its new drive. ({tile.message})")
        except AdapterError as e:
            m["status"], m["message"] = ("canceled" if e.code == "canceled" else "failed"), e.message
        except Exception as e:  # a bug must fail the move, not leave it "moving"
            log.exception("move %s", move_id)
            m["status"], m["message"] = "failed", f"Unexpected error: {e}"
        finally:
            _move_stop.pop(move_id, None)
        db.add(AuditLog(action="move", console_id=m["console_id"], title_id=m["title_id"], target_path=m["source_path"],
                        result={"success": "success", "canceled": "canceled"}.get(m["status"], "failed"),
                        details={"to": dest or m["drive_label"], "message": m["message"]}))
        db.commit()
        if m["status"] != "canceled":
            _notify(f"{m['title']}: {'moved' if m['status'] == 'success' else 'move failed'}", m["message"])
        if m["status"] == "success" and not m["tile_left"]:
            m["message"] += " " + await _logo_once_listed(db, console, m["title_id"])


# ---------------------------------------------------------------- save data backups (read from the console only)

# ponytail: in memory like removals; the backups themselves are folders in SAVE_BACKUP_DIR, listed from disk.
save_backups: dict[str, dict] = {}  # backup id -> status: copying|success|failed, progress, message, folder


def backup_folder(title_id: str, console: Console) -> Path:
    stamp = now().astimezone().strftime("%Y-%m-%d %H%M%S")
    name = re.sub(r'[<>:"/\\|?*]', "_", console.name).strip(" .") or "console"
    return config.SAVE_BACKUP_DIR / title_id / f"{stamp} {name}"


def list_save_backups(title_id: str) -> list[dict]:
    """This title's backups on this PC, newest first."""
    folder = config.SAVE_BACKUP_DIR / title_id
    if not scanner.TITLE_ID_RE.fullmatch(title_id) or not folder.is_dir():
        return []
    out = []
    for d in sorted(folder.iterdir(), reverse=True):
        if d.is_dir():
            files = [f for f in d.rglob("*") if f.is_file()]
            out.append({"name": d.name, "path": str(d), "files": len(files), "bytes": sum(f.stat().st_size for f in files)})
    return out


def start_save_backup(db: Session, console: Console, title_id: str) -> dict:
    if inventory.status.get(console.id, {}).get("state") != "online":
        raise ValueError("The console isn't online.")
    if any(b["status"] == "copying" and b["console_id"] == console.id for b in save_backups.values()):
        raise ValueError("A backup from this console is already running.")
    backup_id = _id()
    b = save_backups[backup_id] = {"id": backup_id, "console_id": console.id, "title_id": title_id, "status": "copying",
                                   "progress": 0.0, "bytes_done": 0, "bytes_total": 0, "files": 0,
                                   "folder": str(backup_folder(title_id, console)), "message": "Looking for save data."}
    _spawn(_save_backup(backup_id))
    return b


async def _save_backup(backup_id: str):
    b = save_backups[backup_id]
    dest = Path(b["folder"])

    def progress(done: int, total: int):  # called from the adapter's thread; plain assignments
        b["bytes_done"], b["bytes_total"], b["progress"] = done, total, done / total if total else 1.0
        b["message"] = "Copying save data to this PC."

    with SessionLocal() as db:
        console = db.get(Console, b["console_id"])
        try:
            if not console:
                raise AdapterError("not_found", "The console was deleted.")
            b["files"] = await inventory.adapter_for(db, console).backup_saves(b["title_id"], dest, progress)
            b["status"] = "success"
            b["message"] = f"Backed up {b['files']} file(s), {b['bytes_total'] / 1e6:.1f} MB, to {dest}."
        except AdapterError as e:
            b["status"], b["message"] = "failed", e.message
        except OSError as e:
            b["status"], b["message"] = "failed", f"Couldn't write the backup: {e.strerror or e}"
        except Exception as e:  # a bug must fail the backup, not leave it "copying"
            log.exception("save backup %s", backup_id)
            b["status"], b["message"] = "failed", f"Unexpected error: {e}"
        if b["status"] != "success":
            await asyncio.to_thread(shutil.rmtree, dest, True)  # a half backup must not pass for a whole one
        db.add(AuditLog(action="save_backup", console_id=b["console_id"], title_id=b["title_id"], target_path=str(dest),
                        result="success" if b["status"] == "success" else "failed",
                        details={"files": b["files"], "bytes": b["bytes_total"], "message": b["message"]}))
        db.commit()


# ---------------------------------------------------------------- folder dump -> .exfat image (exfat.py)

# ponytail: conversions live in memory like removals; the audit log is the lasting record. One at a time: they're disk-bound.
conversions: dict[str, dict] = {}  # game id -> status: converting|success|failed|canceled, progress, message
_convert_stop: dict[str, threading.Event] = {}
CARRIED = ("title", "title_id", "metadata_source", "metadata_confidence", "content_id", "version", "region", "description",
           "genre", "release_date", "owned_copy_type", "backup_date", "provenance_notes", "favorite")


def image_path(game: Game) -> Path:
    """Next to the folder, so the next scan finds it. Disc images get their title ID from the file name."""
    src = Path(game.source_path)
    name = src.name if not game.title_id or scanner.TITLE_ID_RE.search(src.name) else f"{src.name} [{game.title_id}]"
    return src.with_name(name + ".exfat")


def convert_blocker(game: Game) -> str | None:
    if game.format != "folder_dump":
        return "Only folder dumps can be converted to a disc image."
    if game.missing or not os.path.isdir(game.source_path):
        return f"Can't read {game.source_path}."
    if game.detection_evidence.get("partial"):
        return "The folder looks like an incomplete copy."
    if any(c["status"] == "converting" for c in conversions.values()):
        return "Another game is being converted. Wait for it to finish."
    if image_path(game).exists():
        return f"{image_path(game).name} already exists next to the folder."
    return None


def start_convert(game: Game) -> dict:
    """Raises ValueError with the reason when the game can't be converted."""
    if reason := convert_blocker(game):
        raise ValueError(reason)
    c = conversions[game.id] = {"status": "converting", "progress": 0.0, "bytes_done": 0, "bytes_total": game.size_bytes,
                                "message": "Writing the disc image.", "image_path": str(image_path(game)), "image_game_id": None}
    _convert_stop[game.id] = threading.Event()
    _spawn(_convert(game.id))
    return c


def cancel_convert(game_id: str) -> bool:
    if (c := conversions.get(game_id)) and c["status"] == "converting":
        _convert_stop[game_id].set()
        return True
    return False


async def _convert(game_id: str):
    c = conversions[game_id]

    def progress(done: int, total: int):  # called from the build thread; plain assignments
        c["bytes_done"], c["bytes_total"], c["progress"] = done, total, done / total if total else 1.0

    with SessionLocal() as db:
        game = db.get(Game, game_id)
        dest, source, title_id = Path(c["image_path"]), game.source_path, game.title_id
        try:
            size = await asyncio.to_thread(exfat.build, Path(source), dest, progress, _convert_stop[game_id], title_id or "")
            c["message"] = "Adding it to the library."
            while await asyncio.to_thread(scanner.run_scan_locked, SessionLocal) is None:
                await asyncio.sleep(1)  # a scan was already running and may have missed the new file
            db.expire_all()
            image = db.scalar(select(Game).where(Game.source_path == str(dest)))
            if image and (game := db.get(Game, game_id)):
                _carry_over(game, image)
                db.flush()
                scanner.refresh_statuses(db)
                c["image_game_id"] = image.id
            c["status"] = "success"
            c["message"] = (f"Done: {dest.name} ({size / 1e9:.1f} GB). The folder is still there; delete it yourself "
                            f"once you've checked the image.")
        except AdapterError as e:
            c["status"] = "canceled" if e.code == "canceled" else "failed"
            c["message"] = e.message
        except OSError as e:
            c["status"], c["message"] = "failed", f"Couldn't write the image: {e.strerror or e}"
        except Exception as e:  # a bug must fail the conversion, not leave it "converting"
            log.exception("conversion of %s", game_id)
            c["status"], c["message"] = "failed", f"Unexpected error: {e}"
        finally:
            _convert_stop.pop(game_id, None)
        if c["status"] != "canceled":
            _notify(f"{dest.name}: {'converted' if c['status'] == 'success' else 'conversion failed'}", c["message"])
        db.add(AuditLog(action="convert", title_id=title_id, target_path=str(dest),
                        result={"success": "success", "canceled": "canceled"}.get(c["status"], "failed"),
                        details={"source": source, "message": c["message"]}))
        db.commit()


def _carry_over(folder: Game, image: Game):
    """The image holds the same game: it keeps the folder's title ID (from its param.json) and everything the user set."""
    for f in CARRIED:
        setattr(image, f, getattr(folder, f))
    if folder.cover_path and os.path.isfile(folder.cover_path):
        cover = Path(folder.cover_path)
        uploaded = cover.parent == config.COVERS_DIR  # only uploaded covers hide the PlayStation Store poster
        copy = config.COVERS_DIR / f"{image.id}{'' if uploaded else '-icon0'}{cover.suffix.lower()}"
        shutil.copyfile(cover, copy)
        image.cover_path = str(copy)
