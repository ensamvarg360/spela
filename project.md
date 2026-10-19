# Spela — Project Specification

## 1. Project summary

Build a self-hosted, Docker-deployable web application called **Spela**.

The application runs on the user’s home network and provides a private storefront-style interface for a personally maintained library of legally created PlayStation 5 game backups. It scans user-selected directories, catalogs compatible game formats, displays installed games from a connected PS5, queues remote installations over LAN, and removes installed game files safely and explicitly.

This is a **single-user, local-first application**. It must not be exposed directly to the public internet. It is not a piracy tool, download manager, or repository of copyrighted content. It manages files the user already possesses and identifies as legally backed up.

The first deployment target is Docker Compose on the user’s ZimaOS server, but the application must remain portable to any standard Docker host.

## 2. Primary goals

### Must have

- Scan one or more user-configured local or mounted network directories for PS5-compatible backup formats.
- Build and maintain a searchable game library with metadata, cover art, file size, format, title ID, version, source path, and provenance.
- Detect incompatible or unknown formats and prevent unsafe installation.
- Connect to one or more jailbroken PS5 consoles over LAN.
- Read console information, including firmware and available storage drives.
- Discover installed games and reconcile them with the local library.
- Display an **Installed** storefront view.
- Queue and monitor remote installation jobs.
- Support installation of folder dumps, ShadowMountPlus image formats, and package files where the connected console’s firmware and installed helper software support them.
- Remove installed games safely, with explicit confirmation, strict path allowlists, and audit logging.
- Preserve PC-side library backups and save data unless the user explicitly chooses otherwise.
- Run entirely in Docker Compose with persistent volumes for database, configuration, logs, and staging files.

### Non-goals for v1

- No public internet exposure.
- No integrated downloading, searching, scraping, or acquisition of copyrighted games.
- No automatic deletion of source backups.
- No automatic deletion of save data.
- No multi-user accounts in v1; use one administrator account plus optional read-only access.
- No cloud dependency for core scanning, library management, or installation.
- No assumption that every PS5 homebrew tool remains API-compatible; use replaceable adapter interfaces.

## 3. User workflow

### Library management

1. User configures one or more library roots, such as:
   - `/media/ps5-library`
   - `/mnt/nas/ps5-backups`
   - `D:\PS5 Backups`
2. The app recursively scans those roots.
3. It identifies supported formats and extracts available metadata.
4. It displays the library as a storefront grid.
5. User can edit metadata, assign cover art, mark favorites, and record provenance.
6. New or changed files are detected through scheduled rescans and filesystem watchers where available.

### Installation

1. User browses the storefront.
2. User selects a game and chooses **Install**.
3. App selects or asks for a target PS5 and destination drive.
4. App performs preflight validation.
5. App creates an install job.
6. App transfers the selected content over LAN.
7. App invokes the appropriate console-side registration or package-install route.
8. App polls the console and updates job status.
9. On success, the title appears in the Installed view.

### Removal

1. User opens an installed game.
2. User selects **Uninstall**.
3. App performs a dry-run preview showing exact paths, estimated reclaimed space, and warnings.
4. User confirms by entering the title ID.
5. App removes only the approved console-side content.
6. App rescans the console inventory and updates the database.
7. App writes an audit-log entry.

## 4. Recommended stack

Use the following stack unless there is a strong technical reason to change it.

| Component | Recommendation |
|---|---|
| Backend | Python 3.12+, FastAPI |
| Database | SQLite via SQLAlchemy 2.x |
| Background jobs | ARQ with Redis, or FastAPI BackgroundTasks for MVP |
| Frontend | React + Vite + TypeScript |
| UI library | Tailwind CSS + shadcn/ui or a comparable accessible component library |
| API client | Typed fetch client generated from OpenAPI |
| File watching | `watchdog` |
| Containerization | Docker Compose |
| Reverse proxy | Optional Caddy or Traefik; LAN-only by default |
| Authentication | Session cookie auth with bcrypt/argon2 password hashing |
| Logging | Structured JSON logs written to stdout and persistent volume |
| Testing | pytest, pytest-asyncio, Playwright for critical UI flows |

Keep the backend and frontend in one repository:

```text
ps5-local-storefront/
  backend/
    app/
      main.py
      config.py
      database.py
      models/
      schemas/
      api/
      services/
      scanners/
      installers/
      console_adapters/
      jobs/
      security/
    tests/
    pyproject.toml
    Dockerfile
  frontend/
    src/
      components/
      pages/
      api/
      stores/
      types/
    package.json
    Dockerfile
  docker-compose.yml
  docker-compose.override.yml
  .env.example
  README.md
  project.md
```

