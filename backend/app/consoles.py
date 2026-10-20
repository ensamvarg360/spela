"""Console adapter interface (project.md §9). Everything that talks to a PS5 goes
through a ConsoleAdapter; console-specific paths and endpoints stay in here."""
import asyncio
import base64
import binascii
import ftplib
import hmac
import http.client
import io
import json
import re
import socket
import os
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal, Protocol

from . import __version__, config
from .models import Console
from .pkg import PkgMetadata, _read_param_json, _read_param_sfo, parse_pkg

ProgressCallback = Callable[[int, int], None]  # (bytes_done, bytes_total)


class AdapterError(Exception):
    """Structured adapter failure; `code` is machine-readable, `message` (what happened) and `fix` (what to do about
    it, when there's something) are for the UI."""

    def __init__(self, code: str, message: str, fix: str = ""):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


RUN_LOADER = "Run your jailbreak on the PS5. Once its ELF loader is up, Spela sends the helper by itself within a minute."
WAKE_UP = "Press the PS button on the controller, or click Wake if you've saved the console's Remote Play key."
INSTALL_HELPER = "Click Install helper in Settings → Consoles, or run your jailbreak: Spela sends it by itself within a minute."
UPDATE_HELPER = "Click Update helper in Settings → Consoles, or wait: Spela replaces it by itself within a minute."


def not_answering(host: str) -> str:
    """What to check when nothing answers at `host`, with a warning when it isn't on this PC's network."""
    fix = ("Check that the PS5 is switched on, not in rest mode, and on the same network as this PC. Its IP address is "
           "under Settings → Network → Connection Status → View Connection Status on the PS5.")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))  # sends nothing: picks the network card that would reach `host`
            mine = s.getsockname()[0]
    except OSError:
        return fix
    # ponytail: assumes a /24 home network; read the real netmask if people hit false warnings
    if mine.rsplit(".", 1)[0] != host.rsplit(".", 1)[0]:
        fix += f" This PC is on {mine.rsplit('.', 1)[0]}.x, and {host} isn't, so check the address."
    return fix


class UnsupportedConsoleConnectionError(AdapterError):
    def __init__(self, connection_type: str):
        super().__init__("unsupported_connection", f"Connection type '{connection_type}' isn't supported yet.")


@dataclass
class ConsoleStatus:
    state: Literal["online", "standby", "degraded", "offline"]
    message: str = ""
    helper_status: dict = field(default_factory=dict)
    fix: str = ""  # what the user can do about it


@dataclass
class ConsoleInfo:
    firmware: str | None  # None = the connection can't tell
    model: str
    helper_status: dict = field(default_factory=dict)


@dataclass
class DriveInfo:
    external_id: str
    label: str
    path: str
    drive_type: Literal["internal", "usb", "network", "unknown"]
    total_bytes: int
    free_bytes: int


@dataclass
class InstalledGameInfo:
    title_id: str
    title: str
    version: str | None
    install_route: Literal["package_installer", "shadowmountplus", "unknown"]
    drive_external_id: str | None
    installed_path: str
    source_path: str | None
    size_bytes: int
    orphaned: bool = False  # a ShadowMountPlus tile whose files (source_path) are gone


@dataclass
class SpaceCheckResult:
    ok: bool
    free_bytes: int
    bytes_needed: int


@dataclass
class InstallResult:
    ok: bool
    message: str = ""
    installed: InstalledGameInfo | None = None
    checked_files: int = 0  # files whose size on the console was compared with the source after copying


@dataclass
class RemovalResult:
    ok: bool
    message: str = ""
    removed_paths: list[str] = field(default_factory=list)
    bytes_freed: int = 0


class ConsoleAdapter(Protocol):
    async def connect(self) -> ConsoleStatus: ...
    async def get_console_info(self) -> ConsoleInfo: ...
    async def list_drives(self) -> list[DriveInfo]: ...
    async def scan_installed_games(self) -> list[InstalledGameInfo]: ...
    async def get_icons(self, games: list[InstalledGameInfo]) -> dict[str, bytes]: ...  # title ID -> icon0.png
    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult: ...
    # `drive` is where the package is staged (the console installs it to its own default drive).
    async def install_package(self, package_path: str, progress_callback: ProgressCallback, drive: DriveInfo | None = None,
                              cancel: threading.Event | None = None) -> InstallResult: ...
    # `cancel` set = stop between chunks and raise AdapterError("canceled"); the partial upload is removed.
    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback,
                                        cancel: threading.Event | None = None) -> InstallResult: ...
    # Copies a folder's files into an installed ShadowMountPlus game folder, replacing files of the same name.
    async def add_to_game_folder(self, source_path: str, game: InstalledGameInfo, progress_callback: ProgressCallback,
                                 cancel: threading.Event | None = None) -> None: ...
    async def uninstall_package(self, title_id: str) -> RemovalResult: ...
    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult: ...
    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult: ...
    # Moves a ShadowMountPlus game to another drive's homebrew folder; returns (new path, removal of its old tile).
    async def move_shadowmount_source(self, game: InstalledGameInfo, drive_id: str, progress_callback: ProgressCallback,
                                      cancel: threading.Event | None = None) -> tuple[str, RemovalResult]: ...
    async def install_helper(self) -> str: ...
    async def stop_helper(self) -> None: ...  # until the PS5 restarts, or Install helper sends it again
    async def install_console_app(self, files: dict[str, bytes]) -> None: ...  # relative path -> content
    async def remove_console_app(self) -> RemovalResult: ...  # its folder, then its tile (ok False: tile left)
    # Hidden `.<name>.partial` copies in the drives' homebrew folders, left by installs, moves and removals that
    # stopped partway: path -> bytes. remove_leftovers deletes them all and returns the bytes freed.
    async def list_leftovers(self) -> dict[str, int]: ...
    async def remove_leftovers(self) -> int: ...  # AdapterError("busy") while the helper copies a game between drives
    async def get_home_db(self) -> bytes: ...  # the home screen's database (HOME_DB), read-only copy
    # Copies every save folder of `title_id` (all users) under `dest`, mirroring console paths. Reads only. Returns files copied.
    async def backup_saves(self, title_id: str, dest: Path, progress_callback: ProgressCallback) -> int: ...
    async def set_home_logo(self, title_id: str, png: bytes) -> None: ...  # raises AdapterError


GiB = 1024**3
TITLE_ID = re.compile(r"[A-Z]{4}\d{5}")


