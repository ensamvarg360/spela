# Security

Spela is a single-user app for a home network. It controls files on a game console, so security reports are welcome.

## Reporting a problem

Please **don't open a public issue** for a vulnerability. Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/ensamvarg360/spela/security/advisories/new).

Include the version (shown in the app's header), what you did and what happened. You'll get an answer as soon as I can give one; this is a spare-time project, so please allow some days.

## What's in scope

- Reaching the app, or the helper on the console, without the right password or key
- A web page being able to control the app or the console through the browser (including DNS rebinding)
- The helper writing, reading or deleting outside what the [user guide](docs/user-guide.md) says it may touch
- Passwords or keys stored or logged in the clear

## What isn't

- Exposing the app to the internet. It's built for your LAN only and says so; don't do it.
- Anyone who already has your Windows account or your data folder (it holds the keys).
- The console jailbreak, the ELF loader or ShadowMountPlus themselves. Report those to their authors.

## Supported versions

Only the latest release gets fixes.
