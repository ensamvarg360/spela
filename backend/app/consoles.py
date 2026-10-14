"""Console adapter interface (project.md §9). Everything that talks to a PS5 goes
through a ConsoleAdapter; console-specific paths and endpoints stay in here."""
import asyncio
import ftplib
import io
import json
import re
import socket
import struct
import time
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
    state: Literal["online", "degraded", "offline"]
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
    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult: ...
    async def install_package(self, package_path: str, progress_callback: ProgressCallback) -> InstallResult: ...
    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback) -> InstallResult: ...
    async def uninstall_package(self, title_id: str) -> RemovalResult: ...
    async def remove_shadowmount_source(self, installed_game: InstalledGameInfo, progress_callback: ProgressCallback) -> RemovalResult: ...
    async def remove_home_screen_icon(self, title_id: str) -> RemovalResult: ...
    async def install_helper(self) -> str: ...


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

    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult:
        self._check()
        free = self._drive(drive_id).free_bytes
        return SpaceCheckResult(free >= bytes_needed, free, bytes_needed)

    async def install_package(self, package_path: str, progress_callback: ProgressCallback) -> InstallResult:
        self._check()
        return self._install(package_path, "package_installer", self._drive("internal"),
                             "/user/app/{title_id}", None, progress_callback)

    async def stage_shadowmount_content(self, source_path: str, target_drive: DriveInfo, progress_callback: ProgressCallback) -> InstallResult:
        self._check()
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


# ponytail: PS5 paths from community docs, not yet checked on a real console. Adjust them here after the
# first hardware test. (external_id, label, mount path, type, user-data root, ShadowMountPlus source folder)
FTP_DRIVES = [
    ("internal", "Internal SSD", "/user", "internal", "/user", "/data/homebrew"),
    *((f"ext{i}", f"Extended storage {i}", f"/mnt/ext{i}", "internal", f"/mnt/ext{i}/user", f"/mnt/ext{i}/homebrew") for i in range(2)),
    *((f"usb{i}", f"USB drive {i}", f"/mnt/usb{i}", "usb", f"/mnt/usb{i}/user", f"/mnt/usb{i}/homebrew") for i in range(8)),
]
IMAGE_EXTS = (".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")
HELPER_ELF = Path(__file__).parent / "payload" / "storefront-helper.elf"  # built from payload/ by payload/build.ps1


class FTPConsoleAdapter:
    """Talks to an FTP payload on the PS5 (stdlib ftplib, one login per call). Read-only for now:
    installing and removing come in Phases 4-5 and refuse with `not_implemented` until then."""

    TIMEOUT = 10
    HELPER_PORT = 7735  # our helper payload (payload/main.c)
    LOADER_PORT = 9021  # the jailbreak's ELF loader

    def __init__(self, console: Console, password: str | None = None):
        self.host, self.port = console.ip_address, console.port
        self.user, self.password = console.username or "anonymous", password or ""

    def _session(self) -> ftplib.FTP:
        ftp = ftplib.FTP(timeout=self.TIMEOUT, encoding="utf-8")
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
        except (OSError, EOFError, ftplib.Error) as e:
            raise AdapterError("connection_lost", f"The FTP connection failed: {e}")
        finally:
            ftp.close()

    async def _run(self, fn):
        return await asyncio.to_thread(self._call, fn)

    @staticmethod
    def _ls(ftp: ftplib.FTP, path: str) -> dict[str, tuple[bool, int]]:
        """{name: (is_dir, size)} from a Unix-style LIST. ftpsrv on the PS5 has no NLST."""
        lines = []
        try:
            ftp.retrlines(f"LIST {path}", lines.append)
        except ftplib.error_perm:  # missing folder
            return {}
        entries = {}
        for line in lines:  # "drwxr-xr-x 2 0 0 512 Oct 07 01:15 name with spaces"
            parts = line.split(None, 8)
            if len(parts) == 9 and parts[8] not in (".", ".."):
                entries[parts[8]] = (parts[0].startswith("d"), int(parts[4]) if parts[4].isdigit() else 0)
        return entries

    @staticmethod
    def _get(ftp: ftplib.FTP, path: str) -> bytes | None:
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {path}", buf.write)
        except ftplib.error_perm:
            return None
        return buf.getvalue()

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
            if self._ls(ftp, path):
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
            for tid in self._ls(ftp, f"{user}/app"):
                if not TITLE_ID.fullmatch(tid) or tid.startswith("NPXS"):  # NPXS = system apps
                    continue
                path = f"{user}/app/{tid}"
                files = self._ls(ftp, path)
                # mount.lnk = ShadowMountPlus stub, listed from its homebrew source below; no app.pkg = homebrew launcher
                if "mount.lnk" in files or "app.pkg" not in files:
                    continue
                param = "param.sfo" if tid.startswith("CUSA") else "param.json"
                meta = self._meta(tid, self._get(ftp, f"{user}/appmeta/{tid}/{param}"))
                games.append(InstalledGameInfo(tid, meta.title or tid, meta.version, "package_installer",
                                               drive.external_id, path, None, files["app.pkg"][1]))
            for name, (is_dir, size) in self._ls(ftp, homebrew).items():
                path, m = f"{homebrew}/{name}", TITLE_ID.search(name)
                if name.startswith("."):  # hidden, e.g. ".PPSA25381-app0.gc-moving.tmp" mid-copy
                    continue
                if is_dir:  # folder dump; size stays 0, summing a tree over FTP is too slow
                    meta, size = self._meta("PPSA", self._get(ftp, f"{path}/sce_sys/param.json")), 0
                elif name.lower().endswith(IMAGE_EXTS):
                    meta = PkgMetadata()
                else:
                    continue
                tid = meta.title_id or (m and m[0])
                if tid:  # no title ID: other homebrew, not a game
                    games.append(InstalledGameInfo(tid, meta.title or name, meta.version, "shadowmountplus",
                                                   drive.external_id, path, path, size))
        return games

    async def _helper_status(self, banner: str) -> tuple[dict, dict | None]:
        info = await asyncio.to_thread(self._helper)
        return {"ftp": "ok", "banner": banner, "helper": info["version"] if info else "missing"}, info

    async def connect(self) -> ConsoleStatus:
        try:
            banner = await self._run(lambda f: f.getwelcome())
        except AdapterError as e:
            return ConsoleStatus("offline" if e.code == "console_offline" else "degraded", e.message, {"ftp": e.code})
        return ConsoleStatus("online", "", (await self._helper_status(banner))[0])

    async def get_console_info(self) -> ConsoleInfo:
        status, info = await self._helper_status(await self._run(lambda f: f.getwelcome()))
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

    async def check_space(self, drive_id: str, bytes_needed: int) -> SpaceCheckResult:
        drive = next((d for d in await self.list_drives() if d.external_id == drive_id), None)
        if not drive:
            raise AdapterError("drive_not_found", f"No drive '{drive_id}' on this console.")
        return SpaceCheckResult(drive.free_bytes >= bytes_needed, drive.free_bytes, bytes_needed)

    async def _not_yet(self, *args, **kwargs):
        raise AdapterError("not_implemented", "Installing and removing over FTP isn't available yet.")

    install_package = stage_shadowmount_content = uninstall_package = _not_yet
    remove_shadowmount_source = remove_home_screen_icon = _not_yet


def get_adapter(console: Console, password: str | None = None) -> ConsoleAdapter:
    match console.connection_type:
        case "mock":
            return MockConsoleAdapter(console)
        case "ftp":
            return FTPConsoleAdapter(console, password)
        case _:  # http/custom land here until their adapters exist
            raise UnsupportedConsoleConnectionError(console.connection_type)
