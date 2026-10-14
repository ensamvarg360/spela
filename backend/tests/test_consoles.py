import asyncio

import pytest

from app.consoles import GiB, AdapterError, MockConsoleAdapter, UnsupportedConsoleConnectionError, get_adapter
from app.models import Console


def run(coro):
    return asyncio.run(coro)


def test_factory():
    assert isinstance(get_adapter(Console(connection_type="mock")), MockConsoleAdapter)
    with pytest.raises(UnsupportedConsoleConnectionError):
        get_adapter(Console(connection_type="ftp"))


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
