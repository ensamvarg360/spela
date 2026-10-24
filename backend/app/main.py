import asyncio
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import secrets
import socket
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
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

from . import __version__, config, credentials, inventory, jobs, logos, migrations, scanner
from .consoles import CONSOLE_APP_DIR, TITLE_ID, AdapterError, discover, send_wake, wake_credential
from .database import SessionLocal, engine, get_db
from .inventory import console_app_token
from .models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob, LibraryRoot, _id, now

_log_file = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[_log_file, *([logging.StreamHandler()] if sys.stderr else [])])  # the .exe has no stderr
if not logging.getLogger("uvicorn").propagate:  # uvicorn's own log setup (Docker) keeps its errors from the root logger
    logging.getLogger("uvicorn.error").addHandler(_log_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(engine)
    if not config.PASSWORD_FILE.exists():  # on a NAS the browser is elsewhere, so setup needs this code (setup())
        logging.getLogger(__name__).warning("First run: to set the admin password from another device, enter the "
                                            "setup code %s%s", SETUP_CODE, f" (not needed in the first {SETUP_WINDOW // 60}"
                                            " minutes after this start)" if config.DOCKER else "")
    if config.DOCKER:
        logging.getLogger(__name__).warning("%s", _open_hint())
    tasks =[asyncio.create_task(jobs.worker())]
    if config.SCAN_INTERVAL_MINUTES > 0:
        tasks.append(asyncio.create_task(_scheduled_scans()))
    if config.CONSOLE_CHECK_SECONDS > 0:
        tasks.append(asyncio.create_task(inventory.monitor()))
    yield
    for task in tasks:
        task.cancel()


def _open_hint() -> str:
    """Where to open Spela from another device, for the container's log: the first thing to try after installing."""
    ip = ""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 9))  # sends nothing: picks the network card with the default route
            ip = s.getsockname()[0]
        except OSError:
            pass
    # ponytail: 172.16-31.x is taken to be Docker's own bridge network; a home network in that range gets the generic hint
    if not ip or ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
        where = f"http://<your NAS's IP address>:{config.PORT}"
    else:
        where = f"http://{ip}:{config.PORT}"
    return (f"Spela is running. Open {where} on a phone or computer on the same network. Page doesn't load? On Docker "
            f"Desktop use the bridge network (ports: [\"{config.PORT}:{config.PORT}\"]), and allow port {config.PORT} "
            "in the NAS's firewall.")


async def _scheduled_scans():
    # ponytail: interval rescans only; watchdog events are unreliable on SMB/NFS mounts anyway.
    while True:
        await asyncio.sleep(config.SCAN_INTERVAL_MINUTES * 60)
        try:
            await asyncio.to_thread(scanner.run_scan_locked, SessionLocal)
        except Exception:  # one failed scan must not end the rescans
            logging.getLogger(__name__).exception("scheduled scan")


APP_ID = "ps5-local-storefront"
LATEST_RELEASE = "https://api.github.com/repos/ensamvarg360/spela/releases/latest"
app = FastAPI(title="Spela", lifespan=lifespan)
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
        return name == "localhost" or name in config.ALLOWED_HOSTS


@app.middleware("http")
async def check_host(request: Request, call_next):
    if not _direct_host(request.headers.get("host", "")):
        how = "or add this name to ALLOWED_HOSTS" if config.DOCKER else "as the tray icon shows"
        return JSONResponse({"error": "bad_host", "message": f"Open the app by its IP address, {how}."}, 421)
    return await call_next(request)


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Changes come only from Spela's own pages. The session cookie is SameSite, but a browser counts every port of one
    IP address as the same site, so another app on a NAS could otherwise post a form here with the user's login.
    Requests without these headers (the PS5 app, the relay, scripts) aren't from a web page and pass."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
        if (site and site not in ("same-origin", "none")
                or origin and urllib.parse.urlsplit(origin).netloc != request.headers.get("host")):
            return JSONResponse({"error": "cross_site", "message": "Only Spela's own page can change things here."}, 403)
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
    """Errors are {"error": code, "message": text} (project.md §17), not FastAPI's {"detail": ...}, plus "fix" (what
    to do about it) when there's something the user can do. The PS5 app has no JSON parser and shows one line, so it
    gets just the message, as plain text."""
    body = exc.detail if isinstance(exc.detail, dict) else {"error": "http_error", "message": str(exc.detail)}
    if request.url.path.startswith(console_api.prefix + "/"):
        return PlainTextResponse(body["message"], status_code=exc.status_code)
    return JSONResponse(body, status_code=exc.status_code)


# admin: everything. viewer: looks at everything, changes nothing (optional, project.md §2).
PASSWORD_FILES = {"admin": config.PASSWORD_FILE, "viewer": config.VIEWER_PASSWORD_FILE}


def _password_salt(role: str = "admin") -> str | None:
    """Stored in each session at login, so resetting a password (deleting or replacing its file) logs out every
    device that logged in with it."""
    try:
        return PASSWORD_FILES[role].read_text().split(":")[0]
    except OSError:
        return None


def _log_in(request: Request, role: str = "admin"):
    request.session.update(user=role, pw=_password_salt(role))


def require_user(request: Request):
    role = request.session.get("user")
    if role not in PASSWORD_FILES or not request.session.get("pw") or request.session["pw"] != _password_salt(role):
        raise HTTPException(401, {"error": "authentication_required", "message": "Log in first."})
    if role == "viewer" and request.method not in ("GET", "HEAD"):
        raise HTTPException(403, {"error": "read_only", "message": "You're logged in to look only. Log in with the admin "
                                                                   "password to change anything."})


def require_admin(request: Request):
    """For what a view-only login mustn't even read: the PC's folders, the activity log (it holds IP addresses)."""
    if request.session.get("user") != "admin":
        raise HTTPException(403, {"error": "admin_only", "message": "This needs the admin password."})


def _adapter_failed(status: int, e: AdapterError) -> HTTPException:
    return HTTPException(status, {"error": e.code, "message": e.message} | ({"fix": e.fix} if e.fix else {}))


public = APIRouter(prefix="/api/v1")
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_user)])


# ---------------------------------------------------------------- auth

_failures: dict[str, list[float]] = {}  # ponytail: in-memory per-IP limiter, resets on restart


class LoginIn(BaseModel):
    password: str


class SetupIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    code: str = ""  # SETUP_CODE, needed when setting it up from another device


SETUP_CODE = secrets.token_hex(5).upper()  # logged at startup while no password is set; new on every start
SETUP_WINDOW = 600  # seconds after start in which Docker's first setup needs no code from a LAN address
STARTED = time.monotonic()


def _in_setup_window(ip: str) -> bool:
    try:
        lan = ipaddress.ip_address(ip).is_private
    except ValueError:
        return False
    return config.DOCKER and lan and time.monotonic() - STARTED < SETUP_WINDOW


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def _check_password(password: str, role: str = "admin") -> bool:
    try:
        salt, digest = PASSWORD_FILES[role].read_text().split(":")
    except OSError:  # no view-only password set
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt)), bytes.fromhex(digest))


