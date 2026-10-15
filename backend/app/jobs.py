"""Install jobs (project.md §10): a queue in the install_jobs table and one worker that runs them.
Only the ShadowMountPlus route exists: folder dumps and disc images are copied into a drive's homebrew folder.
Removals (project.md §13) delete that copy again, after a preview and the title ID typed back."""
import asyncio
import logging
import os
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import inventory
from .consoles import IMAGE_EXTS, AdapterError, DriveInfo, InstalledGameInfo
from .database import SessionLocal
from .models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob, _id, now

ACTIVE = ("queued", "preflight", "transferring", "registering")
RUNNING = ACTIVE[1:]
_cancel: dict[str, threading.Event] = {}  # job id -> set to stop its transfer
log = logging.getLogger(__name__)


def blocker(db: Session, console: Console, game: Game) -> str | None:
    """Why this game can't be queued for this console, or None. Console-side checks (online, space) run at preflight."""
    if game.format in ("ps4_pkg", "ps5_pkg"):
        return "This console has no package installer. Only folder dumps and disc images can be installed."
    if game.format == "archive":
        return "Extract the archive first, then scan again."
    if game.format not in ("folder_dump", "image"):
        return "Format not recognized."
    if game.compatibility_status == "incompatible":
        return game.compatibility_reason
    if game.title_id and db.scalar(select(InstalledGame.id).where(
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
    """Callback for the transfer thread: writes progress to the job row about once a second."""
    last = 0.0

    def report(done: int, total: int):
        nonlocal last
        t = time.monotonic()
        if t - last < 1 and done < total:
            return
        last = t
        rate = int(done / max(t - started, 0.001))
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
            drive = _pick_drive(db, console, job, await adapter.list_drives(), game.size_bytes)
            job.target_drive_id = db.scalar(select(Drive.id).where(Drive.console_id == console.id,
                                                                   Drive.external_id == drive.external_id))
            job.status, job.message = "transferring", f"Copying to {drive.label}."
            db.commit()

            await adapter.stage_shadowmount_content(game.source_path, drive, _progress(job.id, time.monotonic()), stop)

            db.refresh(job)
            job.status, job.message = "registering", "Reading the console's games."
            db.commit()
            try:
                await inventory.refresh(db, console)
            except AdapterError:
                pass  # the copy is done; the next inventory read will show it
            _finish(db, job, "success", f"Installed on {drive.label}.")
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


def recover():
    """Jobs that were running when the app stopped can't be resumed: mark them interrupted."""
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
_tasks: set[asyncio.Task] = set()  # keeps running removals from being garbage collected
PREVIEW_SECONDS = 600  # a preview older than this must be made again


def removing(installed_game_id: str) -> bool:
    return any(r["installed_game_id"] == installed_game_id and r["status"] == "removing" for r in removals.values())


def removal_blocker(db: Session, row: InstalledGame) -> str | None:
    if row.install_route != "shadowmountplus" or not row.source_path:
        return "Only games installed through ShadowMountPlus can be removed from here. Delete this one on the PS5."
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
    warnings = ["If the game is running on the PS5, close it first."]
    if (row.source_path or "").lower().endswith(IMAGE_EXTS):
        warnings.append("ShadowMountPlus keeps disc images mounted, so the space may only come back after the PS5 restarts.")
    p = {"id": _id(), "console_id": row.console_id, "installed_game_id": row.id, "title_id": row.title_id,
         "title": row.title, "install_route": row.install_route, "source_path": row.source_path,
         "icon_removal_required": True, "estimated_bytes_freed": row.size_bytes or (lib.size_bytes if lib else 0),
         "save_data_retained": True, "library_backup_retained": True, "warnings": warnings,
         "allowed": reason is None, "reason": reason or "",
         "status": "awaiting_confirmation" if reason is None else "blocked", "message": "", "created": time.monotonic()}
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
    task = asyncio.create_task(_remove(removal_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return r


async def _remove(removal_id: str):
    r = removals[removal_id]
    with SessionLocal() as db:
        console = db.get(Console, r["console_id"])
        game = InstalledGameInfo(r["title_id"], r["title"], None, "shadowmountplus", None,
                                 r["source_path"], r["source_path"], 0)
        try:
            if not console:
                raise AdapterError("not_found", "The console was deleted.")
            result = await inventory.adapter_for(db, console).remove_shadowmount_source(game, lambda done, total: None)
            if not result.ok:
                raise AdapterError("failed", result.message)
            try:
                await inventory.refresh(db, console)
            except AdapterError:
                pass  # the files are gone; the next inventory read drops the game
            r["status"] = "success"
            r["message"] = "Removed. Its tile stays on the PS5 home screen until you delete it there, and won't come back."
        except AdapterError as e:
            r["status"], r["message"] = "failed", e.message
        except Exception as e:  # a bug must fail the removal, not leave it "removing"
            log.exception("removal %s", removal_id)
            r["status"], r["message"] = "failed", f"Unexpected error: {e}"
        db.add(AuditLog(action="uninstall", console_id=r["console_id"], title_id=r["title_id"], target_path=r["source_path"],
                        result="success" if r["status"] == "success" else "failed",
                        details={"removal": removal_id, "message": r["message"],
                                 "estimated_bytes_freed": r["estimated_bytes_freed"]}))
        db.commit()
