"""A stand-in for the PC app's console API, for build.ps1 -Preview. Invented games in every state.
The real API is console_api in backend/app/main.py; keep the line format in step with it.
Needs Pillow (python3-pil, which build.sh installs for the preview) for the invented posters."""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps

W, H = 256, 320
GAMES = [  # id, title, title ID, version, size, state, progress, has cover, kind, format[, done, total, rate, eta, step]
    ["g01", "Neon Drift™: Tokyo 2099", "PPSA01001", "01.000.000", 41_200_000_000, "installable", "", "1", "game", "folder_dump"],
    ["g02", "Deep Sea 9", "PPSA01002", "01.020.000", 88_900_000_000, "installed", "", "1", "game", "image"],
    ["g03", "Sugar Rush Rally", "PPSA01003", "01.004.000", 23_400_000_000, "installable", "", "1", "game", "image"],
    ["g04", "Frostline: Expedition Zero", "PPSA01004", "02.000.000", 65_000_000_000, "update", "", "1", "game", "folder_dump"],
    ["g05", "Mecha Brawl: Uprising", "PPSA01005", "01.000.000", 7_800_000_000, "installing", "0.420", "1", "game", "image",
     3_276_000_000, 7_800_000_000, 84_600_000, 53, "Copying to USB Extended Storage."],
    ["g06", "Echoes of the Hollow: Director's Extended Edition", "PPSA01006", "01.100.000", 102_000_000_000, "installable", "", "1",
     "game", "folder_dump"],
    ["g07", "Tidewater", "CUSA01007", "01.05", 12_100_000_000,
     "blocked:This console has no package installer. Only folder dumps and disc images can be installed.", "", "0", "game", "ps4_pkg"],
    ["g08", "Chrono Heist", "PPSA01008", "01.000.000", 30_500_000_000, "queued", "", "1", "game", "image"],
    ["g09", "Tiny Kingdoms® Café", "PPSA01009", "01.002.000", 19_900_000_000, "installable", "", "1", "game", "image"],
    ["g10", "Wasteland Diner", "PPSA01010", "01.000.000", 54_300_000_000, "installable", "", "1", "game", "image"],
    ["g11", "Neon Samurai: Ghost Protocol", "PPSA01011", "01.000.000", 8_200_000_000, "installable", "", "1", "game", "image"],
    ["g12", "Void Runners", "PPSA01012", "01.010.000", 71_700_000_000, "installable", "", "1", "game", "image"],
    ["d01", "Deep Sea 9: Abyss Season Pass", "PPSA01002", "", 2_100_000_000, "installable", "", "1", "dlc", "unlocker"],
    ["d02", "Frostline: Whiteout Pack", "PPSA01004", "01.000.000", 900_000_000, "installable", "", "0", "dlc", "ps5_pkg"],
]
FAVORITES = {"g06", "g10"}  # by id; the PC marks every copy of a game
GENRES = {"g01": "Racing|Action", "g02": "Horror|Adventure", "g03": "Racing|Family", "g04": "Adventure|Action",
          "g05": "Fighting|Action", "g06": "Adventure|Role Playing Games", "g08": "Action|Adventure", "g09": "Strategy|Family",
          "g10": "Simulation|Family", "g11": "Action", "g12": "Shooter|Action", "d01": "Horror|Adventure"}


LOGOS = {"g01", "g02", "g04", "g06", "g08", "g12"}  # games whose store logo the PC has; the rest show their title


def line(i: int, g: list) -> str:
    """The columns above, padded past the install details, then favorite, when it was added (a day apart, g01 first),
    genres (g07 has none: not on the store) and has logo."""
    cols = g + [""] * (15 - len(g)) + ["1" if g[0] in FAVORITES else "0", 1_760_000_000 + i * 86_400, GENRES.get(g[0], ""),
                                       "1" if g[0] in LOGOS else "0"]
    return "\t".join(map(str, cols)) + "\n"


