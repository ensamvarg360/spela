# PS5 Local Storefront

Self-hosted, single-user catalog for your own PS5 game backups. LAN-only; never expose it to the internet.
Full spec: [project.md](project.md). **Status:** Phases 1–3 (foundation, library scanner, console integration). The app can read a PS5's drives and installed games over FTP, but can't install or remove anything yet.

## Run (Windows)

Double-click **PS5 Local Storefront.exe**. It opens in your browser at http://127.0.0.1:8787 and runs in the background with an icon in the system tray.
Right-click the tray icon to **Open** it, **Open data folder**, **Restart** or **Quit**. Starting the .exe again while it's running just opens the browser.
The first time, choose an admin password. Then go to **Settings**, click **Browse…**, pick a folder or a whole drive (for example `G:\`), and click **Scan now**.
A folder is scanned with all its subfolders. System folders such as `$RECYCLE.BIN` and `System Volume Information` are skipped.

Data (database, covers, password, `app.log`) lives in `%LOCALAPPDATA%\PS5 Local Storefront`. To move to another PC, copy that folder.
Forgot the password? Delete `admin_password` in that folder and restart; you'll be asked to choose a new one.
Console passwords (added under **Settings → Consoles**) are encrypted in the database with a key derived from `secret_key` in that folder, or from the `CREDENTIALS_ENCRYPTION_KEY` environment variable if you set it. If you lose or change that key, re-enter the console passwords. The key sits next to the database, so encryption protects a copied database file, not a copied data folder.
Only this PC can reach it by default. To open it from a phone on your LAN, start it with the environment variable `HOST=0.0.0.0` (allow it through Windows Firewall when asked). Never expose it to the internet.

## Consoles

Add your PS5 under **Settings → Consoles** (connection **FTP**, the port of your FTP payload, often 2121 or 1337), then open **Installed**.
The app checks each console every 15 seconds and reads its drives and installed games when it comes online; **Refresh inventory** reads them again.
*Degraded* means the console answers but its FTP server isn't running or rejected the password; *Offline* means it doesn't answer at all.
Installed games are matched to your library by title ID, and marked *Update in library* when your library holds a newer version.

FTP can't report firmware or drive space, so the app also uses its own **helper payload**. When the console is online, click **Install helper** on the **Installed** page. The app sends the helper through the console's ELF loader on port 9021 (the same loader you used for the FTP server), and the console shows a notification when the helper starts. The helper then reports firmware, model, and the size and free space of each drive on port 7735. Like the FTP server, it stops when the console restarts, so install it again after a reboot.
The helper's version always equals the app's version. If they differ, the button changes to **Update helper**, and clicking it replaces the running copy.
What the app can't tell yet: the size of folder dumps.
The console paths it reads (`/user/app`, `/data/homebrew`, `/mnt/usbN`, `/mnt/extN`) are in `FTP_DRIVES` in [backend/app/consoles.py](backend/app/consoles.py) and have been checked against ftpsrv v0.21. A drive shows up only when its mount point has something in it.
Set `CONSOLE_CHECK_SECONDS=0` to turn the checks off.

## Versions

The version number lives in one place: `__version__` in [backend/app/__init__.py](backend/app/__init__.py). The Python package, `/api/v1/health`, the page header and the helper payload all read it.
To release a new version: bump it, run `./payload/build.ps1` (needs Docker Desktop; it rebuilds `backend/app/payload/storefront-helper.elf`, which is committed), then `./build.ps1`. A test fails if the bundled helper's version doesn't match the app's version.

## Build the .exe

Needs Python 3.12+ and Node 22. Run `./build.ps1`; the result is `dist\PS5 Local Storefront.exe`.

## Develop

```sh
# backend (Python 3.12+); data goes to a local ./data folder
cd backend && python -m venv .venv && .venv/Scripts/pip install -e ".[test]"   # bin/ on Linux/macOS
DATA_DIR=../data .venv/Scripts/uvicorn app.main:app --reload
.venv/Scripts/pytest

# frontend: proxies /api to :8000
cd frontend && npm install && npm run dev
```

## How detection works

Classification needs evidence from the file itself. The extension alone isn't enough.

| Format | Evidence |
|---|---|
| Folder dump | `sce_sys/param.json` (title ID, version, and title are read from it) |
| `.exfat` | exFAT boot sector (`EXFAT   ` at offset 3) |
| `.ffpkg` | UFS2 superblock |
| `.ffpfs` / `.ffpfsc` | PFS / `PFSC` header, or an exFAT wrapper |
| `.pkg` | `\x7fCNT` (PS4) or `\x7fFIH` (PS5). Title ID, content ID, title and version come from the CNT header and `param.sfo` / `param.json` ([backend/app/pkg.py](backend/app/pkg.py)). |
| `.zip` `.7z` `.rar` `.001` | Archive magic |

Anything else is marked `unknown`, and its first 16 header bytes are saved under *Detection evidence* so a signature can be added in `SIGNATURES` in [backend/app/scanner.py](backend/app/scanner.py).
A title ID that comes only from the file name is marked low confidence, and the game shows *Needs attention* until you confirm it on its detail page.
A later part of a split set (`.part2.rar`, `.7z.002`) is skipped, unless its first part is missing. Then it's listed as `unknown`.
Files ending `.part`, `.partial` or `.tmp`, and folders containing a `.partial` marker, are flagged as incomplete.
Sidecars (`game.json`/`metadata.json` inside a dump, or `<file>.json` next to an image) only fill in descriptive fields.
An archive and its extracted copy with the same title ID are treated as a backup pair, not as duplicates.
If a root or subfolder can't be read, the scan reports an error and leaves existing records alone. Records disappear only when you delete them, and deleting one never touches the files on disk.

## Deviations from project.md (deliberate, for now)

- **Native Windows app, not Docker.** A container can't see the PC's drives without Docker Desktop mount setup, so it ships as one `.exe` (PyInstaller) that serves the web UI locally. The code is plain Python and still runs on Linux (`python launcher.py`).
- **Admin password set on first run** (from this PC only) and stored as a PBKDF2 hash, instead of an environment variable.
- **SQLite.** No Postgres, Redis or ARQ yet: a scan is the only background task. Models are portable; set `DATABASE_URL` (and add `psycopg`) to switch to Postgres.
- **No Alembic yet.** Tables are created with `create_all`. Add migrations before the first schema change on a live install.
- **Interval rescans, no watchdog.** Filesystem events aren't reliable on SMB/NFS mounts.
- Login is rate-limited to 5 failures per minute per IP. Read-only users aren't built yet.
