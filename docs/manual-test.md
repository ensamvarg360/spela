# Manual test on a real PS5

Run this before tagging a release. It walks the definition of done ([project.md §21](../project.md#21-definition-of-done-for-v1)) end to end on a real console. The automated tests mock the console, so this is the only check of the real paths, helper and ShadowMountPlus behaviour.

Tick each box as you go. When a step doesn't match **Expect**, note what you saw, and the last lines of `app.log` from the data folder, under [Problems found](#problems-found).

| | |
|---|---|
| Date | |
| App version (page header) | |
| PS5 firmware (Installed page) | |
| ShadowMountPlus version | |
| Drives on the console (internal, USB, extended) | |

## Before you start

Prepare a test library folder on the PC, separate from your real one, holding small games you own:

- [ ] one **folder dump** (a folder with `sce_sys/param.json`)
- [ ] one **disc image** (`.exfat`, `.ffpkg`, `.ffpfs` or `.ffpfsc`)
- [ ] one **archive** (`.zip` or `.7z`) holding a folder dump or image
- [ ] one **split archive** (`.7z.001`, `.7z.002`, …), if you have one
- [ ] one **`.pkg`** file
- [ ] one file that isn't a game, renamed to `fake.exfat`
- [ ] one copy of the dump with a `.partial` file inside it, to stand in for an incomplete copy

Also have ready:

- [x] the PS5 jailbroken, with ShadowMountPlus and the ELF loader (port 9021) running
- [x] a phone or second PC on the same network
- [ ] a copy of the data folder from the release you're replacing, to test the database upgrade (see section 15)

---

## 1. Start the app

- [x] Double-click **PS5 Local Storefront.exe**.
  **Expect:** its window opens and a tray icon appears.
- [x] Close the window, then start the `.exe` again.
  **Expect:** the same window comes back. No second copy starts and no "port in use" error appears.

## 2. Log in locally

- [x] On first run, choose an admin password of at least 8 characters.
  **Expect:** you're logged in and land on the Library.
- [x] **Log out**, then log in with a wrong password 5 times.
  **Expect:** the 6th attempt says to wait a minute.
- [x] After a minute, log in with the right password.
  **Expect:** it works.
- [x] Tick **Allow other devices on my network** in the tray menu, then open the address the tray shows on your phone.
  **Expect:** the login page. Nothing else is reachable before you log in.
- [x] On the phone, open the app by the PC's name instead of its IP address (for example `http://mypc:8787`).
  **Expect:** "Open the app by its IP address".

## 3. Add library roots

- [x] In **Settings**, click **Browse…**, pick the test library folder, then **Add this folder**.
  **Expect:** it's listed under **Library folders**.
- [x] Add a second folder, for example on another drive or a network share.
  **Expect:** both are listed and shown as reachable.

## 4. Scan a mixed library

- [x] Click **Scan now** and wait for it to finish.
  **Expect** each test item to show this way:

| Item | Expect |
|---|---|
| Folder dump | Ready, with title, title ID and version from `param.json` |
| Disc image | Ready, or Needs attention if its title ID comes only from the file name |
| Archive | Listed as an archive |
| Split archive | One entry for the whole set, not one per part |
| `.pkg` | Listed, but Install is refused: "no package installer" |
| `fake.exfat` | Unknown, with header bytes under *Detection evidence* |
| Dump with `.partial` | Flagged as incomplete |

- [x] Open a game whose title ID came from the file name, and confirm the title ID under **Edit**.
  **Expect:** it changes to Ready.
- [x] Upload a cover with **Change cover**.
  **Expect:** the tile shows it.

## 5. Statuses are easy to tell apart

- [x] Filter the Library by each status.
  **Expect:** Ready, Needs attention, Incompatible and Unknown each show the right games, and each status has its own clearly different badge.

## 6. Connect the console and view installed games

- [x] In **Settings → Consoles**, add the PS5 with **Helper payload**, a name and its IP address.
- [x] On **Installed**, click **Install helper**.
  **Expect:** the PS5 shows a notification that the helper is running, and the console changes to *Online*.
- [x] Check the games already on the console.
  **Expect:** each one is listed with title, title ID, version, drive and path. Games also in your library show as matched, with the library's cover.
- [x] From the PC, run `curl http://<PS5 IP>:7735/info`.
  **Expect:** `401` "wrong key". The helper answers only to the app.

## 7. Firmware, drives, free space and paths

- [x] Compare the firmware shown with the PS5's own **Settings → System → System Software**.
  **Expect:** they match.
- [x] Compare each drive card's free space with the PS5's **Settings → Storage**.
  **Expect:** within about 1 GB of each other.
- [x] Plug in a USB drive that has a `homebrew` folder, then click **Refresh inventory**.
  **Expect:** it appears as a new drive card.
- [x] Unplug it and click **Refresh inventory** again.
  **Expect:** it disappears. Its games don't show as *Icon only*.
- [x] Put the PS5 in rest mode and wait 30 seconds.
  **Expect:** *Rest mode*, or *Offline* if "Stay connected to the internet" is off. Wake it up and reinstall the helper.
  **Expect:** back to *Online*.

## 8. Queue an install

For each of the folder dump, the disc image and the archive:

- [ ] Open the game and click **Install**.
  **Expect:** "Queued. Follow it on the Installed page."
- [ ] Click **Install** again on the same game.
  **Expect:** refused with "Already queued" or "Already installed".
- [ ] When it finishes, check the PS5 home screen.
  **Expect:** the game's tile appears within about a minute, with its store logo within about 3 minutes, and it launches.

Also check:

- [ ] For the archive: after the install, the `staging` folder in the data folder is empty.
- [ ] Untick **Allow installs here** on every drive, then try an install.
  **Expect:** it fails with "No drive on this console is set as an install target". Tick the boxes again afterwards.

## 9. Progress, cancel and recovery

- [ ] Start a large install and watch the **Installed** page.
  **Expect:** stage, percentage, bytes, speed and time remaining all update about once a second.
- [ ] Click **Cancel** halfway through.
  **Expect:** *Canceled*. No `.<name>.partial` folder is left in the drive's `homebrew` folder, and the game doesn't appear on the home screen.
- [ ] Start it again, then pull the PC's network cable (or turn off Wi-Fi) halfway through.
  **Expect:** *Interrupted*, not *Failed*.
- [ ] Reconnect and queue it again.
  **Expect:** it completes, and the leftover partial copy is replaced.
- [ ] Start an install, then **Quit** from the tray halfway through. Start the app again.
  **Expect:** the job shows *Interrupted* with "The app was closed during the install."

## 10. Preview an uninstall

- [ ] On **Installed**, click **Remove from console** on a game you installed in section 8.
  **Expect:** the dialog shows the exact path, for example `/data/homebrew/PPSA01234`, the space it will free, and that save data and the PC copy are kept.
- [ ] Leave the dialog open for 10 minutes, then confirm.
  **Expect:** "This preview has expired."

## 11. Confirm by typing the title ID

- [ ] Open the preview again and type a wrong title ID.
  **Expect:** refused.
- [ ] Type the correct title ID.
  **Expect:** a progress bar, then "Removed", and its tile is gone from the home screen too.

## 12. Only the approved content is removed

- [ ] Over FTP, or by listing on the console, check the drive's `homebrew` folder.
  **Expect:** only that game's folder or image is gone. Other games and the `PPSA99787` folder (the PS5 app) are untouched.
- [ ] Remove a disc-image game the same way.
  **Expect:** removed. If space doesn't come back, it does after restarting the PS5.
- [ ] On the PS5, delete a ShadowMountPlus game's files some other way (FTP), then click **Refresh inventory**.
  **Expect:** it shows *Icon only*, and **Remove from console** removes just its tile.
- [ ] Try **Remove from console** on a package-installed game.
  **Expect:** refused, with a message to delete it on the PS5.

## 13. Backups and save data are kept

- [ ] After the removals, check the test library folder.
  **Expect:** every source file is still there.
- [ ] Install a removed game again and launch it.
  **Expect:** its save data is still there.

## 14. Audit log

- [ ] Open **Settings → Activity log**.
  **Expect** entries for: setup and logins (including the failed ones), console creation, helper install, each install queued and its result (canceled, interrupted, success), each removal (blocked, success) and each logo.
- [ ] Check that no entry contains a password.

## 15. Move to another PC

- [ ] **Quit** from the tray and copy the data folder to another PC, or to another Windows user on this one.
- [ ] Start the app there.
  **Expect:** the same password works, the library and uploaded covers are there, and the console's saved settings are kept.
- [ ] Click **Install helper**, then **Update PS5 app**.
  **Expect:** both work, and the PS5 app shows the library from the new PC.
- [ ] Put the data folder from the release you're replacing in place and start this version.
  **Expect:** it starts, and games already in the library aren't marked *New*.

## 16. PS5 app

- [ ] Click **Install PS5 app** on the **Installed** page.
  **Expect:** *PS5 Local Storefront* appears on the PS5 home screen.
- [ ] Open it on the PS5.
  **Expect:** your PC library with covers.
- [ ] Press L1 and R1.
  **Expect:** the tabs switch between **Games**, **Installed** and **DLC**. DLC shows only under **DLC**, never as *Installed*.
- [ ] With two install drives set on the PC, press Cross on a game that isn't installed, pick the other drive with the D-pad, and press Cross.
  **Expect:** it queues, the PC's **Installed** page shows the job, and the game goes to the drive you picked. A drive without room says *Not enough space* and can't be picked.
- [ ] Press Square on that game while it's waiting or installing, then Square again.
  **Expect:** the install stops, the same as **Cancel** on the PC.
- [ ] Press Square on an installed game and confirm.
  **Expect:** it's removed, the same as from the PC.
- [ ] Delete the console on the PC, then open the PS5 app.
  **Expect:** "isn't paired".

---

## 17. Helper after a restart, checked copies, moving between drives

- [ ] Restart the PS5, run the jailbreak so the ELF loader is up, and leave the app alone.
  **Expect:** within about a minute the console shows the helper's notification, and the console turns *Online*. The activity log shows *Helper installed* with `automatic: True`.
- [ ] Install a folder dump.
  **Expect:** its message ends with *All N files checked*.
- [ ] On the **Installed** page, click the arrows on a folder-dump game on the internal drive and move it to a USB drive.
  **Expect:** a progress bar, then *Moved to USB drive 0*. The old folder is gone from `/data/homebrew`, the game is in `/mnt/usb0/homebrew`, its tile comes back on the home screen with its logo, and it launches with its save data.
- [ ] Move a disc-image game the same way, and cancel a third move half way.
  **Expect:** the image moves and launches. The canceled one is still on its old drive and no hidden `.partial` is left on the new one.

- [ ] Save the console's RP-RegistKey (from Chiaki-ng) under its Remote Play key, put the PS5 in rest mode, and click **Wake** on the Dashboard.
  **Expect:** the PS5 turns on and the console leaves *Rest mode* within about 30 seconds.

- [ ] On a game's page, click **Back up saves** for a game with save data.
  **Expect:** a backup listed with one or more files, under `save-backups/<title ID>` in the data folder, mirroring `/user/home/<user>/savedata_prospero/<title ID>`. Note the folders it found here if they differ.

## Problems found

| Step | What happened | `app.log` lines |
|---|---|---|
| | | |

## Result

- [ ] Every box above is ticked, or every problem is fixed and its step run again. Then tag the release.
