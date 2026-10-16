import asyncio
import hashlib
import hmac
import ipaddress
import os
import socket
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from PIL import Image, ImageOps
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, config, credentials, inventory, jobs, logos, scanner
from .consoles import CONSOLE_APP_DIR, TITLE_ID, AdapterError
from .database import Base, SessionLocal, engine, get_db
from .models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob, LibraryRoot, _id


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    # ponytail: create_all instead of Alembic; add migrations before the first schema change after v1.
    Base.metadata.create_all(engine)
    tasks = [asyncio.create_task(jobs.worker())]
    if config.SCAN_INTERVAL_MINUTES > 0:
        tasks.append(asyncio.create_task(_scheduled_scans()))
    if config.CONSOLE_CHECK_SECONDS > 0:
        tasks.append(asyncio.create_task(inventory.monitor()))
    yield
    for task in tasks:
        task.cancel()


async def _scheduled_scans():
    # ponytail: interval rescans only; watchdog events are unreliable on SMB/NFS mounts anyway.
    while True:
        await asyncio.sleep(config.SCAN_INTERVAL_MINUTES * 60)
        await asyncio.to_thread(scanner.run_scan_locked, SessionLocal)


APP_ID = "ps5-local-storefront"
app = FastAPI(title="PS5 Local Storefront", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, same_site="strict",
                   session_cookie="ps5sf_session", max_age=7 * 24 * 3600)


def _direct_host(host: str) -> bool:
    """The app is opened by IP (or localhost), never by a domain name. Refusing domain names stops DNS rebinding:
    a website pointing its own name at 127.0.0.1 to reach this app from the browser, e.g. to claim first-run setup."""
    name = urllib.parse.urlsplit(f"//{host}").hostname or ""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return name == "localhost"


@app.middleware("http")
async def check_host(request: Request, call_next):
    if not _direct_host(request.headers.get("host", "")):
        return JSONResponse({"error": "bad_host", "message": "Open the app by its IP address, as the tray icon shows."}, 421)
    return await call_next(request)


@app.middleware("http")
async def revalidate_html(request: Request, call_next):
    """index.html must not be served from browser cache, or a rebuilt app keeps loading the old bundle.
    Hashed assets under /assets are fine to cache."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(StarletteHTTPException)
async def error_body(request: Request, exc: StarletteHTTPException):
    """Errors are {"error": code, "message": text} (project.md §17), not FastAPI's {"detail": ...}."""
    body = exc.detail if isinstance(exc.detail, dict) else {"error": "http_error", "message": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code)


def _password_salt() -> str | None:
    """Stored in each session at login, so resetting the password (deleting its file) logs every device out."""
    try:
        return config.PASSWORD_FILE.read_text().split(":")[0]
    except OSError:
        return None


def _log_in(request: Request):
    request.session.update(user="admin", pw=_password_salt())


def require_user(request: Request):
    if not request.session.get("user") or request.session.get("pw") != _password_salt():
        raise HTTPException(401, {"error": "authentication_required", "message": "Log in first."})


public = APIRouter(prefix="/api/v1")
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


# ---------------------------------------------------------------- auth

_failures: dict[str, list[float]] = {}  # ponytail: in-memory per-IP limiter, resets on restart


class LoginIn(BaseModel):
    password: str


class SetupIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def _check_password(password: str) -> bool:
    salt, digest = config.PASSWORD_FILE.read_text().split(":")
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt)), bytes.fromhex(digest))


def _require_setup_done():
    if not config.PASSWORD_FILE.exists():
        raise HTTPException(409, {"error": "setup_required", "message": "Choose an admin password first."})


@public.post("/auth/setup")
def setup(body: SetupIn, request: Request, db: Session = Depends(get_db)):
    """First run only: sets the admin password. Allowed only from this PC, so nobody on the LAN can claim it first."""
    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, {"error": "local_only", "message": "Set the password on the PC running the app."})
    salt = os.urandom(16)
    try:
        with open(config.PASSWORD_FILE, "x") as f:  # exclusive create: the first setup wins
            f.write(f"{salt.hex()}:{_hash(body.password, salt).hex()}")
    except FileExistsError:
        raise HTTPException(409, {"error": "already_set_up", "message": "A password is already set."})
    db.add(AuditLog(action="setup", result="success", details={}))
    db.commit()
    _log_in(request)
    return {"user": "admin"}


