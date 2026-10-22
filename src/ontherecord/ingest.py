"""Bring recordings in from a camera card or an external drive: copy, verify, name and file them, and only then,
when asked, clear the device.

    scan    every file on the device, with when it was taken and on which camera (from the file's own metadata:
            EXIF for photos, the container for video and audio, the file date as a last resort; each row says which)
    copy    each file is hashed while it is read from the device, written to the archive, then read back from the
            archive and hashed again; it is renamed into place only if the two hashes match
    name    by a template, by default  <project>/<year>/<date>_<device>/<date>_<time>_<device>_<original name>
            (sidecar files such as .THM or .XMP follow their main file's name)
    record  a manifest per ingest (CSV for Excel, JSON for programs) and a catalog of everything in the archive, so
            a file is found by what it is, not where it sits; a file whose bytes are already in the archive is not
            copied twice
    clear   a separate step, offered only when every file is verified: each file on the device is read and hashed
            once more, and deleted only if it still matches its archived copy. Nothing is ever deleted otherwise.

The device is read twice in all (once to copy, once more before clearing), so a bad read shows up as a mismatch
instead of being trusted.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

KINDS = {
    "photo": {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".dng", ".cr2", ".cr3", ".nef", ".nrw",
              ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw", ".gpr"},
    "video": {".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv", ".mxf", ".insv", ".360", ".wmv", ".3gp"},
    "audio": {".wav", ".mp3", ".m4a", ".flac", ".aac", ".wma", ".ogg", ".opus", ".aif", ".aiff", ".ds2", ".dss",
              ".amr"},
}
SIDECARS = {".xmp", ".thm", ".lrv", ".lrf", ".srt", ".xml", ".aae", ".cpi", ".bdm", ".mpl"}
JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini", ".localized"}
JUNK_DIRS = {"system volume information", ".trashes", ".spotlight-v100", ".fseventsd", "$recycle.bin",
             ".temporaryitems", ".documentrevisions-v100"}
DEFAULT_TEMPLATE = "{project}/{date:%Y}/{date:%Y-%m-%d}_{device}/{date:%Y-%m-%d_%H%M%S}_{device}_{name}"
CHUNK = 8 * 1024 * 1024


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    for k, exts in KINDS.items():
        if ext in exts:
            return k
    return "sidecar" if ext in SIDECARS else "other"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:n]


# ---------------------------------------------------------------------------------------------- the devices
def volumes() -> list[dict]:
    """Mounted removable drives and memory cards: USB drives, SD cards, cameras in mass-storage mode."""
    import psutil
    out = []
    for p in psutil.disk_partitions(all=False):
        mp = p.mountpoint
        if os.name == "nt":
            if "removable" not in p.opts and not _windows_external(mp):
                continue
        elif sys.platform == "darwin":
            if not mp.startswith("/Volumes/"):
                continue
        elif not mp.startswith(("/media/", "/run/media/", "/mnt/")):
            continue
        try:
            usage = psutil.disk_usage(mp)
        except OSError:
            continue
        out.append({"mount": mp, "label": _label(mp), "fstype": p.fstype, "total": usage.total, "used": usage.used,
                    "camera": (Path(mp) / "DCIM").is_dir()})
    return out


def _windows_external(mp: str) -> bool:
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(mp) == 2  # DRIVE_REMOVABLE
    except Exception:
        return False


def _label(mp: str) -> str:
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(261)
            if ctypes.windll.kernel32.GetVolumeInformationW(mp, buf, 261, None, None, None, None, 0):
                return buf.value or mp
        except Exception:
            pass
        return mp
    return Path(mp).name or mp


# ---------------------------------------------------------------------------------------------- metadata
_TIFF_TAGS = {0x010F: "make", 0x0110: "model", 0x0132: "datetime", 0x9003: "taken", 0x9004: "digitized",
              0xA431: "serial", 0xC62F: "serial"}


def exif(path: Path) -> dict:
    """Make, model, serial and capture time from a JPEG or a TIFF-based file (most raw formats: CR2, NEF, ARW, DNG,
    ORF, PEF). Reads only the header. Returns {} when there is nothing to read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            if head[:2] == b"\xff\xd8":  # JPEG: find the APP1 Exif segment
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return {}
                    (length,) = struct.unpack(">H", fh.read(2))
                    if marker[1] == 0xE1:
                        data = fh.read(length - 2)
                        return _tiff(data[6:]) if data[:6] == b"Exif\0\0" else {}
                    if marker[1] in (0xDA, 0xD9):
                        return {}
                    fh.seek(length - 2, 1)
            if head in (b"II*\0", b"MM\0*", b"IIRO", b"IIU\0"):  # TIFF, and Olympus / Panasonic variants
                fh.seek(0)
                return _tiff(fh.read(1024 * 1024))
    except (OSError, struct.error, ValueError):
        pass
    return {}


