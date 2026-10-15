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
APP_NAME = "PS5 Local Storefront"
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


def _lan_url() -> str | None:
    """The address other devices use, or None when only this PC may connect."""
    if config.HOST == "127.0.0.1":
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 9))  # UDP connect sends nothing; it just picks the network card with the default route
            return f"http://{s.getsockname()[0]}:{config.PORT}"
        except OSError:
            return None


def _network_is_public() -> bool:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-NetConnectionProfile).NetworkCategory"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return "Public" in out


def _allow_through_firewall() -> bool:
    """Adds an inbound rule for the port on private networks. Asks for admin rights; False if refused."""
    rule = f'name="{APP_NAME}"'
    args = (f"/c netsh advfirewall firewall delete rule {rule} >nul & netsh advfirewall firewall add rule {rule} "
            f"dir=in action=allow protocol=TCP localport={config.PORT} profile=private")
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", args, None, 0) > 32


def _message(text: str, flags: int = 0x40):  # MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


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

    def toggle_lan(icon):
        if config.LAN_FILE.exists():
            config.LAN_FILE.unlink()
        else:
            if not _allow_through_firewall():
                return _message("Other devices weren't allowed: Windows needs admin rights to let them through "
                                "the firewall.", 0x30)  # MB_ICONWARNING
            config.LAN_FILE.touch()
            if _network_is_public():
                _message("Windows treats this network as Public and blocks other devices on it.\n\nIf this is your "
                         "home network, open Settings → Network & internet → your connection, and choose Private "
                         "network.", 0x30)
        restart(icon)

    lan_url = _lan_url()
    icon = pystray.Icon("ps5sf", _icon_image(), f"{APP_NAME}\n{lan_url or URL}", pystray.Menu(
        pystray.MenuItem("Open", lambda: webbrowser.open(URL), default=True),
        pystray.MenuItem("Open data folder", lambda: os.startfile(config.DATA_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Allow other devices on my network", toggle_lan, checked=lambda _: config.LAN_FILE.exists(),
                         enabled="HOST" not in os.environ),  # the variable wins, so the option would do nothing
        pystray.MenuItem(f"On other devices: {lan_url}", lambda: webbrowser.open(lan_url), visible=bool(lan_url)),
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
