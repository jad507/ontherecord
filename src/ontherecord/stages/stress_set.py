"""A labelled stress test set as one file: StressTest and StressPresso (Yosha et al. 2025) from Hugging Face, or a
local set in the WildStressTest items.json shape. Ported from AITranscribe/export_stresstest.py.

The set is written as set.zip: manifest.json (one entry per item, with the labels) and the audio, each clip's bytes
as the source gave them. One file, so the receipt's hash covers every clip and every label at once.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
import zlib
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage

# The revisions the ICDS poster's numbers come from.
KNOWN = {"slprl/StressTest": ("cf2730d7d2a6276d91b7ecc7ccabeae03429b26d", "st"),
         "slprl/StressPresso": ("b02f9aeb456fdcfe6c36a965ffc9d468ee16afd7", "sp")}
FIXED_TIME = (2025, 1, 1, 0, 0, 0)  # zip entries carry no real time, so the same set always hashes the same


def hf_repo(item: Item) -> str | None:
    """'https://huggingface.co/datasets/slprl/StressTest' -> 'slprl/StressTest'."""
    if item.url and "huggingface.co/datasets/" in item.url:
        return "/".join(item.url.split("huggingface.co/datasets/", 1)[1].strip("/").split("/")[:2])
    return None


def read_set(path: Path) -> tuple[dict, zipfile.ZipFile]:
    z = zipfile.ZipFile(path)
    return json.loads(z.read("manifest.json")), z


def clip_audio(z: zipfile.ZipFile, entry: dict):
    """(mono float32 samples, sample rate) for one item."""
    import soundfile as sf
    y, sr = sf.read(io.BytesIO(z.read(entry["audio"])), dtype="float32")
    return (y.mean(axis=1) if y.ndim > 1 else y), sr


def _write(out: Path, manifest: dict, audio: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        def put(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
        put("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False).encode())
        for name in sorted(audio):
            put(name, audio[name])


class StressSet(Stage):
    """Load a set of recordings whose stressed words and meanings are labelled: StressTest (218 readings by one
    actor) or StressPresso (202 readings by 4 speakers) from Hugging Face, or a local set such as WildStressTest.
    Every later stress stage scores itself against these labels."""
    name = "stress_set"
    label = "Load a stress test set"
    version = "1"
    group = "stress"
    makes = ("stress_set",)
    packages = ("huggingface_hub", "pyarrow", "soundfile")
    settings = {
        "revision": Setting("pinned", "Which version of a Hugging Face set: 'pinned' = the one the ICDS poster used "
                            "(StressTest and StressPresso), else a commit id or 'main'."),
        "split": Setting("test", "Which split of a Hugging Face set."),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        repo = hf_repo(item)
        if repo:
            manifest, audio = self._hf(repo, settings)
        elif item.path:
            manifest, audio = self._local(Path(item.path))
        else:
            raise ValueError(f"{item.url or item.id}: give a huggingface.co/datasets/ link or a local items.json")
        _write(out / "set.zip", manifest, audio)
        clips = manifest["items"]
        n_stressed = [sum(c["stress_binary"]) for c in clips]
        chance_top1 = sum(k / len(c["stress_binary"]) for k, c in zip(n_stressed, clips)) / len(clips)
        checks = [Check("labels", "ok", len(clips), f"{len(clips)} labelled items, {len(audio)} recordings")]
        bad = [c["id"] for c in clips if len(c["stress_binary"]) != len(c["transcription"].split())]
        if bad:
            checks.append(Check("label_lengths", "fail", len(bad), f"{len(bad)} items have a stress label per word "
                                f"that does not match their words, e.g. {bad[0]}"))
        return Result(files={"stress_set": "set.zip"},
                      metrics={"items": len(clips), "recordings": len(audio),
                               "texts": len({c["transcription"] for c in clips}),
                               "one_stressed_word": sum(k == 1 for k in n_stressed),
                               "blind_items": sum(bool(c.get("blind")) for c in clips),
                               "chance_top1": round(chance_top1, 3)},
                      checks=checks, tools=manifest["source"])

    def _hf(self, repo: str, settings: dict) -> tuple[dict, dict]:
        import pyarrow.parquet as pq
        import soundfile as sf
        from huggingface_hub import snapshot_download
        pinned, prefix = KNOWN.get(repo, (None, repo.split("/")[-1][:2].lower()))
        rev = pinned if settings["revision"] == "pinned" else settings["revision"]
        if rev is None:
            raise ValueError(f"no pinned revision for {repo}; set revision to a commit id or 'main'")
        try:  # a set already downloaded needs no network (and works with HF_HUB_OFFLINE)
            snap = Path(snapshot_download(repo, repo_type="dataset", revision=rev, local_files_only=True))
        except Exception:
            if os.environ.get("HF_HUB_OFFLINE") == "1":  # stages run offline (config.LOCAL_ONLY)
                raise FileNotFoundError(
                    f"{repo} at {rev} is not downloaded, and stages do not fetch anything. Fetch it once, on purpose: "
                    f"hf download {repo} --repo-type dataset --revision {rev} "
                    f"--include 'data/{settings['split']}-*.parquet'") from None
            snap = Path(snapshot_download(repo, repo_type="dataset", revision=rev,
                                          allow_patterns=[f"data/{settings['split']}-*.parquet"]))
        files = sorted((snap / "data").glob(f"{settings['split']}-*.parquet"))
        if not files:
            raise FileNotFoundError(f"{repo} has no {settings['split']} split at {rev}")
        import pyarrow as pa
        table = pa.concat_tables([pq.read_table(f) for f in files])
        rows = table.drop_columns(["audio"]).to_pylist()
        aud = table.column("audio").combine_chunks()
        kind = "bytes" if aud.type.get_field_index("bytes") >= 0 else "array"
        items, audio = [], {}
        for i, r in enumerate(rows):
            if kind == "bytes":
                data = aud.field("bytes")[i].as_py()
                ext = Path(aud.field("path")[i].as_py() or "x.wav").suffix or ".wav"
            else:  # StressPresso stores decoded samples: keep them exactly, as 32-bit float
                buf = io.BytesIO()
                sf.write(buf, aud.field("array")[i].values.to_numpy(zero_copy_only=False).astype("float32"),
                         aud.field("sampling_rate")[i].as_py(), format="WAV", subtype="FLOAT")
                data, ext = buf.getvalue(), ".wav"
            name = f"audio/{prefix}_{i:03d}{ext}"
            audio[name] = data
            meta = r.get("metadata") or {}
            items.append({"id": f"{prefix}_{i:03d}", "seed": i, "audio": name, "transcription": r["transcription"],
                          "answers": list(r["possible_answers"]), "label": int(r["label"]),
                          "stress_binary": list(r["stress_pattern"]["binary"]),
                          "stress_words": list(r["stress_pattern"]["words"]), "intonation": r["intonation"],
                          "speaker": meta.get("speaker_id") or meta.get("voice_name"),
                          "transcription_id": r.get("transcription_id"),
                          "interpretation_id": r.get("interpretation_id")})
        source = {"dataset": repo, "revision": snap.name, "split": settings["split"],
                  "parquet_sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
        return {"name": repo.split("/")[-1], "source": source, "items": items}, audio

    def _local(self, path: Path) -> tuple[dict, dict]:
        """items.json: a list of {id, path, transcription, answers, label, stress_binary, intonation, blind}. A
        relative audio path is looked for beside the file, then beside each folder above it."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        items, audio, names = [], {}, {}
        for r in raw:
            p = Path(r["path"])
            cands = [p] if p.is_absolute() else [d / p for d in [path.parent, *path.parent.parents]]
            src = next((c for c in cands if c.is_file()), None)
            if src is None:
                raise FileNotFoundError(f"{r['id']}: audio {r['path']} not found near {path}")
            data = src.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest not in names:  # an item asked twice shares one recording
                names[digest] = f"audio/{digest[:16]}{src.suffix}"
                audio[names[digest]] = data
            items.append({"id": r["id"], "seed": zlib.crc32(r["id"].encode()) % 2**31, "audio": names[digest],
                          "transcription": r["transcription"], "answers": list(r["answers"]),
                          "label": int(r["label"]), "stress_binary": list(r["stress_binary"]),
                          "stress_words": [w for w, s in zip(r["transcription"].split(), r["stress_binary"]) if s],
                          "intonation": r.get("intonation"), "blind": bool(r.get("blind")),
                          "source_path": r["path"]})
        source = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return {"name": path.parent.name, "source": source, "items": items}, audio