def _tiff(data: bytes) -> dict:
    end = "<" if data[:2] == b"II" else ">"
    out: dict = {}

    def ifd(offset: int, depth: int = 0) -> None:
        if depth > 3 or offset + 2 > len(data):
            return
        (n,) = struct.unpack(end + "H", data[offset:offset + 2])
        for i in range(n):
            e = offset + 2 + 12 * i
            if e + 12 > len(data):
                return
            tag, typ, count = struct.unpack(end + "HHI", data[e:e + 8])
            if tag in (0x8769,):  # the Exif sub-IFD
                ifd(struct.unpack(end + "I", data[e + 8:e + 12])[0], depth + 1)
            elif tag in _TIFF_TAGS and typ == 2:  # ASCII
                ptr = e + 8 if count <= 4 else struct.unpack(end + "I", data[e + 8:e + 12])[0]
                val = data[ptr:ptr + count].split(b"\0")[0].decode("ascii", "replace").strip()
                if val:
                    out.setdefault(_TIFF_TAGS[tag], val)

    if len(data) >= 8:
        ifd(struct.unpack(end + "I", data[4:8])[0])
    return out


def _exif_time(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S") if s else None
    except ValueError:
        return None


def container_meta(path: Path, ffprobe: str = "ffprobe") -> dict:
    """Creation time and camera from a video or audio file's container tags, through ffprobe."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format_tags:stream_tags", "-of", "json",
                            str(path)], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    tags: dict = {}
    for t in [d.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in d.get("streams", [])]:
        for k, v in (t or {}).items():
            tags.setdefault(k.lower(), v)
    out = {}
    raw = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate") or tags.get("date")
    if raw:
        out["taken_raw"] = raw
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            # stored in UTC (most cameras and phones); shown in this machine's local time, like the file dates
            out["taken"] = dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    out["make"] = tags.get("com.apple.quicktime.make") or tags.get("make")
    out["model"] = tags.get("com.apple.quicktime.model") or tags.get("model") or tags.get("encoder_model")
    return {k: v for k, v in out.items() if v}


def exiftool_meta(paths: list[Path], exiftool: str) -> dict[str, dict]:
    """Everything exiftool can tell, for formats the built-in reader does not cover (HEIC, CR3, many videos)."""
    out: dict[str, dict] = {}
    for i in range(0, len(paths), 200):
        batch = [str(p) for p in paths[i:i + 200]]
        try:
            r = subprocess.run([exiftool, "-json", "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-Make",
                                "-Model", "-SerialNumber", *batch], capture_output=True, text=True, timeout=600)
            for row in json.loads(r.stdout or "[]"):
                t = row.get("DateTimeOriginal") or row.get("CreateDate") or row.get("MediaCreateDate")
                out[row["SourceFile"]] = {"taken": _exif_time(str(t)) if t else None, "make": row.get("Make"),
                                          "model": row.get("Model"), "serial": row.get("SerialNumber")}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    return out


def device_name(make: str | None, model: str | None, fallback: str) -> str:
    """'Canon EOS R6' -> 'Canon-EOS-R6'; a model that already names its maker keeps it once."""
    make, model = (make or "").strip(), (model or "").strip()
    if model:
        name = model if make and model.lower().startswith(make.split()[0].lower()) else f"{make} {model}".strip()
        return slug(name, 32) or slug(fallback, 32) or "device"
    return slug(fallback, 32) or "device"


# ---------------------------------------------------------------------------------------------- scan and name
@dataclass
class SourceFile:
    rel: str                          # path on the device, relative to the scanned folder
    size: int
    mtime: float
    kind: str
    taken: str | None = None          # ISO local time
    taken_from: str = "file date"     # exif | exiftool | container | file date | main file
    device: str = "device"
    camera: dict = field(default_factory=dict)
    dest: str | None = None           # relative to the archive
    sha256: str | None = None
    status: str = "pending"           # pending | copied | already archived | failed | deleted | kept
    note: str = ""


def scan(root: Path, kinds=("photo", "video", "audio", "sidecar", "other"), ffprobe: str = "ffprobe",
         exiftool: str | None = None, fallback_device: str | None = None) -> list[SourceFile]:
    root = Path(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in JUNK_DIRS and not d.lower().startswith(".trash")]
        for f in filenames:
            if f.lower() in JUNK_FILES or f.startswith("._"):
                continue
            p = Path(dirpath) / f
            if kind_of(p) in kinds:
                found.append(p)
    found.sort()
    fallback = fallback_device or _label(str(root))
    extra = exiftool_meta([p for p in found if kind_of(p) in ("photo", "video")], exiftool) if exiftool else {}
    files: list[SourceFile] = []
    for p in found:
        st = p.stat()
        sf = SourceFile(rel=p.relative_to(root).as_posix(), size=st.st_size, mtime=st.st_mtime, kind=kind_of(p))
        meta, src = {}, "file date"
        if sf.kind == "photo":
            e = exif(p)
            if e:
                meta = {**e, "taken": _exif_time(e.get("taken") or e.get("digitized") or e.get("datetime"))}
                src = "exif"
        elif sf.kind in ("video", "audio"):
            meta = container_meta(p, ffprobe)
            src = "container" if meta.get("taken") else src
        x = extra.get(str(p))
        if x and (x.get("taken") and not meta.get("taken")):
            meta, src = {**meta, **{k: v for k, v in x.items() if v}}, "exiftool"
        taken = meta.get("taken") if isinstance(meta.get("taken"), datetime) else None
        if taken is None:
            taken, src = datetime.fromtimestamp(st.st_mtime), "file date"
        sf.taken, sf.taken_from = taken.isoformat(timespec="seconds"), src
        sf.camera = {k: meta[k] for k in ("make", "model", "serial") if meta.get(k)}
        sf.device = device_name(meta.get("make"), meta.get("model"), fallback)
        files.append(sf)
    # Sidecars (GoPro .THM, Lightroom .XMP, Sony .XML...) take their main file's time and device, so they stay together.
    mains = {(str(Path(f.rel).parent), Path(f.rel).stem.lower()): f for f in files if f.kind not in ("sidecar",)}
    for f in files:
        if f.kind == "sidecar":
            m = mains.get((str(Path(f.rel).parent), Path(f.rel).stem.lower()))
            if m:
                f.taken, f.taken_from, f.device = m.taken, "main file", m.device
    return files


def render(template: str, f: SourceFile, project: str) -> str:
    """The archive path for one file. Tokens: {project}, {device}, {kind}, {name} (the original file name),
    {stem}, {ext}, and {date:FORMAT} with strftime codes, e.g. {date:%Y-%m-%d}."""
    taken = datetime.fromisoformat(f.taken) if f.taken else datetime.fromtimestamp(f.mtime)
    p = Path(f.rel)

    def date(m: re.Match) -> str:
        return taken.strftime(m.group(1))

    out = re.sub(r"\{date:([^}]+)\}", date, template)
    out = out.format(project=slug(project, 60) or "Project", device=f.device, kind=f.kind, name=p.name,
                     stem=p.stem, ext=p.suffix)
    parts = [re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", s).strip(" .") or "_" for s in out.replace("\\", "/").split("/")]
    return "/".join(parts)


def plan(files: list[SourceFile], template: str, project: str) -> list[SourceFile]:
    """Give every file its archive name; two files that would get the same name are told apart with _2, _3."""
    seen: dict[str, int] = {}
    for f in files:
        dest = render(template, f, project)
        key = dest.lower()
        if key in seen:
            seen[key] += 1
            p = Path(dest)
            dest = (p.parent / f"{p.stem}_{seen[key]}{p.suffix}").as_posix()
        else:
            seen[key] = 1
        f.dest = dest
    return files


# ---------------------------------------------------------------------------------------------- the catalog
class Catalog:
    """Every file in an archive, by content. Lives in the archive (`_ingests/catalog.sqlite`), so it travels with
    the archive's drive."""

    def __init__(self, archive: Path):
        self.dir = Path(archive) / "_ingests"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "catalog.sqlite", timeout=30)
        self.db.execute("""create table if not exists files (
            sha256 text not null, path text not null, size integer not null, kind text, taken text, taken_from text,
            device text, camera text, project text, original_name text, source_volume text, ingest_id text,
            ingested_utc text, primary key (sha256, path))""")

    def find(self, sha256: str) -> str | None:
        row = self.db.execute("select path from files where sha256 = ? order by ingested_utc limit 1",
                              (sha256,)).fetchone()
        return row[0] if row else None

    def add(self, f: SourceFile, project: str, volume: str, ingest_id: str) -> None:
        self.db.execute("insert or replace into files values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f.sha256, f.dest, f.size, f.kind, f.taken, f.taken_from, f.device, json.dumps(f.camera),
                         project, Path(f.rel).name, volume, ingest_id, utc()))
        self.db.commit()