class MockConsoleAdapter:
    """Deterministic fake PS5 for tests and UI work. State lives in the instance, so
    installs and removals are visible to later calls on the same adapter."""

    MOCK_GAME_SIZE = 40 * GiB

    def __init__(self, console: Console | None = None, online: bool = True):
        self.online = online
        self.leftovers: dict[str, int] = {}  # hidden .partial path -> bytes
        self.drives = [
            DriveInfo("internal", "Internal SSD", "/user", "internal", 825 * GiB, 210 * GiB),
            DriveInfo("usb0", "USB Extended", "/mnt/usb0", "usb", 2000 * GiB, 1500 * GiB),
        ]
        self.games = [
            InstalledGameInfo("PPSA01234", "Mock Racer", "01.000.000", "package_installer", "internal",
                              "/user/app/PPSA01234", None, 50 * GiB),
            InstalledGameInfo("PPSA05678", "Mock Quest", "01.020.000", "shadowmountplus", "usb0",
                              "/mnt/usb0/shadowmount/PPSA05678.ffpkg", "/mnt/usb0/shadowmount/PPSA05678.ffpkg", 30 * GiB),
        ]
        self.icons = {g.title_id for g in self.games}  # home-screen icons
        self.logos: dict[str, bytes] = {}  # title ID -> home-screen logo
        self.added: list[tuple[str, str]] = []  # (source folder, game folder) of each add_to_game_folder

    def _check(self):
        if not self.online:
            raise AdapterError("console_offline", "The console isn't reachable.")

    def _drive(self, external_id: str) -> DriveInfo:
        for d in self.drives:
            if d.external_id == external_id:
                return d
        raise AdapterError("drive_not_found", f"No drive '{external_id}' on this console.")

    def _install(self, path: str, route, drive: DriveInfo, installed_path: str, source_path: str | None, progress) -> InstallResult:
        # ponytail: title ID from the file name; real adapters read it from the package/image.
        m = TITLE_ID.search(path)
        if not m:
            raise AdapterError("title_id_unknown", "Can't tell the title ID from this file.")
        size = self.MOCK_GAME_SIZE
        if drive.free_bytes < size:
            return InstallResult(False, "Not enough free space.")
        for done in (0, size // 2, size):
            progress(done, size)
        drive.free_bytes -= size
        game = InstalledGameInfo(m[0], m[0], None, route, drive.external_id,
                                 installed_path.format(title_id=m[0]), source_path, size)
        self.games = [g for g in self.games if g.title_id != m[0]] + [game]
        self.icons.add(m[0])
        return InstallResult(True, "Installed.", replace(game))

    async def connect(self) -> ConsoleStatus:
        if not self.online:
            return ConsoleStatus("offline", "No response.")
        return ConsoleStatus("online", "", {"ftp": "ok", "installer": "ok", "shadowmountplus": "ok", "helper": __version__})

    async def get_console_info(self) -> ConsoleInfo:
        self._check()
        return ConsoleInfo("4.03", "CFI-1216A", {"ftp": "ok", "installer": "ok", "shadowmountplus": "ok", "helper": __version__})

    async def list_drives(self) -> list[DriveInfo]:
        self._check()
        return [replace(d) for d in self.drives]

    async def scan_installed_games(self) -> list[InstalledGameInfo]:
        self._check()
        return [replace(g) for g in self.games]

    async def get_icons(self, games: list[InstalledGameInfo]) -> dict[str, bytes]:
        self._check()
        return {}

    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult:
        self._check()
        free = self._drive(drive_id).free_bytes
        return SpaceCheckResult(free >= bytes_needed, free, bytes_needed)

    async def install_package(self, package_path: str, progress_callback: ProgressCallback, drive: DriveInfo | None = None,
                              cancel: threading.Event | None = None) -> InstallResult:
        self._check()
        return self._install(package_path, "package_installer", self._drive("internal"),
                             "/user/app/{title_id}", None, progress_callback)

    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback,
                                        cancel: threading.Event | None = None) -> InstallResult:
        self._check()
        if cancel and cancel.is_set():
            raise AdapterError("canceled", "Canceled.")
        drive = self._drive(target_drive.external_id)
        name = re.split(r"[\\/]", source_path)[-1]
        dest = f"{drive.path}/shadowmount/{name}"
        return self._install(source_path, "shadowmountplus", drive, dest, dest, progress_callback)

    async def add_to_game_folder(self, source_path: str, game: InstalledGameInfo, progress_callback: ProgressCallback,
                                 cancel: threading.Event | None = None) -> None:
        self._check()
        if not any(g.source_path == game.source_path for g in self.games):
            raise AdapterError("not_found", f"{game.source_path} isn't on the console any more.")
        progress_callback(1, 1)
        self.added.append((source_path, game.source_path))

    def _remove(self, title_id: str, route: str) -> RemovalResult:
        game = next((g for g in self.games if g.title_id == title_id and g.install_route == route), None)
        if not game:
            return RemovalResult(False, f"{title_id} isn't installed that way.")
        self.games.remove(game)
        if game.drive_external_id:
            self._drive(game.drive_external_id).free_bytes += game.size_bytes
        return RemovalResult(True, "Removed.", [game.installed_path], game.size_bytes)

    async def uninstall_package(self, title_id: str) -> RemovalResult:
        self._check()
        result = self._remove(title_id, "package_installer")
        if result.ok:
            self.icons.discard(title_id)
        return result

    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult:
        self._check()
        result = self._remove(installed_game.title_id, "shadowmountplus")
        progress_callback(result.bytes_freed, result.bytes_freed)
        return result  # icon stays until remove_home_screen_icon, like the real thing

    async def move_shadowmount_source(self, game: InstalledGameInfo, drive_id: str, progress_callback: ProgressCallback,
                                      cancel: threading.Event | None = None) -> tuple[str, RemovalResult]:
        self._check()
        if cancel and cancel.is_set():
            raise AdapterError("canceled", "Canceled.")
        g = next((g for g in self.games if g.source_path == game.source_path and g.title_id == game.title_id), None)
        if not g:
            raise AdapterError("not_found", f"{game.source_path} isn't on the console any more.")
        drive = self._drive(drive_id)
        progress_callback(g.size_bytes, g.size_bytes)
        g.drive_external_id = drive.external_id
        g.installed_path = g.source_path = f"{drive.path}/homebrew/{game.source_path.rpartition('/')[2]}"
        return g.source_path, RemovalResult(True, "Tile removed.")

    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult:
        self._check()
        if title_id not in self.icons:
            return RemovalResult(False, f"No home-screen icon for {title_id}.")
        self.icons.discard(title_id)
        return RemovalResult(True, "Icon removed.")

    async def install_helper(self) -> str:
        self._check()
        return __version__

    async def stop_helper(self) -> None:
        self._check()

    async def install_console_app(self, files: dict[str, bytes]) -> None:
        self._check()
        self.console_app = dict(files)

    async def remove_console_app(self) -> RemovalResult:
        self._check()
        self.console_app = {}
        return RemovalResult(True, "Tile removed.")

    async def list_leftovers(self) -> dict[str, int]:
        self._check()
        return dict(self.leftovers)

    async def remove_leftovers(self) -> int:
        self._check()
        freed = sum(self.leftovers.values())
        self.leftovers.clear()
        return freed

    async def backup_saves(self, title_id: str, dest: Path, progress_callback: ProgressCallback) -> int:
        self._check()
        save = dest / "user/home/10000000/savedata_prospero" / title_id / f"sdimg_SAVE{title_id}"
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_bytes(b"mock save")
        progress_callback(9, 9)
        return 1

    async def get_home_db(self) -> bytes:
        self._check()
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE tbl_contentinfo (titleId TEXT PRIMARY KEY, conceptId INT)")
        db.executemany("INSERT INTO tbl_contentinfo VALUES (?, ?)", [(g, int(g[4:])) for g in self.icons])  # concept = digits
        return db.serialize()

    async def set_home_logo(self, title_id: str, png: bytes) -> None:
        self._check()
        if title_id not in self.icons:
            raise AdapterError("not_found", "The home screen has no entry for this game.")
        self.logos[title_id] = png


# ponytail: PS5 paths from community docs, not yet checked on a real console. Adjust them here after the
# first hardware test. (external_id, label, mount path, type, user-data root, ShadowMountPlus source folder)
FTP_DRIVES = [
    ("internal", "Internal SSD", "/user", "internal", "/user", "/data/homebrew"),
    *((f"ext{i}", f"Extended storage {i}", f"/mnt/ext{i}", "internal", f"/mnt/ext{i}/user", f"/mnt/ext{i}/homebrew") for i in range(2)),
    *((f"usb{i}", f"USB drive {i}", f"/mnt/usb{i}", "usb", f"/mnt/usb{i}/user", f"/mnt/usb{i}/homebrew") for i in range(8)),
]
IMAGE_EXTS = (".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")
HELPER_ELF = Path(__file__).parent / "payload" / "spela.elf"  # built from payload/ by payload/build.ps1
HELPER_KEY_SLOT = b"storefront-helper-key:" + b"?" * 64  # KEY_SLOT in payload/main.c
HELPER_KEY_HEADER = "X-Storefront-Key"


DDP_PORT = 9302  # Sony's device discovery (UDP), what Remote Play uses to find and wake a PS5
DDP_PROBE = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"