def _write_password(role: str, password: str):
    salt = os.urandom(16)
    PASSWORD_FILES[role].write_text(f"{salt.hex()}:{_hash(password, salt).hex()}")


def _require_setup_done():
    if not config.PASSWORD_FILE.exists():
        raise HTTPException(409, {"error": "setup_required", "message": "Choose an admin password first."})


@public.post("/auth/setup")
def setup(body: SetupIn, request: Request, db: Session = Depends(get_db)):
    """First run only: sets the admin password. From another device only with the setup code from Spela's log, so
    nobody else on the LAN can claim it first. In Docker, devices on the LAN may also claim it without the code for
    SETUP_WINDOW seconds after the container starts, so installing on a NAS needs no terminal (as Portainer does)."""
    ip = request.client.host if request.client else "?"
    if (ip not in ("127.0.0.1", "::1") and not _in_setup_window(ip)
            and not hmac.compare_digest(body.code.strip().upper(), SETUP_CODE)):
        recent = [t for t in _failures.get(ip, []) if time.time() - t < 60]
        if len(recent) >= 5:
            raise HTTPException(429, {"error": "rate_limited", "message": "Too many attempts. Wait a minute."})
        if body.code:
            _failures[ip] = recent + [time.time()]
        where = ("in the container's log (docker logs spela). Or restart the container and set the password within "
                 f"{SETUP_WINDOW // 60} minutes, without a code" if config.DOCKER else
                 "in app.log in Spela's data folder. Or set the password on the PC running Spela, from its tray icon")
        raise HTTPException(403, {"error": "setup_code", "message": "Wrong setup code." if body.code else "Enter the setup code.",
                                  "fix": f"It's {where}."})
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
        raise HTTPException(429, {"error": "rate_limited", "message": "Too many attempts. Wait a minute.",
                                  "fix": f"Forgot it? Delete admin_password in {config.DATA_DIR} and {config.RESTART} "
                                         "to choose a new one."})
    _failures[ip] = recent + [time.time()]  # counted before the slow hash, or tries sent at once all get past the limit
    role = next((r for r in PASSWORD_FILES if _check_password(body.password, r)), None)
    db.add(AuditLog(action="login", result="success" if role else "failed", details={"ip": ip, **({"role": role} if role else {})}))
    db.commit()
    if not role:
        raise HTTPException(401, {"error": "invalid_credentials", "message": "Wrong password."})
    _failures.pop(ip, None)
    _log_in(request, role)
    return {"user": role}


@public.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@public.get("/auth/me")
def me(request: Request):
    _require_setup_done()
    require_user(request)
    return {"user": request.session["user"]}


class ViewerIn(BaseModel):
    password: str | None = Field(None, min_length=8, max_length=200)  # null turns view-only access off


@api.get("/auth/viewer", dependencies=[Depends(require_admin)])
def viewer_status():
    return {"enabled": config.VIEWER_PASSWORD_FILE.exists()}


@api.post("/auth/viewer")
def set_viewer(body: ViewerIn, db: Session = Depends(get_db)):
    """Sets (or replaces, or with null removes) the view-only password. Either way, devices using the old one are logged out."""
    if body.password is None:
        config.VIEWER_PASSWORD_FILE.unlink(missing_ok=True)
    elif _check_password(body.password):
        raise HTTPException(409, {"error": "same_as_admin", "message": "Choose a different password from the admin password."})
    else:
        _write_password("viewer", body.password)
    db.add(AuditLog(action="viewer_password", result="success", details={"enabled": body.password is not None}))
    db.commit()
    return viewer_status()


class HelperKeyIn(BaseModel):
    key: str | None = None  # null goes back to this install's own key


@api.get("/log", dependencies=[Depends(require_admin)])
def log_tail():
    """The last lines of app.log, for the bug-report button to put in the issue. Admin only: it holds IPs and paths."""
    try:
        lines = config.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    return {"log": "\n".join(lines[-50:])}


@api.get("/helper-key", dependencies=[Depends(require_admin)])
def helper_key_status():
    """This install's helper key, to paste into another Spela so both use one helper. `source`: own, pasted or env."""
    source = "env" if os.environ.get("SPELA_HELPER_KEY") else "pasted" if config.HELPER_KEY_FILE.exists() else "own"
    return {"key": config.helper_key(), "source": source}


@api.post("/helper-key")
def set_helper_key(body: HelperKeyIn, db: Session = Depends(get_db)):
    if os.environ.get("SPELA_HELPER_KEY"):
        raise HTTPException(409, {"error": "key_from_env", "message": "SPELA_HELPER_KEY sets this install's helper key.",
                                  "fix": "Change or remove it where Spela is started, then restart it."})
    if body.key is None:
        config.HELPER_KEY_FILE.unlink(missing_ok=True)
    elif not config.HELPER_KEY_RE.fullmatch(key := body.key.strip().lower()):
        raise HTTPException(422, {"error": "invalid_key", "message": "That isn't a helper key.",
                                  "fix": "Copy it again from Settings → Helper key in the other Spela: 64 letters and digits."})
    else:
        config.HELPER_KEY_FILE.write_text(key)
    db.add(AuditLog(action="helper_key", result="success", details={"source": helper_key_status()["source"]}))
    db.commit()
    return helper_key_status()


@public.get("/health")
def health():
    return {"status": "ok", "app": APP_ID, "version": __version__}  # the launcher checks `app` to recognise a running copy


_release_checked: float | None = None
_release: dict | None = None


