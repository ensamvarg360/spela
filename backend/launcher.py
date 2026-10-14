"""Entry point for the Windows .exe: serve the app in the background, control it from a tray icon."""
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser

import pystray
import uvicorn
from PIL import Image, ImageDraw

from app import config
from app.main import APP_ID, app

URL = f"http://127.0.0.1:{config.PORT}"
logging.basicConfig(filename=config.DATA_DIR / "app.log", level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill="#0070d1")
    d.polygon([(24, 16), (24, 48), (50, 32)], fill="white")
    return img


def _port_owner() -> str:
    """'free', 'us' (this app is already running) or 'other' (another program holds the port)."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", config.PORT)) != 0:
            return "free"
    try:
        with urllib.request.urlopen(URL + "/api/v1/health", timeout=3) as r:
            return "us" if json.load(r).get("app") == APP_ID else "other"
    except (OSError, ValueError):
        return "other"


def main():
    owner = _port_owner()
    if owner == "us":  # already running: just show it
        webbrowser.open(URL)
        return
    if owner == "other":
        ctypes.windll.user32.MessageBoxW(None, f"Port {config.PORT} is already in use by another program, so PS5 Local "
                                         "Storefront can't start.\n\nClose that program and try again.",
                                         "PS5 Local Storefront", 0x10)  # MB_ICONERROR
        return
    # log_config=None: a windowed .exe has no console, so uvicorn must log through the file handler above.
    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=config.PORT, log_config=None))

    def serve():
        try:
            server.run()
        except SystemExit:  # uvicorn exits this way when it can't bind the port
            pass
        if not server.should_exit:  # stopped on its own, e.g. the port is taken by another program
            icon.notify(f"Couldn't start. Details are in {config.DATA_DIR / 'app.log'}", "PS5 Local Storefront")

    thread = threading.Thread(target=serve)

    def stop():
        server.should_exit = True
        thread.join()

    def quit_(icon):
        stop()
        icon.stop()

    def restart(icon):
        stop()  # free the port before the new instance checks it
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, __file__]
        # A one-file .exe must not reuse this instance's unpacked files, which are deleted when it exits.
        subprocess.Popen([*cmd, "--no-browser"], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
        icon.stop()

    icon = pystray.Icon("ps5sf", _icon_image(), f"PS5 Local Storefront\n{URL}", pystray.Menu(
        pystray.MenuItem("Open", lambda: webbrowser.open(URL), default=True),
        pystray.MenuItem("Open data folder", lambda: os.startfile(config.DATA_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart", restart),
        pystray.MenuItem("Quit", quit_),
    ))
    thread.start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, webbrowser.open, [URL]).start()
    icon.run()


if __name__ == "__main__":
    main()
