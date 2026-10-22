"""Ingest from a camera card: names from metadata, verified copies, no double copies, and clearing only what is
verified and unchanged."""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from ontherecord import ingest


def jpeg_with_exif(make: str, model: str, taken: str) -> bytes:
    """A minimal JPEG whose APP1 segment carries Make, Model and DateTimeOriginal, little-endian TIFF."""
    def ascii_entry(tag, text, data_off):
        b = text.encode() + b"\0"
        return struct.pack("<HHII", tag, 2, len(b), data_off), b
    ifd0_off, n0 = 8, 3
    data_off = ifd0_off + 2 + 12 * n0 + 4
    e_make, b_make = ascii_entry(0x010F, make, data_off)
    e_model, b_model = ascii_entry(0x0110, model, data_off + len(b_make))
    exif_off = data_off + len(b_make) + len(b_model)
    e_ptr = struct.pack("<HHII", 0x8769, 4, 1, exif_off)
    e_taken, b_taken = ascii_entry(0x9003, taken, exif_off + 2 + 12 + 4)
    tiff = (b"II*\0" + struct.pack("<I", ifd0_off) + struct.pack("<H", n0) + e_make + e_model + e_ptr
            + struct.pack("<I", 0) + b_make + b_model + struct.pack("<H", 1) + e_taken + struct.pack("<I", 0) + b_taken)
    app1 = b"Exif\0\0" + tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "EOS_DIGITAL"
    d = root / "DCIM" / "100CANON"
    d.mkdir(parents=True)
    (d / "IMG_0001.JPG").write_bytes(jpeg_with_exif("Canon", "Canon EOS R6", "2026:10:22 14:30:05") + b"x" * 5000)
    (d / "IMG_0001.XMP").write_text("<xmp/>")
    (d / "MVI_0002.MP4").write_bytes(os.urandom(300_000))  # no readable metadata: falls back to the file date
    os.utime(d / "MVI_0002.MP4", (1761000000, 1761000000))
    (root / "MISC").mkdir()
    (root / "MISC" / "CATALOG.CTG").write_bytes(b"camera index")
    (root / ".DS_Store").write_bytes(b"junk")
    (d / "._IMG_0001.JPG").write_bytes(b"junk")
    return root


def ingest_once(card, tmp_path, run_id):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    spec = {"source": str(card), "archive": str(tmp_path / "Archive"), "project": "Reverse Telescope"}
    return ingest.run_ingest(spec, run_dir, run_id, log=lambda *_: None)


def test_exif_reader():
    p = Path(__file__).parent / "_tmp_exif.jpg"
    p.write_bytes(jpeg_with_exif("NIKON CORPORATION", "NIKON Z 6", "2025:01:02 03:04:05"))
    try:
        e = ingest.exif(p)
    finally:
        p.unlink()
    assert e == {"make": "NIKON CORPORATION", "model": "NIKON Z 6", "taken": "2025:01:02 03:04:05"}
    assert ingest.device_name(e["make"], e["model"], "card") == "NIKON-Z-6"
    assert ingest.device_name("Canon", "Canon EOS R6", "card") == "Canon-EOS-R6"


def test_ingest_names_verifies_and_records(card, tmp_path):
    rec = ingest_once(card, tmp_path, "imp1")
    assert rec["status"] == "done"
    man = json.loads(Path(rec["manifest"]).read_text())
    by = {Path(f["rel"]).name: f for f in man["files"]}
    assert by["IMG_0001.JPG"]["dest"] == ("Reverse-Telescope/2026/2026-10-22_Canon-EOS-R6/"
                                          "2026-10-22_143005_Canon-EOS-R6_IMG_0001.JPG")
    assert by["IMG_0001.JPG"]["taken_from"] == "exif"
    assert by["IMG_0001.XMP"]["dest"].endswith("2026-10-22_143005_Canon-EOS-R6_IMG_0001.XMP")  # follows its photo
    assert by["MVI_0002.MP4"]["taken_from"] == "file date" and "EOS_DIGITAL" in by["MVI_0002.MP4"]["dest"]
    assert by["CATALOG.CTG"]["status"] == "skipped"
    assert ".DS_Store" not in by and "._IMG_0001.JPG" not in by
    for f in man["files"]:
        if f["status"] == "copied":
            assert ingest.hash_file(tmp_path / "Archive" / f["dest"]) == f["sha256"]
    assert not list((tmp_path / "Archive").rglob("*.partial"))
    assert (tmp_path / "Archive" / "_ingests" / "imp1.csv").read_text(encoding="utf-8-sig").startswith("status,")
    ok, why = ingest.clearable(man)
    assert ok, why

    again = ingest_once(card, tmp_path, "imp2")  # an uncleared card plugged in again: nothing copied twice
    statuses = {c["ingest"]["status"] for c in again["cells"].values()}
    assert statuses == {"cached"}
    assert len([p for p in (tmp_path / "Archive").rglob("*") if p.is_file() and "_ingests" not in p.parts]) == 3