## 5. Docker Compose architecture

Use separate services:

```yaml
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ps5_storefront
      POSTGRES_USER: ps5
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - db_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    restart: unless-stopped

  backend:
    build: ./backend
    restart: unless-stopped
    depends_on:
      - db
      - redis
    environment:
      DATABASE_URL: postgresql+psycopg://ps5:${DB_PASSWORD}@db:5432/ps5_storefront
      REDIS_URL: redis://redis:6379/0
      SECRET_KEY: ${SECRET_KEY}
      LAN_ONLY: "true"
    volumes:
      - ./data:/data
      - ${PS5_LIBRARY_ROOT}:/library:ro
      - ${PS5_STAGING_ROOT}:/staging
    ports:
      - "127.0.0.1:8787:8000"

  worker:
    build: ./backend
    restart: unless-stopped
    depends_on:
      - db
      - redis
    command: arq app.jobs.worker.WorkerSettings
    environment:
      DATABASE_URL: postgresql+psycopg://ps5:${DB_PASSWORD}@db:5432/ps5_storefront
      REDIS_URL: redis://redis:6379/0
      SECRET_KEY: ${SECRET_KEY}
    volumes:
      - ./data:/data
      - ${PS5_LIBRARY_ROOT}:/library:ro
      - ${PS5_STAGING_ROOT}:/staging

  frontend:
    build: ./frontend
    restart: unless-stopped
    depends_on:
      - backend
    ports:
      - "127.0.0.1:8788:80"

volumes:
  db_data:
```

Important implementation detail: the library mount should be read-only inside the application. The staging directory is the only directory where the application should create temporary transfer payloads.

The user’s existing PS5 network share may be mounted at a host path such as `/media/192.168.0.40/PS5` and mapped into containers as `/library` or `/ps5`. Do not hard-code the user’s current ZimaOS path into application logic; make it configuration-driven.

## 6. Data model

Use PostgreSQL in Docker, but keep models portable to SQLite for local development.

### `games`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `title` | string | Human-readable title |
| `title_id` | string | Example: `PPSA01234` |
| `region` | string | Optional |
| `version` | string | Game or backup version |
| `format` | enum | `folder_dump`, `image`, `ps5_pkg`, `ps4_pkg`, `archive`, `unknown` |
| `image_subtype` | string | `exfat`, `ffpkg`, `ffpfs`, `ffpfsc`, etc. |
| `source_path` | string | Absolute path inside mounted library |
| `source_fingerprint` | string | Size, mtime, and/or content hash |
| `size_bytes` | bigint | Logical installed/content size |
| `compressed_size_bytes` | bigint | Archive size, if applicable |
| `cover_path` | string | Local cover image path |
| `description` | text | User-editable |
| `genre` | string | Optional |
| `release_date` | date | Optional |
| `owned_copy_type` | enum | `disc`, `digital`, `other`, `unknown` |
| `backup_date` | date | When the user created the backup |
| `provenance_notes` | text | User-entered ownership/backup details |
| `verified` | boolean | Whether integrity checks passed |
| `compatibility_status` | enum | `ready`, `needs_attention`, `incompatible`, `unknown` |
| `compatibility_reason` | text | Human-readable explanation |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

### `consoles`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `name` | string | Example: `Living Room PS5` |
| `ip_address` | string | |
| `connection_type` | enum | `ftp`, `http`, `custom` |
| `port` | integer | |
| `username` | string | Optional |
| `credentials_secret_id` | UUID | Reference to encrypted credential |
| `firmware` | string | Detected firmware |
| `helper_status` | json | Detected helper versions/status |
| `last_seen_at` | timestamp | |
| `last_inventory_scan_at` | timestamp | |
| `enabled` | boolean | |

### `drives`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `console_id` | UUID | FK |
| `external_id` | string | Console-provided identifier |
| `label` | string | Internal SSD, USB drive, etc. |
| `path` | string | Mount path |
| `drive_type` | enum | `internal`, `usb`, `network`, `unknown` |
| `total_bytes` | bigint | |
| `free_bytes` | bigint | |
| `is_install_target` | boolean | |
| `last_seen_at` | timestamp | |

### `installed_games`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `console_id` | UUID | FK |
| `game_id` | UUID | Nullable FK to local library |
| `title_id` | string | |
| `title` | string | |
| `version` | string | |
| `install_route` | enum | `package_installer`, `shadowmountplus`, `unknown` |
| `drive_id` | UUID | Nullable FK |
| `installed_path` | string | Exact console-side path |
| `source_path` | string | For ShadowMountPlus-managed content |
| `size_bytes` | bigint | |
| `status` | enum | `installed`, `update_available`, `partially_installed`, `orphaned_icon`, `unknown` |
| `last_verified_at` | timestamp | |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

