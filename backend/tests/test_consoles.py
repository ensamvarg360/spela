import asyncio

import pytest

from app.consoles import GiB, AdapterError, not_answering, parse_ddp, MockConsoleAdapter, UnsupportedConsoleConnectionError, get_adapter
from app.models import Console


def run(coro):
    return asyncio.run(coro)


def test_factory():
    assert isinstance(get_adapter(Console(connection_type="mock")), MockConsoleAdapter)
    with pytest.raises(UnsupportedConsoleConnectionError):
        get_adapter(Console(connection_type="http"))


def test_mock_install_and_remove_roundtrip():
    a, progress = MockConsoleAdapter(), []
    drives = run(a.list_drives())
    usb = next(d for d in drives if d.external_id == "usb0")

    r = run(a.stage_shadowmount_content(r"G:\games\PPSA09999.ffpkg", usb, lambda d, t: progress.append(d)))
    assert r.ok and r.installed.install_route == "shadowmountplus" and progress[-1] == r.installed.size_bytes
    assert run(a.check_space("usb0", 0)).free_bytes == usb.free_bytes - r.installed.size_bytes
    assert "PPSA09999" in {g.title_id for g in run(a.scan_installed_games())}

    # wrong route is refused; right route frees the space, icon stays until removed explicitly
    assert not run(a.uninstall_package("PPSA09999")).ok
    gone = run(a.remove_shadowmount_source(r.installed, lambda d, t: None))
    assert gone.ok and gone.bytes_freed == r.installed.size_bytes
    assert run(a.check_space("usb0", 0)).free_bytes == usb.free_bytes
    assert run(a.remove_home_screen_icon("PPSA09999")).ok
    assert not run(a.remove_home_screen_icon("PPSA09999")).ok

    assert run(a.install_package("PPSA01234-v2.pkg", lambda d, t: None)).ok  # reinstall replaces, no duplicate
    assert [g.title_id for g in run(a.scan_installed_games())].count("PPSA01234") == 1
    assert run(a.uninstall_package("PPSA01234")).ok


def test_mock_refusals():
    a = MockConsoleAdapter()
    a.drives[0].free_bytes = 1 * GiB
    assert not run(a.install_package("PPSA01111.pkg", lambda d, t: None)).ok
    with pytest.raises(AdapterError, match="title ID"):
        run(a.install_package("game.pkg", lambda d, t: None))

    a.online = False
    assert run(a.connect()).state == "offline"
    with pytest.raises(AdapterError) as e:
        run(a.list_drives())
    assert e.value.code == "console_offline"


def test_not_answering_warns_about_another_network(monkeypatch):
    """The fix for an offline console flags an address that isn't on this PC's network."""
    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def connect(self, addr): pass
        def getsockname(self): return ("192.168.0.40", 5000)
    monkeypatch.setattr("app.consoles.socket.socket", Sock)
    assert "Connection Status" in not_answering("192.168.0.50") and "isn't" not in not_answering("192.168.0.50")
    assert "This PC is on 192.168.0.x, and 192.168.1.50 isn't" in not_answering("192.168.1.50")


def test_parse_ddp():
    """Discovery answers become found PS5s; a PS4 or anything else is skipped."""
    awake = b"HTTP/1.1 200 Ok\nhost-id:ABC\nhost-type:PS5\nhost-name:PS5-806\nhost-request-port:997\n"
    assert parse_ddp(awake, "192.168.0.3") == {"ip": "192.168.0.3", "name": "PS5-806", "state": "online"}
    assert parse_ddp(awake.replace(b"200 Ok", b"620 Server Standby"), "192.168.0.3")["state"] == "standby"
    assert parse_ddp(awake.replace(b"host-name:PS5-806\n", b""), "10.0.0.2")["name"] == "10.0.0.2"
    assert parse_ddp(awake.replace(b"PS5", b"PS4", 1), "192.168.0.3") is None
    assert parse_ddp(b"garbage", "192.168.0.3") is None
