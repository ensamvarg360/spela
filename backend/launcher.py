"""Entry point for the Windows .exe: serve the app in the background and show it in its own window
(WebView2, built into Windows), with a tray icon. Closing the window hides it; Quit is in the tray menu."""
import base64
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import pystray
import uvicorn
import webview
from PIL import Image

from app import config, jobs
from app.main import APP_ID, app

try:
    import pyi_splash  # only exists in the built .exe (build.ps1 --splash)
except ImportError:
    pyi_splash = None

URL = f"http://127.0.0.1:{config.PORT}"
APP_NAME = "Spela"
logging.basicConfig(filename=config.DATA_DIR / "app.log", level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _icon_image() -> Image.Image:
    return Image.open(Path(config.__file__).parent / "assets" / "tray.png")


def _loading_page() -> str:
    """Shown in the window until the server is up; the bar sweeps for as long as that takes."""
    logo = base64.b64encode((Path(config.__file__).parent / "assets" / "wordmark.png").read_bytes()).decode()
    return f"""<body style="margin:0;height:100vh;display:grid;place-content:center;justify-items:center;gap:22px;
background:linear-gradient(135deg,#181c34,#261e48)"><img src="data:image/png;base64,{logo}" width="220">
<div style="width:140px;height:3px;border-radius:2px;background:#ffffff16;overflow:hidden">
<div style="width:40%;height:100%;border-radius:2px;background:#6e6eff;animation:s 1.2s ease-in-out infinite"></div></div>
<style>@keyframes s{{from{{transform:translateX(-100%)}}to{{transform:translateX(250%)}}}}</style></body>"""


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


def _allow_through_firewall() -> bool:
    """Adds an inbound rule for the port on private networks. Asks for admin rights; False if refused."""
    rule = f'name="{APP_NAME}"'
    args = (f"/c netsh advfirewall firewall delete rule {rule} >nul & netsh advfirewall firewall add rule {rule} "
            f"dir=in action=allow protocol=TCP localport={config.PORT} profile=private")
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", args, None, 0) > 32


def _message(text: str, flags: int = 0x40):  # MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


def _show_running_window() -> bool:
    """Brings the window of the copy that's already running to the front, even when it's hidden in the tray."""
    hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return False
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE: shows it, un-minimised
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def main():
    owner = _port_owner()
    if pyi_splash and owner != "free":  # no window of our own coming, so don't leave the splash over the message box
        pyi_splash.close()
    if owner == "us":  # already running: just show it
        _show_running_window() or webbrowser.open(URL)
        return
    if owner == "other":
        ctypes.windll.user32.MessageBoxW(None, f"Port {config.PORT} is already in use by another program, so PS5 Local "
                                         "Storefront can't start.\n\nClose that program and try again.",
                                         "Spela", 0x10)  # MB_ICONERROR
        return
    # log_config=None: a windowed .exe has no console, so uvicorn must log through the file handler above.
    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=config.PORT, log_config=None))

    def serve():
        try:
            server.run()
        except SystemExit:  # uvicorn exits this way when it can't bind the port
            pass
        if not server.should_exit:  # stopped on its own, e.g. the port is taken by another program
            icon.notify(f"Couldn't start. Details are in {config.DATA_DIR / 'app.log'}", "Spela")

    thread = threading.Thread(target=serve)
    window = webview.create_window(APP_NAME, html=_loading_page(), width=1280, height=860, min_size=(720, 480))
    quitting = False

    def on_closing():
        if quitting:
            return True
        window.hide()  # keep running in the tray
        return False  # cancels the close

    def open_window():
        window.show()
        window.restore()

    def stop():
        nonlocal quitting
        quitting = True
        server.should_exit = True
        thread.join()

    def quit_(icon):
        stop()
        icon.stop()
        window.destroy()  # ends webview.start() below, and with it the program

    def restart(icon):
        stop()  # free the port before the new instance checks it
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, __file__]
        # A one-file .exe must not reuse this instance's unpacked files, which are deleted when it exits.
        subprocess.Popen(cmd, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
        icon.stop()
        window.destroy()

    pending: str | None = None  # "quit" or "restart" while waiting for running work to finish

    def when_done(verb: str, action):
        """Runs action now when nothing is running. Otherwise asks: when it's done, now, or not at all."""
        nonlocal pending
        busy = jobs.in_progress()
        if not busy:
            return action()
        shown = "\n".join(f"•  {b}" for b in busy[:8]) + (f"\n…and {len(busy) - 8} more" if len(busy) > 8 else "")
        choice = ctypes.windll.user32.MessageBoxW(
            None, f"Spela is still working:\n\n{shown}\n\n"
                  f"Yes: {verb} when all of it is done.\n"
                  f"No: {verb} now. Running work stops; an install has to be queued again.\n"
                  "Cancel: keep Spela running.",
            APP_NAME, 0x3 | 0x30 | 0x10000 | 0x40000)  # MB_YESNOCANCEL, MB_ICONWARNING, MB_SETFOREGROUND, MB_TOPMOST
        if choice == 7:  # IDNO
            pending = None
            return action()
        if choice != 6:  # IDCANCEL
            return
        already, pending = pending, verb
        icon.update_menu()
        icon.notify(f"Spela will {verb} once the work in progress is done. To stop that, use the tray menu.", APP_NAME)
        if already:  # its thread is still waiting
            return

        def wait():
            nonlocal pending
            while pending and jobs.in_progress():
                time.sleep(2)
            if pending:
                pending = None
                action()

        threading.Thread(target=wait, daemon=True).start()

    def keep_running():
        nonlocal pending
        pending = None
        icon.update_menu()

    def toggle_lan(icon):
        if config.LAN_FILE.exists():
            config.LAN_FILE.unlink()
        else:
            if not _allow_through_firewall():
                return _message("Other devices weren't allowed: Windows needs admin rights to let them through "
                                "the firewall.", 0x30)  # MB_ICONWARNING
            config.LAN_FILE.touch()
        restart(icon)

    lan_url = _lan_url()
    icon = pystray.Icon("ps5sf", _icon_image(), f"{APP_NAME}\n{lan_url or URL}", pystray.Menu(
        pystray.MenuItem("Open", open_window, default=True),
        pystray.MenuItem("Open data folder", lambda: os.startfile(config.DATA_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Allow other devices on my network", lambda: when_done("restart", lambda: toggle_lan(icon)),
                         checked=lambda _: config.LAN_FILE.exists(),
                         enabled="HOST" not in os.environ),  # the variable wins, so the option would do nothing
        pystray.MenuItem(f"On other devices: {lan_url}", lambda: webbrowser.open(lan_url), visible=bool(lan_url)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: f"Don't {pending} after all", keep_running, visible=lambda _: pending is not None),
        pystray.MenuItem("Restart", lambda: when_done("restart", lambda: restart(icon))),
        pystray.MenuItem("Quit", lambda: when_done("quit", lambda: quit_(icon))),
    ))
    jobs.notify = lambda title, message: icon.notify(message, title)  # installs and conversions that end
    thread.start()

    def load_when_ready():  # runs once the window is on screen
        if pyi_splash:
            pyi_splash.close()
        while not server.started and thread.is_alive():
            time.sleep(0.1)
        if server.started:
            window.load_url(URL)

    window.events.closing += on_closing
    icon.run_detached()
    # The window must own the main thread. Logins persist in DATA_DIR/webview, as in a browser profile.
    webview.start(load_when_ready, private_mode=False, storage_path=str(config.DATA_DIR / "webview"))


if __name__ == "__main__":
    main()
