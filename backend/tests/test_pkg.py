from app.pkg import parse_pkg
from conftest import PARAM_JSON, cnt, ps5_pkg, sfo, write


def test_ps5_param_json(tmp_path):
    m = parse_pkg(str(write(tmp_path / "x.pkg", ps5_pkg(cnt("EP0001-PPSA02222_00-EXAMPLEGAME00000", {0x2000: PARAM_JSON})))))
    assert (m.platform, m.title_id, m.title, m.version) == ("ps5_pkg", "PPSA02222", "Native Game", "01.020.000")
    assert (m.source, m.confidence, m.region, m.warnings) == ("ps5_param_json", "high", "EU", [])
    assert m.content_id == "EP0001-PPSA02222_00-EXAMPLEGAME00000" and m.min_firmware == "0x0400000000000000"


def test_ps5_encrypted_param_falls_back_to_cnt(tmp_path):
    data = ps5_pkg(cnt("EP0001-PPSA02222_00-EXAMPLEGAME00000", {0x2000: PARAM_JSON}, encrypted={0x2000}))
    m = parse_pkg(str(write(tmp_path / "x.pkg", data)))
    assert (m.title_id, m.source, m.confidence) == ("PPSA02222", "ps5_cnt", "high")
    assert any("encrypted" in w for w in m.warnings)


def test_ps4_param_sfo(tmp_path):
    data = cnt("UP0000-CUSA00001_00-GAME000000000000", {0x1000: sfo({"TITLE_ID": "CUSA00001", "TITLE": "Old", "APP_VER": "01.05"})})
    m = parse_pkg(str(write(tmp_path / "x.pkg", data)))
    assert (m.platform, m.title_id, m.title, m.version, m.source) == ("ps4_pkg", "CUSA00001", "Old", "01.05", "ps4_param_sfo")


def test_platform_mismatch_rejected(tmp_path):
    # A PS5 container claiming a PS4 title ID is not trusted.
    m = parse_pkg(str(write(tmp_path / "x.pkg", ps5_pkg(cnt("UP0000-CUSA00001_00-GAME000000000000", {})))))
    assert m.title_id is None and m.confidence == "unknown"


def test_broken_cnt(tmp_path):
    m = parse_pkg(str(write(tmp_path / "x.pkg", ps5_pkg(b"junk" * 50))))
    assert m.platform == "ps5_pkg" and m.title_id is None and "CNT metadata could not be parsed" in m.warnings[0]