### `install_jobs`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `console_id` | UUID | FK |
| `game_id` | UUID | FK |
| `target_drive_id` | UUID | Nullable FK |
| `status` | enum | `queued`, `preflight`, `transferring`, `registering`, `success`, `failed`, `canceled`, `interrupted` |
| `progress` | float | 0.0 to 1.0 |
| `bytes_transferred` | bigint | |
| `total_bytes` | bigint | |
| `transfer_rate_bps` | bigint | |
| `eta_seconds` | integer | |
| `message` | text | |
| `error_detail` | json | |
| `created_at` | timestamp | |
| `started_at` | timestamp | |
| `finished_at` | timestamp | |

### `removal_jobs`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `console_id` | UUID | FK |
| `installed_game_id` | UUID | FK |
| `status` | enum | `preview`, `awaiting_confirmation`, `removing`, `success`, `failed`, `canceled` |
| `planned_paths` | json | Exact paths approved for deletion |
| `bytes_freed` | bigint | |
| `save_data_retained` | boolean | |
| `message` | text | |
| `created_at` | timestamp | |
| `finished_at` | timestamp | |

### `audit_logs`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `user_id` | UUID | Nullable |
| `console_id` | UUID | Nullable |
| `action` | string | `install`, `uninstall`, `scan`, `credential_update`, etc. |
| `title_id` | string | Nullable |
| `target_path` | string | Nullable |
| `result` | enum | `success`, `failed`, `blocked`, `canceled` |
| `details` | json | |
| `created_at` | timestamp | |

## 7. Format support and compatibility

### Supported format categories

| Format | Internal category | Install route | v1 behavior |
|---|---|---|---|
| Extracted game folder / dump | `folder_dump` | `shadowmountplus` | Support |
| `.exfat` image | `image` / `exfat` | `shadowmountplus` | Support |
| `.ffpkg` image | `image` / `ffpkg` | `shadowmountplus` | Support |
| `.ffpfs` image | `image` / `ffpfs` | `shadowmountplus` | Support |
| `.ffpfsc` image | `image` / `ffpfsc` | `shadowmountplus` | Support |
| PS5 package file | `ps5_pkg` | `package_installer` | Conditional support |
| PS4 package file | `ps4_pkg` | `package_installer` | Conditional support |
| `.zip`, `.7z`, `.rar`, split archive | `archive` | Extract, then reclassify | Support extraction |
| Unknown file/folder | `unknown` | None | Block install |

### Content signatures

Classification requires a matching signature; the file extension alone yields `unknown`.

| Format | Signature |
|---|---|
| `.exfat` | ASCII `EXFAT   ` at offset 3 (exFAT boot sector) |
| `.ffpkg` | UFS2 superblock magic `0x19540119` (LE) at offset `0x1055C` |
| `.ffpfs` | PFS superblock: version u64 at 0, magic `20130315` (u64 LE) at 8; or exFAT wrapper |
| `.ffpfsc` | ASCII `PFSC` + u32 `0` at offset 0; or exFAT wrapper (MkPFS default) |
| PS4 `.pkg` | `\x7FCNT` at offset 0 |
| PS5 `.pkg` | `\x7FFIH` at offset 0; embedded CNT offset is a u64 LE at `0x58` |
| PS5 multi-part slice | ASCII `PS5MPKG1` at offset 0 |

### Package metadata parsing

The scanner must use platform-specific package parsers.

CNT layout (shared by PS4 and the CNT embedded in PS5 packages, per LibProsperoPKG): big-endian header; entry count u32 at `0x10`, entry-table offset u32 at `0x18`, 36-character content ID at `0x40`. Entry records are `0x20` bytes (id, name offset, flags1, flags2, data offset, data size); offsets are relative to the CNT start; `flags1 & 0x80000000` marks an encrypted entry. `param.sfo` is entry `0x1000`, `param.json` is entry `0x2000`.

#### PS4 packages

- Detect PS4 `.pkg` packages.
- Read `param.sfo` from package entry `0x1000`.
- Extract:
  - `TITLE_ID`
  - title
  - application version
  - category
  - region
- Set `metadata_source` to `ps4_param_sfo`.
- Set `metadata_confidence` to `high` when parsing succeeds.

#### PS5 packages

