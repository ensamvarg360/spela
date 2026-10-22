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