@public.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    _require_setup_done()
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failures.get(ip, []) if time.time() - t < 60]
    if len(recent) >= 5:
        raise HTTPException(429, {"error": "rate_limited", "message": "Too many attempts. Wait a minute."})
    ok = _check_password(body.password)
    db.add(AuditLog(action="login", result="success" if ok else "failed", details={"ip": ip}))
    db.commit()
    if not ok:
        _failures[ip] = recent + [time.time()]
        raise HTTPException(401, {"error": "invalid_credentials", "message": "Wrong password."})
    _failures.pop(ip, None)
    _log_in(request)
    return {"user": "admin"}


@public.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@public.get("/auth/me")
def me(request: Request):
    _require_setup_done()
    require_user(request)
    return {"user": request.session["user"]}


@public.get("/health")
def health():
    return {"status": "ok", "app": APP_ID, "version": __version__}  # the launcher checks `app` to recognise a running copy


# ---------------------------------------------------------------- library

class GameOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    title_id: str | None
    content_id: str | None
    metadata_source: str
    metadata_confidence: str
    parser_warnings: list[str]
    region: str | None
    version: str | None
    format: str
    image_subtype: str | None
    source_path: str
    size_bytes: int
    compressed_size_bytes: int | None
    has_cover: bool = False
    description: str | None
    genre: str | None
    release_date: date | None
    owned_copy_type: str
    backup_date: date | None
    provenance_notes: str | None
    favorite: bool
    verified: bool
    missing: bool
    compatibility_status: str
    compatibility_reason: str
    detection_evidence: dict
    created_at: datetime
    updated_at: datetime
    installed: bool = False  # on any console, per its last storage scan


def _out(g: Game, installed: bool = False) -> GameOut:
    return GameOut.model_validate(g).model_copy(update={"has_cover": bool(g.cover_path), "installed": installed})


def _installed_title_ids(db: Session) -> set[str]:
    return set(db.scalars(select(InstalledGame.title_id)))


class GamePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=1, max_length=300)
    title_id: str | None = Field(None, pattern=r"^(PPSA|CUSA)\d{5}$")
    region: str | None = None
    version: str | None = None
    description: str | None = None
    genre: str | None = None
    release_date: date | None = None
    owned_copy_type: Literal["disc", "digital", "other", "unknown"] | None = None
    backup_date: date | None = None
    provenance_notes: str | None = None
    favorite: bool | None = None


def _game(db: Session, game_id: str) -> Game:
    g = db.get(Game, game_id)
    if not g:
        raise HTTPException(404, {"error": "not_found", "message": "Game not found."})
    return g


@api.get("/games", response_model=list[GameOut])
def list_games(db: Session = Depends(get_db)):
    # ponytail: returns the whole library; filtering is client-side. Paginate past a few thousand titles.
    installed = _installed_title_ids(db)
    return [_out(g, g.title_id in installed) for g in db.scalars(select(Game).order_by(Game.title))]