LW, LH = 384, 112  # LOGO_SIZE in backend/app/main.py


def logo(title: str) -> bytes:
    """An invented logo: the title in white capitals on transparency, then set in its box the way the PC app does
    (console_logo in backend/app/main.py): trimmed, scaled to fit, bottom-left."""
    import textwrap
    from PIL import ImageDraw, ImageFont
    text = textwrap.fill(title.split(":")[0].upper(), 12)  # stacked, as store logos usually are
    im = Image.new("RGBA", (1800, 800), (0, 0, 0, 0))
    ImageDraw.Draw(im).multiline_text((20, 20), text, font=ImageFont.load_default(size=200), fill=(255, 255, 255, 255),
                                      align="left", spacing=10)
    im = ImageOps.contain(im.crop(im.getchannel("A").getbbox()), (LW, LH), Image.LANCZOS)
    canvas = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    canvas.paste(im, (0, LH - im.height))
    return canvas.tobytes()


DRIVES = [  # id, label, free, total
    ["dr1", "Internal", 61_000_000_000, 825_000_000_000],
    ["dr2", "USB Extended Storage", 1_420_000_000_000, 2_000_000_000_000],
]


# The posters docs/screenshots/make_screenshots.py uses too; games without one get a generated gradient.
POSTERS = {"g01": "neon-drift", "g02": "deep-sea-9", "g03": "sugar-rush-rally", "g04": "frostline", "g05": "mecha-brawl",
           "g06": "echoes-of-the-hollow", "g08": "chrono-heist", "g09": "tiny-kingdoms", "g10": "wasteland-diner",
           "g11": "neon-samurai", "g12": "void-runners", "d01": "deep-sea-9"}
POSTER_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots" / "demo-covers"


def poster(name: str) -> bytes:
    """Cut to the console app's cover size the way the PC app does (ImageOps.fit, backend/app/main.py)."""
    with Image.open(POSTER_DIR / f"{name}.jpg") as im:
        return ImageOps.fit(im.convert("RGBA"), (W, H)).tobytes()


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
            text = "".join(line(i, g) for i, g in enumerate(GAMES))
            return self._send(200, text.encode())
        if self.path == "/api/v1/console-app/drives":
            return self._send(200, "".join("\t".join(map(str, d)) + "\n" for d in DRIVES).encode())
        if self.path.startswith("/api/v1/console-app/covers/"):
            gid = self.path.rsplit("/", 1)[1]
            idx = next((i for i, g in enumerate(GAMES) if g[0] == gid), None)
            if idx is not None and GAMES[idx][7] == "1":
                data = poster(POSTERS[gid]) if gid in POSTERS else cover(idx + 3)
                return self._send(200, data, "application/octet-stream")
        if self.path.startswith("/api/v1/console-app/logos/"):
            game = next((g for g in GAMES if g[0] == self.path.rsplit("/", 1)[1]), None)
            if game and game[0] in LOGOS:
                return self._send(200, logo(game[1]), "application/octet-stream")
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
        path = self.path.split("?")[0]  # install/<id>?drive=<id>, install/<id>/cancel or favorite/<id>?on=true
        if parts[4] == "favorite":
            on = self.path.endswith("on=true")
            (FAVORITES.add if on else FAVORITES.discard)(path.rsplit("/", 1)[1])
            return self._send(200, b"Added to Favorites." if on else b"Taken out of Favorites.")
        if path.endswith("/cancel"):
            game = next((g for g in GAMES if g[0] == path.split("/")[-2]), None)
            if not game or game[5] not in ("queued", "installing"):
                return self._send(409, b"It isn't installing.")
            game[5], game[6] = "installable", ""
            return self._send(200, b"Stopping.")
        gid = path.rsplit("/", 1)[1]
        game = next((g for g in GAMES if g[0] == gid), None)
        if not game or game[5] != "installable":
            return self._send(409, b"Already installed on this console.")
        game[5] = "queued"
        self._send(200, b"Queued.")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
