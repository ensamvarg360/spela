"""Console adapter interface (project.md §9). Everything that talks to a PS5 goes
through a ConsoleAdapter; console-specific paths and endpoints stay in here."""
import asyncio
import ftplib
import http.client
import io
import json
import re
import socket
import os
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal, Protocol

from . import __version__
from .models import Console
from .pkg import PkgMetadata, _read_param_json, _read_param_sfo

ProgressCallback = Callable[[int, int], None]  # (bytes_done, bytes_total)


class AdapterError(Exception):
    """Structured adapter failure; `code` is machine-readable, `message` is for the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class UnsupportedConsoleConnectionError(AdapterError):
    def __init__(self, connection_type: str):
        super().__init__("unsupported_connection", f"Connection type '{connection_type}' isn't supported yet.")


@dataclass
class ConsoleStatus:
    state: Literal["online", "standby", "degraded", "offline"]
    message: str = ""
    helper_status: dict = field(default_factory=dict)


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
    async def install_package(self, package_path: str, progress_callback: ProgressCallback) -> InstallResult: ...
    # `cancel` set = stop between chunks and raise AdapterError("canceled"); the partial upload is removed.
    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback,
                                        cancel: threading.Event | None = None) -> InstallResult: ...
    async def uninstall_package(self, title_id: str) -> RemovalResult: ...
    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult: ...
    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult: ...
    async def install_helper(self) -> str: ...
    async def install_console_app(self, files: dict[str, bytes]) -> None: ...  # relative path -> content


GiB = 1024**3
TITLE_ID = re.compile(r"[A-Z]{4}\d{5}")


class MockConsoleAdapter:
    """Deterministic fake PS5 for tests and UI work. State lives in the instance, so
    installs and removals are visible to later calls on the same adapter."""

    MOCK_GAME_SIZE = 40 * GiB

    def __init__(self, console: Console | None = None, online: bool = True):
        self.online = online
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

    async def install_package(self, package_path: str, progress_callback: ProgressCallback) -> InstallResult:
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

    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult:
        self._check()
        if title_id not in self.icons:
            return RemovalResult(False, f"No home-screen icon for {title_id}.")
        self.icons.discard(title_id)
        return RemovalResult(True, "Icon removed.")

    async def install_helper(self) -> str:
        self._check()
        return __version__

    async def install_console_app(self, files: dict[str, bytes]) -> None:
        self._check()
        self.console_app = dict(files)


# ponytail: PS5 paths from community docs, not yet checked on a real console. Adjust them here after the
# first hardware test. (external_id, label, mount path, type, user-data root, ShadowMountPlus source folder)
FTP_DRIVES = [
    ("internal", "Internal SSD", "/user", "internal", "/user", "/data/homebrew"),
    *((f"ext{i}", f"Extended storage {i}", f"/mnt/ext{i}", "internal", f"/mnt/ext{i}/user", f"/mnt/ext{i}/homebrew") for i in range(2)),
    *((f"usb{i}", f"USB drive {i}", f"/mnt/usb{i}", "usb", f"/mnt/usb{i}/user", f"/mnt/usb{i}/homebrew") for i in range(8)),
]
IMAGE_EXTS = (".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")
HELPER_ELF = Path(__file__).parent / "payload" / "storefront-helper.elf"  # built from payload/ by payload/build.ps1
# The PS5 app (console-app/, built by console-app/build.ps1). ShadowMountPlus shows it as a home-screen title.
CONSOLE_APP_TITLE_ID = "PPSA99787"
CONSOLE_APP_DIR = Path(__file__).parent / "console_app" / CONSOLE_APP_TITLE_ID
CONSOLE_APP_PATH = f"/data/homebrew/{CONSOLE_APP_TITLE_ID}"


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


class FTPConsoleAdapter:
    """Talks to an FTP payload on the PS5 (stdlib ftplib, one login per call). Installs and removals go through
    ShadowMountPlus only (no package installer on the console)."""

    TIMEOUT = 10
    HELPER_PORT = 7735  # our helper payload (payload/main.c)
    LOADER_PORT = 9021  # the jailbreak's ELF loader
    DDP_PORT = 9302  # Sony's device discovery (UDP); answers in rest mode too

    def __init__(self, console: Console, password: str | None = None):
        self.host, self.port = console.ip_address, console.port
        self.user, self.password = console.username or "anonymous", password or ""

    def _session(self) -> ftplib.FTP:
        ftp = _FTP(timeout=self.TIMEOUT, encoding="utf-8")
        try:
            ftp.connect(self.host, self.port)
        except ConnectionRefusedError:  # the console answered, but nothing listens on the port
            raise AdapterError("helper_unreachable", "The console is on, but its FTP server isn't running.")
        except (OSError, EOFError, ftplib.Error):
            raise AdapterError("console_offline", "The console isn't reachable.")
        try:
            ftp.login(self.user, self.password)
            ftp.voidcmd("TYPE I")
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
            raise AdapterError("auth_failed", "The FTP server rejected the username or password.")
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
        req = urllib.request.Request(f"http://{self.host}:{self.HELPER_PORT}{path}", method=method)
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

    def _games(self, ftp: ftplib.FTP) -> list[InstalledGameInfo]:
        games = []
        for drive, user, homebrew in self._drives(ftp):
            for tid in ftp.ls(f"{user}/app"):
                if not TITLE_ID.fullmatch(tid) or tid.startswith("NPXS"):  # NPXS = system apps
                    continue
                path = f"{user}/app/{tid}"
                files = ftp.ls(path)
                # mount.lnk = ShadowMountPlus stub, listed from its homebrew source below; no app.pkg = homebrew launcher
                if "mount.lnk" in files or "app.pkg" not in files:
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
                s.sendto(b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n", (self.host, self.DDP_PORT))
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
                return ConsoleStatus("standby", "The console is in rest mode.", {"ftp": e.code})
            return ConsoleStatus("offline" if e.code == "console_offline" else "degraded", e.message, {"ftp": e.code})
        return ConsoleStatus("online", "", status)

    async def get_console_info(self) -> ConsoleInfo:
        status, info = await self._helper_status()
        return ConsoleInfo(info and info["firmware"] or None, info and info["model"] or "unknown", status)

    def _install_helper(self) -> str:
        self._helper("/quit", "POST")  # a running copy frees the port for the new one
        try:
            with socket.create_connection((self.host, self.LOADER_PORT), timeout=self.TIMEOUT) as s:
                s.sendall(HELPER_ELF.read_bytes())
        except OSError:
            raise AdapterError("loader_unreachable", f"The console's ELF loader isn't running (port {self.LOADER_PORT}). "
                                                     "Start it with your jailbreak, then try again.")
        for _ in range(15):  # it may wait for the old copy to let go of the port
            time.sleep(1)
            if (info := self._helper()) and info.get("version") == __version__:
                return __version__
        raise AdapterError("helper_not_started", "The helper was sent but didn't start. Check the console for a message.")

    async def install_helper(self) -> str:
        """Sends the bundled helper payload through the console's ELF loader. Returns the running version."""
        return await asyncio.to_thread(self._install_helper)

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
        for name, (is_dir, _) in ftp.ls(path).items():
            if is_dir:
                FTPConsoleAdapter._rm_partial(ftp, f"{path}/{name}")
            else:
                ftp.delete(f"{path}/{name}")
        try:
            ftp.rmd(path)
        except ftplib.error_perm:  # it was a file (an image upload)
            ftp.delete(path)

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
        ftp.rename(tmp, dest)  # only now does ShadowMountPlus see it
        m = TITLE_ID.search(src.name)
        return InstallResult(True, "Installed.", InstalledGameInfo(
            m[0] if m else "", src.name, None, "shadowmountplus", drive_id, dest, dest, total))

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
            ftp.storbinary(f"STOR {CONSOLE_APP_PATH}/{rel}", io.BytesIO(files[rel]), self.BLOCK)

    async def install_console_app(self, files: dict[str, bytes]) -> None:
        """Writes the PS5 app into /data/homebrew/<its title ID>; ShadowMountPlus adds its tile.
        ponytail: overwrites in place, so files dropped from a newer build linger. Fine while the file set is stable."""
        def put(ftp: ftplib.FTP):
            try:
                ftp.mkd(CONSOLE_APP_PATH)
            except ftplib.error_perm:
                pass  # an update
            self._put_files(ftp, files)
        await self._run(put)

    def _remove_source(self, ftp: ftplib.FTP, game: InstalledGameInfo) -> RemovalResult:
        """Deletes one game's folder or image. Only a direct entry of a homebrew folder (project.md §13 allowlist),
        and only while the console still lists exactly that path with that title ID."""
        path = game.source_path or ""
        homebrew, _, name = path.rpartition("/")
        if (homebrew not in {hb for *_, hb in FTP_DRIVES} or not name or name.startswith(".")
                or any(ch in name for ch in "*?[]") or name == CONSOLE_APP_TITLE_ID):
            raise AdapterError("path_outside_allowlist", f"Won't delete {path}: it isn't a game in a homebrew folder.")
        if not any(g.source_path == path and g.title_id == game.title_id for g in self._games(ftp)):
            raise AdapterError("not_found", f"{path} isn't on the console any more, or holds a different game.")
        entries = ftp.ls(homebrew)
        is_dir, size = entries[name]
        tmp = f"{homebrew}/.{name}.partial"
        if f".{name}.partial" in entries:  # left over from an interrupted install
            self._rm_partial(ftp, tmp)
        # Hidden first, so ShadowMountPlus never mounts a half-deleted game. A symlink is renamed, never followed.
        # ponytail: a connection lost mid-delete leaves the hidden .partial; reinstalling the game clears it.
        ftp.rename(path, tmp)
        if is_dir:
            self._rm_partial(ftp, tmp)
        else:
            ftp.delete(tmp)
        return RemovalResult(True, "Removed.", [path], size)

    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult:
        result = await self._run(lambda ftp: self._remove_source(ftp, installed_game))
        progress_callback(1, 1)
        return result

    async def install_package(self, *args, **kwargs):
        raise AdapterError("not_implemented", "This console has no package installer. Install folder dumps or disc images instead.")

    async def _not_yet(self, *args, **kwargs):
        raise AdapterError("not_implemented", "Not available yet. Delete it on the PS5 instead.")

    # The home-screen tile stays after the source is gone; deleting it on the PS5 then removes it for good.
    uninstall_package = remove_home_screen_icon = _not_yet