@api.get("/games/{game_id}", response_model=GameOut)
def get_game(game_id: str, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    return _out(g, g.title_id in _installed_title_ids(db))


@api.patch("/games/{game_id}", response_model=GameOut)
def patch_game(game_id: str, body: GamePatch, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is None or changes.get("owned_copy_type", "") is None:
        raise HTTPException(422, {"error": "invalid_field", "message": "Title and owned copy type can't be empty."})
    for k, v in changes.items():
        setattr(g, k, v)
    if "title_id" in changes:  # user set or confirmed it: trusted over parsers, kept across rescans
        g.metadata_source, g.metadata_confidence = "manual", "medium"
    db.flush()
    scanner.refresh_statuses(db)
    db.commit()
    return _out(g)


@api.delete("/games/{game_id}")
def delete_game(game_id: str, db: Session = Depends(get_db)):
    """Removes the catalog record only. Library files are never touched (mount is read-only)."""
    db.delete(_game(db, game_id))
    db.flush()
    scanner.refresh_statuses(db)
    db.commit()
    return {"ok": True}


def _cover_file(g: Game) -> str:
    """The cover's file. An uploaded one is found in COVERS_DIR by name too, since a data folder copied to another
    PC (or another Windows user) keeps the old absolute path."""
    if g.cover_path and os.path.isfile(g.cover_path):
        return g.cover_path
    moved = config.COVERS_DIR / Path(g.cover_path or "").name
    if g.cover_path and moved.is_file():
        return str(moved)
    raise HTTPException(404, {"error": "not_found", "message": "No cover."})


@api.get("/games/{game_id}/cover")
def get_cover(game_id: str, db: Session = Depends(get_db)):
    return FileResponse(_cover_file(_game(db, game_id)))


@api.get("/games/{game_id}/poster")
def get_poster(game_id: str, db: Session = Depends(get_db)):
    """The game's portrait key art from PlayStation Store (logos.poster). 404 when it has none, or a cover was
    uploaded: the tile then shows the cover."""
    g = _game(db, game_id)
    uploaded = any(config.COVERS_DIR.glob(f"{g.id}.*"))
    if uploaded or not (path := g.title_id and logos.poster(g.title_id, g.content_id)):
        raise HTTPException(404, {"error": "not_found", "message": "No poster."})
    return FileResponse(path)


@api.get("/games/{game_id}/store")
def get_store_info(game_id: str, db: Session = Depends(get_db)):
    """Publisher, release date, genres and ratings from PlayStation Store (logos.store_info); null when it has none."""
    g = _game(db, game_id)
    return g.title_id and logos.store_info(g.title_id) or None


COVER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@api.post("/games/{game_id}/cover", response_model=GameOut)
async def upload_cover(game_id: str, file: UploadFile, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    ext = COVER_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(415, {"error": "unsupported_media", "message": "Use JPEG, PNG or WebP."})
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, {"error": "too_large", "message": "Cover must be under 10 MB."})
    for old in config.COVERS_DIR.glob(f"{g.id}.*"):
        old.unlink()
    dest = config.COVERS_DIR / f"{g.id}{ext}"
    dest.write_bytes(data)
    g.cover_path = str(dest)
    db.commit()
    return _out(g)


@api.post("/library/scan", status_code=202)
def start_scan(background: BackgroundTasks):
    if scanner.scan_state["running"]:
        raise HTTPException(409, {"error": "scan_running", "message": "A scan is already running."})
    background.add_task(scanner.run_scan_locked, SessionLocal)
    return {"started": True}


@api.get("/library/scan-status")
def scan_status():
    return scanner.scan_state


# ---------------------------------------------------------------- library roots

class RootIn(BaseModel):
    path: str
    enabled: bool = True
    exclude_patterns: list[str] = []


class RootPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    exclude_patterns: list[str] | None = None


class RootOut(RootIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    reachable: bool = False


def _root_out(r: LibraryRoot) -> RootOut:
    return RootOut.model_validate(r).model_copy(update={"reachable": os.path.isdir(r.path)})


@api.get("/library/roots", response_model=list[RootOut])
def list_roots(db: Session = Depends(get_db)):
    return [_root_out(r) for r in db.scalars(select(LibraryRoot).order_by(LibraryRoot.path))]


@api.post("/library/roots", response_model=RootOut, status_code=201)
def add_root(body: RootIn, db: Session = Depends(get_db)):
    path = os.path.realpath(body.path)
    if not os.path.isabs(body.path) or not os.path.isdir(path):
        raise HTTPException(400, {"error": "invalid_root", "message": f"Not a readable directory: {body.path}"})
    if db.scalar(select(LibraryRoot).where(LibraryRoot.path == path)):
        raise HTTPException(409, {"error": "duplicate_root", "message": "Root already added."})
    r = LibraryRoot(path=path, enabled=body.enabled, exclude_patterns=body.exclude_patterns)
    db.add(r)
    db.commit()
    return _root_out(r)


@api.patch("/library/roots/{root_id}", response_model=RootOut)
def patch_root(root_id: str, body: RootPatch, db: Session = Depends(get_db)):
    r = db.get(LibraryRoot, root_id) or _not_found()
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    db.commit()
    return _root_out(r)


@api.delete("/library/roots/{root_id}")
def delete_root(root_id: str, db: Session = Depends(get_db)):
    """Forgets the root and its catalog records, except those still covered by another root. Files are never touched."""
    r = db.get(LibraryRoot, root_id) or _not_found()
    others = [o.path for o in db.scalars(select(LibraryRoot).where(LibraryRoot.id != r.id))]
    for g in db.scalars(select(Game)):
        if scanner._is_under(g.source_path, [r.path]) and not scanner._is_under(g.source_path, others):
            db.delete(g)
    db.delete(r)
    db.flush()
    scanner.refresh_statuses(db)
    db.commit()
    return {"ok": True}


@api.get("/library/browse")
def browse(path: str = ""):
    """Subfolders of `path`, for the folder picker. Empty path lists drives (Windows) or `/`."""
    if not path:
        drives = os.listdrives() if os.name == "nt" else ["/"]
        return {"path": "", "parent": None, "dirs": drives}
    path = os.path.realpath(path)
    try:
        with os.scandir(path) as it:
            dirs = sorted((e.name for e in it if e.is_dir() and not scanner.is_system_dir(e.name)), key=str.lower)
    except OSError as e:
        raise HTTPException(400, {"error": "unreadable", "message": f"Can't open {path}: {e.strerror or e}"})
    parent = os.path.dirname(path)
    return {"path": path, "parent": "" if parent == path else parent,
            "dirs": [os.path.join(path, d) for d in dirs]}


# ---------------------------------------------------------------- consoles

ConnectionType = Literal["helper", "ftp", "http", "custom", "mock"]


class ConsoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    ip_address: IPvAnyAddress
    connection_type: ConnectionType
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(None, max_length=100)
    password: str | None = Field(None, max_length=200)
    enabled: bool = True


class ConsolePatch(BaseModel):
    """Omitted fields stay as they are. `password`: a string replaces it, "" or null forgets it."""
    model_config = ConfigDict(extra="forbid")
    # Defaults of None aren't validated, but an explicit null is rejected for these.
    name: str = Field(None, min_length=1, max_length=100)
    ip_address: IPvAnyAddress = None
    connection_type: ConnectionType = None
    port: int = Field(None, ge=1, le=65535)
    username: str | None = Field(None, max_length=100)
    password: str | None = Field(None, max_length=200)
    enabled: bool = None


class ConsoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    ip_address: str
    connection_type: str
    port: int
    username: str | None
    has_password: bool = False
    firmware: str | None
    helper_status: dict
    last_seen_at: datetime | None
    last_inventory_scan_at: datetime | None
    enabled: bool
    state: str = "unknown"  # online|standby|degraded|offline|disabled|unknown (not checked yet)
    status_message: str = ""


def _console_out(c: Console) -> ConsoleOut:
    s = inventory.status.get(c.id, {})
    return ConsoleOut.model_validate(c).model_copy(update={
        "has_password": bool(c.credentials_secret_id),
        "state": s.get("state", "unknown") if c.enabled else "disabled",
        "status_message": s.get("message", ""),
    })


def _audit_console(db: Session, action: str, c: Console, fields: list[str]):
    # Field names only, never values: the password must not reach the audit log.
    db.add(AuditLog(action=action, console_id=c.id, result="success", details={"name": c.name, "fields": fields}))
    if "password" in fields:
        db.add(AuditLog(action="credential_update", console_id=c.id, result="success", details={"name": c.name}))


def _save_console(db: Session, c: Console, changes: dict):
    if "ip_address" in changes:
        changes["ip_address"] = str(changes["ip_address"])
    for k, v in changes.items():
        if k == "password":
            credentials.set_password(db, c, v)
        else:
            setattr(c, k, v)


@api.get("/consoles", response_model=list[ConsoleOut])
def list_consoles(db: Session = Depends(get_db)):
    return [_console_out(c) for c in db.scalars(select(Console).order_by(Console.name))]


@api.post("/consoles", response_model=ConsoleOut, status_code=201)
def add_console(body: ConsoleIn, db: Session = Depends(get_db)):
    c = Console(id=_id())
    db.add(c)
    changes = body.model_dump()
    _save_console(db, c, changes)
    _audit_console(db, "console_create", c, [k for k, v in changes.items() if v is not None])
    db.commit()
    return _console_out(c)


@api.patch("/consoles/{console_id}", response_model=ConsoleOut)
def patch_console(console_id: str, body: ConsolePatch, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    changes = body.model_dump(exclude_unset=True)
    _save_console(db, c, changes)
    _audit_console(db, "console_update", c, list(changes))
    db.commit()
    return _console_out(c)


@api.get("/consoles/{console_id}", response_model=ConsoleOut)
def get_console(console_id: str, db: Session = Depends(get_db)):
    return _console_out(db.get(Console, console_id) or _not_found())


@api.delete("/consoles/{console_id}")
def delete_console(console_id: str, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    if db.scalar(select(InstallJob.id).where(InstallJob.console_id == c.id, InstallJob.status.in_(jobs.ACTIVE))):
        raise HTTPException(409, {"error": "job_active", "message": "Cancel this console's installs first."})
    inventory.forget(db, c.id)
    credentials.set_password(db, c, None)
    _audit_console(db, "console_delete", c, [])
    db.delete(c)
    db.commit()
    return {"ok": True}


@api.get("/consoles/{console_id}/status")
def console_status(console_id: str, db: Session = Depends(get_db)):
    c = _console_out(db.get(Console, console_id) or _not_found())
    return {"state": c.state, "message": c.status_message, "last_seen_at": c.last_seen_at}


@api.post("/consoles/{console_id}/test-connection", response_model=ConsoleOut)
async def test_connection(console_id: str, db: Session = Depends(get_db)):
    """Checks now instead of waiting for the monitor. Reads the inventory too when the console just came online."""
    db.get(Console, console_id) or _not_found()
    await inventory.check(console_id)
    db.expire_all()  # check() wrote through its own session
    return _console_out(db.get(Console, console_id))


@api.post("/consoles/{console_id}/rescan", response_model=ConsoleOut)
async def rescan_console(console_id: str, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    try:
        await inventory.refresh(db, c)
    except AdapterError as e:
        raise HTTPException(502, {"error": e.code, "message": e.message})
    inventory.record(c.id, "online")
    return _console_out(c)


@api.post("/consoles/{console_id}/helper", response_model=ConsoleOut)
async def install_helper(console_id: str, db: Session = Depends(get_db)):
    """Loads the bundled helper payload on the console (replacing an older one), then re-reads the inventory."""
    c = db.get(Console, console_id) or _not_found()
    try:
        version = await inventory.adapter_for(db, c).install_helper()
        await inventory.refresh(db, c)
    except AdapterError as e:
        db.add(AuditLog(action="helper_install", console_id=c.id, result="failed", details={"error": e.code}))
        db.commit()
        raise HTTPException(502, {"error": e.code, "message": e.message})
    db.add(AuditLog(action="helper_install", console_id=c.id, result="success", details={"version": version}))
    db.commit()
    inventory.record(c.id, "online")
    return _console_out(c)


class DriveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    external_id: str
    label: str
    path: str
    drive_type: str
    total_bytes: int
    free_bytes: int
    is_install_target: bool
    last_seen_at: datetime | None
    installed_count: int = 0


class DrivePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_install_target: bool


@api.get("/consoles/{console_id}/drives", response_model=list[DriveOut])
def list_drives(console_id: str, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    counts: dict[str | None, int] = {}
    for drive_id in db.scalars(select(InstalledGame.drive_id).where(InstalledGame.console_id == console_id)):
        counts[drive_id] = counts.get(drive_id, 0) + 1
    drives = db.scalars(select(Drive).where(Drive.console_id == console_id).order_by(Drive.external_id))
    # Only drives seen in the latest inventory; unplugged ones stay in the table to keep their setting.
    return [DriveOut.model_validate(d).model_copy(update={"installed_count": counts.get(d.id, 0)})
            for d in drives if d.last_seen_at == c.last_inventory_scan_at]


@api.patch("/consoles/{console_id}/drives/{drive_id}", response_model=DriveOut)
def patch_drive(console_id: str, drive_id: str, body: DrivePatch, db: Session = Depends(get_db)):
    d = db.get(Drive, drive_id)
    if not d or d.console_id != console_id:
        _not_found()
    d.is_install_target = body.is_install_target
    db.commit()
    return DriveOut.model_validate(d)


class InstalledOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    game_id: str | None
    cover_url: str | None = None  # the library copy's cover, else the icon read from the console
    title_id: str
    title: str
    version: str | None
    library_version: str | None = None
    install_route: str
    drive_id: str | None
    installed_path: str
    source_path: str | None
    size_bytes: int
    status: str
    last_verified_at: datetime | None
    removing: bool = False


@api.get("/consoles/{console_id}/installed", response_model=list[InstalledOut])
def list_installed(console_id: str, db: Session = Depends(get_db)):
    db.get(Console, console_id) or _not_found()
    out = []
    for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == console_id).order_by(InstalledGame.title)):
        lib = db.get(Game, g.game_id) if g.game_id else None
        cover = (f"/api/v1/games/{lib.id}/cover" if lib and lib.cover_path
                 else f"/api/v1/icons/{g.title_id}" if inventory.icon_path(g.title_id).is_file() else None)
        out.append(InstalledOut.model_validate(g).model_copy(update={
            "cover_url": cover, "library_version": lib and lib.version, "removing": jobs.removing(g.id)}))
    return out


# ---------------------------------------------------------------- removal

class RemovalOut(BaseModel):
    id: str
    installed_game_id: str
    title_id: str
    title: str
    install_route: str
    source_path: str | None
    icon_removal_required: bool
    orphaned: bool = False
    estimated_bytes_freed: int
    save_data_retained: bool
    library_backup_retained: bool
    warnings: list[str]
    allowed: bool
    reason: str  # why not, when allowed is false
    status: str  # blocked|awaiting_confirmation|removing|success|failed
    message: str
    progress: float  # 0-1 while deleting files
    bytes_done: int
    bytes_total: int
    tile_left: bool  # done, but its home-screen tile must be deleted on the PS5


class PreviewIn(BaseModel):
    installed_game_id: str


class UninstallIn(BaseModel):
    removal_id: str
    confirm_title_id: str


def _installed(db: Session, console_id: str, installed_game_id: str) -> InstalledGame:
    row = db.get(InstalledGame, installed_game_id)
    if not row or row.console_id != console_id:
        _not_found()
    return row


@api.post("/consoles/{console_id}/uninstall/preview", response_model=RemovalOut)
def uninstall_preview(console_id: str, body: PreviewIn, db: Session = Depends(get_db)):
    return jobs.preview(db, _installed(db, console_id, body.installed_game_id))


@api.post("/consoles/{console_id}/uninstall", response_model=RemovalOut, status_code=202)
async def uninstall(console_id: str, body: UninstallIn, db: Session = Depends(get_db)):
    try:
        return jobs.start_removal(db, db.get(Console, console_id) or _not_found(), body.removal_id, body.confirm_title_id)
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_remove", "message": str(e)})


@api.get("/removal-jobs/{removal_id}", response_model=RemovalOut)
def get_removal(removal_id: str):
    return jobs.removals.get(removal_id) or _not_found()


@api.post("/games/{game_id}/home-logo")
async def set_home_logo(game_id: str, db: Session = Depends(get_db)):
    """Puts the game's title logo from PlayStation Store on its home-screen page, on each console it's installed on."""
    g = _game(db, game_id)
    console_ids = set(db.scalars(select(InstalledGame.console_id).where(InstalledGame.title_id == g.title_id))) if g.title_id else set()
    if not console_ids:
        raise HTTPException(409, {"error": "not_installed", "message": "Install the game on a console first."})
    results = []
    for c in db.scalars(select(Console).where(Console.id.in_(console_ids)).order_by(Console.name)):
        ok, message = True, "Logo set."
        try:
            await logos.apply(db, c, g.title_id)
        except AdapterError as e:
            ok, message = False, e.message
        results.append({"console": c.name, "ok": ok, "message": message})
        logos.audit(db, c, g.title_id, ok, message)
    db.commit()
    return results


@api.get("/icons/{title_id}")
def get_icon(title_id: str):
    """A title's icon as read from a console (inventory.refresh)."""
    path = inventory.icon_path(title_id)
    if not TITLE_ID.fullmatch(title_id) or not path.is_file():
        _not_found()
    return FileResponse(path)


# ---------------------------------------------------------------- installs

class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: str
    drive_id: str | None = None  # None = the install-target drive with the most free space


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    console_id: str
    game_id: str
    title: str = ""
    target_drive_id: str | None
    status: str
    progress: float
    bytes_transferred: int
    total_bytes: int
    transfer_rate_bps: int
    eta_seconds: int | None
    message: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _job_out(db: Session, j: InstallJob) -> JobOut:
    g = db.get(Game, j.game_id)
    return JobOut.model_validate(j).model_copy(update={"title": g.title if g else ""})


def queue_install(db: Session, c: Console, game_id: str, drive_id: str | None = None) -> InstallJob:
    try:
        return jobs.queue(db, c, _game(db, game_id), drive_id)
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_install", "message": str(e)})


@api.post("/consoles/{console_id}/install", response_model=JobOut, status_code=201)
def install(console_id: str, body: InstallIn, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    return _job_out(db, queue_install(db, c, body.game_id, body.drive_id))


@api.get("/consoles/{console_id}/jobs", response_model=list[JobOut])
def list_jobs(console_id: str, db: Session = Depends(get_db)):
    # ponytail: the latest 50 only; add paging if anyone needs older history (the audit log has it).
    rows = db.scalars(select(InstallJob).where(InstallJob.console_id == console_id)
                      .order_by(InstallJob.created_at.desc()).limit(50))
    return [_job_out(db, j) for j in rows]


@api.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _job_out(db, db.get(InstallJob, job_id) or _not_found())


@api.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    j = db.get(InstallJob, job_id) or _not_found()
    if not jobs.cancel(db, j):
        raise HTTPException(409, {"error": "job_finished", "message": "This install has already ended."})
    return _job_out(db, j)


# ---------------------------------------------------------------- audit log

class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    action: str
    result: str
    console_id: str | None
    console_name: str | None = None
    title_id: str | None
    target_path: str | None
    details: dict
    created_at: datetime


@api.get("/maintenance/audit-logs", response_model=list[AuditOut])
def audit_logs(before: datetime | None = None, db: Session = Depends(get_db)):
    """Newest first, 100 per page; pass the last row's created_at as `before` for the next page."""
    q = select(AuditLog, Console.name).outerjoin(Console, Console.id == AuditLog.console_id)
    if before:
        q = q.where(AuditLog.created_at < before)
    rows = db.execute(q.order_by(AuditLog.created_at.desc()).limit(100))
    return [AuditOut.model_validate(a).model_copy(update={"console_name": name}) for a, name in rows]


# ---------------------------------------------------------------- PS5 app

def console_app_token(console_id: str) -> str:
    """The PS5 app's key: names its console and proves the PC issued it. Nothing is stored; deleting the console revokes it."""
    sig = hmac.new(config.SECRET_KEY.encode(), f"console-app:{console_id}".encode(), "sha256").hexdigest()
    return f"{console_id}.{sig}"


def console_from_token(request: Request, db: Session = Depends(get_db)) -> Console:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    c = db.get(Console, token.split(".")[0]) if token else None
    if not c or not hmac.compare_digest(token, console_app_token(c.id)):
        raise HTTPException(401, {"error": "invalid_token", "message": "This PS5 app isn't paired. Install it again from the PC."})
    return c


# The PS5 app's API. A token reaches only its own console: browse the library and queue installs to it, nothing else.
# Plain text, one game per line, so the console app needs no JSON parser.
console_api = APIRouter(prefix="/api/v1/console-app")
COVER_SIZE = (256, 320)  # the console app expects exactly this many RGBA pixels


def _field(v) -> str:
    return str(v if v is not None else "").replace("\t", " ").replace("\n", " ").replace("\r", " ")


@console_api.get("/library", response_class=PlainTextResponse)
def console_library(c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """id, title, title ID, version, size, state, progress (0-1 while installing or removing), has cover (1/0).
    state: installable | installed | update | queued | installing | removing | blocked:<reason>"""
    installed = {g.title_id: g for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == c.id))}
    active = {j.game_id: j for j in db.scalars(select(InstallJob).where(
        InstallJob.console_id == c.id, InstallJob.status.in_(jobs.ACTIVE)))}
    lines = []
    for g in db.scalars(select(Game).where(Game.format != "unknown", Game.missing.is_(False)).order_by(Game.title)):
        j, progress = active.get(g.id), None
        if j:
            state, progress = "queued" if j.status == "queued" else "installing", j.progress
        elif g.title_id in installed:
            row = installed[g.title_id]
            r = jobs.active_removal(row.id)
            state = "removing" if r else "update" if row.status == "update_available" else "installed"
            progress = r and r["progress"]
        else:
            reason = jobs.blocker(db, c, g)
            state = f"blocked:{reason}" if reason else "installable"
        lines.append("\t".join(map(_field, (g.id, g.title, g.title_id, g.version, g.size_bytes, state,
                                             f"{progress:.3f}" if progress is not None else "",
                                             int(bool(g.cover_path))))))
    return "".join(f"{line}\n" for line in lines)


@console_api.get("/covers/{game_id}")
def console_cover(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """The cover as raw RGBA pixels, COVER_SIZE, cropped to fill. The console has no image decoder."""
    # ponytail: resized on every request; cache the bytes in COVERS_DIR if the grid ever loads slowly.
    with Image.open(_cover_file(_game(db, game_id))) as im:
        data = ImageOps.fit(im.convert("RGBA"), COVER_SIZE).tobytes()
    return Response(data, media_type="application/octet-stream")


@console_api.post("/install/{game_id}", response_class=PlainTextResponse)
def console_install(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    try:
        jobs.queue(db, c, _game(db, game_id))
    except ValueError as e:
        return PlainTextResponse(str(e), 409)
    return "Queued."


@console_api.post("/uninstall/{game_id}/preview", response_class=PlainTextResponse)
def console_uninstall_preview(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """removal id, path to delete, estimated bytes freed, warnings (joined by spaces). 409 with the reason when it can't."""
    g = _game(db, game_id)
    rows = db.scalars(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == g.title_id)
                      .order_by(InstalledGame.install_route.desc()))  # shadowmountplus before package_installer
    row = next(iter(rows), None)
    if not g.title_id or not row:
        return PlainTextResponse("It isn't installed on this console.", 409)
    p = jobs.preview(db, row)
    if not p["allowed"]:
        return PlainTextResponse(p["reason"], 409)
    return "\t".join(map(_field, (p["id"], p["source_path"], p["estimated_bytes_freed"], " ".join(p["warnings"])))) + "\n"


@console_api.post("/uninstall/{removal_id}/confirm/{title_id}", response_class=PlainTextResponse)
async def console_uninstall(removal_id: str, title_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """The console's confirm dialog stands in for typing: it sends the title ID it showed."""
    try:
        jobs.start_removal(db, c, removal_id, title_id)
    except ValueError as e:
        return PlainTextResponse(str(e), 409)
    return "Removing."


@console_api.get("/removals/{removal_id}", response_class=PlainTextResponse)
def console_removal(removal_id: str, c: Console = Depends(console_from_token)):
    """status (removing|success|failed), tile left on the home screen (1/0), message. For the app's done dialog."""
    r = jobs.removals.get(removal_id)
    if not r or r["console_id"] != c.id:
        _not_found()
    return "\t".join(map(_field, (r["status"], int(r["tile_left"]), r["message"]))) + "\n"


def _address_seen_by(ip: str) -> str:
    """This PC's address on the network card that reaches `ip`."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ip, 9))  # UDP connect sends nothing; it only picks the route
        return s.getsockname()[0]


@api.post("/consoles/{console_id}/console-app", response_model=ConsoleOut)
async def install_console_app(console_id: str, db: Session = Depends(get_db)):
    """Puts the PS5 app on the console (or updates it), paired with this PC."""
    c = db.get(Console, console_id) or _not_found()
    if config.HOST == "127.0.0.1":
        raise HTTPException(409, {"error": "lan_off", "message": "The PS5 can't reach this PC yet. Right-click the tray icon, "
                                  "tick \"Allow other devices on my network\", then try again."})
    if not CONSOLE_APP_DIR.is_dir():
        raise HTTPException(409, {"error": "not_bundled", "message": "This build doesn't include the PS5 app."})
    files = {p.relative_to(CONSOLE_APP_DIR).as_posix(): p.read_bytes() for p in CONSOLE_APP_DIR.rglob("*") if p.is_file()}
    files["assets/server.txt"] = (f"url=http://{_address_seen_by(c.ip_address)}:{config.PORT}\n"
                                  f"token={console_app_token(c.id)}\n").encode()
    files["assets/version.txt"] = __version__.encode()
    try:
        await inventory.adapter_for(db, c).install_console_app(files)
        await inventory.refresh(db, c)
    except AdapterError as e:
        db.add(AuditLog(action="console_app_install", console_id=c.id, result="failed", details={"error": e.code}))
        db.commit()
        raise HTTPException(502, {"error": e.code, "message": e.message})
    db.add(AuditLog(action="console_app_install", console_id=c.id, result="success", details={"version": __version__}))
    db.commit()
    inventory.record(c.id, "online")
    return _console_out(c)


def _not_found():
    raise HTTPException(404, {"error": "not_found", "message": "Not found."})


app.include_router(public)
app.include_router(api)
app.include_router(console_api)
if config.STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="frontend")