- Detect PS5 `.pkg` packages independently from PS4 packages.
- Parse the CNT metadata container.
- Read the content ID from CNT metadata.
- Locate and parse `param.json` from the CNT entry directory.
- Extract:
  - title ID
  - content ID
  - localized title
  - application version
  - category
  - minimum firmware, when present
- Set `metadata_source` to `ps5_cnt` or `ps5_param_json`.
- Set `metadata_confidence` to `high` when parsing succeeds.
- Do not use the PS4 `param.sfo` parser for PS5 packages.

#### Filename fallback

Use filename parsing only when structured metadata parsing fails.

- Match strict title-ID patterns:
  - PS5: `PPSA\d{5}`
  - PS4: `CUSA\d{5}`
- Set `metadata_source` to `filename`.
- Set `metadata_confidence` to `low`.
- Add a parser warning.
- Display a metadata warning in the UI.
- Block automatic installation until metadata is verified or manually confirmed.

#### Split packages

- Treat multi-part package sets as one logical game.
- Parse the first/index part.
- Ignore `.part2`, `.part3`, etc. as separate library entries.
- Require all parts to be present before installation.

### Firmware compatibility matrix

Store this matrix in configuration, not hard-coded business logic:

```yaml
compatibility:
  folder_dump:
    min_firmware: null
    max_firmware: null
    install_route: shadowmountplus
    status: supported

  image_exfat:
    min_firmware: null
    max_firmware: null
    install_route: shadowmountplus
    status: supported

  image_ffpkg:
    min_firmware: null
    max_firmware: null
    install_route: shadowmountplus
    status: supported

  image_ffpfs:
    min_firmware: null
    max_firmware: null
    install_route: shadowmountplus
    status: supported

  image_ffpfsc:
    min_firmware: null
    max_firmware: null
    install_route: shadowmountplus
    status: supported

  ps5_pkg:
    min_firmware: null
    max_firmware: "11.60"
    install_route: package_installer
    status: conditional

  ps4_pkg:
    min_firmware: null
    max_firmware: null
    install_route: package_installer
    status: supported
```

The application must treat this matrix as editable configuration because PS5 homebrew compatibility changes over time.

### Compatibility rules

A game is `ready` only when all checks pass:

- Format is recognized.
- Firmware is within the configured range for that format.
- Required console helper is detected and reachable.
- Required base game is installed, if this is DLC or an update.
- Required free space is available.
- Source path exists and is readable.
- Archive has enough temporary space for compressed and unpacked data.
- No conflicting duplicate title ID is already installed.
- No active job is using the same source path or target title ID.

A game is `incompatible` when:

- Format is unknown.
- Format is explicitly unsupported for detected firmware.
- Required helper is missing.
- Base game dependency is missing.
- Source path is missing or unreadable.
- Console storage is insufficient.

A game is `needs_attention` when:

- It is an archive requiring extraction.
- Metadata is incomplete.
- A duplicate title ID exists.
- The source is marked partial.
- User confirmation is required for package installation.

Never silently convert one format into another. Extraction is allowed only into the application-controlled staging directory.

## 8. Scanner requirements

### Scan behavior

The scanner must:

- Accept multiple library roots.
- Recursively scan directories.
- Skip configured exclude patterns.
- Detect folder dumps, image files, package files, and archives.
- Read optional sidecar metadata files, such as `game.json` or `metadata.json`.
- Derive provisional metadata from directory and file names when sidecars are absent.
- Detect incomplete transfers using `.partial`, `.part`, `.tmp`, or app-defined marker files.
- Calculate source fingerprints to avoid unnecessary rescans.
- Detect duplicate title IDs.
- Detect duplicate source paths.
- Generate a scan report with added, updated, missing, invalid, and unknown items.

### Scanner output

For each discovered item, produce:

```json
{
  "source_path": "/library/Example Game (PPSA01234)",
  "detected_format": "folder_dump",
  "title_id": "PPSA01234",
  "provisional_title": "Example Game",
  "version": "1.02",
  "size_bytes": 74342195200,
  "fingerprint": "sha256:...",
  "warnings": [],
  "status": "ready"
}
```

### Sidecar metadata

Support optional JSON sidecars:

```json
{
  "title": "Example Game",
  "title_id": "PPSA01234",
  "version": "1.02",
  "region": "US",
  "format": "folder_dump",
  "owned_copy_type": "disc",
  "backup_date": "2026-10-01",
  "cover": "cover.jpg",
  "description": "Personal backup created from owned disc.",
  "minimum_firmware": null,
  "base_game_title_id": null
}
```

The app must never treat sidecar metadata as security or compatibility truth. It is descriptive metadata only.

## 9. Console adapter architecture

