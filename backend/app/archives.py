"""Archive installs (project.md §7): an archive is extracted into the private staging folder on this PC, the scanner's
own detection finds the folder dump or disc image inside, and that copy goes to the console. Staging is emptied after."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event

from . import config, scanner
from .consoles import AdapterError, ProgressCallback

# Windows' own tar.exe is bsdtar (libarchive), which reads zip, 7z and rar. GNU tar can't, so a bsdtar on PATH wins.
_SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
TAR = shutil.which("bsdtar") or (str(_SYSTEM_TAR) if _SYSTEM_TAR.is_file() else None)
_NO_PASSWORD = ["--passphrase", "-"]  # without it bsdtar would wait for a password at a console nobody sees
_SIZE_RE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s")  # `tar -tv`: mode, links, owner, group, size, ...
_FIRST_SPLIT_RE = re.compile(r"\.0*1$")  # first part of a byte-split set: x.7z.001, x.zip.001, x.001
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
JOB_PREFIX = "job-"  # cleanup only ever deletes these, whatever STAGING_DIR points at


def blocker(path: str) -> str | None:
    """Why this archive can't be installed, or None."""
    if not TAR:
        return "There's no archive extractor (bsdtar, or Windows' tar.exe). Extract the archive yourself, then scan again."
    p = Path(path)
    if re.search(r"\.part0*1\.rar$", p.name, re.I) or p.with_suffix(".z01").exists():
        return "Multi-part RAR and spanned ZIP sets can't be extracted here. Extract it yourself, then scan again."
    return None


def job_dir(job_id: str) -> Path:
    return config.STAGING_DIR / f"{JOB_PREFIX}{job_id}"


def clean(job_id: str | None = None):
    """Deletes one job's staging folder, or every job's (at startup, when no job runs)."""
    dirs = [job_dir(job_id)] if job_id else config.STAGING_DIR.glob(f"{JOB_PREFIX}*")
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def _volumes(path: str) -> list[Path]:
    p = Path(path)
    if not _FIRST_SPLIT_RE.search(p.name):
        return [p]
    stem = p.name[:p.name.rfind(".") + 1]
    parts = sorted((q for q in p.parent.iterdir() if q.name.startswith(stem) and q.name[len(stem):].isdigit()),
                   key=lambda q: int(q.name[len(stem):]))
    if [int(q.name[len(stem):]) for q in parts] != list(range(1, len(parts) + 1)):
        raise AdapterError("archive_incomplete", "A part of the split archive is missing.")
    return parts


def _need_space(needed: int):
    free = shutil.disk_usage(config.STAGING_DIR).free
    if free < needed:
        raise AdapterError("no_staging_space", f"Not enough space on this PC to extract: needs {needed / 1024**3:.1f} GiB "
                                               f"in {config.STAGING_DIR}, {free / 1024**3:.1f} GiB free.")


def prepare(path: str, job_id: str, stop: Event) -> tuple[Path, int]:
    """(archive to extract, its unpacked size). A split set is first joined into staging, since 7z can't be streamed."""
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    parts = _volumes(path)
    archive = parts[0]
    if len(parts) > 1:
        _need_space(sum(q.stat().st_size for q in parts))
        archive = job_dir(job_id) / "joined"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with open(archive, "wb") as out:
            for q in parts:
                if stop.is_set():
                    raise AdapterError("canceled", "Canceled.")
                with open(q, "rb") as f:
                    shutil.copyfileobj(f, out, 8 * 1024**2)
    r = subprocess.run([TAR, "-tvf", str(archive), *_NO_PASSWORD], capture_output=True, text=True,
                       errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if r.returncode:
        raise AdapterError("archive_unreadable", f"Couldn't read the archive: {_last_line(r.stderr)}")
    unpacked = sum(int(m[1]) for line in r.stdout.splitlines() if (m := _SIZE_RE.match(line)))
    _need_space(unpacked)
    return archive, unpacked


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and "Error exit delayed" not in ln]
    return lines[-1].strip() if lines else "unknown error"


def _size(folder: Path) -> int:
    return sum(os.lstat(os.path.join(d, f)).st_size for d, _, files in os.walk(folder) for f in files)


def extract(archive: Path, unpacked: int, name: str, job_id: str, progress: ProgressCallback, stop: Event) -> scanner.Item:
    """Extracts into staging/job-<id>/<name> and returns the one folder dump, disc image or DLC unlocker found there.
    bsdtar refuses absolute paths and `..` entries; links are refused here, so nothing points outside staging."""
    dest = job_dir(job_id) / name
    dest.mkdir(parents=True)
    log_path = job_dir(job_id) / "tar.log"
    with open(log_path, "w+b") as log:  # a file, not a pipe: a chatty tar can't fill it and stall
        proc = subprocess.Popen([TAR, "-xf", str(archive), "-C", str(dest), *_NO_PASSWORD], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=log, creationflags=_NO_WINDOW)
        while proc.poll() is None:
            if stop.is_set():
                proc.kill()
                proc.wait()
                raise AdapterError("canceled", "Canceled.")
            progress(min(_size(dest), unpacked), unpacked)
            time.sleep(0.5)
    if proc.returncode:
        raise AdapterError("archive_extraction_failed",
                           f"Couldn't extract the archive: {_last_line(log_path.read_text(errors='replace'))}")
    progress(unpacked, unpacked)
    for d, dirs, files in os.walk(dest):
        for n in dirs + files:
            p = Path(d, n)
            if p.is_symlink() or p.is_junction():
                raise AdapterError("archive_unsafe", "The archive contains links, which aren't allowed. Nothing was installed.")
    # the dump may sit at the archive's top level
    found = next((i for det in scanner.FOLDER_DETECTORS if (i := det(dest))), None)
    items = [found] if found else scanner.scan_root(str(dest))[0]
    games = [i for i in items if i.detected_format in ("folder_dump", "image", "unlocker") and not i.partial]
    if len(games) != 1:
        what = ", ".join(sorted({i.detected_format for i in items})) or "nothing recognizable"
        raise AdapterError("archive_contents", f"The archive must hold exactly one folder dump, disc image or DLC unlocker. "
                                               f"Found {len(games)} ({what}).")
    return games[0]
