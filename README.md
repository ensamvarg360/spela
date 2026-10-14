# PS5 Local Storefront

Self-hosted, single-user catalog for your own PS5 game backups. LAN-only; never expose it to the internet.
Full spec: [project.md](project.md). **Status:** Phases 1–2 (foundation + library scanner). Console integration isn't built yet.

## Run (Windows)

Double-click **PS5 Local Storefront.exe**. It opens in your browser at http://127.0.0.1:8787 and runs in the background with an icon in the system tray.
Right-click the tray icon to **Open** it, **Open data folder**, **Restart** or **Quit**. Starting the .exe again while it's running just opens the browser.
The first time, choose an admin password. Then go to **Settings**, click **Browse…**, pick a folder or a whole drive (for example `G:\`), and click **Scan now**.
A folder is scanned with all its subfolders. System folders such as `$RECYCLE.BIN` and `System Volume Information` are skipped.

Data (database, covers, password, `app.log`) lives in `%LOCALAPPDATA%\PS5 Local Storefront`. To move to another PC, copy that folder.
Forgot the password? Delete `admin_password` in that folder and restart; you'll be asked to choose a new one.
Only this PC can reach it by default. To open it from a phone on your LAN, start it with the environment variable `HOST=0.0.0.0` (allow it through Windows Firewall when asked). Never expose it to the internet.

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