def parse_ddp(data: bytes, ip: str) -> dict | None:
    """A discovery answer ("HTTP/1.1 200 Ok" or "... 620 Server Standby", then "key:value" lines) as a found PS5,
    or None for anything else (a PS4 answers too)."""
    lines = data.decode(errors="replace").splitlines()
    if not lines or not lines[0].startswith("HTTP/"):
        return None
    h = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
    if h.get("host-type", "").strip().upper() != "PS5":
        return None
    return {"ip": ip, "name": h.get("host-name", "").strip() or ip, "state": "standby" if " 620 " in lines[0] else "online"}


def discover(seconds: float = 2.0) -> list[dict]:
    """PS5s on this PC's network that answer Sony's discovery probe: switched on, or in rest mode with "Stay connected
    to the internet" on. Asks the whole network (255.255.255.255) and this PC's own subnet, which Windows sends out
    the right network card even when there are several."""
    targets = {"255.255.255.255"}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 9))  # sends nothing: picks the network card with the default route
            targets.add(s.getsockname()[0].rsplit(".", 1)[0] + ".255")  # ponytail: /24, like not_answering()
        except OSError:
            pass
    found: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.25)
        for _ in range(2):  # UDP: a probe can get lost
            for t in targets:
                try:
                    s.sendto(DDP_PROBE, (t, DDP_PORT))
                except OSError:
                    pass
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                break
            if ps5 := parse_ddp(data, ip):
                found[ip] = ps5
    return sorted(found.values(), key=lambda p: p["name"].lower())


def wake_credential(key: str) -> str:
    """The number a wake-up message carries, from the console's RP-RegistKey as Remote Play apps show it: 8 hex
    characters (as Chiaki shows it), that key base64-encoded (as Chiaki's settings file keeps it), or already the
    decimal number. Raises ValueError for anything else."""
    k = key.strip()
    if re.fullmatch(r"\d{9,20}", k):  # decimal: a hex key has at most 8 characters
        return str(int(k))
    if not re.fullmatch(r"[0-9a-fA-F]{1,16}", k):
        try:
            k = base64.b64decode(k, validate=True).rstrip(b"\0").decode("ascii")
        except (binascii.Error, UnicodeDecodeError):
            k = ""
        if not re.fullmatch(r"[0-9a-fA-F]{1,16}", k):
            raise ValueError("That isn't a Remote Play key. Paste the RP-RegistKey: 8 letters and digits, like 1a2b3c4d.")
    return str(int(k, 16))