class HelperFS:
    """The helper payload's file API (payload/main.c), with the methods of _FTP the adapter uses, so both
    connections share the adapter code. The console saying no raises ftplib.error_perm, as over FTP."""

    def __init__(self, host: str, port: int, timeout: float, info: dict):
        self.host, self.port, self.timeout, self.info = host, port, timeout, info

    def _req(self, method: str, op: str, path: str, to: str | None = None, f=None, block: int = 0, callback=None) -> bytes:
        query = urllib.parse.urlencode({"p": path, **({"to": to} if to else {})}, quote_via=urllib.parse.quote)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.putrequest(method, f"/{op}?{query}", skip_accept_encoding=True)
            size = f.seek(0, 2) if f else 0
            if f:
                f.seek(0)
            conn.putheader("Content-Length", str(size))
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

    def storbinary(self, cmd: str, f, block: int, callback=None):
        self._req("PUT", "file", cmd.removeprefix("STOR "), f=f, block=block, callback=callback)

    def mkd(self, path: str):
        self._req("POST", "mkdir", path)

    def rename(self, src: str, dest: str):
        self._req("POST", "rename", src, to=dest)

    def delete(self, path: str):
        self._req("POST", "rm", path)

    rmd = delete

    def close(self):
        pass  # one HTTP connection per request


class HelperConsoleAdapter(FTPConsoleAdapter):
    """Talks only to our helper payload, no FTP server needed. install_helper goes through the ELF loader,
    so it works before the helper runs."""

    def __init__(self, console: Console, password: str | None = None):
        self.host = console.ip_address

    def _session(self) -> HelperFS:
        info = self._helper()
        if not info:
            raise AdapterError("helper_unreachable", "The helper payload isn't running. Install it on the Installed page.")
        if info.get("version") != __version__:  # an older helper has no file API; its 404s would read as an empty console
            raise AdapterError("helper_outdated", f"The console runs helper v{info.get('version')}; this app needs "
                                                  f"v{__version__}. Update it on the Installed page.")
        return HelperFS(self.host, self.HELPER_PORT, self.TIMEOUT, info)

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
            return ConsoleStatus("degraded", error.message, helper)
        ddp = await asyncio.to_thread(self._ddp)
        if b" 620 " in ddp:  # payloads don't run in rest mode
            return ConsoleStatus("standby", "The console is in rest mode.", helper)
        if b" 200 " in ddp:
            return ConsoleStatus("degraded", "The console is on, but the helper isn't running.", helper)
        return ConsoleStatus("offline", "The console doesn't answer. If it's on, install the helper.", helper)


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