def latest_release() -> dict | None:
    """The newest published release on GitHub, asked at most every 6 hours. None when GitHub can't be reached."""
    global _release_checked, _release
    if _release_checked is None or time.monotonic() - _release_checked > 6 * 3600:
        _release_checked = time.monotonic()
        req = urllib.request.Request(LATEST_RELEASE, headers={"Accept": "application/vnd.github+json", "User-Agent": f"Spela/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read(1_000_000))
            assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
            version = data["tag_name"].lstrip("v")
            _release = {"version": version, "url": data["html_url"],
                        "zip": assets.get(f"Spela-{version}.zip"), "sums": assets.get("SHA256SUMS.txt")}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _release = None  # offline or rate-limited: say nothing until the next check
    return _release


@api.get("/notifications")
def notifications():
    return list(jobs.recent)  # installs, moves and conversions that ended, newest first


# Set by launcher.py in the Windows app: takes the unzipped new Spela folder, quits, swaps it in and starts it.
apply_update = None


def _newer_release() -> dict | None:
    r = latest_release()
    return r if r and inventory._ver(r["version"]) > inventory._ver(__version__) else None


@api.get("/update")
def update():
    r = _newer_release()
    return {"version": __version__, "latest": r, "installable": bool(apply_update and r and r["zip"] and r["sums"])}


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"Spela/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _stage_update(release: dict, dest: Path) -> Path:
    """Downloads the release's zip, checks it against its SHA256SUMS.txt and unzips it into dest. Returns the new
    Spela folder in it."""
    data = _download(release["zip"])
    name = release["zip"].rsplit("/", 1)[1]
    if f"{hashlib.sha256(data).hexdigest()} *{name}" not in _download(release["sums"]).decode().splitlines():
        raise ValueError("the download doesn't match its checksum")
    shutil.rmtree(dest, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)  # extractall drops absolute paths and ".." from member names
    if not (dest / "Spela" / "Spela.exe").is_file():
        raise ValueError("the download has no Spela.exe")
    return dest / "Spela"


@api.post("/update/install", dependencies=[Depends(require_admin)])
def install_update():
    r = _newer_release()
    if not (apply_update and r and r["zip"] and r["sums"]):
        raise HTTPException(409, {"error": "no_update", "message": "There's no update Spela can install by itself.",
                                  "fix": "Download it from GitHub instead."})
    if jobs.in_progress():
        raise HTTPException(409, {"error": "busy", "message": "Spela is still installing, moving or converting.",
                                  "fix": "Update once that's done."})
    app_dir = Path(sys.executable).parent
    staging = app_dir.with_name(app_dir.name + ".update-new")  # next to the app, so the swap is a rename on one drive
    try:
        new = _stage_update(r, staging)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(502, {"error": "update_failed", "message": f"Couldn't get the update: {e}",
                                  "fix": "Try again later, or download it from GitHub."})
    threading.Timer(0.5, apply_update, [new]).start()  # lets this response reach the page first
    return {"version": r["version"]}


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
    kind: str  # game|dlc
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
    store: dict | None = None  # logos.store_info, when already looked up; the library filters by it
    new: bool = False  # added in the last 30 days and not opened yet


def _out(g: Game, installed: set[str] = frozenset()) -> GameOut:
    """`installed`: title IDs on any console. DLC shares its game's title ID, so it's never marked installed."""
    installed = g.kind == "game" and g.title_id in installed
    store = g.title_id and logos.store_info(g.title_id, cached=True) or None
    new = g.seen_at is None and g.created_at > now() - timedelta(days=30)
    return GameOut.model_validate(g).model_copy(update={"has_cover": bool(g.cover_path), "installed": installed, "store": store, "new": new})


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
    return [_out(g, installed) for g in db.scalars(select(Game).order_by(Game.title))]


@api.get("/games/{game_id}", response_model=GameOut)
def get_game(game_id: str, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    if not g.seen_at:  # opening a game is what clears its "new" mark; installing or editing it starts here too
        g.seen_at = now()
        db.commit()
    return _out(g, _installed_title_ids(db))


@api.patch("/games/{game_id}", response_model=GameOut)
def patch_game(game_id: str, body: GamePatch, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is None or changes.get("owned_copy_type", "") is None:
        raise HTTPException(422, {"error": "invalid_field", "message": "Title and owned copy type can't be empty."})
    for k, v in changes.items():
        setattr(g, k, v)
    g.seen_at = g.seen_at or now()  # favoriting from the library tile counts as opening it
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
    PC (or another Windows user) keeps the old absolute path. Only image files, whatever an older scan stored."""
    if not g.cover_path or Path(g.cover_path).suffix.lower() not in scanner.COVER_SUFFIXES:
        raise HTTPException(404, {"error": "not_found", "message": "No cover."})
    if os.path.isfile(g.cover_path):
        return g.cover_path
    moved = config.COVERS_DIR / Path(g.cover_path).name
    if moved.is_file():
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
    return FileResponse(path, headers=NO_CACHE)


@api.get("/games/{game_id}/store")
def get_store_info(game_id: str, db: Session = Depends(get_db)):
    """Publisher, release date, genres and ratings from PlayStation Store (logos.store_info); null when it has none."""
    g = _game(db, game_id)
    return g.title_id and logos.store_info(g.title_id) or None


@api.get("/games/{game_id}/store-images")
async def get_store_images(game_id: str, db: Session = Depends(get_db)):
    """Every image PlayStation Store has for the game, per store and edition (logos.store_images)."""
    g = _game(db, game_id)
    if not g.title_id:
        raise HTTPException(404, {"error": "not_found", "message": "This game has no title ID."})
    return await _store_images(g.title_id, g.content_id)


@api.get("/titles/{title_id}/store-images")
async def get_title_store_images(title_id: str):
    """The same for a title ID, for games on a console that aren't in the library."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    return await _store_images(title_id, None)


async def _store_images(title_id: str, content_id: str | None):
    try:
        return await asyncio.to_thread(logos.store_images, title_id, content_id)
    except (OSError, ValueError):
        raise HTTPException(502, {"error": "store_unreachable", "message": "Couldn't reach PlayStation Store. Is this PC online?"})


@api.get("/titles/{title_id}/poster")
def get_title_poster(title_id: str):
    """The cover uploaded for a title ID, else its portrait key art (logos.poster); 404 when PlayStation Store has none."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    if uploaded := next((config.COVERS_DIR / "titles").glob(f"{title_id}.*"), None):
        return FileResponse(uploaded, headers=NO_CACHE)
    if not (path := logos.poster(title_id)):
        _not_found()
    return FileResponse(path, headers=NO_CACHE)


NO_CACHE = {"Cache-Control": "no-cache"}  # store art can be looked up again (refresh_store): browsers check their copy


@api.post("/store/refresh", status_code=202)
def refresh_store(background: BackgroundTasks, db: Session = Depends(get_db)):
    """Forgets everything PlayStation Store gave this PC (store pages, posters, logos) and looks it up again for the
    library's games, after answering. Games only on a console are looked up when next shown."""
    shutil.rmtree(logos.STORE_DIR, ignore_errors=True)
    games = db.execute(select(Game.title_id, Game.content_id).where(Game.title_id.is_not(None), Game.missing.is_(False)))
    background.add_task(_warm_store, sorted(set(games), key=lambda t: t[0]))
    db.add(AuditLog(action="store_refresh", result="success", details={"message": "Looking up all artwork again."}))
    db.commit()


COVER_TYPES = {"image/jpeg", "image/png", "image/webp"}  # what the browser says; IMAGE_FORMATS decides
IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}  # what the file really is: its extension
MAX_IMAGE_PIXELS = 40_000_000


async def _read_cover(file: UploadFile, what: str = "Cover") -> tuple[bytes, str]:
    """An uploaded image's bytes and file extension, once it's checked to be a JPEG, PNG or WebP under 10 MB and
    MAX_IMAGE_PIXELS."""
    if file.content_type not in COVER_TYPES:
        raise HTTPException(415, {"error": "unsupported_media", "message": "Use JPEG, PNG or WebP."})
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, {"error": "too_large", "message": f"{what} must be under 10 MB."})
    try:
        # only these decoders, whatever the browser claimed: none of Pillow's others (TIFF, PSD, EPS...) see the file
        with Image.open(io.BytesIO(data), formats=list(IMAGE_FORMATS)) as im:
            ext, pixels = IMAGE_FORMATS[im.format], im.width * im.height  # from the header, nothing decoded yet
            im.verify()
    except Exception:  # Pillow raises many kinds for a broken or disguised file
        raise HTTPException(415, {"error": "unsupported_media", "message": "That file isn't a readable image.",
                                  "fix": "Use a JPEG, PNG or WebP picture."})
    if pixels > MAX_IMAGE_PIXELS:  # a small file can unpack to gigabytes
        raise HTTPException(413, {"error": "too_large", "message": f"{what} must be at most {MAX_IMAGE_PIXELS // 10**6} megapixels."})
    return data, ext


@api.post("/games/{game_id}/cover", response_model=GameOut)
async def upload_cover(game_id: str, file: UploadFile, db: Session = Depends(get_db)):
    g = _game(db, game_id)
    data, ext = await _read_cover(file)
    for old in config.COVERS_DIR.glob(f"{g.id}.*"):
        old.unlink()
    dest = config.COVERS_DIR / f"{g.id}{ext}"
    dest.write_bytes(data)
    g.cover_path = str(dest)
    db.commit()
    return _out(g)


@api.post("/titles/{title_id}/cover", status_code=204)
async def upload_title_cover(title_id: str, file: UploadFile):
    """A cover for a game on a console that isn't in the library, by its title ID: /titles/<id>/poster serves it then."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    data, ext = await _read_cover(file)
    folder = config.COVERS_DIR / "titles"
    folder.mkdir(exist_ok=True)
    for old in folder.glob(f"{title_id}.*"):
        old.unlink()
    (folder / f"{title_id}{ext}").write_bytes(data)


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
    kind: Literal["game", "dlc"] = "game"


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
    if not os.path.isabs(body.path):
        raise HTTPException(400, {"error": "invalid_root", "message": f"{body.path} isn't a full path.",
                                  "fix": ("Start it with a drive letter, like G:\\Games" if os.name == "nt" else "Start it with /, like /games")
                                         + ", or click Browse… to pick the folder."})
    if not os.path.isdir(path):
        raise HTTPException(400, {"error": "invalid_root", "message": f"There's no folder at {body.path}.",
                                  "fix": "Is the drive plugged in? Drive letters can change when it's plugged in again. "
                                         "Click Browse… to pick the folder."})
    try:
        os.scandir(path).close()
    except OSError:
        raise HTTPException(400, {"error": "invalid_root", "message": f"Spela isn't allowed to read {body.path}.",
                                  "fix": "Pick a folder inside it instead, or check its permissions."})
    if db.scalar(select(LibraryRoot).where(LibraryRoot.path == path)):
        raise HTTPException(409, {"error": "duplicate_root", "message": "That folder is already in the list."})
    r = LibraryRoot(path=path, enabled=body.enabled, exclude_patterns=body.exclude_patterns, kind=body.kind)
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


@api.get("/library/browse", dependencies=[Depends(require_admin)])
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
    wake_key: str | None = Field(None, max_length=200)  # RP-RegistKey (consoles.wake_credential)
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
    wake_key: str | None = Field(None, max_length=200)  # like password: a string replaces it, "" or null forgets it
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
    has_wake_key: bool = False
    firmware: str | None
    helper_status: dict
    last_seen_at: datetime | None
    last_inventory_scan_at: datetime | None
    enabled: bool
    auto_helper: bool = True  # false after Remove helper, until Install helper
    state: str = "unknown"  # online|standby|degraded|offline|disabled|unknown (not checked yet)
    status_message: str = ""
    status_fix: str = ""  # what the user can do about status_message


def _console_out(c: Console) -> ConsoleOut:
    s = inventory.status.get(c.id, {})
    return ConsoleOut.model_validate(c).model_copy(update={
        "has_password": bool(c.credentials_secret_id),
        "has_wake_key": bool(c.wake_secret_id),
        "state": s.get("state", "unknown") if c.enabled else "disabled",
        "status_message": s.get("message", ""),
        "status_fix": s.get("fix", ""),
    })


def _audit_console(db: Session, action: str, c: Console, fields: list[str]):
    # Field names only, never values: the password and wake key must not reach the audit log.
    db.add(AuditLog(action=action, console_id=c.id, result="success", details={"name": c.name, "fields": fields}))
    if "password" in fields or "wake_key" in fields:
        db.add(AuditLog(action="credential_update", console_id=c.id, result="success", details={"name": c.name}))


def _save_console(db: Session, c: Console, changes: dict):
    if "ip_address" in changes:
        changes["ip_address"] = str(changes["ip_address"])
    if changes.get("wake_key"):
        try:
            changes["wake_key"] = wake_credential(changes["wake_key"])
        except ValueError as e:
            raise HTTPException(422, {"error": "invalid_wake_key", "message": str(e)})
    for k, v in changes.items():
        if k == "password":
            credentials.set_password(db, c, v)
        elif k == "wake_key":
            credentials.set_password(db, c, v, "wake_secret_id")
        else:
            setattr(c, k, v)


@api.get("/consoles", response_model=list[ConsoleOut])
def list_consoles(db: Session = Depends(get_db)):
    return [_console_out(c) for c in db.scalars(select(Console).order_by(Console.name))]


@api.post("/consoles/discover", dependencies=[Depends(require_admin)])
async def discover_consoles(db: Session = Depends(get_db)):
    """PS5s on this network that answer discovery (about 2 s), each marked when it's already added."""
    known = set(db.scalars(select(Console.ip_address)))
    return [p | {"added": p["ip"] in known} for p in await asyncio.to_thread(discover)]


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
    credentials.set_password(db, c, None, "wake_secret_id")
    _audit_console(db, "console_delete", c, [])
    db.delete(c)
    db.commit()
    return {"ok": True}


@api.get("/consoles/{console_id}/status")
def console_status(console_id: str, db: Session = Depends(get_db)):
    c = _console_out(db.get(Console, console_id) or _not_found())
    return {"state": c.state, "message": c.status_message, "fix": c.status_fix, "last_seen_at": c.last_seen_at}


@api.post("/consoles/{console_id}/test-connection", response_model=ConsoleOut)
async def test_connection(console_id: str, db: Session = Depends(get_db)):
    """Checks now instead of waiting for the monitor. Reads the inventory too when the console just came online."""
    db.get(Console, console_id) or _not_found()
    await inventory.check(console_id)
    db.expire_all()  # check() wrote through its own session
    return _console_out(db.get(Console, console_id))


@api.post("/consoles/{console_id}/wake", response_model=ConsoleOut)
async def wake_console(console_id: str, db: Session = Depends(get_db)):
    """Wakes a console in rest mode with its saved Remote Play key, then checks on it until it's up (about 30 s)."""
    c = db.get(Console, console_id) or _not_found()
    try:
        credential = credentials.get_password(db, c, "wake_secret_id")
    except AdapterError as e:
        raise _adapter_failed(409, e)
    if not credential:
        raise HTTPException(409, {"error": "no_wake_key", "message": "Spela can't wake this console yet.",
                                  "fix": "Add its Remote Play key under Settings → Consoles → Edit."})
    if c.connection_type != "mock":
        await asyncio.to_thread(send_wake, c.ip_address, credential)
    db.add(AuditLog(action="wake", console_id=c.id, result="success", details={"name": c.name}))
    db.commit()
    for _ in range(WAKE_WAIT_SECONDS // 3):
        if (await inventory.check(console_id)).get("state") != "standby":
            break
        await asyncio.sleep(3)
    db.expire_all()  # check() wrote through its own session
    return _console_out(db.get(Console, console_id))


WAKE_WAIT_SECONDS = 30


@api.post("/consoles/{console_id}/rescan", response_model=ConsoleOut)
async def rescan_console(console_id: str, db: Session = Depends(get_db)):
    c = db.get(Console, console_id) or _not_found()
    try:
        await inventory.refresh(db, c)
    except AdapterError as e:
        raise _adapter_failed(502, e)
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
        raise _adapter_failed(502, e)
    c.auto_helper = True  # sent by hand: from now on it's sent by itself again
    db.add(AuditLog(action="helper_install", console_id=c.id, result="success", details={"version": version}))
    db.commit()
    inventory.record(c.id, "online")
    return _console_out(c)


@api.delete("/consoles/{console_id}/helper", response_model=ConsoleOut)
async def remove_helper(console_id: str, db: Session = Depends(get_db)):
    """Stops the helper payload and stops sending it by itself, for someone moving to other tools. Nothing of it stays
    on the PS5: it lives in memory. Games, the PS5 app and logos are left alone. Install helper turns it back on."""
    c = db.get(Console, console_id) or _not_found()
    if db.scalar(select(InstallJob.id).where(InstallJob.console_id == c.id, InstallJob.status.in_(jobs.ACTIVE))):
        raise HTTPException(409, {"error": "job_active", "message": "This console has installs waiting or running.",
                                  "fix": "Cancel them on the Installed page first."})
    try:
        await inventory.adapter_for(db, c).stop_helper()
    except AdapterError as e:
        db.add(AuditLog(action="helper_remove", console_id=c.id, result="failed", details={"error": e.code}))
        db.commit()
        raise _adapter_failed(502, e)
    c.auto_helper = False
    db.add(AuditLog(action="helper_remove", console_id=c.id, result="success", details={"name": c.name}))
    db.commit()
    await inventory.check(c.id)
    db.expire_all()  # check() wrote through its own session
    return _console_out(db.get(Console, console_id))


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
    moving: bool = False


@api.get("/consoles/{console_id}/installed", response_model=list[InstalledOut])
def list_installed(console_id: str, db: Session = Depends(get_db)):
    db.get(Console, console_id) or _not_found()
    out = []
    for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == console_id).order_by(InstalledGame.title)):
        lib = db.get(Game, g.game_id) if g.game_id else None
        cover = (f"/api/v1/games/{lib.id}/cover" if lib and lib.cover_path
                 else f"/api/v1/icons/{g.title_id}" if inventory.icon_path(g.title_id).is_file() else None)
        out.append(InstalledOut.model_validate(g).model_copy(update={
            "cover_url": cover, "library_version": lib and lib.version, "removing": jobs.removing(g.id), "moving": jobs.moving(g.id)}))
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


class MoveIn(BaseModel):
    installed_game_id: str
    drive_id: str | None = None
    root_id: str | None = None  # a game library folder on this PC, instead of a drive


class MoveOut(BaseModel):
    id: str
    installed_game_id: str
    title_id: str
    title: str
    source_path: str | None
    drive_label: str
    status: str  # moving|success|failed|canceled
    progress: float
    bytes_done: int
    bytes_total: int
    tile_left: bool  # moved, but its old home-screen tile must be deleted on the PS5
    message: str


@api.post("/consoles/{console_id}/move", response_model=MoveOut, status_code=202)
async def move_game(console_id: str, body: MoveIn, db: Session = Depends(get_db)):
    """Moves an installed game to another drive of the same console (jobs.start_move), or to this PC (jobs.start_move_to_pc)."""
    console = db.get(Console, console_id) or _not_found()
    try:
        if body.root_id:
            return jobs.start_move_to_pc(db, console, body.installed_game_id, body.root_id)
        return jobs.start_move(db, console, body.installed_game_id, body.drive_id or "")
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_move", "message": str(e)})


@api.get("/moves/{move_id}", response_model=MoveOut)
def get_move(move_id: str):
    return jobs.moves.get(move_id) or _not_found()


@api.post("/moves/{move_id}/cancel")
def cancel_move(move_id: str):
    return {"ok": jobs.cancel_move(move_id)}


class SaveBackupIn(BaseModel):
    title_id: str = Field(pattern=r"^[A-Z]{4}\d{5}$")


@api.post("/consoles/{console_id}/save-backups", status_code=202)
async def backup_saves(console_id: str, body: SaveBackupIn, db: Session = Depends(get_db)):
    """Copies a title's save data from the console to this PC (jobs.start_save_backup). Nothing on the console changes."""
    try:
        return jobs.start_save_backup(db, db.get(Console, console_id) or _not_found(), body.title_id)
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_back_up", "message": str(e)})


@api.get("/save-backups/{backup_id}")
def get_save_backup(backup_id: str):
    return jobs.save_backups.get(backup_id) or _not_found()


class SaveRestoreIn(SaveBackupIn):
    backup: str  # a folder name from GET /save-backups
    overwrite: bool = False  # the user confirmed replacing the saves on the console


@api.post("/consoles/{console_id}/save-restores", status_code=202)
async def restore_saves(console_id: str, body: SaveRestoreIn, db: Session = Depends(get_db)):
    """Writes a save backup back to the console (jobs.start_save_restore). Ends as "confirm", with nothing changed, when it
    would replace saves on the console and `overwrite` isn't set."""
    try:
        return jobs.start_save_restore(db, db.get(Console, console_id) or _not_found(), body.title_id, body.backup, body.overwrite)
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_restore", "message": str(e)})


@api.get("/save-restores/{restore_id}")
def get_save_restore(restore_id: str):
    return jobs.save_restores.get(restore_id) or _not_found()


@api.get("/save-backups", dependencies=[Depends(require_admin)])
def list_save_backups(title_id: str):
    """The backups of one title on this PC, newest first. Admin only: it shows folders of this PC."""
    return jobs.list_save_backups(title_id)


@api.get("/games/{game_id}/history")
def game_history(game_id: str, db: Session = Depends(get_db)):
    """Where the game is installed now, and its installs, removals and moves, newest first (the latest 50)."""
    g = _game(db, game_id)
    consoles = {c.id: c.name for c in db.scalars(select(Console))}
    drives = {d.id: d.label for d in db.scalars(select(Drive))}
    installed = [] if g.kind != "game" or not g.title_id else [
        {"id": r.id, "console_id": r.console_id, "console": consoles.get(r.console_id, ""), "drive": drives.get(r.drive_id, ""), "path": r.installed_path,
         "version": r.version, "status": r.status}
        for r in db.scalars(select(InstalledGame).where(InstalledGame.title_id == g.title_id).order_by(InstalledGame.console_id))]
    events = [{"at": j.finished_at or j.created_at, "what": "install", "result": j.status, "console": consoles.get(j.console_id, ""),
               "drive": drives.get(j.target_drive_id, ""), "message": j.message}
              for j in db.scalars(select(InstallJob).where(InstallJob.game_id == g.id).order_by(InstallJob.created_at.desc()).limit(50))]
    if g.title_id and g.kind == "game":
        events += [{"at": a.created_at, "what": a.action, "result": a.result, "console": consoles.get(a.console_id, ""), "drive": "",
                    "message": a.details.get("message") or a.details.get("reason") or ""}
                   for a in db.scalars(select(AuditLog).where(AuditLog.title_id == g.title_id, AuditLog.action.in_(("uninstall", "move")))
                                       .order_by(AuditLog.created_at.desc()).limit(50))]
    return {"installed": installed, "events": sorted(events, key=lambda e: e["at"], reverse=True)[:50]}


@api.post("/games/{game_id}/convert", status_code=202)
async def convert_game(game_id: str, db: Session = Depends(get_db)):
    """Starts writing the folder dump into an .exfat disc image next to it (jobs.start_convert). The folder is kept."""
    try:
        return jobs.start_convert(_game(db, game_id))
    except ValueError as e:
        raise HTTPException(409, {"error": "cannot_convert", "message": str(e)})


@api.get("/games/{game_id}/convert")
def get_conversion(game_id: str):
    return jobs.conversions.get(game_id) or _not_found()


@api.post("/games/{game_id}/convert/cancel")
def cancel_conversion(game_id: str):
    return {"ok": jobs.cancel_convert(game_id)}


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


@api.get("/consoles/{console_id}/blockers")
def install_blockers(console_id: str, game_ids: str, db: Session = Depends(get_db)):
    """Why each game (comma-separated IDs) can't be installed on this console; installable ones are left out."""
    c = db.get(Console, console_id) or _not_found()
    out = {}
    for gid in filter(None, game_ids.split(",")):
        if (g := db.get(Game, gid)) and (reason := jobs.blocker(db, c, g)):
            out[gid] = reason
    return out


@api.get("/consoles/{console_id}/firmware-blocked")
def firmware_blocked(console_id: str, db: Session = Depends(get_db)) -> list[str]:
    """IDs of the packages this console's firmware is too new for. Empty while the firmware is unknown."""
    c = db.get(Console, console_id) or _not_found()
    if not c.firmware:
        return []
    return [g.id for g in db.scalars(select(Game).where(Game.format.in_(jobs.PKG_FORMATS))) if jobs.pkg_blocker(c, g)]


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


# ---------------------------------------------------------------- setup checklist

class ChecklistIn(BaseModel):
    dismissed: bool


@api.get("/setup", dependencies=[Depends(require_admin)])
def setup_status(db: Session = Depends(get_db)):
    """How far first-run setup has come, worked out from what's already there, so the checklist picks up where the
    user left off."""
    roots = list(db.scalars(select(LibraryRoot).where(LibraryRoot.enabled)))
    consoles = list(db.scalars(select(Console)))
    return {
        "dismissed": config.SETUP_DISMISSED_FILE.exists(),
        "folder": any(r.kind == "game" and os.path.isdir(r.path) for r in roots),
        "scan": bool(db.scalar(select(Game.id).where(Game.kind == "game", Game.missing.is_(False)).limit(1))),
        "console": bool(consoles),
        "helper": any(inventory.status.get(c.id, {}).get("state") == "online" for c in consoles),
        "console_app": any(c.helper_status.get("console_app") == __version__ for c in consoles),
        "wake": any(c.wake_secret_id for c in consoles),
        "dlc_folder": any(r.kind == "dlc" for r in roots),
        "viewer": config.VIEWER_PASSWORD_FILE.exists(),
    }


@api.post("/setup", dependencies=[Depends(require_admin)])
def set_setup(body: ChecklistIn):
    """Hides (or brings back) the setup checklist on the Dashboard."""
    if body.dismissed:
        config.SETUP_DISMISSED_FILE.touch()
    else:
        config.SETUP_DISMISSED_FILE.unlink(missing_ok=True)
    return {"dismissed": body.dismissed}


@api.get("/maintenance/audit-logs", response_model=list[AuditOut], dependencies=[Depends(require_admin)])
def audit_logs(before: datetime | None = None, db: Session = Depends(get_db)):
    """Newest first, 100 per page; pass the last row's created_at as `before` for the next page."""
    q = select(AuditLog, Console.name).outerjoin(Console, Console.id == AuditLog.console_id)
    if before:
        q = q.where(AuditLog.created_at < before)
    rows = db.execute(q.order_by(AuditLog.created_at.desc()).limit(100))
    return [AuditOut.model_validate(a).model_copy(update={"console_name": name}) for a, name in rows]


# ---------------------------------------------------------------- PS5 app

def console_from_token(request: Request, db: Session = Depends(get_db)) -> Console:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    c = db.get(Console, token.split(".")[0]) if token else None
    if not c or not hmac.compare_digest(token, console_app_token(c.id)):
        raise HTTPException(401, {"error": "invalid_token", "message": "This PS5 app isn't paired. Install it again from the PC."})
    return c


# The PS5 app's API. A token reaches only its own console: browse the library and its drives, queue and cancel installs
# to it, and remove games from it. Nothing else.
# Plain text, one game per line, so the console app needs no JSON parser.
console_api = APIRouter(prefix="/api/v1/console-app")
COVER_SIZE = (256, 320)  # the console app expects exactly this many RGBA pixels
LOGO_SIZE = (384, 112)  # likewise for a logo: the width of its game panel's text, drawn 1:1


def _field(v) -> str:
    return str(v if v is not None else "").replace("\t", " ").replace("\n", " ").replace("\r", " ")


_store_warming = threading.Lock()


def _warm_store(games: list[tuple[str, str | None]]):
    """Looks up the store pages, posters and logos of games the PC hasn't shown yet, so their genres, covers and logos
    reach the PS5 on a later poll. (title ID, content ID) each. Fetched while the PS5 waited, a poster would outlast
    the few seconds the helper holds a request."""
    if not _store_warming.acquire(blocking=False):
        return  # a lookup from an earlier poll is still going
    try:
        for title_id, content_id in games:
            logos.concept(title_id)
            if not (logos.STORE_DIR / f"{title_id}.json").is_file():
                break  # offline: try again on a later poll
            logos.poster(title_id, content_id)
            logos.title_logo(title_id)
    finally:
        _store_warming.release()


def _stored(title_id: str | None, suffix: str) -> Path | None:
    """A store file of the game that's already on this PC, without asking the store: ".jpg" its poster (logos.poster),
    "-logo.png" its logo (logos.title_logo), ".none"/"-logo.none" when the store had neither."""
    path = logos.STORE_DIR / f"{title_id}{suffix}"
    return path if title_id and path.is_file() else None


def _poster(title_id: str | None) -> Path | None:
    return _stored(title_id, ".jpg")


def _logo(g: Game) -> Path | None:
    """A game's logo, once on this PC. Not DLC's: it shares the title ID, so the logo would be its game's."""
    return _stored(g.title_id, "-logo.png") if g.kind == "game" else None


@functools.lru_cache(maxsize=4096)
def _store_genres(title_id: str, mtime_ns: int) -> tuple[str, ...]:
    """Parsed once per version of the store file: the PS5 asks for the library every few seconds while installing."""
    store = logos.store_info(title_id, cached=True)
    return tuple(store["genres"]) if store else ()


def _genres(g: Game) -> list[str]:
    """The store's genres, else the one from the game's sidecar, like the PC's Genre filter."""
    path = logos.STORE_DIR / f"{g.title_id}.json"
    store = _store_genres(g.title_id, path.stat().st_mtime_ns) if g.title_id and path.is_file() else ()
    return list(store) if store else [g.genre] if g.genre else []


@console_api.get("/library", response_class=PlainTextResponse)
def console_library(background: BackgroundTasks, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """id, title, title ID, version, size, state, progress (0-1 while installing or removing), has cover (1/0; a store
    poster counts once it's on this PC, so the PS5 asks for it then), kind, format,
    then while installing or removing: bytes done, bytes total, bytes/s, seconds left, the step it's on (empty when unknown),
    then favorite (1/0), when it was added to the library (Unix seconds), its genres (joined by |; empty when unknown)
    and has logo (1/0; /logos/<id> then has it).
    state: installable | installed | update | queued | installing | removing | blocked:<reason>
    kind: game | dlc. DLC shares its game's title ID, so it never shows as installed or removable."""
    installed = {g.title_id: g for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == c.id))}
    active = {j.game_id: j for j in db.scalars(select(InstallJob).where(
        InstallJob.console_id == c.id, InstallJob.status.in_(jobs.ACTIVE)))}
    lines = []
    games = list(db.scalars(select(Game).where(Game.format != "unknown", Game.missing.is_(False)).order_by(Game.title)))
    for g in games:
        j, progress, detail = active.get(g.id), None, ("",) * 5
        if j:
            state, progress = "queued" if j.status == "queued" else "installing", j.progress
            detail = (j.bytes_transferred, j.total_bytes, j.transfer_rate_bps, j.eta_seconds, j.message)
        elif g.kind == "game" and g.title_id in installed:
            row = installed[g.title_id]
            r = jobs.active_removal(row.id)
            state = "removing" if r else "update" if row.status == "update_available" else "installed"
            progress = r and r["progress"]
            if r:
                detail = (r["bytes_done"], r["bytes_total"], "", "", r["message"])
        else:
            reason = jobs.blocker(db, c, g)
            state = f"blocked:{reason}" if reason else "installable"
        lines.append("\t".join(map(_field, (g.id, g.title, g.title_id, g.version, g.size_bytes, state,
                                             f"{progress:.3f}" if progress is not None else "",
                                             int(bool(g.cover_path or _poster(g.title_id))), g.kind, g.format, *detail,
                                             int(g.favorite), int(g.created_at.timestamp()),
                                             "|".join(x.replace("|", " ") for x in _genres(g)), int(bool(_logo(g)))))))
    t = lambda g, suffix: _stored(g.title_id, suffix)  # noqa: E731
    unknown = {(g.title_id, g.content_id) for g in games if g.title_id and not (
        t(g, ".json") and (g.cover_path or t(g, ".jpg") or t(g, ".none")) and (t(g, "-logo.png") or t(g, "-logo.none")))}
    if unknown:
        background.add_task(_warm_store, sorted(unknown, key=lambda t: t[0]))
    return "".join(f"{line}\n" for line in lines)


