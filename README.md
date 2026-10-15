# PS5 Local Storefront

Self-hosted, single-user catalog for your own PS5 game backups. LAN-only; never expose it to the internet.
Full spec: [project.md](project.md). **Status:** Phases 1–3 plus installing through ShadowMountPlus, and a PS5 app. The app can copy folder dumps and disc images to a PS5, and remove them again (from the Installed page or the PS5 app), but can't install `.pkg` files yet.

## Run (Windows)

Double-click **PS5 Local Storefront.exe**. It opens in its own window (Windows' built-in WebView2, not your browser) and runs in the background with an icon in the system tray. Closing the window keeps it running in the tray.
Right-click the tray icon to **Open** it, **Open data folder**, **Restart** or **Quit**. Starting the .exe again while it's running brings its window back. The address http://127.0.0.1:8787 still works in a browser too.
The first time, choose an admin password. Then go to **Settings**, click **Browse…**, pick a folder or a whole drive (for example `G:\`), and click **Scan now**.
A folder is scanned with all its subfolders. System folders such as `$RECYCLE.BIN` and `System Volume Information` are skipped.

Data (database, covers, password, `app.log`) lives in `%LOCALAPPDATA%\PS5 Local Storefront`. To move to another PC, copy that folder.
Forgot the password? Delete `admin_password` in that folder and restart; you'll be asked to choose a new one.
Console passwords (added under **Settings → Consoles**) are encrypted in the database with a key derived from `secret_key` in that folder, or from the `CREDENTIALS_ENCRYPTION_KEY` environment variable if you set it. If you lose or change that key, re-enter the console passwords. The key sits next to the database, so encryption protects a copied database file, not a copied data folder.
Only this PC can reach it by default. To open it from a phone or another computer at home, right-click the tray icon and tick **Allow other devices on my network**. Windows asks for admin rights once, to let the app through the firewall on private networks, and the app restarts. The tray menu then shows the address to open on the other device, for example `http://192.168.0.40:8787`. If Windows treats your network as *Public*, other devices stay blocked; the app tells you how to switch it to *Private*. Never expose the app to the internet. (The `HOST` environment variable, if set, overrides the tray option.)

## Consoles

Add your PS5 under **Settings → Consoles** (connection **Helper payload**, just a name and IP address), then open **Installed** and click **Install helper**. The app sends its own **helper payload** through the console's ELF loader on port 9021, and the console shows a notification when it starts. The helper does everything on port 7735: firmware, model, drive space, reading the installed games, and copying installs. No FTP server is needed. Like any payload, it stops when the console restarts, so install it again after a reboot.
The app checks each console every 15 seconds and reads its drives and installed games when it comes online; **Refresh inventory** reads them again.
*Rest mode* means the console is asleep (detected only when the PS5's rest mode setting *Stay connected to the internet* is on); *Degraded* means the console is on but the helper isn't running or is outdated; *Offline* means it doesn't answer at all.
Installed games are matched to your library by title ID, and marked *Update in library* when your library holds a newer version.

The helper only changes files inside the `homebrew` folders (`/data/homebrew`, `/mnt/usbN/homebrew`, `/mnt/extN/homebrew`); anything else on the console is read-only to it. It has no password, so keep the console on a network you trust.
**FTP (fallback)** still works: pick it with the port of an FTP payload (often 2121 or 1337). File access then goes over FTP, and the helper still reports firmware and drive space.
The helper's version always equals the app's version. If they differ, the button changes to **Update helper**, and clicking it replaces the running copy.
What the app can't tell yet: the size of folder dumps.
The console paths it reads (`/user/app`, `/data/homebrew`, `/mnt/usbN`, `/mnt/extN`) are in `FTP_DRIVES` in [backend/app/consoles.py](backend/app/consoles.py) and have been checked against ftpsrv v0.21. A drive shows up only when its mount point has something in it.
Set `CONSOLE_CHECK_SECONDS=0` to turn the checks off.

## Installing games

Open a game in the library and click **Install**. The app copies it through the helper (or FTP) into the `homebrew` folder of a drive where **Allow installs here** is ticked (the one with the most free space, unless you pick one). ShadowMountPlus then adds it to the home screen.
It copies under a hidden `.<name>.partial` name and renames it when the copy is complete, so ShadowMountPlus never sees half a game. Installs run one at a time; follow them, or cancel them, on the **Installed** page.
Only folder dumps and disc images (`.exfat`, `.ffpkg`, `.ffpfs`, `.ffpfsc`) can be installed: the console has no package installer for `.pkg` files, and archives must be extracted first.
The helper must be running, so the app can check free space first. If the app or the connection stops mid-copy, the install shows *Interrupted*; queue it again, and the leftover partial copy is replaced.

## Removing games

Deleting a ShadowMountPlus game on the PS5 only removes its home-screen tile. Its files stay in the `homebrew` folder, so ShadowMountPlus adds it back and no space is freed. Remove it from the app instead: **Remove from console** on the **Installed** page (type the title ID to confirm), or Square on an installed game in the PS5 app.
The app shows exactly which folder or image it will delete first, then deletes only that. It must be directly inside a drive's `homebrew` folder, and the console must still list it under that title ID. It's renamed to a hidden name first, so ShadowMountPlus never mounts a half-deleted game. Save data and the copy in your PC library are kept, and every attempt is written to the audit log.
Both show a progress bar while the files are deleted. Then the helper removes the game's home-screen tile. It asks ShadowMountPlus's API to do it (1.7 and later, on `127.0.0.1:10101`, which is on by default). Without that API, it calls the console's own uninstall (`sceAppInstUtilAppUnInstall`), as ShadowMountPlus does. The helper only removes a tile whose `/user/app/<title ID>/mount.lnk` (or `mount_img.lnk`) points at files that are gone, so nobody on the network can remove a game that's still there.
When it's done, the PC dialog and the PS5 app both say whether the tile is gone. If it isn't (an older helper, or the console refused), they tell you to delete it on the PS5: that removes it for good. Space from a disc image may only come back after the PS5 restarts, if ShadowMountPlus still has it mounted. Games installed as packages can't be removed from the app; delete those on the PS5.

## PS5 app

**Install PS5 app** on the **Installed** page puts *PS5 Local Storefront* on the console's home screen (in `/data/homebrew/PPSA99787`, where ShadowMountPlus finds it). It shows the games on your PC: Cross queues an install, the same as clicking Install on the PC, and Square removes an installed one.
The PS5 must be able to reach this PC, so tick **Allow other devices on my network** in the tray menu first. The app is paired with this PC: the PC writes its address and a key into the app's `assets/server.txt`. The key only lets that console see the library, queue installs to itself and remove games from itself. If the PC's address changes, click **Update PS5 app** again. Deleting the console on the PC revokes the key.
The app is built from [ps5-homebrew-ui](https://github.com/blackbearreloaded/ps5-homebrew-ui) (GPL-3.0-or-later) with our screen in [console-app/](console-app/). Run `./console-app/build.ps1` (needs Docker Desktop) after changing it: it rebuilds `backend/app/console_app/PPSA99787`, which is committed. `./console-app/build.ps1 -Preview` renders the screen on the PC into `console-app/preview/`, against a fake PC app.

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
- **Removals are tracked in memory**, not in the `removal_jobs` table: its foreign key would block dropping the installed game once it's gone. The audit log keeps the record; a restart forgets previews and progress. Previews expire after 10 minutes. No orphan detection yet.