Do not tightly couple the app to one PS5 homebrew implementation. Create an adapter interface.

```python
class ConsoleAdapter(Protocol):
    async def connect(self) -> ConsoleStatus: ...

    async def get_console_info(self) -> ConsoleInfo: ...

    async def list_drives(self) -> list[DriveInfo]: ...

    async def scan_installed_games(self) -> list[InstalledGameInfo]: ...

    async def check_space(
        self,
        drive_id: str,
        bytes_needed: int,
    ) -> SpaceCheckResult: ...

    async def install_package(
        self,
        package_path: str,
        progress_callback: ProgressCallback,
    ) -> InstallResult: ...

    async def stage_shadowmount_content(
        self,
        source_path: str,
        target_drive: DriveInfo,
        progress_callback: ProgressCallback,
    ) -> InstallResult: ...

    async def uninstall_package(
        self,
        title_id: str,
    ) -> RemovalResult: ...

    async def remove_shadowmount_source(
        self,
        installed_game: InstalledGameInfo,
        progress_callback: ProgressCallback,
    ) -> RemovalResult: ...

    async def remove_home_screen_icon(
        self,
        title_id: str,
    ) -> RemovalResult: ...
```

Implement adapters behind a factory:

```python
def get_adapter(console: Console) -> ConsoleAdapter:
    match console.connection_type:
        case "ftp":
            return FTPConsoleAdapter(console)
        case "http":
            return HTTPConsoleAdapter(console)
        case "custom":
            return CustomConsoleAdapter(console)
        case _:
            raise UnsupportedConsoleConnectionError()
```

### Adapter responsibilities

Each adapter must implement:

- Connection health check.
- Credential validation.
- Console information retrieval.
- Drive enumeration.
- Installed-game inventory.
- Free-space checks.
- Package installation.
- ShadowMountPlus content staging.
- Safe uninstall.
- Progress callbacks.
- Cancellation where supported.
- Structured errors.

### Connection maintenance

Implement a console health monitor:

- Ping or TCP-check the configured IP every 15 seconds.
- Probe FTP or HTTP service endpoint.
- Mark console `online`, `degraded`, or `offline`.
- Use exponential reconnect backoff: 2, 5, 10, 30, 60 seconds.
- Do not retry destructive operations automatically after connection loss.
- Mark active install jobs `interrupted`, not `failed`, when a transfer drops.
- Allow explicit resume only when the adapter confirms partial-transfer support.
- Distinguish network loss from console-side helper loss.
- Refresh firmware and drive data after successful reconnection.

## 10. Installation jobs

### Job lifecycle

```text
queued
  -> preflight
  -> transferring
  -> registering
  -> success

Any state may transition to:
  -> failed
  -> canceled
  -> interrupted
```

### Preflight checks

Before transferring, the worker must verify:

1. Console is online.
2. Firmware is readable.
3. Selected format is compatible.
4. Install route is known.
5. Target drive exists and accepts installs.
6. Required free space is available.
7. No conflicting active install job exists.
8. Title ID is not already installed, unless this is an update.
9. Base game is installed, if required.
10. Source is complete and readable.
11. Archive has enough staging space.
12. User has permission to install.

### Progress tracking

The UI must show:

- Current stage.
- Percentage complete.
- Bytes transferred and total bytes.
- Transfer rate.
- ETA.
- Target drive.
- Source path.
- Error message.
- Cancel button.
- Resume button when supported.

### Concurrency

Default to one active install job per console.

Allow configuration for additional concurrent jobs, but warn that simultaneous transfers to the same drive can reduce performance and complicate failure recovery.

### Cancellation

Cancellation must:

- Stop future transfer chunks.
- Mark the job `canceled`.
- Retain partial files only if resume is supported.
- Offer an explicit **Clean partial files** action.
- Never delete the original library backup.

## 11. Installed games view

Create a dedicated **Installed** page.

### Console dashboard

Show:

- Console name.
- Online/offline/degraded status.
- Firmware.
- Last seen time.
- Helper status.
- Last inventory scan.
- Drive usage cards.
- Installed-game count.
- Warnings count.

### Drive cards

Each drive card shows:

- Drive label.
- Drive type.
- Mount path.
- Total capacity.
- Free space.
- Used space.
- Number of installed games.
- Whether it is an allowed install target.

### Game cards

Each installed-game card shows:

- Cover art.
- Title.
- Title ID.
- Installed version.
- Install route.
- Drive.
- Installed path.
- Size.
- Status.
- Update availability.
- Last verified time.
- Warnings.

### Installed-game detail

Include:

