"""Console adapter interface (project.md §9). Everything that talks to a PS5 goes
through a ConsoleAdapter; console-specific paths and endpoints stay in here."""
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Literal, Protocol

from .models import Console

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
    firmware: str
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
        return ConsoleStatus("online", "", {"ftp": "ok", "installer": "ok", "shadowmountplus": "ok"})

    async def get_console_info(self) -> ConsoleInfo:
        self._check()
        return ConsoleInfo("4.03", "CFI-1216A", {"ftp": "ok", "installer": "ok", "shadowmountplus": "ok"})

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


def get_adapter(console: Console) -> ConsoleAdapter:
    match console.connection_type:
        case "mock":
            return MockConsoleAdapter(console)
        case _:  # ftp/http/custom land here until their adapters exist
            raise UnsupportedConsoleConnectionError(console.connection_type)