# ---------------------------------------------------------------------------------------------- copy and verify
def hash_file(path: Path, cancelled=lambda: False) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            if cancelled():
                raise InterruptedError
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src: Path, dest: Path, cancelled=lambda: False) -> str:
    """Copy src to dest, hashing what was read; read the copy back and hash it; rename it into place only if the
    hashes match. Returns the sha256. The partial file is removed on any failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while chunk := fi.read(CHUNK):
                if cancelled():
                    raise InterruptedError
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        read = h.hexdigest()
        written = hash_file(tmp, cancelled)
        if written != read:
            raise OSError(f"the copy does not match what was read (read {read[:12]}, copy {written[:12]})")
        st = src.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, dest)
        return read
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------------------------- jobs
class _Progress:
    """The job's run record, in the same shape as a recipe run (one 'ingest' column, one row per file), so the Jobs
    tab, `ontherecord wait` and `show` read it the same way. Saved at most once a second: a card can hold
    thousands of files."""

    def __init__(self, run_dir: Path, run_id: str, name: str, stage: str, label: str, files: list[SourceFile],
                 extra: dict):
        from .store import write_json_atomic
        self._write, self.path, self._last = write_json_atomic, run_dir / "run.json", 0.0
        self.rec = {"run_id": run_id, "name": name, "description": extra.pop("description", ""), "status": "running",
                    "created_utc": utc(), "finished_utc": None, "pid": os.getpid(), "host": socket.gethostname(),
                    "kind": stage, "stages": [{"name": stage, "stage": stage, "label": label}],
                    "items": [{"id": f.rel, "path": f.rel, "title": Path(f.rel).name} for f in files],
                    "cells": {f.rel: {stage: {"status": "pending"}} for f in files}, **extra}
        self.stage = stage
        self.save(force=True)

    def cell(self, f: SourceFile, status: str, **kw) -> None:
        self.rec["cells"][f.rel][self.stage] = {"status": status, **kw}
        self.save()

    def save(self, force: bool = False) -> None:
        if force or time.time() - self._last >= 1:
            self._write(self.path, self.rec)
            self._last = time.time()

    def finish(self, status: str) -> None:
        self.rec.update(status=status, finished_utc=utc())
        self.save(force=True)


def write_manifest(archive: Path, run_id: str, header: dict, files: list[SourceFile]) -> Path:
    d = Path(archive) / "_ingests"
    d.mkdir(parents=True, exist_ok=True)
    rows = [asdict(f) for f in files]
    (d / f"{run_id}.json").write_text(json.dumps({**header, "files": rows}, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    cols = ["status", "rel", "dest", "kind", "taken", "taken_from", "device", "size", "sha256", "note"]
    with open(d / f"{run_id}.csv", "w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel reads it as UTF-8
        w = csv.writer(fh)
        w.writerow(["status", "original file", "archived as", "kind", "taken", "time from", "device", "bytes",
                    "sha256", "note"])
        for r in rows:
            w.writerow([r[c] for c in cols])
    return d / f"{run_id}.json"


def run_ingest(spec: dict, run_dir: Path, run_id: str, cancelled=lambda: False, log=print, ffprobe="ffprobe",
               exiftool: str | None = None) -> dict:
    """spec: source (folder), archive (folder), project, template, kinds, device (name to use when the files'
    metadata names no camera)."""
    source, archive = Path(spec["source"]).expanduser(), Path(spec["archive"]).expanduser()
    project, template = spec.get("project") or "Project", spec.get("template") or DEFAULT_TEMPLATE
    if not source.is_dir():
        raise FileNotFoundError(f"the source folder is not there: {source}")
    log(f"scanning {source} ...")
    wanted = set(spec.get("kinds") or (*KINDS, "sidecar"))
    found = scan(source, ffprobe=ffprobe, exiftool=exiftool, fallback_device=spec.get("device"))
    skipped = [f for f in found if f.kind not in wanted]
    for f in skipped:
        f.status, f.note = "skipped", f"a {f.kind} file; this import takes {', '.join(sorted(wanted))}. Left on the device."
    files = plan([f for f in found if f.kind in wanted], template, project)
    total = sum(f.size for f in files)
    log(f"{len(files)} files, {total / 1e9:.2f} GB; archive {archive}")
    archive.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(archive).free
    header = {"ingest_id": run_id, "source": str(source), "volume": _label(str(source)), "archive": str(archive),
              "project": project, "template": template, "started_utc": utc(), "host": socket.gethostname(),
              "files_total": len(files), "bytes_total": total, "skipped": len(skipped)}
    prog = _Progress(run_dir, run_id, spec.get("name") or f"import {header['volume']}", "ingest", "Copy and verify",
                     files, {"ingest": header, "description": f"{len(files)} files from {source} into {archive}"})
    if total > free:
        prog.finish("failed")
        raise OSError(f"not enough space in {archive}: {total / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free")
    cat = Catalog(archive)
    status = "done"
    try:
        for i, f in enumerate(files, 1):
            if cancelled():
                status = "cancelled"
                break
            src, dest = source / f.rel, archive / f.dest
            t0 = time.perf_counter()
            prog.cell(f, "running")
            try:
                if dest.exists():  # the same name from an earlier import: the same bytes, or a name to move aside
                    same = dest.stat().st_size == f.size and (hs := hash_file(src, cancelled)) == hash_file(dest, cancelled)
                    if same:
                        f.sha256 = hs
                        f.status, f.note = "already archived", "the same file is already at this name"
                        cat.add(f, project, header["volume"], run_id)
                        prog.cell(f, "cached", note=f.note)
                        continue
                    n = 2
                    while dest.exists():
                        dest = dest.with_name(f"{Path(f.dest).stem}_{n}{Path(f.dest).suffix}")
                        n += 1
                    f.dest = dest.relative_to(archive).as_posix()
                f.sha256 = copy_verified(src, dest, cancelled)
                elsewhere = cat.find(f.sha256)
                if elsewhere and elsewhere != f.dest and (archive / elsewhere).is_file():
                    dest.unlink()  # its bytes were archived before under another name: keep one copy
                    f.dest, f.status, f.note = elsewhere, "already archived", "the same bytes were archived earlier"
                    prog.cell(f, "cached", note=f.note)
                else:
                    f.status = "copied"
                    cat.add(f, project, header["volume"], run_id)
                    prog.cell(f, "ran", elapsed_s=round(time.perf_counter() - t0, 2), checks="ok")
            except InterruptedError:
                status = "cancelled"
                f.status, f.note = "failed", "cancelled"
                prog.cell(f, "cancelled")
                break
            except OSError as e:
                f.status, f.note = "failed", str(e)
                prog.cell(f, "failed", error=str(e))
                log(f"FAILED {f.rel}: {e}")
            if i % 50 == 0 or i == len(files):
                log(f"{i}/{len(files)} files")
        if status == "done" and any(f.status == "failed" for f in files):
            status = "failed"
    finally:
        for f in files:
            if prog.rec["cells"][f.rel]["ingest"]["status"] in ("pending", "running"):
                prog.cell(f, "cancelled")
        header.update(finished_utc=utc(), status=status,
                      verified=sum(f.status in ("copied", "already archived") for f in files))
        manifest = write_manifest(archive, run_id, header, files + skipped)
        prog.rec["ingest"] = header
        prog.rec["manifest"] = str(manifest)
        prog.finish(status)
        log(f"ingest {status}: {header['verified']} of {len(files)} verified; manifest {manifest}")
    return prog.rec


def clearable(manifest: dict) -> tuple[bool, str]:
    """Whether a device may be cleared after this ingest: only when every file on it was verified."""
    if manifest.get("status") != "done":
        return False, f"the import ended as '{manifest.get('status')}', so not every file is known to be safe"
    bad = [f for f in manifest["files"] if f["status"] not in ("copied", "already archived", "skipped")]
    if bad:
        return False, f"{len(bad)} file(s) were not verified, e.g. {bad[0]['rel']}: {bad[0]['note']}"
    n = sum(f["status"] != "skipped" for f in manifest["files"])
    skipped = len(manifest["files"]) - n
    return True, (f"all {n} imported files are in the archive and verified"
                  + (f"; {skipped} other file(s) were not imported and stay on the device" if skipped else ""))


def run_clear(spec: dict, run_dir: Path, run_id: str, cancelled=lambda: False, log=print) -> dict:
    """Delete from the device exactly the files an ingest verified, each only after hashing it once more and finding
    it unchanged and identical to its archived copy. spec: manifest (path to the ingest's manifest JSON)."""
    manifest = json.loads(Path(spec["manifest"]).read_text(encoding="utf-8"))
    ok, why = clearable(manifest)
    if not ok:
        raise ValueError(f"not clearing: {why}")
    source, archive = Path(manifest["source"]), Path(manifest["archive"])
    files = [SourceFile(**f) for f in manifest["files"] if f["status"] != "skipped"]
    prog = _Progress(run_dir, run_id, spec.get("name") or f"clear {manifest['volume']}", "clear",
                     "Check and delete from the device", files,
                     {"description": f"delete the {len(files)} verified files of import {manifest['ingest_id']} "
                                     f"from {source}", "clear": {"manifest": spec["manifest"]}})
    status, deleted, kept = "done", 0, 0
    try:
        for f in files:
            if cancelled():
                status = "cancelled"
                break
            src, copy = source / f.rel, archive / f.dest
            prog.cell(f, "running")
            try:
                if not src.exists():
                    f.status, f.note = "deleted", "already gone from the device"
                    prog.cell(f, "cached", note=f.note)
                    continue
                st = src.stat()
                if st.st_size != f.size or abs(st.st_mtime - f.mtime) > 2:
                    raise ValueError("changed on the device since the import")
                if not copy.is_file() or copy.stat().st_size != f.size:
                    raise ValueError(f"the archived copy is missing or a different size: {copy}")
                if hash_file(src, cancelled) != f.sha256:
                    raise ValueError("reads differently from the device now than during the import")
                src.unlink()
                f.status = "deleted"
                deleted += 1
                prog.cell(f, "ran", checks="ok")
            except InterruptedError:
                status = "cancelled"
                prog.cell(f, "cancelled")
                break
            except (OSError, ValueError) as e:
                f.status, f.note = "kept", str(e)
                kept += 1
                prog.cell(f, "failed", error=f"kept on the device: {e}")
                log(f"KEPT {f.rel}: {e}")
        if status == "done" and kept:
            status = "failed"
    finally:
        prog.rec["clear"].update(deleted=deleted, kept=kept)
        prog.finish(status)
        log(f"clear {status}: {deleted} deleted, {kept} kept")
    return prog.rec
