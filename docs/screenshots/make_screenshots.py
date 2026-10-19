"""Regenerates the README screenshots from an invented library (fake titles, generated covers, a mock console).

    python docs/screenshots/make_screenshots.py

Needs, in whatever Python runs it: `pip install playwright pillow httpx` (uses the installed Microsoft Edge, no
browser download), and the backend's dependencies (run it from backend/.venv). Build the frontend first
(`npm run build` in frontend/). Nothing touches your real data folder: it runs its own copy of the app on port 8799.
The PS5 screenshots (ps5-*.png) are renders from `console-app/build.ps1 -Preview`, resized to 1280x720.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "demo-password-1"

GAMES = [  # title, title ID, version
    ("Harbor Lights", "PPSA01234", "01.000.000"),
    ("Iron Orchard", "PPSA01002", "01.020.000"),
    ("Night Courier", "PPSA05678", "01.004.000"),
    ("Pale Summit", "PPSA01004", "02.000.000"),
    ("Small Hours", "PPSA01005", "01.000.000"),
    ("The Long Signal", "PPSA01006", "01.100.000"),
    ("Undertow", "PPSA01008", "01.000.000"),
    ("Velvet Circuit", "PPSA01009", "01.002.000"),
    ("Winter Ledger", "PPSA01010", "01.000.000"),
    ("Yellow Line", "PPSA01011", "01.000.000"),
    ("Zero Meridian", "PPSA01012", "01.010.000"),
    ("Copper Tide", "PPSA01013", "01.000.000"),
]


def font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def cover(seed: int, title: str) -> bytes:
    a = ((seed * 67) % 170 + 30, (seed * 131) % 170 + 30, (seed * 29) % 170 + 60)
    b = (a[2] // 3, a[0] // 3, a[1] // 3)
    im = Image.new("RGB", (600, 800))
    d = ImageDraw.Draw(im)
    for y in range(800):
        t = y / 800
        d.line([(0, y), (600, y)], fill=tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3)))
    d.ellipse((180, 140, 420, 380), outline=(255, 255, 255), width=6)
    words, lines, line = title.split(), [], ""
    for w in words:
        if d.textlength(f"{line} {w}".strip(), font=font(64)) > 520 and line:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    lines.append(line)
    for i, text in enumerate(lines):
        d.text((40, 520 + i * 76), text, font=font(64), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def seed(library: Path):
    for i, (title, tid, version) in enumerate(GAMES):
        sys_dir = library / title / "sce_sys"
        sys_dir.mkdir(parents=True)
        (sys_dir / "param.json").write_text(json.dumps({
            "titleId": tid, "contentId": f"EP0001-{tid}_00-DEMO000000000000", "contentVersion": version,
            "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": title}},
        }))
        (library / title / "eboot.bin").write_bytes(b"\0" * (1 + i) * 4096)


def main():
    work = Path(tempfile.mkdtemp(prefix="spela-shots-"))
    library = work / "Library"
    seed(library)
    env = {**os.environ, "DATA_DIR": str(work / "data"), "PORT": str(PORT), "HOST": "127.0.0.1",
           "SCAN_INTERVAL_MINUTES": "0", "STATIC_DIR": str(ROOT / "frontend" / "dist")}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                              cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                httpx.get(f"{BASE}/api/v1/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        c = httpx.Client(base_url=f"{BASE}/api/v1", timeout=30)
        c.post("/auth/setup", json={"password": PASSWORD}).raise_for_status()
        c.post("/library/roots", json={"path": str(library)}).raise_for_status()
        c.post("/library/scan")
        for _ in range(60):
            s = c.get("/library/scan-status").json()
            if not s.get("running"):
                break
            time.sleep(0.5)
        games = c.get("/games").json()
        for i, g in enumerate(games):
            c.post(f"/games/{g['id']}/cover", files={"file": ("c.png", cover(i + 1, g["title"]), "image/png")})
        r = c.post("/consoles", json={"name": "Living Room PS5", "ip_address": "192.168.1.50",
                                      "connection_type": "mock", "port": 9021})
        r.raise_for_status()
        console = r.json()["id"]
        c.post(f"/consoles/{console}/rescan")

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge")
            page = browser.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
            page.goto(BASE)
            page.wait_for_selector("input[type=password]")
            page.fill("input[type=password]", PASSWORD)
            page.keyboard.press("Enter")
            page.wait_for_selector("nav")

            def snap(name, height=900):
                page.set_viewport_size({"width": 1400, "height": height})
                # the demo library lives in a temp folder under your user name; show a tidy path instead
                page.evaluate("""([a, b]) => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    for (let n; (n = w.nextNode());) n.nodeValue = n.nodeValue.replaceAll(a, b); }""", [os.path.realpath(library), "G:/PS5 Backups"])
                page.screenshot(path=str(OUT / f"{name}.png"))
                print("wrote", name)

            def shot(name, route, height=900, wait_ms=1500):
                page.goto(f"{BASE}/#{route}")
                page.wait_for_timeout(wait_ms)
                snap(name, height)

            shot("dashboard", "/dashboard", 700)
            shot("library", "/")
            page.get_by_text("Pale Summit").first.click()
            page.wait_for_timeout(1500)
            snap("game")
            browser.close()
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
