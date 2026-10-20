# Spela user guide

The full reference for [Spela](../README.md): how every part works, what it changes on your console, and what to do when something goes wrong. New here? Start with the [README](../README.md).

**Spela provides no game files, at any point.** Everything below is about managing backups you made yourself, of games you own.

Full spec: [project.md](../project.md). **Status:** Phases 1–5 done, Phase 6 (hardening) in progress. The app copies folder dumps, disc images and archives holding them to a PS5 through ShadowMountPlus, and removes them again (from the Installed page or the PS5 app). It can't install `.pkg` files yet.

## Run (Windows)

Double-click **Spela-<version>.exe**. It opens in its own window (Windows' built-in WebView2, not your browser) and runs in the background with an icon in the system tray. Closing the window keeps it running in the tray.
Right-click the tray icon to **Open** it, **Open data folder**, **Restart** or **Quit**. Starting the .exe again while it's running brings its window back. The address http://127.0.0.1:8787 still works in a browser too.
The first time, choose an admin password. The **setup guide** (*Get started*) opens next and takes you through adding your game folder, scanning it, adding the PS5 and starting the helper. It works out from what's already set up which steps are done, so you can leave and come back. Until every step is done, a card on the **Dashboard** shows how far you've got; **Hide** removes it, and **Settings** links to the guide again. Without the guide: go to **Settings**, click **Browse…**, pick a folder or a whole drive (for example `G:\`), and click **Scan now**.
A folder is scanned with all its subfolders. System folders such as `$RECYCLE.BIN` and `System Volume Information` are skipped.
Each game in the library shows its poster, publisher, release year, genre and star rating from PlayStation Store, looked up by the game's title ID (no account needed) the first time its tile comes into view and kept in the `store` folder of the data folder. The poster is the game's portrait art, looked for in the US, then UK, then Japanese store, and in its other editions (a Deluxe Edition's art says so); only when none has any does the app make one from the game's landscape art and logo. A cover you upload yourself replaces the store's art. Games the store doesn't know show their own cover and are asked about again after a week.

Data (database, covers, password, `app.log`) lives in `%LOCALAPPDATA%\Spela`. To back it up or move to another PC, see [Backup and restore](#backup-and-restore).
Forgot the password? Delete `admin_password` in that folder and restart; you'll be asked to choose a new one. That also logs out every device that was logged in.
For a phone in the living room or a guest, set a **view-only password** under **Settings → View-only access**. Logging in with it shows the library, dashboard, DLC and installed games, but nothing can be installed, removed or changed, and Settings, the activity log and the PC's folders stay hidden; the app shows *View only* at the top. Changing or turning off that password logs out every device that used it. (It's kept in `viewer_password` in the data folder.)
Console passwords (added under **Settings → Consoles**) are encrypted in the database with a key derived from `secret_key` in that folder, or from the `CREDENTIALS_ENCRYPTION_KEY` environment variable if you set it. If you lose or change that key, re-enter the console passwords. The key sits next to the database, so encryption protects a copied database file, not a copied data folder.
Only this PC can reach it by default. To open it from a phone or another computer at home, right-click the tray icon and tick **Allow other devices on my network**. Windows asks for admin rights once, to let the app through the firewall on private networks, and the app restarts. The tray menu then shows the address to open on the other device, for example `http://192.168.0.40:8787`. If Windows treats your network as *Public*, other devices stay blocked: switch it to *Private* in Windows' network settings. The PS5 needs none of this: the PC only connects out to it, and the [PS5 app](#ps5-app) reaches the PC through the helper. Never expose the app to the internet. (The `HOST` environment variable, if set, overrides the tray option.) Open it by IP address (or `localhost`), not by the PC's name: requests addressed to a name are refused, so a malicious website can't reach the app by pointing its own name at your PC (DNS rebinding).

## Dashboard

**Dashboard** gives the overview: how many games are in the library and installed, each console's state, firmware, helper and drive space, installs in progress, and what needs a look (games whose files are gone, title IDs to check, and installs, removals or logins that failed in the last 7 days). **Scan library** and **Refresh inventory** are there too.

## Consoles

Add your PS5 under **Settings → Consoles → Add console**, or in the setup guide. **Find it on my network** lists the PS5s that answer Sony's discovery request (switched on, or in rest mode with *Stay connected to the internet* on) and adds one with a click, under its PS5 name. Or type a name and its IP address: the PS5 shows it under *Settings → Network → Connection Status → View Connection Status*. Either way it uses the **Helper payload** connection; **Edit** switches to FTP. Then click **Install helper** there. The app sends its own **helper payload** through the console's ELF loader on port 9021, and the console shows a notification when it starts. The helper does everything on port 7735: firmware, model, drive space, reading the installed games, and copying installs. No FTP server is needed. Like any payload, it stops when the console restarts. You don't have to click again: once the console is back on and your jailbreak's ELF loader is running, the app sends the helper by itself (it tries once a minute, and also replaces an outdated helper). Each automatic install is in the audit log.
The app checks each console every 15 seconds and reads its drives and installed games when it comes online; **Refresh inventory** reads them again.
To wake a console from rest mode with a **Wake** button (on the Dashboard, the Installed page and in Settings), save its Remote Play key: register Remote Play once with [Chiaki-ng](https://github.com/chiaki-ng/chiaki-ng) (on the PS5: Settings → System → Remote Play → Link Device), copy the console's *RP-RegistKey* from it, and paste it under **Settings → Consoles → Edit → Remote Play key**. The 8-character key, the base64 form in Chiaki's settings file and the decimal number all work. It's stored encrypted like console passwords. The PS5 needs *Stay connected to the internet* and *Enable turning on PS5 from network* on in its rest-mode settings. After it wakes, the helper is sent again by itself once the ELF loader is running.
*Rest mode* means the console is asleep (detected only when the PS5's rest mode setting *Stay connected to the internet* is on); *Degraded* means the console is on but the helper isn't running or is outdated; *Offline* means it doesn't answer at all.
Installed games are matched to your library by title ID, and marked *Update in library* when your library holds a newer version.

The helper only changes files inside the `homebrew` folders (`/data/homebrew`, `/mnt/usbN/homebrew`, `/mnt/extN/homebrew`), plus one exception for [home-screen logos](#home-screen-logos); anything else on the console is read-only to it. It answers only to the PC that installed it: each copy the app sends carries a key for that console, and every request must include it. Web pages are refused too, so a website you visit can't control the console through your browser. If you move the app to another PC or lose its data folder, click **Install helper** again.
**FTP (fallback)** still works: pick it with the port of an FTP payload (often 2121 or 1337). File access then goes over FTP, and the helper still reports firmware and drive space.
The helper's version always equals the app's version. If they differ, the button changes to **Update helper**, and clicking it replaces the running copy.
**Remove helper** (next to it) stops the helper and stops Spela from sending it by itself, so the console is free for other tools; the activity log records it. The helper lives only in the console's memory, so nothing of it is left on the PS5. Installed games, the PS5 app and home-screen logos are kept. Spela can't read or install to that console until you click **Install helper** again, which also turns the automatic sending back on. To stop using Spela altogether: click **Remove helper** for each console, then quit Spela from the tray menu and delete the `.exe` and the data folder. Before that, click **Remove PS5 app** (under **Settings → Consoles**, while the helper still runs): it deletes `/data/homebrew/PPSA99787` and the *Spela* tile. If the tile stays (an older ShadowMountPlus that refuses), delete it on the PS5 (Options, then Delete); it won't come back, since its files are gone.

Removing the console under **Settings → Consoles** also revokes the PS5 app's key, so it can't reach the PC.

What the app can't tell yet: the size of folder dumps.
The console paths it reads (`/user/app`, `/data/homebrew`, `/mnt/usbN`, `/mnt/extN`) are in `FTP_DRIVES` in [backend/app/consoles.py](../backend/app/consoles.py) and have been checked against ftpsrv v0.21. A drive shows up only when its mount point has something in it.
Set `CONSOLE_CHECK_SECONDS=0` to turn the checks off.

## Installing games

Open a game in the library and click **Install**. A game's page also lists the consoles and drives it's installed on, and its installs, removals and moves with their results. To install several, click **Select** above the library, tick the games (or **Select all**, which takes what the search and filters show), and click **Install**; each goes to the drive with the most free space, and any that can't be queued are listed with the reason. The app copies it through the helper (or FTP) into the `homebrew` folder of a drive where **Allow installs here** is ticked (the one with the most free space, unless you pick one). ShadowMountPlus then adds it to the home screen.
It copies under a hidden `.<name>.partial` name and renames it when the copy is complete, so ShadowMountPlus never sees half a game. Before the rename it checks that every file is on the console at the size it has on this PC (the install says *All N files checked*); if one isn't, the copy is deleted and the install fails, so install it again. It compares sizes, not contents: reading the game back would take as long as copying it. Installs run one at a time; follow them, or cancel them, on the **Installed** page. When one ends (installed, failed or interrupted), Windows shows a notification from the tray icon, so you can leave a long copy running.
Folder dumps and disc images (`.exfat`, `.ffpkg`, `.ffpfs`, `.ffpfsc`) can be installed, and so can `.zip`, `.7z` and `.rar` archives (also split sets like `.7z.001`) that hold one of them. `.pkg` files are copied to the drive's homebrew folder and the helper then starts the console's package installer on them; this is untested on a console. It is refused above firmware 11.60, since fake packages don't launch there yet; the limit is `PKG_MAX_FIRMWARE` in [backend/app/jobs.py](../backend/app/jobs.py). An archive is first extracted on this PC, with Windows' own `tar.exe`, into a private staging folder (`staging` in the data folder, or `STAGING_DIR`), so the PC needs room for the unpacked game. The game's title ID is then read from its own `param.json`, and the staging copy is deleted when the install ends, also when it fails or is canceled. Multi-part RAR (`.part1.rar`) and spanned ZIP (`.z01`) sets, password-protected archives and archives with links in them can't be installed: extract those yourself.
The helper must be running, so the app can check free space first. If the app or the connection stops mid-copy, the install shows *Interrupted*; queue it again, and the leftover partial copy is replaced.

### DLC

DLC shows on the **DLC** page, not in the library. Add the folder you keep it in under *DLC folders* in Settings. DLC that sits inside game folders is found as well, since what counts as DLC is read from the files themselves. The one exception is an archive: the app can't tell what's inside without extracting it, so an archive counts as DLC only in a DLC folder or under a folder named like `DLC`, `DLCs`, `*.DLC.Unlocker*` or `*.DLC.only*`.

- **DLC unlockers** (a folder with `dlc_emu.ini` and `fakelib/`) can be installed. **Add to game** copies their files into the game's folder on the console and replaces files there with the same names, as the release notes ask. This needs the game installed as a folder dump through ShadowMountPlus. A disc image can't take extra files, so for a game installed as an image the app says so. To undo it, remove the game and install it again. An archive holding one unlocker folder is extracted first, like a game archive.
- **DLC packages** (`.pkg`) can't be installed, for the same reason game packages can't (see above).

### Converting a folder dump to `.exfat`

Open a folder dump and click **Convert to .exfat**. The app writes the folder into one exFAT disc image next to it (`<folder>.exfat`, with ` [PPSAxxxxx]` added when the folder name has no title ID, since an image's title ID comes from its file name). The image holds the folder's contents at its root, as ShadowMountPlus expects. It is written in plain Python ([backend/app/exfat.py](../backend/app/exfat.py)), so no OSFMount, WSL or admin rights are needed, and the drive needs room for a second copy of the game. When it's done the image is scanned into the library with the folder's title, title ID and everything you set on it. The folder is never deleted: check the image, then delete the folder yourself. Until then both show *Duplicate title ID*.
While it's written the file is named `<name>.exfat.building`, which the scanner ignores; it is deleted if the conversion fails or is canceled. If the app is closed mid-way, delete that file yourself.

## Removing games

Deleting a ShadowMountPlus game on the PS5 only removes its home-screen tile. Its files stay in the `homebrew` folder, so ShadowMountPlus adds it back and no space is freed. Remove it from the app instead: **Remove from console** on the **Installed** page (type the title ID to confirm), or Square on an installed game in the PS5 app.
The app shows exactly which folder or image it will delete first, then deletes only that. It must be directly inside a drive's `homebrew` folder, and the console must still list it under that title ID. It's renamed to a hidden name first, so ShadowMountPlus never mounts a half-deleted game. Save data and the copy in your PC library are kept, and every attempt is written to the audit log.
Both show a progress bar while the files are deleted. Then the helper removes the game's home-screen tile. It asks ShadowMountPlus's API to do it (1.7 and later, on `127.0.0.1:10101`, which is on by default). Without that API, it calls the console's own uninstall (`sceAppInstUtilAppUnInstall`), as ShadowMountPlus does. The helper only removes a tile whose `/user/app/<title ID>/mount.lnk` (or `mount_img.lnk`) points at files that are gone, so nobody on the network can remove a game that's still there.
When it's done, the PC dialog and the PS5 app both say whether the tile is gone. If it isn't (an older helper, or the console refused), they tell you to delete it on the PS5: that removes it for good. Space from a disc image may only come back after the PS5 restarts, if ShadowMountPlus still has it mounted.
A tile whose files were deleted some other way shows as *Icon only*. **Remove from console** then removes just the tile. A game on a USB drive that isn't plugged in isn't marked, because its files may come back. Games installed as packages can't be removed from the app; delete those on the PS5.

## Moving games between drives

On the **Installed** page, click the arrows on a game's tile to move it to another drive of the same console, for example from the internal SSD to a USB drive. The helper copies it on the console itself (nothing crosses the network), into a hidden `.<name>.partial` folder on the new drive. The app then checks every file's size, deletes the original the same way **Remove from console** does, removes its home-screen tile, and renames the copy into place. ShadowMountPlus adds it again from the new drive, and the app puts its logo back. Save data is kept. Close the game on the PS5 first.
If the copy fails, is canceled or doesn't match, the copy is deleted and the game stays where it was. Moves need helper 0.7.0 or later, and one runs at a time per console.

## Backing up save data

On a game's page, **Back up saves** copies that game's save data from the console to this PC, for every user on the console, into `save-backups/<title ID>/<date> <console>` in the data folder (so a backup of the data folder includes them). The page lists the backups already made. The console is only read: the app looks in each user's folder under `/user/home` for folders named after the title ID (`savedata_prospero` for PS5 games, `savedata` for PS4 games, and their metadata folders) and keeps their console paths. A backup that fails part way is deleted.
Saves stay encrypted and only work on the same console with the same account. The app doesn't restore them, since that would mean letting the helper write outside the homebrew folders. To restore, copy the folders back to the same paths with an FTP payload while the game is closed.

## Home-screen logos

A game you install shows its name in plain text on its PS5 home-screen page, where a store-bought game shows its logo: the console gets logos from PlayStation Network, which a jailbroken console doesn't reach. The app adds the logo itself after each install, once ShadowMountPlus has put the game on the home screen (it waits up to 3 minutes); the install's message on the **Installed** page then says whether it worked. For a game installed earlier, open it in the library and click **Show logo on PS5 home screen**, which does it on every console the game is installed on. It shows right away, with no restart. Removing a game takes its logo away with its home-screen tile.
This PC looks the logo up in PlayStation Store's catalog (no account needed), by the game's concept ID from the console's home-screen database, so this PC needs internet access; the console doesn't. Some games have no logo there. The helper writes it to `/user/appmeta/<title ID>/logo.png` and sets the game's `logoImageInfo` in `/system_data/priv/mms/app.db` with SQLite, in place, the way the home screen itself writes it. Those are the only two things outside the `homebrew` folders the helper ever changes, and only for a game the console already has.
Before each change, the app saves a copy of that database as `console-backups/<console id>-app.db` in the data folder. If the home screen ever acts up afterwards, put that copy back over `/system_data/priv/mms/app.db` with an FTP payload while the PS5 shows its Settings page, and restart the console.

## PS5 app

**Install PS5 app** under **Settings → Consoles** puts *Spela* on the console's home screen (in `/data/homebrew/PPSA99787`, where ShadowMountPlus finds it). It shows the games on your PC, with L1 and R1 switching between **Games**, **Installed** and **DLC**. Cross opens the install dialog: pick the drive (the one with the most free space is chosen for you) and press Cross again to queue it, the same as clicking Install on the PC. Square stops an install that's waiting or running, and removes an installed game.
The PS5 app reaches the PC through the helper: it asks the helper on its own console (`127.0.0.1:7735`), and Spela on the PC keeps picking up those requests from the helper and answering them ([backend/app/relay.py](../backend/app/relay.py)). The PC only ever connects out to the console, so no firewall rule, network setting or tray option is needed, and a PC whose address changes needs nothing either. The helper must be running, as it must for installs; only the PS5 app on that console itself can reach the PC this way. The app is paired with this PC: the PC writes a key into the app's `assets/server.txt`. The key only lets that console see the library and its own drives, queue and cancel installs to itself, and remove games from itself. Deleting the console on the PC revokes the key.
The app is built from [ps5-homebrew-ui](https://github.com/blackbearreloaded/ps5-homebrew-ui) (GPL-3.0-or-later) with our screen in [console-app/](../console-app/). Run `./console-app/build.ps1` (needs Docker Desktop) after changing it: it rebuilds `backend/app/console_app/PPSA99787`, which is committed. `./console-app/build.ps1 -Preview` renders the screen on the PC into `console-app/preview/`, against a fake PC app, plus the screen for a PC that doesn't answer in `preview/unreachable/`. The logo at the top of the screen is [frontend/src/wordmark.png](../frontend/src/wordmark.png), built into the app by `python console-app/make_logo.py`: run that after changing the wordmark, then build.

## Versions

The version number lives in one place: `__version__` in [backend/app/__init__.py](../backend/app/__init__.py). The Python package, `/api/v1/health`, the page header and the helper payload all read it.
To release a new version, first run the [manual test on a real PS5](manual-test.md), then `./release.ps1 -Bump patch -Publish` (or `minor` / `major`); see [Releasing](../CONTRIBUTING.md#releasing). A plain `./build.ps1` builds only the `.exe`, and `./build.ps1 -Bump patch` also raises the version and rebuilds the committed helper (`backend/app/payload/spela.elf`, needs Docker Desktop). A test fails if the bundled helper's version doesn't match the app's version.

## Backup and restore

Everything the app knows is in the data folder (`%LOCALAPPDATA%\Spela`, or `DATA_DIR`). Your game files are never in it.

1. **Quit** from the tray menu, so the database isn't being written.
2. Copy the whole folder. `staging` (temporary archive extractions), `store` (PlayStation Store art and facts, fetched again when needed), `webview` (the window's browser profile) and `app.log` can be left out.
3. To restore, quit the app, put the folder back (or copy it to the same place on the new PC) and start it.

Keep `secret_key` together with `app.db`. Without it, saved console passwords can't be decrypted (enter them again) and the PS5 app's pairing key stops working (click **Update PS5 app**). Library folders are stored by full path, so on a new PC give the drives the same letters, or remove the old folders under **Settings** and add the new ones. Games in a removed folder leave the library, and the next scan adds them back. Uploaded covers move with the folder.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Port 8787 is already in use" at start | Another program holds the port. Close it, or set `PORT` to another number. |
| The app's window stays blank or the tray says it couldn't start | Look at `app.log` in the data folder. |
| A phone or other PC can't open the app | Tick **Allow other devices on my network** in the tray, make sure the network is *Private* in Windows, and open the address the tray shows (an IP address, not the PC's name). |
| "Open the app by its IP address" | You opened it by a name. Use `http://127.0.0.1:8787` on this PC, or the IP address from the tray elsewhere. |
| Console shows *Offline* | Check the IP address, and that the PS5 is on and on the same network. The console's status says when its address isn't on this PC's network. |
| **Find it on my network** finds no PS5 | The PS5 is off, in rest mode without *Stay connected to the internet*, on another network, or the router keeps devices from seeing each other (client or AP isolation, common on guest Wi-Fi). Type its IP address instead. |
| Console shows *Degraded* | The PS5 is on but the helper isn't running (it stops on every reboot) or is outdated: click **Install helper** / **Update helper**. If that fails with "ELF loader isn't running", start your jailbreak's loader first. |
| Console shows *Rest mode* | Wake the PS5. Payloads don't run in rest mode. |
| Install fails with "can't see free space" | Install the helper; FTP alone can't report drive space. |
| Install fails with "Not enough space on this PC to extract" | Free space on the PC, or point `STAGING_DIR` at a bigger drive. |
| Install shows *Interrupted* | The app or the connection stopped mid-copy. Queue it again; the leftover partial copy is replaced. |
| Removed game's tile is still on the home screen | Delete the tile on the PS5 (Options, then Delete). It won't come back, since its files are gone. |
| PS5 app says "The Spela helper isn't running" | The PS5 restarted, which stops the helper. Run your jailbreak's ELF loader: Spela on the PC sends the helper within a minute. The PS5 app tries again by itself. |
| PS5 app says "Spela on your PC isn't answering" | Start Spela on the PC, and check that this PS5 is added under **Settings → Consoles**. Within about half a minute the PS5 app shows the library again. |
| PS5 app says it isn't paired | The console was deleted on the PC, or `secret_key` changed. Click **Update PS5 app**. |
| Saved console password "can't be decrypted" | `secret_key` or `CREDENTIALS_ENCRYPTION_KEY` changed. Enter the password again. |
| Covers or games missing after moving to a new PC | See [Backup and restore](#backup-and-restore): drive letters must match, or re-add the library folders. |

## Build the .exe

Needs Python 3.12+ and Node 22. Run `./build.ps1`; the result is `dist\Spela-<version>.exe`.

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
| `.pkg` | `\x7fCNT` (PS4) or `\x7fFIH` (PS5). Title ID, content ID, title and version come from the CNT header and `param.sfo` / `param.json` ([backend/app/pkg.py](../backend/app/pkg.py)). |
| `.zip` `.7z` `.rar` `.001` | Archive magic |
| DLC package | A `.pkg` whose CNT content type (u32 at 0x74) is add-on: `0x21` on PS5, `0x1B`/`0x1C` on PS4. Games are `0x20`/`0x1A`. |
| DLC unlocker | A folder with `dlc_emu.ini`. The game's title ID comes from the content IDs listed in it. |

Anything else is marked `unknown`, and its first 16 header bytes are saved under *Detection evidence* so a signature can be added in `SIGNATURES` in [backend/app/scanner.py](../backend/app/scanner.py).
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
- **Numbered migrations, not Alembic.** `backend/app/migrations.py` upgrades an older database at startup, tracked in SQLite's `user_version`. Adding a model column means appending a step there. A data folder from a newer version is refused rather than opened by an older one.
- **Interval rescans, no watchdog.** Filesystem events aren't reliable on SMB/NFS mounts.
- Login is rate-limited to 5 failures per minute per IP. Read-only users aren't built yet.
- **Removals are tracked in memory**, not in the `removal_jobs` table: its foreign key would block dropping the installed game once it's gone. The audit log keeps the record; a restart forgets previews and progress. Previews expire after 10 minutes.
