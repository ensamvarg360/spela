# Contributing

Thanks for looking. Spela is a spare-time project with a narrow scope (see [What it doesn't do](README.md#what-it-doesnt-do)), so for anything bigger than a fix, please open an issue first.

**Out of scope, whatever the pull request:** anything that finds, downloads or unlocks games for people who don't own them.

## Set up

```sh
cd backend && python -m venv .venv && .venv/Scripts/pip install -e ".[test]"   # bin/ on Linux/macOS
.venv/Scripts/pytest

cd frontend && npm install && npm run dev      # proxies /api to :8000
DATA_DIR=../data backend/.venv/Scripts/uvicorn app.main:app --reload   # from backend/
```

The tests mock the console. A **`mock` console** (Settings → Consoles → connection `mock`) behaves like a PS5 for UI work, so you don't need hardware for most changes. Anything that touches the helper, the PS5 app or real installs needs the [manual test on a real console](docs/manual-test.md).

The helper payload ([payload/](payload/)) and the PS5 app ([console-app/](console-app/)) are built in Docker and their results are committed, so a normal checkout needs neither Docker nor the PS5 SDK. After changing `payload/main.c` or `console-app/`, run `payload/build.ps1` or `console-app/build.ps1` and commit the rebuilt output.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:` …), each ending with a `Changelog:` trailer written for the people who use the app, not for developers:

```
fix: retry token refresh after sleep

Changelog: Fixed: Pairing no longer fails after the computer wakes from sleep.
```

- The section is one of `Added`, `Changed`, `Fixed`, `Removed`, `Security`. One trailer per user-visible change; several are fine.
- Use `Changelog: skip` when users won't notice (refactors, tests, tooling).
- Use `Changelog: Beta: <text>` for a fix to something that has only been in a beta (see [Betas](#betas)). It's listed under *Fixed in this beta* in the next beta's notes and left out of the stable release's, since its users never had the problem.
- Release notes are collected from these trailers, so please don't leave one out.

## Pull requests

- Keep them small and say what you ran. `pytest` and `npm run build` must pass; CI runs both.
- A new behavior gets a test, unless it can only be checked on a real console. Then say how you checked it.
- Anything that lets the helper write somewhere new, or widens what the app trusts, needs a sentence on why it's safe. See [SECURITY.md](SECURITY.md).
- The user-facing docs are [README.md](README.md) and [docs/user-guide.md](docs/user-guide.md). Update the guide when behavior changes.

## Releasing

For the maintainer. Needs Windows, PowerShell 7.3+, Python 3.12, Node 22, [GitHub CLI](https://cli.github.com/) (`gh auth login`), and Docker Desktop when the version is raised (it rebuilds the helper).

1. Run the [manual test on a real PS5](docs/manual-test.md).
2. Make sure every commit since the last tag has a `Changelog:` trailer, and the working tree is clean.
3. Build everything and look at the notes first:

   ```powershell
   ./release.ps1 -Bump patch          # or minor / major
   ```

   This raises the version, rebuilds the helper, builds the `.zip`, runs the tests and writes `dist\release\v<version>\`:

   | File | What it is |
   |---|---|
   | `Spela-<version>.zip` | The Windows app: a Spela folder with `Spela.exe`. It bundles the helper and the PS5 app. |
   | `SHA256SUMS.txt` | Its checksum |
   | `RELEASE_NOTES.md` | Built from the `Changelog:` trailers; not uploaded as a file |

   The helper (`.elf`) and PS5 app aren't separate downloads: both only work once Spela has paired them with a console, so the app installs them itself.

4. Happy with it? Publish. This commits the version bump, tags `v<version>`, pushes both, and creates the GitHub release with the two files and the notes:

   ```powershell
   ./release.ps1 -Publish            # add -Draft to review the release on GitHub before it goes live
   ```

   Or do steps 3 and 4 in one go with `./release.ps1 -Bump patch -Publish`. Publishing refuses to run with other uncommitted changes, with an existing tag, or when the branch is behind `origin`.

5. GitHub then posts the release in Discord as *Spela Releases* ([release-announce.yml](.github/workflows/release-announce.yml)): the version, its notes and the download link, once the release is public and both files are on it. Drafts are announced when you publish them; prereleases and tags other than `vX.Y.Z` never are. Mentions are turned off, so notes can't ping anyone. A marker in the release notes (`<!-- spela:discord-announced -->`, hidden on GitHub) stops a rerun from posting twice. If the announcement fails, the release stays as it is and the workflow run shows why in red: fix it and click **Re-run jobs**. To see the message without sending it: `bash .github/scripts/announce-release.sh v<version> --dry-run` (needs `gh` and `jq`).

   One-time setup: in Discord, **Server Settings → Integrations → Webhooks → New Webhook**, pick the announcements channel, and copy its URL. On GitHub, **Settings → Secrets and variables → Actions → New repository secret**, named `DISCORD_WEBHOOK_URL`, with that URL as its value. Never put the URL in a file.

### Betas

Bigger changes go out as a beta first, from `master`, so testers can try them while everyone else stays on the stable version:

```powershell
./release.ps1 -Bump patch -Beta -Publish   # 0.7.6 -> 0.7.7-beta.1 (or -Bump minor -Beta for 0.8.0-beta.1)
./release.ps1 -Bump beta -Publish          # a fix for testers: 0.7.7-beta.2
./release.ps1 -Bump stable -Publish        # the final 0.7.7
```

A beta is published as a GitHub prerelease. That keeps it out of the in-app update check for people on a stable version (it asks GitHub for the latest release, which skips prereleases), out of Discord, and off Docker's `:latest` (it only gets its own `:0.7.7-beta.1` tag). Testers download it from the release page. Once on a beta, they're offered the final version when it's out: a beta counts as older than its own release (`release_order` in [backend/app/main.py](backend/app/main.py)). Every beta needs its own number, even for a one-line fix, because consoles only get a new helper and PS5 app when the version changes. Release notes for a beta cover what changed since the beta before it; the final release's cover everything since the last stable one.

**Fixing the stable version while a beta is out:** branch from the last stable tag (`git switch -c release/0.7 v0.7.6`), fix it there, and release from that branch. That makes `0.7.7`, so on `master` the beta line has to move up: merge the fix into `master`, set `__version__` in [backend/app/__init__.py](backend/app/__init__.py) to `0.7.7`, commit, and start the next beta with `-Bump patch -Beta` (`0.7.8-beta.1`).