- Full title metadata.
- Local library link, if matched.
- Console-side path.
- Source path for ShadowMountPlus-managed content.
- File count and total size.
- Install history.
- Verification history.
- Update history.
- Save-data indicator.
- Uninstall preview.
- Uninstall confirmation.

## 12. Remote installation UI

### Install modal

Show:

- Game cover and metadata.
- Source format.
- Compatibility status.
- Target console.
- Target drive.
- Estimated transfer size.
- Estimated post-install free space.
- Required base game, if applicable.
- Warnings.
- Confirmation checkbox for package installs.
- Install button, enabled only when all checks pass.

### Package install confirmation

For package files, require explicit confirmation:

> This will submit a package file to the PS5 package installer. Confirm the title ID and version before continuing.

### Archive install confirmation

For archives, require explicit confirmation:

> This archive will be extracted to the private staging directory before installation. Ensure sufficient temporary storage is available.

## 13. Safe removal

### Removal routes

#### Package-installed games

Use the console’s native uninstall route when available.

Workflow:

1. Verify title ID.
2. Verify game is not running.
3. Call console uninstall function.
4. Poll installed inventory until title disappears.
5. Update local database.
6. Write audit log.
7. Retain PC-side backup and save data.

#### ShadowMountPlus-managed games

For folder dumps and image files, remove the source content first, then remove any residual home-screen icon.

Workflow:

1. Verify title ID.
2. Verify game is not running.
3. Resolve the exact source path.
4. Confirm source path is inside an allowed root.
5. Confirm source path matches the installed-game record.
6. Unregister or dismount if the adapter supports it.
7. Delete the exact source folder or image.
8. Rescan ShadowMountPlus inventory.
9. Remove residual home-screen icon if needed.
10. Update local database.
11. Write audit log.

### Deletion safety rules

These must be enforced in the backend, not merely in the frontend.

- Maintain a strict deletion allowlist:
  - `/data/homebrew/**`
  - `/mnt/usb*/homebrew/**`
  - Configured ShadowMountPlus scan roots
  - Application-controlled staging paths
- Reject any deletion path outside the allowlist.
- Resolve symlinks before deletion.
- Require exact path match with the recorded installed-game source.
- Require title ID match.
- Forbid wildcard deletion.
- Forbid deletion of the entire library root.
- Forbid deletion while the game is running.
- Forbid deletion while another job uses the same path.
- Require dry-run preview before final deletion.
- Require typed title ID confirmation.
- Retain save data unless explicitly selected.
- Retain PC-side library backup unless explicitly selected.
- Write a detailed audit entry for every removal attempt.

### Uninstall preview response

```json
{
  "installed_game_id": "uuid",
  "title_id": "PPSA01234",
  "title": "Example Game",
  "install_route": "shadowmountplus",
  "source_path": "/mnt/usb0/homebrew/PPSA01234",
  "icon_removal_required": true,
  "estimated_bytes_freed": 74342195200,
  "save_data_retained": true,
  "library_backup_retained": true,
  "warnings": [],
  "allowed": true
}
```

### Confirmation modal

> **Remove Example Game (PPSA01234)?**
>
> This will remove the ShadowMountPlus source folder:
>
> `/mnt/usb0/homebrew/PPSA01234`
>
> It will also remove the associated home-screen registration.
>
> Estimated space reclaimed: 74.3 GB.
>
> Save data and the PC-side library backup will be retained.
>
> Type the title ID to confirm: `PPSA01234`

## 14. API design

Use a versioned REST API under `/api/v1`.

### Authentication

```http
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
```

### Library

```http
GET    /api/v1/games
GET    /api/v1/games/{game_id}
PATCH  /api/v1/games/{game_id}
DELETE /api/v1/games/{game_id}
POST   /api/v1/games/{game_id}/verify
POST   /api/v1/library/scan
GET    /api/v1/library/scan-status
GET    /api/v1/library/roots
POST   /api/v1/library/roots
PATCH  /api/v1/library/roots/{root_id}
DELETE /api/v1/library/roots/{root_id}
```

### Consoles

```http
GET    /api/v1/consoles
POST   /api/v1/consoles
GET    /api/v1/consoles/{console_id}
PATCH  /api/v1/consoles/{console_id}
DELETE /api/v1/consoles/{console_id}
POST   /api/v1/consoles/{console_id}/test-connection
GET    /api/v1/consoles/{console_id}/status
GET    /api/v1/consoles/{console_id}/drives
GET    /api/v1/consoles/{console_id}/installed
POST   /api/v1/consoles/{console_id}/rescan
```

### Installation