def send_wake(host: str, credential: str):
    """Sends Sony's wake-up message (the one Remote Play sends) to a console in rest mode. No answer comes back;
    the console turns on if the key is the one it registered."""
    msg = (f"WAKEUP * HTTP/1.1\nclient-type:vr\nauth-type:R\nmodel:w\napp-type:r\nuser-credential:{credential}\n"
           "device-discovery-protocol-version:00030010\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(msg, (host, DDP_PORT))


def helper_key(console_id: str | None) -> str:
    """The key the helper on this console demands on every request. Derived, not stored: one per console and PC."""
    return hmac.new(config.SECRET_KEY.encode(), f"helper:{console_id}".encode(), "sha256").hexdigest()


def helper_elf(key: str) -> bytes:
    """The bundled helper with `key` written into it, so it answers only to this PC."""
    elf = HELPER_ELF.read_bytes()
    if elf.count(HELPER_KEY_SLOT) != 1:
        raise AdapterError("helper_broken", "The bundled helper has no place for its key. Rebuild it with payload/build.ps1.")
    return elf.replace(HELPER_KEY_SLOT, HELPER_KEY_SLOT[:-64] + key.encode())
# The PS5 app (console-app/, built by console-app/build.ps1). ShadowMountPlus shows it as a home-screen title.
CONSOLE_APP_TITLE_ID = "PPSA99787"
CONSOLE_APP_DIR = Path(__file__).parent / "console_app" / CONSOLE_APP_TITLE_ID
CONSOLE_APP_PATH = f"/data/homebrew/{CONSOLE_APP_TITLE_ID}"
HOME_DB = "/system_data/priv/mms/app.db"  # the home screen's database: titles, concept IDs, tile images
SAVE_HOME = "/user/home"  # one folder per user, holding their save data (FTPConsoleAdapter._backup_saves)


class _FTP(ftplib.FTP):
    """ftplib plus the two reads the adapter needs; HelperFS offers the same methods over the helper payload."""

    def ls(self, path: str) -> dict[str, tuple[bool, int]]:
        """{name: (is_dir, size)} from a Unix-style LIST. ftpsrv on the PS5 has no NLST."""
        lines = []
        try:
            self.retrlines(f"LIST {path}", lines.append)
        except ftplib.error_perm:  # missing folder
            return {}
        entries = {}
        for line in lines:  # "drwxr-xr-x 2 0 0 512 Oct 07 01:15 name with spaces"
            parts = line.split(None, 8)
            if len(parts) == 9 and parts[8] not in (".", ".."):
                entries[parts[8]] = (parts[0].startswith("d"), int(parts[4]) if parts[4].isdigit() else 0)
        return entries

    def get(self, path: str) -> bytes | None:
        buf = io.BytesIO()
        try:
            self.retrbinary(f"RETR {path}", buf.write)
        except ftplib.error_perm:
            return None
        return buf.getvalue()

    def fetch(self, path: str, write: Callable[[bytes], object]):
        """Streams a file to `write` in chunks, for files too big to hold in memory."""
        self.retrbinary(f"RETR {path}", write)

    def make_executable(self, path: str):
        """A file STOR creates has no execute bit, and the PS5 won't start an eboot.bin without it (CE-107750-0)."""
        try:
            self.sendcmd(f"SITE CHMOD 777 {path}")
        except ftplib.error_perm:
            pass  # a server without SITE CHMOD


class FTPConsoleAdapter:
    """Talks to an FTP payload on the PS5 (stdlib ftplib, one login per call). Installs and removals go through
    ShadowMountPlus only (no package installer on the console)."""

    TIMEOUT = 10
    HELPER_PORT = 7735  # our helper payload (payload/main.c)
    LOADER_PORT = 9021  # the jailbreak's ELF loader
    DDP_PORT = DDP_PORT  # answers in rest mode too

    def __init__(self, console: Console, password: str | None = None):
        self.host, self.port = console.ip_address, console.port
        self.user, self.password = console.username or "anonymous", password or ""
        self.key = helper_key(console.id)

    def _session(self) -> ftplib.FTP:
        ftp = _FTP(timeout=self.TIMEOUT, encoding="utf-8")
        try:
            ftp.connect(self.host, self.port)
        except ConnectionRefusedError:  # the console answered, but nothing listens on the port
            raise AdapterError("helper_unreachable", "The console is on, but its FTP server isn't running.",
                               f"Start your FTP payload on the PS5, and check that it listens on port {self.port}.")
        except (OSError, EOFError, ftplib.Error):
            raise AdapterError("console_offline", f"Nothing answers at {self.host}.", not_answering(self.host))
        try:
            ftp.login(self.user, self.password)
            ftp.voidcmd("TYPE I")
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
            raise AdapterError("auth_failed", "The FTP server rejected the username or password.",
                               "Most PS5 FTP payloads need no login: clear both under Settings → Consoles → Edit.")
        return ftp

    def _call(self, fn):
        ftp = self._session()
        try:
            return fn(ftp)
        except ftplib.error_perm as e:  # the console answered "no" (permissions, full disk, bad path)
            raise AdapterError("ftp_refused", f"The console refused: {e}")
        except (OSError, EOFError, ftplib.Error) as e:
            raise AdapterError("connection_lost", f"The connection to the console failed: {e}")
        finally:
            ftp.close()

    async def _run(self, fn):
        return await asyncio.to_thread(self._call, fn)

    def _helper(self, path: str = "/info", method: str = "GET") -> dict | None:
        """Calls the helper payload; None when it isn't running. It reports what FTP can't: firmware and drive space."""
        req = urllib.request.Request(f"http://{self.host}:{self.HELPER_PORT}{path}", method=method,
                                     headers={HELPER_KEY_HEADER: self.key})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return None

    def _drives(self, ftp: ftplib.FTP) -> list[tuple[DriveInfo, str, str]]:
        info = self._helper()
        space = {m["mount"]: (m["total"], m["free"]) for m in info["mounts"]} if info else {}
        drives = []
        for ext, label, path, kind, user, homebrew in FTP_DRIVES:
            # unused mount points exist as empty folders, so a drive counts only if its mount point has contents
            if ftp.ls(path):
                # the drive's own mount, or the deepest one above it (never "/": that's not the drive)
                mount = max((m for m in space if path == m or path.startswith(m + "/")), key=len, default=None)
                total, free = space.get(mount, (0, 0))
                drives.append((DriveInfo(ext, label, path, kind, total, free), user, homebrew))
        return drives

    @staticmethod
    def _meta(tid: str, data: bytes | None) -> PkgMetadata:
        meta = PkgMetadata(platform="ps4_pkg" if tid.startswith("CUSA") else "ps5_pkg")
        try:
            (_read_param_sfo if meta.platform == "ps4_pkg" else _read_param_json)(meta, data)
        except (ValueError, KeyError, struct.error, AttributeError):
            pass  # unreadable metadata: the title ID stands in for the title
        return meta

    def _orphan(self, ftp: ftplib.FTP, tid: str, path: str, files: dict, user: str, homebrews: dict[str, str],
                listings: dict[str, dict]) -> InstalledGameInfo | None:
        """The ShadowMountPlus tile at `path` when the files its .lnk names are gone; same rule as the helper's
        uninstall_tile (payload/main.c). Judged only for a homebrew folder on a plugged-in drive: an unplugged
        USB drive's games aren't orphans."""
        lnk = (ftp.get(f"{path}/mount_img.lnk") if "mount_img.lnk" in files else None) or ftp.get(f"{path}/mount.lnk")
        source = ((lnk or b"").decode(errors="replace").splitlines() or [""])[0]
        parent, _, name = source.rpartition("/")
        if parent not in homebrews or not name:
            return None
        if parent not in listings:
            listings[parent] = ftp.ls(parent)
        if name in listings[parent]:
            return None
        meta = self._meta(tid, ftp.get(f"{user}/appmeta/{tid}/param.json"))
        return InstalledGameInfo(tid, meta.title or tid, meta.version, "shadowmountplus", homebrews[parent],
                                 path, source, 0, orphaned=True)

    def _games(self, ftp: ftplib.FTP) -> list[InstalledGameInfo]:
        games, drives, listings = [], self._drives(ftp), {}
        homebrews = {hb: d.external_id for d, _, hb in drives}
        for drive, user, homebrew in drives:
            for tid in ftp.ls(f"{user}/app"):
                if not TITLE_ID.fullmatch(tid) or tid.startswith("NPXS"):  # NPXS = system apps
                    continue
                path = f"{user}/app/{tid}"
                files = ftp.ls(path)
                if "mount.lnk" in files:  # ShadowMountPlus stub, listed from its homebrew source below unless it's gone
                    if orphan := self._orphan(ftp, tid, path, files, user, homebrews, listings):
                        games.append(orphan)
                    continue
                if "app.pkg" not in files:  # homebrew launcher
                    continue
                param = "param.sfo" if tid.startswith("CUSA") else "param.json"
                meta = self._meta(tid, ftp.get(f"{user}/appmeta/{tid}/{param}"))
                games.append(InstalledGameInfo(tid, meta.title or tid, meta.version, "package_installer",
                                               drive.external_id, path, None, files["app.pkg"][1]))
            for name, (is_dir, size) in ftp.ls(homebrew).items():
                path, m = f"{homebrew}/{name}", TITLE_ID.search(name)
                if name.startswith("."):  # hidden, e.g. ".PPSA25381-app0.gc-moving.tmp" mid-copy
                    continue
                if is_dir:  # folder dump; size stays 0, summing a tree over FTP is too slow
                    meta, size = self._meta("PPSA", ftp.get(f"{path}/sce_sys/param.json")), 0
                elif name.lower().endswith(IMAGE_EXTS):
                    meta = PkgMetadata()
                else:
                    continue
                tid = meta.title_id or (m and m[0])
                if tid and tid != CONSOLE_APP_TITLE_ID:  # no title ID: other homebrew, not a game
                    games.append(InstalledGameInfo(tid, meta.title or name, meta.version, "shadowmountplus",
                                                   drive.external_id, path, path, size))
        return games

    async def _helper_status(self) -> tuple[dict, dict | None]:
        banner, app = await self._run(lambda f: (f.getwelcome(), f.get(f"{CONSOLE_APP_PATH}/assets/version.txt")))
        info = await asyncio.to_thread(self._helper)
        return {"ftp": "ok", "banner": banner, "helper": info["version"] if info else "missing",
                "console_app": app.decode(errors="replace").strip() if app else "missing"}, info

    def _ddp(self) -> bytes:
        """Status line of Sony's discovery probe (what Remote Play uses): "... 200 Ok" awake, "... 620 Server Standby"
        in rest mode, b"" for no answer. In rest mode it needs "Stay connected to the internet" on in the PS5's settings."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            try:
                s.sendto(DDP_PROBE, (self.host, self.DDP_PORT))
                return s.recv(2048).split(b"\n", 1)[0]
            except OSError:
                return b""

    def _asleep(self) -> bool:
        return b" 620 " in self._ddp()

    async def connect(self) -> ConsoleStatus:
        try:
            status, _ = await self._helper_status()
        except AdapterError as e:
            if await asyncio.to_thread(self._asleep):  # payloads don't run in rest mode, so FTP fails too
                return ConsoleStatus("standby", "The console is in rest mode.", {"ftp": e.code}, WAKE_UP)
            return ConsoleStatus("offline" if e.code == "console_offline" else "degraded", e.message, {"ftp": e.code}, e.fix)
        return ConsoleStatus("online", "", status)

    async def get_console_info(self) -> ConsoleInfo:
        status, info = await self._helper_status()
        return ConsoleInfo(info and info["firmware"] or None, info and info["model"] or "unknown", status)

    def _install_helper(self) -> str:
        self._helper("/quit", "POST")  # a running copy frees the port for the new one
        try:
            with socket.create_connection((self.host, self.LOADER_PORT), timeout=self.TIMEOUT) as s:
                s.sendall(helper_elf(self.key))
        except OSError:
            raise AdapterError("loader_unreachable", f"The PS5's ELF loader isn't running (port {self.LOADER_PORT}).",
                               "Run your jailbreak on the PS5, wait until it says the loader is up, then try again.")
        for _ in range(15):  # it may wait for the old copy to let go of the port
            time.sleep(1)
            if (info := self._helper()) and info.get("version") == __version__:
                return __version__
        raise AdapterError("helper_not_started", "The helper was sent but didn't start.",
                           f"Look for a message on the PS5. Another payload may be using port {self.HELPER_PORT}: restart "
                           "the PS5, run your jailbreak, and try again.")

    async def install_helper(self) -> str:
        """Sends the bundled helper payload through the console's ELF loader. Returns the running version."""
        return await asyncio.to_thread(self._install_helper)

    def _stop_helper(self):
        if self._helper() is None:
            return  # not running: nothing to stop
        self._helper("/quit", "POST")
        for _ in range(10):  # it answers, then exits
            time.sleep(0.5)
            if self._helper() is None:
                return
        raise AdapterError("helper_still_running", "The helper didn't stop.",
                           "Restart the PS5: like any payload, the helper doesn't survive a restart.")

    async def stop_helper(self) -> None:
        """Asks the helper payload to exit. It's only in the console's memory, so nothing is left on the PS5."""
        await asyncio.to_thread(self._stop_helper)

    async def list_drives(self) -> list[DriveInfo]:
        return [d for d, _, _ in await self._run(self._drives)]

    async def scan_installed_games(self) -> list[InstalledGameInfo]:
        return await self._run(self._games)

    async def get_icons(self, games: list[InstalledGameInfo]) -> dict[str, bytes]:
        """Each game's icon0.png: from a folder dump's sce_sys, else the appmeta folder the console keeps per title
        (ShadowMountPlus fills it for images too)."""
        def fetch(ftp: ftplib.FTP):
            icons = {}
            for g in games:
                user = g.installed_path.split("/app/")[0] if g.install_route == "package_installer" else "/user"
                for path in (f"{g.installed_path}/sce_sys/icon0.png", f"{user}/appmeta/{g.title_id}/icon0.png"):
                    if data := ftp.get(path):
                        icons[g.title_id] = data
                        break
            return icons
        return await self._run(fetch)

    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult:
        drive = next((d for d in await self.list_drives() if d.external_id == drive_id), None)
        if not drive:
            raise AdapterError("drive_not_found", f"No drive '{drive_id}' on this console.")
        return SpaceCheckResult(drive.free_bytes >= bytes_needed, drive.free_bytes, bytes_needed)

    BLOCK = 1 << 20  # upload chunk; progress and cancel are checked once per chunk

    @staticmethod
    def _rm_partial(ftp: ftplib.FTP, path: str):
        """Deletes one of our own `.<name>.partial` uploads, files first. Never called on anything else."""
        if not re.fullmatch(r".*/\.[^/]+\.partial(/.*)?", path):
            raise ValueError(f"refusing to delete {path}")
        try:  # an image upload is a file; listing a file would list the file itself
            return ftp.delete(path)
        except ftplib.error_perm:  # a folder: refused by FTP, and by the helper unless it's empty
            pass
        for name, (is_dir, _) in ftp.ls(path).items():
            if is_dir:
                FTPConsoleAdapter._rm_partial(ftp, f"{path}/{name}")
            else:
                ftp.delete(f"{path}/{name}")
        ftp.rmd(path)

    def _stage(self, ftp: ftplib.FTP, src: Path, drive_id: str, progress: ProgressCallback, cancel: threading.Event) -> InstallResult:
        homebrew = next((hb for d, _, hb in self._drives(ftp) if d.external_id == drive_id), None)
        if not homebrew:
            raise AdapterError("drive_not_found", f"No drive '{drive_id}' on this console.")
        try:
            ftp.mkd(homebrew)  # missing on a fresh USB drive
        except ftplib.error_perm:
            pass  # already there
        dest, tmp = f"{homebrew}/{src.name}", f"{homebrew}/.{src.name}.partial"
        existing = ftp.ls(homebrew)
        if src.name in existing:
            raise AdapterError("already_installed", f"{dest} already exists on the console.")
        if f".{src.name}.partial" in existing:  # left over from an interrupted install
            self._rm_partial(ftp, tmp)

        files = [(src, tmp)] if src.is_file() else []
        dirs = [] if src.is_file() else [tmp]
        for root, subdirs, names in os.walk(src):
            rel = Path(root).relative_to(src).as_posix()
            base = tmp if rel == "." else f"{tmp}/{rel}"
            dirs += [f"{base}/{d}" for d in sorted(subdirs)]
            files += [(Path(root) / n, f"{base}/{n}") for n in sorted(names)]
        total, done = sum(f.stat().st_size for f, _ in files), 0

        def chunk(block: bytes):
            nonlocal done
            if cancel.is_set():
                raise AdapterError("canceled", "Canceled.")
            done += len(block)
            progress(done, total)

        for d in dirs:
            ftp.mkd(d)
        for local, remote in files:
            with open(local, "rb") as f:
                ftp.storbinary(f"STOR {remote}", f, self.BLOCK, chunk)
        # Every file must be on the console at its full size before ShadowMountPlus may see it. Catches copies cut short
        # or files the console dropped; it doesn't read the data back, which would take as long as the copy.
        want = {remote[len(tmp):]: local.stat().st_size for local, remote in files}
        got = {"": ftp.ls(homebrew).get(f".{src.name}.partial", (False, -1))[1]} if src.is_file() else self._sizes(ftp, tmp)
        if got != want:
            bad = sorted(k for k in want.keys() | got.keys() if want.get(k) != got.get(k))
            self._rm_partial(ftp, tmp)
            raise AdapterError("verify_failed", f"The copy on the console didn't match the game on this PC ({len(bad)} "
                                                f"file(s) differ, e.g. {(bad[0] or src.name).lstrip('/')}), so it was deleted. Install it again.")
        ftp.rename(tmp, dest)  # only now does ShadowMountPlus see it
        m = TITLE_ID.search(src.name)
        return InstallResult(True, "Installed.", InstalledGameInfo(
            m[0] if m else "", src.name, None, "shadowmountplus", drive_id, dest, dest, total), len(files))

    @staticmethod
    def _sizes(ftp: ftplib.FTP, folder: str) -> dict[str, int]:
        """{"/relative/path": size} of every file under a folder on the console."""
        sizes, todo = {}, [""]
        while todo:
            rel = todo.pop()
            for name, (is_dir, size) in ftp.ls(folder + rel).items():
                if is_dir:
                    todo.append(f"{rel}/{name}")
                else:
                    sizes[f"{rel}/{name}"] = size
        return sizes

    def _stage_or_clean(self, source_path: str, drive_id: str, progress: ProgressCallback, cancel: threading.Event) -> InstallResult:
        src = Path(source_path)
        if not src.exists():
            raise AdapterError("source_missing", f"Can't read {source_path}.")
        try:
            return self._call(lambda ftp: self._stage(ftp, src, drive_id, progress, cancel))
        except AdapterError as e:
            if e.code == "canceled":  # the aborted session is unusable, so clean up in a new one
                self._call(lambda ftp: self._rm_partial(ftp, next(
                    f"{hb}/.{src.name}.partial" for d, _, hb in self._drives(ftp) if d.external_id == drive_id)))
            raise  # connection_lost leaves the partial; the next attempt removes it

    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback,
                                        cancel: threading.Event | None = None) -> InstallResult:
        """Uploads a folder dump or image into the drive's homebrew folder, where ShadowMountPlus picks it up.
        It's uploaded under a hidden `.partial` name and renamed when complete, so a half copy is never mounted."""
        return await asyncio.to_thread(self._stage_or_clean, source_path, target_drive.external_id,
                                       progress_callback, cancel or threading.Event())

    def _put_files(self, ftp: ftplib.FTP, files: dict[str, bytes]):
        made = set()  # subfolders already created this run
        for rel in sorted(files):
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in made:
                    try:
                        ftp.mkd(f"{CONSOLE_APP_PATH}/{d}")
                    except ftplib.error_perm:
                        pass  # already there
                    made.add(d)
            dest = f"{CONSOLE_APP_PATH}/{rel}"
            ftp.storbinary(f"STOR {dest}.partial", io.BytesIO(files[rel]), self.BLOCK)
            try:
                ftp.delete(dest)  # a running app's file can't be written to ("Text file busy"), but it can be replaced
            except ftplib.error_perm:
                pass  # a first install
            ftp.rename(f"{dest}.partial", dest)
            ftp.make_executable(dest)

    def _verify_files(self, ftp: ftplib.FTP, files: dict[str, bytes]):
        """Reads the app folder back: every file must be there at its full size, or the PS5 can't start it."""
        listings: dict[str, dict] = {}
        bad = []
        for rel, data in sorted(files.items()):
            folder, _, name = rel.rpartition("/")
            if folder not in listings:
                listings[folder] = ftp.ls(f"{CONSOLE_APP_PATH}/{folder}".rstrip("/"))
            if listings[folder].get(name, (False, -1))[1] != len(data):
                bad.append(rel)
        if bad:
            raise AdapterError("app_incomplete", f"The PS5 app didn't copy completely ({len(bad)} of {len(files)} files "
                               f"missing or the wrong size, for example {bad[0]}). Try Reinstall PS5 app again.")

    async def install_console_app(self, files: dict[str, bytes]) -> None:
        """Writes the PS5 app into /data/homebrew/<its title ID>; ShadowMountPlus adds its tile.
        ponytail: overwrites in place, so files dropped from a newer build linger. Fine while the file set is stable."""
        def put(ftp: ftplib.FTP):
            try:
                ftp.mkd(CONSOLE_APP_PATH)
            except ftplib.error_perm:
                pass  # an update
            self._put_files(ftp, files)
            self._verify_files(ftp, files)
        await self._run(put)

    async def remove_console_app(self) -> RemovalResult:
        """Deletes the PS5 app's folder, then its tile (which ShadowMountPlus would otherwise keep or add back)."""
        def rm(ftp: ftplib.FTP):
            tmp = f"{CONSOLE_APP_PATH.rpartition('/')[0]}/.{CONSOLE_APP_TITLE_ID}.partial"
            try:
                ftp.rename(CONSOLE_APP_PATH, tmp)  # hidden first, so ShadowMountPlus never mounts half of it
            except ftplib.error_perm:
                pass  # already gone; still remove a tile left behind
            if f".{CONSOLE_APP_TITLE_ID}.partial" in ftp.ls(tmp.rpartition("/")[0]):
                self._rm_partial(ftp, tmp)
        await self._run(rm)
        return await self.remove_home_screen_icon(CONSOLE_APP_TITLE_ID)

    def _game_entry(self, ftp: ftplib.FTP, game: InstalledGameInfo, verb: str) -> tuple[str, str]:
        """(homebrew folder, name) of an installed game. Only a direct entry of a homebrew folder (project.md §13
        allowlist), and only while the console still lists exactly that path with that title ID."""
        path = game.source_path or ""
        homebrew, _, name = path.rpartition("/")
        if (homebrew not in {hb for *_, hb in FTP_DRIVES} or not name or name.startswith(".")
                or any(ch in name for ch in "*?[]") or name == CONSOLE_APP_TITLE_ID):
            raise AdapterError("path_outside_allowlist", f"Won't {verb} {path}: it isn't a game in a homebrew folder.")
        if not any(g.source_path == path and g.title_id == game.title_id and not g.orphaned for g in self._games(ftp)):
            raise AdapterError("not_found", f"{path} isn't on the console any more, or holds a different game.")
        return homebrew, name

    def _add_to_folder(self, ftp: ftplib.FTP, src: Path, game: InstalledGameInfo, progress: ProgressCallback,
                       cancel: threading.Event):
        homebrew, name = self._game_entry(ftp, game, "change")
        if not ftp.ls(homebrew).get(name, (False,))[0]:
            raise AdapterError("not_a_folder", f"{game.source_path} is a disc image; only a game folder can take DLC files.")
        dirs, files = [], []
        for root, subdirs, names in os.walk(src):
            rel = Path(root).relative_to(src).as_posix()
            base = game.source_path if rel == "." else f"{game.source_path}/{rel}"
            dirs += [f"{base}/{d}" for d in sorted(subdirs)]
            files += [(Path(root) / n, f"{base}/{n}") for n in sorted(names)]
        total, done = sum(f.stat().st_size for f, _ in files), 0

        def chunk(block: bytes):
            nonlocal done
            if cancel.is_set():
                raise AdapterError("canceled", "Canceled. Some DLC files may already be in the game's folder.")
            done += len(block)
            progress(done, total)

        for d in dirs:
            try:
                ftp.mkd(d)
            except ftplib.error_perm:
                pass  # the game already has this folder
        # ponytail: written in place, not staged and renamed: an interrupted copy leaves some files; installing again
        # finishes it. Staging would mean renaming over the game's own files, which the console may refuse.
        for local, remote in files:
            with open(local, "rb") as f:
                ftp.storbinary(f"STOR {remote}", f, self.BLOCK, chunk)
        progress(total, total)

    async def add_to_game_folder(self, source_path: str, game: InstalledGameInfo, progress_callback: ProgressCallback,
                                 cancel: threading.Event | None = None) -> None:
        src = Path(source_path)
        if not src.is_dir():
            raise AdapterError("source_missing", f"Can't read {source_path}.")
        await self._run(lambda ftp: self._add_to_folder(ftp, src, game, progress_callback, cancel or threading.Event()))

    def _remove_source(self, ftp: ftplib.FTP, game: InstalledGameInfo, progress: ProgressCallback) -> RemovalResult:
        """Deletes one game's folder or image (see _game_entry for which)."""
        path = game.source_path or ""
        homebrew, name = self._game_entry(ftp, game, "delete")
        entries = ftp.ls(homebrew)
        is_dir, size = entries[name]
        tmp = f"{homebrew}/.{name}.partial"
        if f".{name}.partial" in entries:  # left over from an interrupted install
            self._rm_partial(ftp, tmp)
        # Hidden first, so ShadowMountPlus never mounts a half-deleted game. A symlink is renamed, never followed.
        # A connection lost mid-delete leaves the hidden .partial: Clean up on the Installed page deletes it.
        ftp.rename(path, tmp)
        files, dirs, todo = ([] if is_dir else [(tmp, size)]), [], ([tmp] if is_dir else [])
        while todo:  # list everything first, so progress knows the total
            d = todo.pop()
            dirs.append(d)
            for n, (sub, sz) in ftp.ls(d).items():
                if sub:
                    todo.append(f"{d}/{n}")
                else:
                    files.append((f"{d}/{n}", sz))
        total, done = sum(sz for _, sz in files), 0
        progress(0, total)
        for f, sz in files:
            ftp.delete(f)
            done += sz
            progress(done, total)
        for d in reversed(dirs):  # children were listed after their parent
            ftp.rmd(d)
        return RemovalResult(True, "Removed.", [path], total)

    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult:
        return await self._run(lambda ftp: self._remove_source(ftp, installed_game, progress_callback))

    def _leftovers(self, ftp: ftplib.FTP) -> dict[str, int]:
        found = {}
        for _, _, homebrew in self._drives(ftp):
            for name, (is_dir, size) in ftp.ls(homebrew).items():
                if re.fullmatch(r"\.[^/]+\.partial", name):
                    path = f"{homebrew}/{name}"
                    found[path] = sum(self._sizes(ftp, path).values()) if is_dir else size
        return found

    async def list_leftovers(self) -> dict[str, int]:
        return await self._run(self._leftovers)

    def _remove_leftovers(self, ftp: ftplib.FTP) -> int:
        copy = self._helper("/copy")  # a move between drives runs on the console, even after the PC app restarts
        if copy and copy.get("running"):
            raise AdapterError("busy", "The PS5 is copying a game to another drive. Clean up once that's done.")
        found = self._leftovers(ftp)
        for path in found:
            self._rm_partial(ftp, path)
        return sum(found.values())

    async def remove_leftovers(self) -> int:
        return await self._run(self._remove_leftovers)

    def _remove_tile(self, title_id: str) -> RemovalResult:
        req = urllib.request.Request(f"http://{self.host}:{self.HELPER_PORT}/uninstall?id={title_id}", method="POST",
                                     headers={HELPER_KEY_HEADER: self.key})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return RemovalResult(True, "Tile removed.")
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read())["error"]
            except (ValueError, KeyError, TypeError):
                why = e.reason
            return RemovalResult(False, "The helper is too old to do it." if e.code == 404 else str(why))
        except OSError:
            return RemovalResult(False, "The helper isn't running.")

    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult:
        """Asks the helper to remove a ShadowMountPlus tile whose game files are gone (payload/main.c, uninstall_tile)."""
        return await asyncio.to_thread(self._remove_tile, title_id)

    def _ask_helper(self, method: str, op: str, **query) -> dict:
        """One request to the helper; its JSON answer. Raises AdapterError when it says no or isn't running."""
        q = f"?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}" if query else ""
        req = urllib.request.Request(f"http://{self.host}:{self.HELPER_PORT}{op}{q}", method=method,
                                     headers={HELPER_KEY_HEADER: self.key})
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                body = r.read()
            return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read())["error"]
            except (ValueError, KeyError, TypeError):
                why = e.reason
            if why == "not_found":  # the helper's answer to an endpoint it doesn't have
                raise AdapterError("helper_outdated", "The helper is too old to do this.", UPDATE_HELPER)
            raise AdapterError("helper_refused", f"The console refused: {why}")
        except (OSError, ValueError):
            raise AdapterError("helper_unreachable", "The helper isn't running.", INSTALL_HELPER)

    MOVE_POLL_SECONDS = 1.0

    def _move(self, game: InstalledGameInfo, drive_id: str, progress: ProgressCallback,
              cancel: threading.Event) -> tuple[str, RemovalResult]:
        """The helper copies the game into a hidden `.partial` on the other drive (payload/main.c, copy_start), the sizes
        are checked as after an install, the original is deleted as a removal deletes it, its home-screen tile is
        removed, and only then is the copy renamed into place, so ShadowMountPlus adds it like a new install."""
        def plan(ftp):
            homebrew, name = self._game_entry(ftp, game, "move")
            target = next((hb for d, _, hb in self._drives(ftp) if d.external_id == drive_id), None)
            if not target:
                raise AdapterError("drive_not_found", f"No drive '{drive_id}' on this console.")
            if target == homebrew:
                raise AdapterError("same_drive", "It's already on that drive.")
            try:
                ftp.mkd(target)  # missing on a fresh USB drive
            except ftplib.error_perm:
                pass
            existing = ftp.ls(target)
            if name in existing:
                raise AdapterError("already_installed", f"{target}/{name} already exists on the console.")
            if f".{name}.partial" in existing:  # left over from an interrupted install or move
                self._rm_partial(ftp, f"{target}/.{name}.partial")
            return homebrew, name, target

        homebrew, name, target = self._call(plan)
        src, tmp, dest = f"{homebrew}/{name}", f"{target}/.{name}.partial", f"{target}/{name}"

        def clean():
            self._call(lambda ftp: f".{name}.partial" in ftp.ls(target) and self._rm_partial(ftp, tmp))

        self._ask_helper("POST", "/copy", p=src, to=tmp)
        asked_to_stop = False
        while True:
            cancel.wait(self.MOVE_POLL_SECONDS)
            if cancel.is_set() and not asked_to_stop:
                self._ask_helper("POST", "/copy/cancel")
                asked_to_stop = True
            st = self._ask_helper("GET", "/copy")
            progress(st["done"], st["total"])
            if not st["running"]:
                break
        if st["error"] or st["to"] != tmp:
            clean()
            if st["error"] == "canceled":
                raise AdapterError("canceled", "Canceled. The game is still on its old drive.")
            raise AdapterError("copy_failed", f"The console couldn't copy it: {st['error'] or 'another copy took over'}. "
                                              f"The game is still on its old drive.")

        def check(ftp):
            if ftp.ls(homebrew).get(name, (False,))[0]:
                same = self._sizes(ftp, src) == self._sizes(ftp, tmp)
            else:
                same = ftp.ls(homebrew)[name][1] == ftp.ls(target)[f".{name}.partial"][1]
            if not same:
                self._rm_partial(ftp, tmp)
                raise AdapterError("verify_failed", "The copy didn't match the original, so it was deleted. The game is "
                                                    "still on its old drive.")
        self._call(check)
        self._call(lambda ftp: self._remove_source(ftp, game, lambda done, total: None))
        tile = self._remove_tile(game.title_id)
        try:
            self._call(lambda ftp: ftp.rename(tmp, dest))
        except AdapterError as e:
            raise AdapterError(e.code, f"The game was copied and the original deleted, but the copy couldn't be put in "
                                       f"place: {e.message} Rename {tmp} to {dest} with an FTP payload.")
        return dest, tile

    async def move_shadowmount_source(self, game: InstalledGameInfo, drive_id: str, progress_callback: ProgressCallback,
                                      cancel: threading.Event | None = None) -> tuple[str, RemovalResult]:
        return await asyncio.to_thread(self._move, game, drive_id, progress_callback, cancel or threading.Event())

    def _backup_saves(self, ftp, title_id: str, dest: Path, progress: ProgressCallback) -> int:
        """Save data lives per user in /user/home/<user id>/: PS5 games' in savedata_prospero/<title ID>, PS4 games' in
        savedata/<title ID>, with metadata folders beside them. Any folder there holding a <title ID> entry is copied."""
        files = []  # (console path, size)
        for user, (is_dir, _) in ftp.ls(SAVE_HOME).items():
            for kind, (sub, _) in (ftp.ls(f"{SAVE_HOME}/{user}") if is_dir else {}).items():
                if entry := (ftp.ls(f"{SAVE_HOME}/{user}/{kind}") if sub else {}).get(title_id):
                    path = f"{SAVE_HOME}/{user}/{kind}/{title_id}"
                    files += [(path + rel, size) for rel, size in self._sizes(ftp, path).items()] if entry[0] else [(path, entry[1])]
        if not files:
            raise AdapterError("no_saves", f"The console has no save data for {title_id}.")
        total, done = sum(size for _, size in files), 0
        progress(0, total)
        for path, size in files:
            local = dest / path.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            with open(local, "wb") as f:
                def write(block: bytes):
                    nonlocal done
                    f.write(block)
                    done += len(block)
                    progress(done, total)
                ftp.fetch(path, write)
            if local.stat().st_size != size:
                raise AdapterError("backup_incomplete", f"{path} came back with {local.stat().st_size} of {size} bytes.")
        return len(files)

    async def backup_saves(self, title_id: str, dest: Path, progress_callback: ProgressCallback) -> int:
        if not TITLE_ID.fullmatch(title_id):
            raise AdapterError("bad_title_id", f"{title_id} isn't a title ID.")
        return await self._run(lambda ftp: self._backup_saves(ftp, title_id, dest, progress_callback))

    async def get_home_db(self) -> bytes:
        if not (data := await self._run(lambda fs: fs.get(HOME_DB))):
            raise AdapterError("home_db_unreadable", "Can't read the home screen's database on the console.")
        return data

    def _set_logo(self, title_id: str, png: bytes):
        req = urllib.request.Request(f"http://{self.host}:{self.HELPER_PORT}/logo?id={title_id}", data=png, method="POST",
                                     headers={HELPER_KEY_HEADER: self.key})
        try:
            with urllib.request.urlopen(req, timeout=60):
                return
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read())["error"]
            except (ValueError, KeyError, TypeError):
                why = e.reason
            if why == "not_found":  # the helper's answer to an endpoint it doesn't have
                raise AdapterError("helper_outdated", "The helper is too old to do this.", UPDATE_HELPER)
            if e.code == 404:  # the game isn't on the home screen (yet)
                raise AdapterError("not_found", f"The console says {why}.")
            raise AdapterError("logo_refused", f"The console refused: {why}")
        except OSError:
            raise AdapterError("helper_unreachable", "The helper isn't running.", INSTALL_HELPER)

    async def set_home_logo(self, title_id: str, png: bytes) -> None:
        """Has the helper put `png` on the game's home-screen page as its title logo (payload/main.c, set_logo)."""
        await asyncio.to_thread(self._set_logo, title_id, png)

    INSTALL_POLL_SECONDS = 5  # ponytail: a full inventory scan per poll; fine at this rate, a helper status call if it hurts
    INSTALL_STALL_SECONDS = 180  # no sign of the game, or its size not settling, for this long = give up waiting

    def _install_pkg(self, path: str, drive_id: str, progress: ProgressCallback, cancel: threading.Event) -> InstallResult:
        """UNTESTED on a console. Stages the package in the drive's homebrew folder, has the helper start the console's
        installer (payload/main.c, install_pkg), waits until the game's app.pkg has appeared and stopped growing, then
        deletes the staged copy. A package that never shows up leaves the staged copy in place: the installer may still
        be reading it."""
        meta = parse_pkg(path)
        if meta.dlc:
            raise AdapterError("dlc_unsupported", "DLC packages can't be installed yet.")
        if not (meta.title_id and meta.content_id):
            raise AdapterError("pkg_unreadable", "Can't read the title ID and content ID from this package.")
        staged = self._stage_or_clean(path, drive_id, progress, cancel)
        dest = staged.installed.installed_path
        try:
            self._ask_helper("POST", "/install", p=dest, cid=meta.content_id)
        except AdapterError:
            self._call(lambda ftp: ftp.delete(dest))
            raise
        last, since = None, time.monotonic()
        while True:
            if cancel.wait(self.INSTALL_POLL_SECONDS):
                raise AdapterError("canceled", f"Canceled. The console may still finish installing it; {dest} is left on it.")
            game = next((g for g in self._call(self._games)
                         if g.title_id == meta.title_id and g.install_route == "package_installer"), None)
            size = game.size_bytes if game else 0
            if game and size == last:
                break
            if size != last:
                last, since = size, time.monotonic()
            elif time.monotonic() - since > self.INSTALL_STALL_SECONDS:
                raise AdapterError("install_stalled", f"The console hasn't finished installing {meta.title_id}. Check the PS5 for "
                                                      f"a message; the copy at {dest} was left in place.")
        self._call(lambda ftp: ftp.delete(dest))
        progress(staged.installed.size_bytes, staged.installed.size_bytes)
        return InstallResult(True, "Installed.", game, 1)

    async def install_package(self, package_path: str, progress_callback: ProgressCallback, drive: DriveInfo | None = None,
                              cancel: threading.Event | None = None) -> InstallResult:
        if not drive:
            raise AdapterError("drive_not_found", "Pick a drive to stage the package on.")
        return await asyncio.to_thread(self._install_pkg, package_path, drive.external_id, progress_callback,
                                       cancel or threading.Event())

    async def uninstall_package(self, title_id: str) -> RemovalResult:
        raise AdapterError("not_implemented", "Not available yet. Delete it on the PS5 instead.")


