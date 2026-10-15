"""A stand-in for the PC app's console API, for build.ps1 -Preview. Invented games in every state.
The real API is console_api in backend/app/main.py; keep the line format in step with it."""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

W, H = 256, 320
GAMES = [  # id, title, title ID, version, size, state, progress, has cover
    ["g01", "Harbor Lights", "PPSA01001", "01.000.000", 41_200_000_000, "installable", "", "1"],
    ["g02", "Iron Orchard", "PPSA01002", "01.020.000", 88_900_000_000, "installed", "", "1"],
    ["g03", "Night Courier", "PPSA01003", "01.004.000", 23_400_000_000, "installable", "", "1"],
    ["g04", "Pale Summit", "PPSA01004", "02.000.000", 65_000_000_000, "update", "", "1"],
    ["g05", "Small Hours", "PPSA01005", "01.000.000", 7_800_000_000, "installing", "0.420", "1"],
    ["g06", "The Long Signal: Director's Extended Edition", "PPSA01006", "01.100.000", 102_000_000_000, "installable", "", "1"],
    ["g07", "Tidewater", "CUSA01007", "01.05", 12_100_000_000,
     "blocked:This console has no package installer. Only folder dumps and disc images can be installed.", "", "0"],
    ["g08", "Undertow", "PPSA01008", "01.000.000", 30_500_000_000, "queued", "", "1"],
    ["g09", "Velvet Circuit", "PPSA01009", "01.002.000", 19_900_000_000, "installable", "", "0"],
    ["g10", "Winter Ledger", "PPSA01010", "01.000.000", 54_300_000_000, "installable", "", "1"],
    ["g11", "Yellow Line", "PPSA01011", "01.000.000", 8_200_000_000, "installable", "", "1"],
    ["g12", "Zero Meridian", "PPSA01012", "01.010.000", 71_700_000_000, "installable", "", "1"],
]


def cover(seed: int) -> bytes:
    """A two-colour gradient with a band, so covers look different from each other."""
    a = ((seed * 67) % 200 + 30, (seed * 131) % 200 + 30, (seed * 29) % 200 + 40)
    b = (a[2] // 3, a[0] // 3, a[1] // 3)
    rows = []
    for y in range(H):
        t = y / H
        band = 0.35 < t < 0.42
        c = (240, 240, 240) if band else tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))
        rows.append(bytes((*c, 255)) * W)
    return b"".join(rows)


class Handler(BaseHTTPRequestHandler):
    removal_asks = 0
    def _send(self, status: int, body: bytes, kind="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == "Bearer preview":
            return True
        self._send(401, b'{"error":"invalid_token"}')
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/api/v1/console-app/library":
            text = "".join("\t".join(map(str, g)) + "\n" for g in GAMES)
            return self._send(200, text.encode())
        if self.path.startswith("/api/v1/console-app/covers/"):
            gid = self.path.rsplit("/", 1)[1]
            idx = next((i for i, g in enumerate(GAMES) if g[0] == gid), None)
            if idx is not None and GAMES[idx][7] == "1":
                return self._send(200, cover(idx + 3), "application/octet-stream")
        if self.path.startswith("/api/v1/console-app/removals/"):  # still running on the first ask, then done
            game = next((g for g in GAMES if g[0] == self.path.rsplit("/", 1)[1].removeprefix("rm-")), None)
            if game:
                Handler.removal_asks += 1
                if Handler.removal_asks == 1:
                    return self._send(200, b"removing\t0\tDeleting the game's files from the console.\n")
                game[5], game[6] = "installable", ""
                return self._send(200, b"success\t1\tRemoved. Freed 88.9 GB. Its tile is still on the PS5 home screen: "
                                       b"delete it there (Options, then Delete). It won't come back. "
                                       b"(The helper is too old to do it.)\n")
        self._send(404, b"not found")

    def do_POST(self):
        if not self._authorized():
            return
        parts = self.path.split("/")  # "", api, v1, console-app, uninstall, <id>, preview | confirm, <title ID>
        if len(parts) > 6 and parts[4] == "uninstall":
            game = next((g for g in GAMES if g[0] == parts[5].removeprefix("rm-")), None)
            if not game or game[5] not in ("installed", "update"):
                return self._send(409, b"It isn't installed on this console.")
            if parts[6] == "preview":
                line = f"rm-{game[0]}\t/data/homebrew/{game[1]} {game[2]}\t{game[4]}\tIf the game is running on the PS5, close it first.\n"
                return self._send(200, line.encode())
            game[5], game[6] = "removing", "0.400"
            return self._send(200, b"Removing.")
        gid = self.path.rsplit("/", 1)[1]
        game = next((g for g in GAMES if g[0] == gid), None)
        if not game or game[5] != "installable":
            return self._send(409, b"Already installed on this console.")
        game[5] = "queued"
        self._send(200, b"Queued.")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
