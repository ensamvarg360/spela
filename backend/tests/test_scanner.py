import shutil

from app import scanner
from app.database import Base, SessionLocal, engine
from app.models import Game, LibraryRoot
from sqlalchemy import delete, select


def by_name(items):
    return {i.source_path.replace("\\", "/").rsplit("/", 1)[-1]: i for i in items}


def test_detection(library):
    items, failed = scanner.scan_root(str(library), ["skip"])
    found = by_name(items)
    assert failed == []
    assert set(found) == {"Example Game (PPSA01234)", "Other_PPSA05555.exfat", "fake.ffpfs", "Old CUSA00001.pkg",
                          "renamed.pkg", "Broken PPSA03333.pkg", "Archived CUSA00002.7z.001", "Lost.part2.rar",
                          "Half PPSA07777.exfat.part", "Copy (PPSA01234).ffpkg", "Comp PPSA04444.ffpfsc",
                          "main.pkg", "addon.pkg", "unlocker.rar", "duplex-game.dlc.unlocker"}  # backports, artbooks skipped
    assert any("mislabeled" in w for w in found["main.pkg"].warnings)
    assert {n for n, i in found.items() if i.kind == "dlc"} == {"addon.pkg", "unlocker.rar", "duplex-game.dlc.unlocker"}
    addon = found["addon.pkg"]
    assert (addon.detected_format, addon.title_id, addon.metadata_confidence) == ("ps5_pkg", "PPSA02222", "high")
    unlocker = found["duplex-game.dlc.unlocker"]
    assert (unlocker.detected_format, unlocker.title_id, unlocker.metadata_source) == ("unlocker", "PPSA02222", "unlocker_ini")
    assert found["renamed.pkg"].kind == "game"

    dump = found["Example Game (PPSA01234)"]
    assert (dump.detected_format, dump.title_id, dump.version) == ("folder_dump", "PPSA01234", "01.020.000")
    assert (dump.metadata_source, dump.metadata_confidence) == ("ps5_param_json", "high")
    assert dump.provisional_title == "Example Game"
    assert dump.cover_path.endswith("icon0.png")  # sidecar cover escaping the folder was refused
    assert any("ignored" in w for w in dump.warnings)  # sidecar format claim ignored
    assert dump.sidecar["region"] == "EU"

    exfat = found["Other_PPSA05555.exfat"]
    assert (exfat.detected_format, exfat.image_subtype, exfat.title_id) == ("image", "exfat", "PPSA05555")
    assert (exfat.metadata_source, exfat.metadata_confidence, exfat.provisional_title) == ("filename", "low", "Other")

    assert found["fake.ffpfs"].detected_format == "unknown"  # extension alone is not evidence
    assert found["Copy (PPSA01234).ffpkg"].image_subtype == "ffpkg"  # UFS2 superblock
    assert found["Comp PPSA04444.ffpfsc"].image_subtype == "ffpfsc"  # PFSC magic
    assert found["Archived CUSA00002.7z.001"].detected_format == "archive"
    assert found["Lost.part2.rar"].detected_format == "unknown"  # orphaned later volume
    assert found["Half PPSA07777.exfat.part"].partial

    ps4 = found["Old CUSA00001.pkg"]
    assert (ps4.detected_format, ps4.title_id, ps4.provisional_title, ps4.metadata_source) == ("ps4_pkg", "CUSA00001", "Old Game", "ps4_param_sfo")
    ps5 = found["renamed.pkg"]  # nothing in the name: everything comes from CNT + param.json
    assert (ps5.detected_format, ps5.title_id, ps5.provisional_title) == ("ps5_pkg", "PPSA02222", "Native Game")
    assert ps5.content_id == "EP0001-PPSA02222_00-EXAMPLEGAME00000" and ps5.metadata_confidence == "high"
    broken = found["Broken PPSA03333.pkg"]
    assert (broken.detected_format, broken.title_id, broken.metadata_source, broken.metadata_confidence) == ("ps5_pkg", "PPSA03333", "filename", "low")
    assert any("Title ID derived from filename" in w for w in broken.warnings)


def test_scan_sync_statuses_and_missing(library):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.execute(delete(Game))
        db.execute(delete(LibraryRoot))
        db.add(LibraryRoot(path=str(library), exclude_patterns=["skip"]))
        stale = str(library / "Rel.PS5-PPSA02222[FPKG]" / "backport (4.xx Only)" / "x-4xx.pkg")
        db.add(Game(source_path=stale, title="addon", format="ps5_pkg", source_fingerprint="x"))  # catalogued by an older scanner
        db.commit()

        report = scanner.run_scan(db)
        assert len(report["added"]) == 15 and report["unknown"]
        assert db.scalar(select(Game).where(Game.source_path == stale)) is None
        db.add(Game(source_path="/gone/root/x.pkg", title="orphan", format="ps5_pkg", source_fingerprint="x"))
        db.commit()
        scanner.run_scan(db)
        assert db.scalar(select(Game).where(Game.title == "orphan")) is None  # outside every root
        status = {g.title: g.compatibility_status for g in db.scalars(select(Game))}
        assert status["Other"] == "needs_attention"  # title ID only from filename
        assert status["fake"] == "incompatible"
        assert status["Example Game"] == "needs_attention"  # duplicate PPSA01234 with the .ffpkg copy
        assert status["Old Game"] == "ready"  # packages are confirmed at install time
        assert status["Half"] == "needs_attention"  # partial transfer
        assert {g.kind for g in db.scalars(select(Game).where(Game.title_id == "PPSA02222"))} == {"game", "dlc"}
        unlocker = db.scalar(select(Game).where(Game.format == "unlocker"))
        assert "isn't in your library" not in unlocker.compatibility_reason  # renamed.pkg is its game
        unlocker.title_id = "PPSA06666"
        scanner.refresh_statuses(db)
        assert unlocker.compatibility_status == "needs_attention"
        assert "Its game (PPSA06666) isn't in your library." in unlocker.compatibility_reason
        db.rollback()
        dump = db.scalar(select(Game).where(Game.format == "folder_dump"))
        assert dump.region == "EU"

        # Unchanged rescan touches nothing.
        assert scanner.run_scan(db)["updated"] == []

        # Removing a file marks it missing; it is never deleted from the catalog.
        (library / "Other_PPSA05555.exfat").unlink()
        assert len(scanner.run_scan(db)["missing"]) == 1
        assert db.scalar(select(Game).where(Game.title == "Other")).compatibility_status == "incompatible"

        # An unreachable root must not mark anything missing.
        shutil.rmtree(library)
        report = scanner.run_scan(db)
        assert report["errors"] and report["missing"] == []
        assert db.scalar(select(Game).where(Game.title == "fake")).missing is False


def test_sidecar_cover_must_be_an_image(tmp_path):
    for name in ("cover.jpg", "cover.html", "cover.svg"):
        (tmp_path / name).write_bytes(b"x")
    assert scanner._safe_child(tmp_path, "cover.jpg").endswith("cover.jpg")
    assert scanner._safe_child(tmp_path, "cover.html") is None  # would run script on the app's origin
    assert scanner._safe_child(tmp_path, "cover.svg") is None