class HelperFS:
    """The helper payload's file API (payload/main.c), with the methods of _FTP the adapter uses, so both
    connections share the adapter code. The console saying no raises ftplib.error_perm, as over FTP."""

    def __init__(self, host: str, port: int, timeout: float, info: dict, key: str):
        self.host, self.port, self.timeout, self.info, self.key = host, port, timeout, info, key

    def _req(self, method: str, op: str, path: str, to: str | None = None, f=None, block: int = 0, callback=None) -> bytes:
        query = urllib.parse.urlencode({"p": path, **({"to": to} if to else {})}, quote_via=urllib.parse.quote)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.putrequest(method, f"/{op}?{query}", skip_accept_encoding=True)
            size = f.seek(0, 2) if f else 0
            if f:
                f.seek(0)
            conn.putheader("Content-Length", str(size))
            conn.putheader(HELPER_KEY_HEADER, self.key)
            conn.endheaders()
            while f and (chunk := f.read(block)):
                conn.send(chunk)
                if callback:
                    callback(chunk)
            r = conn.getresponse()
            data = r.read()
        finally:
            conn.close()
        if r.status >= 400:
            try:
                why = json.loads(data)["error"]
            except (ValueError, KeyError, TypeError):
                why = r.reason
            raise ftplib.error_perm(f"{r.status} {why}")
        return data

    def ls(self, path: str) -> dict[str, tuple[bool, int]]:
        try:
            entries = json.loads(self._req("GET", "ls", path).decode(errors="replace"))
        except ftplib.error_perm:  # missing folder
            return {}
        return {e["name"]: (e["dir"], e["size"]) for e in entries}

    def get(self, path: str) -> bytes | None:
        try:
            return self._req("GET", "file", path)
        except ftplib.error_perm:
            return None

    def fetch(self, path: str, write: Callable[[bytes], object]):
        query = urllib.parse.urlencode({"p": path}, quote_via=urllib.parse.quote)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("GET", f"/file?{query}", headers={HELPER_KEY_HEADER: self.key})
            r = conn.getresponse()
            if r.status >= 400:
                raise ftplib.error_perm(f"{r.status} {r.read()[:200]!r}")
            while block := r.read(1 << 20):
                write(block)
        finally:
            conn.close()

    def storbinary(self, cmd: str, f, block: int, callback=None):
        self._req("PUT", "file", cmd.removeprefix("STOR "), f=f, block=block, callback=callback)

    def mkd(self, path: str):
        self._req("POST", "mkdir", path)

    def rename(self, src: str, dest: str):
        self._req("POST", "rename", src, to=dest)

    def make_executable(self, path: str):
        pass  # the helper creates files executable (receive() in payload/main.c)

    def delete(self, path: str):
        self._req("POST", "rm", path)

    rmd = delete

    def close(self):
        pass  # one HTTP connection per request