def test_clear_deletes_only_verified_unchanged_files(card, tmp_path):
    rec = ingest_once(card, tmp_path, "imp1")
    jpg = card / "DCIM" / "100CANON" / "IMG_0001.JPG"
    st = jpg.stat()
    data = bytearray(jpg.read_bytes())
    data[-1] ^= 0xFF  # one flipped byte, same size and date: only the second read can tell
    jpg.write_bytes(bytes(data))
    os.utime(jpg, (st.st_atime, st.st_mtime))
    run_dir = tmp_path / "runs" / "clr"
    run_dir.mkdir(parents=True)
    out = ingest.run_clear({"manifest": rec["manifest"]}, run_dir, "clr", log=lambda *_: None)
    assert out["status"] == "failed"  # one file was kept, so the clear is not complete
    assert jpg.exists()  # the changed file stays
    assert not (card / "DCIM" / "100CANON" / "MVI_0002.MP4").exists()
    assert not (card / "DCIM" / "100CANON" / "IMG_0001.XMP").exists()
    assert (card / "MISC" / "CATALOG.CTG").exists()  # never imported, never deleted
    assert (card / "DCIM" / "100CANON").is_dir()  # the camera's folders stay
    assert out["clear"] == {"manifest": rec["manifest"], "deleted": 2, "kept": 1}


def test_clear_refuses_an_incomplete_import(card, tmp_path):
    rec = ingest_once(card, tmp_path, "imp1")
    man = json.loads(Path(rec["manifest"]).read_text())
    man["files"][0]["status"] = "failed"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(man))
    with pytest.raises(ValueError, match="not verified"):
        ingest.run_clear({"manifest": str(bad)}, tmp_path, "x", log=lambda *_: None)


def test_a_copy_that_reads_back_differently_is_not_kept(tmp_path, monkeypatch):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(1000))
    monkeypatch.setattr(ingest, "hash_file", lambda p, cancelled=None: "0" * 64)  # the read-back disagrees
    with pytest.raises(OSError, match="does not match"):
        ingest.copy_verified(src, tmp_path / "out" / "a.bin")
    assert not list((tmp_path / "out").iterdir())


def test_ingest_and_clear_through_the_queue(card, tmp_path, capsys):
    import threading
    from ontherecord import cli
    from ontherecord.config import MachineConfig
    from ontherecord.jobs import JobQueue
    q = JobQueue(MachineConfig(workspace=tmp_path / "ws"))
    stop = threading.Event()
    t = threading.Thread(target=q.dispatch_forever, kwargs={"every_s": 0.2, "stop": stop, "log": lambda m: None})
    t.start()
    ws = ["--config", "none", "--workspace", str(tmp_path / "ws")]
    try:
        assert cli.main(ws + ["ingest", str(card), "--project", "Telescope", "--archive", str(tmp_path / "A"),
                              "--wait", "--json"]) == 0
        rec = json.loads(capsys.readouterr().out)
        assert rec["kind"] == "ingest" and rec["status"] == "done"
        assert q.get(rec["run_id"])["backend"] == "io"
        assert cli.main(ws + ["clear", rec["run_id"], "--wait", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "done" and out["clear"]["deleted"] == 3
        left = sorted(p.name for p in card.rglob("*") if p.is_file())
        assert left == [".DS_Store", "._IMG_0001.JPG", "CATALOG.CTG"]  # only what was never imported
    finally:
        stop.set()
        t.join()