@console_api.get("/drives", response_class=PlainTextResponse)
def console_drives(c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """The drives games can be installed to: id, label, free bytes, total bytes (0: unknown without the helper)."""
    return "".join("\t".join(map(_field, (d.id, d.label, d.free_bytes, d.total_bytes))) + "\n"
                   for d in list_drives(c.id, db) if d.is_install_target)


@console_api.get("/covers/{game_id}")
def console_cover(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """The cover as raw RGBA pixels, COVER_SIZE, cropped to fill. The console has no image decoder. Without a cover
    of its own the game gets its PlayStation Store poster, like the PC app's tiles, once _warm_store has it."""
    # ponytail: resized on every request; cache the bytes in COVERS_DIR if the grid ever loads slowly.
    g = _game(db, game_id)
    try:
        path = _cover_file(g)
    except HTTPException:
        if not (path := _poster(g.title_id)):
            raise
    with Image.open(path) as im:
        data = ImageOps.fit(im.convert("RGBA"), COVER_SIZE).tobytes()
    return Response(data, media_type="application/octet-stream")


@console_api.get("/logos/{game_id}")
def console_logo(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """The game's store logo as raw RGBA pixels, LOGO_SIZE: trimmed to its lettering, scaled to fit and set in the
    bottom-left corner, so it sits where the game panel's title would. 404 until _warm_store has it."""
    if not (path := _logo(_game(db, game_id))):
        _not_found()
    with Image.open(path) as im:
        logo = im.convert("RGBA")
    if box := logo.getchannel("A").getbbox():
        logo = logo.crop(box)
    logo = ImageOps.contain(logo, LOGO_SIZE, Image.LANCZOS)
    canvas = Image.new("RGBA", LOGO_SIZE, (0, 0, 0, 0))
    canvas.paste(logo, (0, LOGO_SIZE[1] - logo.height))
    return Response(canvas.tobytes(), media_type="application/octet-stream")


@console_api.post("/install/{game_id}", response_class=PlainTextResponse)
def console_install(game_id: str, drive: str | None = None, c: Console = Depends(console_from_token),
                    db: Session = Depends(get_db)):
    """`drive`: a drive id from /drives; without it the PC picks the target drive with the most free space."""
    try:
        jobs.queue(db, c, _game(db, game_id), drive or None)
    except ValueError as e:
        return PlainTextResponse(str(e), 409)
    return "Queued."


@console_api.post("/favorite/{game_id}", response_class=PlainTextResponse)
def console_favorite(game_id: str, on: bool, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    """Marks a game a favorite, or not, with its other copies: the PS5 shows them as one card."""
    g = _game(db, game_id)
    copies = (db.scalars(select(Game).where(Game.kind == "game", Game.title_id == g.title_id))
              if g.kind == "game" and g.title_id else [g])
    for copy in copies:
        copy.favorite = on
    db.commit()
    return "Added to Favorites." if on else "Taken out of Favorites."


@console_api.post("/install/{game_id}/cancel", response_class=PlainTextResponse)
def console_cancel(game_id: str, c: Console = Depends(console_from_token), db: Session = Depends(get_db)):
    j = db.scalar(select(InstallJob).where(InstallJob.console_id == c.id, InstallJob.game_id == game_id,
                                           InstallJob.status.in_(jobs.ACTIVE)))
    if not j or not jobs.cancel(db, j):
        return PlainTextResponse("It isn't installing.", 409)
    return "Canceled." if j.status == "canceled" else "Stopping."


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


@api.post("/consoles/{console_id}/console-app", response_model=ConsoleOut)
async def install_console_app(console_id: str, db: Session = Depends(get_db)):
    """Puts the PS5 app on the console (or updates it), paired with this PC. The monitor updates an installed one by itself."""
    c = db.get(Console, console_id) or _not_found()
    if not CONSOLE_APP_DIR.is_dir():
        raise HTTPException(409, {"error": "not_bundled", "message": "This build doesn't include the PS5 app."})
    try:
        await inventory.adapter_for(db, c).install_console_app(inventory.console_app_files(c))
        await inventory.refresh(db, c)
    except AdapterError as e:
        db.add(AuditLog(action="console_app_install", console_id=c.id, result="failed", details={"error": e.code}))
        db.commit()
        raise _adapter_failed(502, e)
    db.add(AuditLog(action="console_app_install", console_id=c.id, result="success", details={"version": __version__}))
    db.commit()
    inventory.app_logo_set.discard(c.id)  # a new tile needs the logo again; the monitor puts it there
    inventory.record(c.id, "online")
    return _console_out(c)


@api.post("/consoles/{console_id}/home-logo")
async def push_home_logo(console_id: str, body: SaveBackupIn, db: Session = Depends(get_db)):
    """Gives the game's home-screen page on the console the logo picked for it now (logos.apply): PlayStation Store's,
    fetched fresh, the user's own, or none. {"message"}."""
    c = db.get(Console, console_id) or _not_found()
    try:
        use = await logos.apply(db, c, body.title_id)
    except AdapterError as e:
        logos.audit(db, c, body.title_id, False, e.message)
        db.commit()
        raise _adapter_failed(502, e)
    message = "Logo removed from the home screen." if use == "none" else "Logo is on the home screen."
    logos.audit(db, c, body.title_id, True, message)
    db.commit()
    return {"message": message}


@api.get("/titles/{title_id}/home-logo")
def get_home_logo(title_id: str):
    """{"use": "store" | "custom" | "none"}: the home-screen logo picked for the game (logos.choice)."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    return {"use": logos.choice(title_id)}


class HomeLogoIn(BaseModel):
    use: Literal["store", "none"]


@api.put("/titles/{title_id}/home-logo")
def set_home_logo(title_id: str, body: HomeLogoIn):
    """Back to PlayStation Store's logo, or none at all. Consoles get it from /consoles/<id>/home-logo, or when they're
    next online, or within logos.SYNC_SECONDS (logos.restore)."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    logos.choose(title_id, body.use)
    return {"use": body.use}


HOME_LOGO_MAX = (2880, 1184)  # PlayStation Store's widest logos; smaller ones are kept as they are


@api.post("/titles/{title_id}/home-logo")
async def upload_home_logo(title_id: str, file: UploadFile):
    """The user's own home-screen logo, kept as a PNG (the helper takes nothing else) of at most HOME_LOGO_MAX."""
    TITLE_ID.fullmatch(title_id) or _not_found()
    data, _ = await _read_cover(file, "Logo")
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
    im.thumbnail(HOME_LOGO_MAX)
    out = io.BytesIO()
    im.save(out, "PNG")
    logos.choose(title_id, "custom", out.getvalue())
    return {"use": "custom"}


@api.get("/consoles/{console_id}/leftovers")
async def leftovers(console_id: str, db: Session = Depends(get_db)):
    """Hidden copies left on the console by installs, moves and removals that stopped partway. {"bytes", "paths"}."""
    c = db.get(Console, console_id) or _not_found()
    try:
        found = await inventory.adapter_for(db, c).list_leftovers()
    except AdapterError as e:
        raise _adapter_failed(502, e)
    return {"bytes": sum(found.values()), "paths": sorted(found)}


@api.post("/consoles/{console_id}/leftovers/clean")
async def clean_leftovers(console_id: str, db: Session = Depends(get_db)):
    """Deletes them all. Refused while anything Spela runs on that console could be writing one. {"bytes_freed"}."""
    c = db.get(Console, console_id) or _not_found()
    if reason := jobs.leftovers_blocker(db, c.id):
        raise HTTPException(409, {"error": "busy", "message": reason})
    try:
        freed = await inventory.adapter_for(db, c).remove_leftovers()
    except AdapterError as e:
        db.add(AuditLog(action="leftovers_clean", console_id=c.id, result="failed", details={"error": e.message}))
        db.commit()
        raise _adapter_failed(409 if e.code == "busy" else 502, e)
    db.add(AuditLog(action="leftovers_clean", console_id=c.id, result="success", details={"bytes_freed": freed}))
    db.commit()
    return {"bytes_freed": freed}


@api.delete("/consoles/{console_id}/console-app")
async def remove_console_app(console_id: str, db: Session = Depends(get_db)):
    """Deletes the PS5 app's folder and its home-screen tile. {"tile_left", "message"}."""
    c = db.get(Console, console_id) or _not_found()
    try:
        tile = await inventory.adapter_for(db, c).remove_console_app()
        await inventory.refresh(db, c)
    except AdapterError as e:
        db.add(AuditLog(action="console_app_remove", console_id=c.id, result="failed", details={"error": e.code}))
        db.commit()
        raise _adapter_failed(502, e)
    message = f"The PS5 app is removed from {c.name}. " + jobs.tile_note(tile)
    db.add(AuditLog(action="console_app_remove", console_id=c.id, result="success",
                    details={"tile_left": not tile.ok, "message": message}))
    db.commit()
    return {"tile_left": not tile.ok, "message": message}


def _not_found():
    raise HTTPException(404, {"error": "not_found", "message": "Not found."})


app.include_router(public)
app.include_router(api)
app.include_router(console_api)
if config.STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="frontend")