class HelperConsoleAdapter(FTPConsoleAdapter):
    """Talks only to our helper payload, no FTP server needed. install_helper goes through the ELF loader,
    so it works before the helper runs."""

    def __init__(self, console: Console, password: str | None = None):
        self.host, self.key = console.ip_address, helper_key(console.id)

    def _session(self) -> HelperFS:
        info = self._helper()
        if not info:
            raise AdapterError("helper_unreachable", "The helper isn't running.", INSTALL_HELPER)
        if info.get("version") != __version__:  # an older helper has no file API; its 404s would read as an empty console
            raise AdapterError("helper_outdated", f"The console runs helper v{info.get('version')}; this app needs "
                                                  f"v{__version__}.", UPDATE_HELPER)
        return HelperFS(self.host, self.HELPER_PORT, self.TIMEOUT, info, self.key)

    async def _helper_status(self) -> tuple[dict, dict | None]:
        info, app = await self._run(lambda fs: (fs.info, fs.get(f"{CONSOLE_APP_PATH}/assets/version.txt")))
        return {"helper": info["version"], "console_app": app.decode(errors="replace").strip() if app else "missing"}, info

    async def connect(self) -> ConsoleStatus:
        try:
            status, _ = await self._helper_status()
            return ConsoleStatus("online", "", status)
        except AdapterError as e:
            error = e
        info = await asyncio.to_thread(self._helper)
        helper = {"helper": info["version"] if info else "missing"}
        if info:  # running, but outdated
            return ConsoleStatus("degraded", error.message, helper, error.fix)
        ddp = await asyncio.to_thread(self._ddp)
        if b" 620 " in ddp:  # payloads don't run in rest mode
            return ConsoleStatus("standby", "The console is in rest mode.", helper, WAKE_UP)
        if b" 200 " in ddp:
            return ConsoleStatus("degraded", "Your PS5 is on, but the helper isn't running yet.", helper, RUN_LOADER)
        return ConsoleStatus("offline", f"Nothing answers at {self.host}.", helper,
                             not_answering(self.host) + " If it is on and awake, run your jailbreak: Spela sends the helper by itself.")


def get_adapter(console: Console, password: str | None = None) -> ConsoleAdapter:
    match console.connection_type:
        case "mock":
            return MockConsoleAdapter(console)
        case "helper":
            return HelperConsoleAdapter(console)
        case "ftp":
            return FTPConsoleAdapter(console, password)
        case _:  # http/custom land here until their adapters exist
            raise UnsupportedConsoleConnectionError(console.connection_type)