```http
POST /api/v1/consoles/{console_id}/install
GET  /api/v1/consoles/{console_id}/jobs
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
POST /api/v1/jobs/{job_id}/resume
POST /api/v1/jobs/{job_id}/clean-partial
```

### Removal

```http
POST /api/v1/consoles/{console_id}/uninstall/preview
POST /api/v1/consoles/{console_id}/uninstall
GET  /api/v1/removal-jobs/{removal_job_id}
POST /api/v1/removal-jobs/{removal_job_id}/cancel
```

### Maintenance

```http
GET  /api/v1/maintenance/orphans
POST /api/v1/maintenance/clean-staging
GET  /api/v1/maintenance/audit-logs
GET  /api/v1/maintenance/system-health
```

## 15. Frontend pages

### Dashboard

- Console status.
- Library size.
- Installed-game count.
- Active jobs.
- Recent warnings.
- Storage overview.
- Quick actions: scan library, refresh console, clean staging.

### Storefront

- Cover-art grid.
- Search.
- Filters:
  - Installed
  - Not installed
  - Ready
  - Needs attention
  - Incompatible
  - Favorites
  - Format
  - Region
  - Drive
- Sort:
  - Title
  - Recently added
  - Size
  - Version
  - Install status

### Game detail

- Cover art.
- Metadata.
- Compatibility panel.
- Source path.
- Size.
- Install history.
- Installed instances.
- Edit metadata.
- Install button.
- Uninstall button when installed.

### Installed

- Console selector.
- Drive cards.
- Installed-game grid.
- Filters and search.
- Refresh inventory.
- Maintenance tools.

### Jobs

- Active jobs.
- Historical jobs.
- Progress bars.
- Error details.
- Cancel/resume controls.
- Clean partial files.

### Settings

- Library roots.
- Exclusions.
- Console profiles.
- Credentials.
- Staging path.
- Compatibility matrix.
- Security settings.
- Audit logs.
- Docker/deployment information.

## 16. Security requirements

### Network

- Bind web services to `127.0.0.1` by default.
- If LAN exposure is enabled, require authentication.
- Do not expose the app through Cloudflare Tunnel or any public proxy in v1.
- Use HTTPS only if the user explicitly configures a reverse proxy.
- Restrict CORS to the app’s own frontend origin.
- Add rate limiting to authentication and destructive endpoints.

### Credentials

- Encrypt PS5 credentials at rest using a key derived from `SECRET_KEY` or a separate `CREDENTIALS_ENCRYPTION_KEY`.
- Never return plaintext passwords in API responses.
- Mask credentials in logs.
- Support environment-variable or Docker-secret-based credential injection.

### Filesystem safety

- Library roots are read-only.
- Staging is write-enabled and isolated.
- Deletion is restricted to explicit allowlisted console paths.
- Validate and normalize all paths.
- Reject path traversal.
- Reject symlink escapes.
- Use exact resolved paths for deletion.
- Never execute shell commands with unsanitized user input.

### Auditability

Log every:

- Login attempt.
- Library scan.
- Console connection.
- Install request.
- Install result.
- Uninstall preview.
- Uninstall request.
- Deletion path.
- Credential change.
- Compatibility override.

## 17. Error handling

Use typed errors and clear user-facing messages.

Examples:

```json
{
  "error": "incompatible_format",
  "message": "PS5 FPKG is not supported on detected firmware 13.60.",
  "details": {
    "format": "ps5_pkg",
    "detected_firmware": "13.60",
    "maximum_supported_firmware": "11.60",
    "recommended_formats": [
      "folder_dump",
      "image_exfat",
      "image_ffpkg",
      "image_ffpfs",
      "image_ffpfsc"
    ]
  }
}
```

Other required error types:

- `console_offline`
- `console_helper_unavailable`
- `insufficient_space`
- `source_missing`
- `source_incomplete`
- `duplicate_title_id`
- `base_game_missing`
- `path_outside_allowlist`
- `deletion_blocked_game_running`
- `archive_extraction_failed`
- `unsupported_connection_type`
- `authentication_required`

## 18. Testing requirements

### Backend tests

Cover:

- Scanner format detection.
- Sidecar parsing.
- Duplicate detection.
- Compatibility matrix evaluation.
- Preflight validation.
- Install-job state transitions.
- Cancellation.
- Connection failure handling.
- Drive-space calculations.
- Path allowlist enforcement.
- Path traversal rejection.
- Symlink escape rejection.
- Package uninstall flow.
- ShadowMountPlus source removal flow.
- Audit logging.

### Frontend tests

Cover:

- Storefront rendering.
- Search and filters.
- Game detail.
- Install modal validation.
- Installed-game view.
- Uninstall preview and confirmation.
- Job progress display.
- Error states.

### Integration tests

Use mocked console adapters for automated tests. Do not require a real PS5 for CI.

Provide an optional manual test checklist for the user’s real console.

## 19. Implementation plan

### Phase 1: Foundation

1. Initialize repository.
2. Create Docker Compose skeleton.
3. Set up FastAPI backend.
4. Set up React frontend.
5. Configure PostgreSQL and Redis.
6. Implement authentication.
7. Implement configuration loading.
8. Create database models and migrations.
9. Implement health endpoint.
10. Create base UI layout and navigation.

### Phase 2: Library scanner

1. Implement library-root CRUD.
2. Implement recursive scanner.
3. Detect folder dumps, images, packages, and archives.
4. Parse optional sidecars.
5. Calculate fingerprints and sizes.
6. Detect partial files.
7. Detect duplicates.
8. Implement scheduled and manual scans.
9. Build storefront library UI.
10. Add metadata editing and cover upload.

### Phase 3: Console integration

1. Implement console profile CRUD.
2. Implement credential encryption.
3. Implement adapter interface.
4. Implement FTP adapter skeleton.
5. Implement connection health monitor.
6. Implement console info retrieval.
7. Implement drive enumeration.
8. Implement installed-game scanning.
9. Build Installed page.
10. Build drive dashboard.

### Phase 4: Installation

1. Implement job queue.
2. Implement preflight checks.
3. Implement transfer progress tracking.
4. Implement package-install adapter route.
5. Implement ShadowMountPlus staging route.
6. Implement archive extraction.
7. Implement cancellation and interruption states.
8. Build install modal.
9. Build job dashboard.
10. Add post-install inventory reconciliation.

### Phase 5: Safe removal

1. Implement uninstall preview.
2. Implement package uninstall route.
3. Implement ShadowMountPlus source-removal route.
4. Implement home-screen icon cleanup.
5. Enforce deletion allowlists.
6. Add typed confirmation.
7. Add audit logging.
8. Build uninstall UI.
9. Add orphan detection.
10. Add staging cleanup.

### Phase 6: Hardening

1. Add comprehensive tests.
2. Add structured logging.
3. Add audit-log UI.
4. Add rate limiting.
5. Add backup/restore documentation.
6. Add Docker deployment documentation.
7. Add troubleshooting documentation.
8. Perform security review.
9. Test on a real PS5 only after mocked tests pass.
10. Tag v1.0.

## 20. Initial implementation instructions for Claude

Start with Phases 1 and 2 only.

Do not implement real PS5 network transfer or deletion until:

1. The database schema is complete.
2. The scanner works against sample directories.
3. The web UI can browse and edit the library.
4. Compatibility states render correctly.
5. Unit tests pass.
6. A mock console adapter can simulate drives, installed games, installation, and removal.

When implementing the scanner:

- Make format detection pluggable.
- Never trust filenames alone for final classification.
- Store raw detection evidence in the database.
- Mark uncertain items as `unknown`, not as a guessed format.
- Ensure scanning works against mounted SMB/NFS paths.
- Ensure scanner failures do not erase existing library records.

When implementing console integration:

- Build the adapter interface first.
- Create a `MockConsoleAdapter` with deterministic test data.
- Add real FTP/HTTP adapters only after the mock workflow is complete.
- Keep all console-specific paths and endpoints in adapter configuration.
- Never place console-specific logic in frontend components.

When implementing deletion:

- Implement dry-run preview first.
- Enforce allowlists in the backend.
- Require exact resolved-path matching.
- Require typed title-ID confirmation.
- Log every deletion attempt.
- Default to retaining save data and PC-side backups.

## 21. Definition of done for v1

The project is complete when the user can:

1. Start the app (the Windows `.exe`; Docker Compose was dropped, see README "Deviations").
2. Log in locally.
3. Add one or more library roots.
4. Scan a library containing folder dumps, image files, package files, and archives.
5. See compatible, incompatible, and unknown titles clearly distinguished.
6. View installed games from a connected PS5.
7. See console firmware, drives, free space, and installed paths.
8. Select a compatible game and queue a remote installation.
9. Watch installation progress and cancel or recover from interruptions.
10. Preview an uninstall and see exactly what will be removed.
11. Confirm removal by typing the title ID.
12. Remove only the approved console-side content.
13. Retain PC-side backups and save data by default.
14. Review an audit log of installs and removals.
15. Move the entire application to another PC by copying its data folder (README "Backup and restore").