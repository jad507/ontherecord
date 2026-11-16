"""First-run setup: what this machine can run, the helper programs it needs, the models, and a machine config.

    ontherecord setup              check, fetch ffmpeg and llama-server if missing, write the machine config
    ontherecord setup --check      only report
    ontherecord models             the model catalog, what is downloaded, what fits this machine
    ontherecord models get NAME    download one (the speech models need a Hugging Face token; see below)

Every download is pinned and checked against SHA-256 digests written below: llama.cpp at build b11249, ffmpeg 8.1.3
(Windows and Linux, BtbN's month-end build of Sep 30, 2026) and 9.0.2 (macOS), and each model file at a fixed
Hugging Face revision. A file that does not match is deleted. Nothing is taken as "the latest": a project whose
publishing account is taken over can replace its newest release, but not a digest written here.

The hardware-fit rule is deliberately simple: which model sizes fit in GPU memory (or, on a Mac, in shared memory),
with the CPU as the fallback. Which builds of PyTorch and llama.cpp suit the card and its driver is hwdetect.py's
job. The choice is written to the machine config, and every receipt records the model.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

LLAMA_BUILD = "b11249"
LLAMA_ASSETS = {  # name: (sha256, bytes), from the GitHub release's published digests
    "cudart-llama-b11249-bin-ubuntu-cuda-12.8-x64.tar.gz": ("5d2b081d228c91bc7d357ffff1db378080ed11d7bbb1be4d37171d26565a6048", 594377587),
    "cudart-llama-b11249-bin-ubuntu-cuda-13.4-arm64.tar.gz": ("a21f3501928c40470c005a5de189c8b605025fc16c17d03009b2e9a242ef6128", 552521394),
    "cudart-llama-b11249-bin-ubuntu-cuda-13.4-x64.tar.gz": ("fbfbfdee790d4c4ed0e9c34f356133437999dc424553f4d5953997f9eccced34", 440236548),
    "cudart-llama-bin-win-cuda-12.4-x64.zip": ("8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6", 391443627),
    "cudart-llama-bin-win-cuda-13.4-arm64.zip": ("642dcde8805b3e3165ca710a5443b3b4044b27d96bd3ee3132473988c9bcb774", 153262407),
    "cudart-llama-bin-win-cuda-13.4-x64.zip": ("738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668", 423535356),
    "llama-b11249-bin-macos-arm64.tar.gz": ("d5fb90058f8d0c73c1e687b406dc1a6f53c64de725d3e642ae210db90c0b6613", 11763877),
    "llama-b11249-bin-macos-x64.tar.gz": ("8f446f75e176b25580faeba7f7d78dc624a7b5360a931655f2d0eb1befad5096", 11316747),
    "llama-b11249-bin-ubuntu-arm64.tar.gz": ("2024b392c2c71e937119a317ad47e215cc84483cf11bc90c814361612daba7a4", 13498718),
    "llama-b11249-bin-ubuntu-cuda-12.8-x64.tar.gz": ("a550c6157cc587d7c5845669d66f3f57b856c6fd4987fa706393d0d9f7e0b66b", 172238409),
    "llama-b11249-bin-ubuntu-cuda-13.4-arm64.tar.gz": ("23df87fb9005d16cede661a59709a76ad5be8b0edec85b1e347472f3ece7d250", 148173189),
    "llama-b11249-bin-ubuntu-cuda-13.4-x64.tar.gz": ("0ccab79ff4df798436f863368bdd70517d4769469cc437a2513dd630d4cd8c7c", 152874240),
    "llama-b11249-bin-ubuntu-vulkan-arm64.tar.gz": ("cf037d16d9952afafbbd883b118e7e7e476b25814a0de32a4002b743853edc96", 24656947),
    "llama-b11249-bin-ubuntu-vulkan-x64.tar.gz": ("f8d39878bcc9144b3bda8cbd75d8703b1d6466ab9b8f25e14840c746960b75c8", 31348947),
    "llama-b11249-bin-ubuntu-x64.tar.gz": ("d1aad0580a1f48cb55a9e8f7de0d6eed5e6415acfce65b677023f1fcd5c06083", 17405122),
    "llama-b11249-bin-win-cpu-arm64.zip": ("fe371ccdf7ba441de9c7dcf349de0c79056a55bfc3a7a07bd6820ed07dfd481f", 12051575),
    "llama-b11249-bin-win-cpu-x64.zip": ("ba0beb2076250970c113b90c0e60611909bc1e7e973ee92a416756a64dd0763c", 19163703),
    "llama-b11249-bin-win-cuda-12.4-x64.zip": ("500ea1076423d729b9537083b045c6f2dbeedb97f3bb02661022c75251e5c3c3", 264527344),
    "llama-b11249-bin-win-cuda-13.4-arm64.zip": ("0a7d2c171c18bf52da5a05e4fb51111db7367071557b5a81a2b7770b69ab7717", 145788945),
    "llama-b11249-bin-win-cuda-13.4-x64.zip": ("6128f9d4ed2cec0a6ab07b7368b57949b90b7d4882624be72736479bb3912b38", 153544476),
    "llama-b11249-bin-win-vulkan-x64.zip": ("32cf3b0e3477263c959ae17a08882f9b1e091c4d6d93ccc0631efd2185f5f87d", 33077226),
}
# hwdetect.llama_build names a build; these are its archives (the CUDA builds' runtime libraries come separately)
_B = LLAMA_BUILD
LLAMA_BUILDS = {
    "windows": {"cuda-13.4": [f"llama-{_B}-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"],
                "cuda-13.4-arm64": [f"llama-{_B}-bin-win-cuda-13.4-arm64.zip", "cudart-llama-bin-win-cuda-13.4-arm64.zip"],
                "cuda-12": [f"llama-{_B}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"],
                "vulkan": [f"llama-{_B}-bin-win-vulkan-x64.zip"], "cpu": [f"llama-{_B}-bin-win-cpu-x64.zip"],
                "cpu-arm64": [f"llama-{_B}-bin-win-cpu-arm64.zip"]},
    "linux": {"cuda-13.4": [f"llama-{_B}-bin-ubuntu-cuda-13.4-x64.tar.gz", f"cudart-llama-{_B}-bin-ubuntu-cuda-13.4-x64.tar.gz"],
              "cuda-13.4-arm64": [f"llama-{_B}-bin-ubuntu-cuda-13.4-arm64.tar.gz",
                                  f"cudart-llama-{_B}-bin-ubuntu-cuda-13.4-arm64.tar.gz"],
              "cuda-12": [f"llama-{_B}-bin-ubuntu-cuda-12.8-x64.tar.gz", f"cudart-llama-{_B}-bin-ubuntu-cuda-12.8-x64.tar.gz"],
              "vulkan": [f"llama-{_B}-bin-ubuntu-vulkan-x64.tar.gz"],
              "vulkan-arm64": [f"llama-{_B}-bin-ubuntu-vulkan-arm64.tar.gz"],
              "cpu": [f"llama-{_B}-bin-ubuntu-x64.tar.gz"], "cpu-arm64": [f"llama-{_B}-bin-ubuntu-arm64.tar.gz"]},
    "macos": {"macos-arm64": [f"llama-{_B}-bin-macos-arm64.tar.gz"], "macos-x64": [f"llama-{_B}-bin-macos-x64.tar.gz"]},
}
MAC_FFMPEG = {  # ffmpeg 9.0.2 from ffmpeg.martin-riedl.de, with the digests it publishes
    ("arm64", "ffmpeg"): ("download/macos/arm64/1789931890_9.0.2/ffmpeg.zip", "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
    ("arm64", "ffprobe"): ("download/macos/arm64/1789931890_9.0.2/ffprobe.zip", "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"),
    ("x86_64", "ffmpeg"): ("download/macos/amd64/1789931006_9.0.2/ffmpeg.zip", "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
    ("x86_64", "ffprobe"): ("download/macos/amd64/1789931006_9.0.2/ffprobe.zip", "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd"),
}
BTBN_TAG = "autobuild-2026-09-30-13-08"  # BtbN keeps month-end builds; uploaded by its GitHub Actions, never replaced
BTBN = {  # name, sha256, bytes (GitHub's digests for the release assets)
    "win": ("ffmpeg-n8.1.3-9-g29e619e767-win64-lgpl-8.1.zip",
            "4a7642b2264c03e8a0ce8a3825b933ee5580656f45695a086fe7e294045ffc0a", 170611883),
    "linux": ("ffmpeg-n8.1.3-9-g29e619e767-linux64-lgpl-8.1.tar.xz",
              "dfa863a00ca81f1bdf58a372b18cff4820f0017e55de32778de8ecd8ed92a02e", 137034436),
}

# The models On the Record offers, each pinned. gguf models are single files in models_root; hf models go to the
# Hugging Face cache, where faster-whisper and pyannote look for them: only the files they load (`patterns`), each
# checked against model_hashes.py.
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]  # faster-whisper's own list
MODELS = {
    "gemma-4-E2B": {"kind": "gguf", "repo": "google/gemma-4-E2B-it-qat-q4_0-gguf",
                    "rev": "675cff42a74c774d6cb76f76d8eacb49b48c9b93", "file": "gemma-4-E2B_q4_0-it.gguf",
                    "sha256": "fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634", "bytes": 3349516256,
                    "role": "assistant", "needs_gb": 5, "about": "Google's own 4-bit Gemma 4 E2B: the assistant on small machines"},
    "gemma-4-E4B": {"kind": "gguf", "repo": "google/gemma-4-E4B-it-qat-q4_0-gguf",
                    "rev": "4b4a2c1d584be7264f87aac328a1bc739ce81b6c", "file": "gemma-4-E4B_q4_0-it.gguf",
                    "sha256": "676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee", "bytes": 5154941280,
                    "role": "assistant", "needs_gb": 8, "about": "Google's own 4-bit Gemma 4 E4B: the assistant"},
    "gemma-4-26B": {"kind": "gguf", "repo": "unsloth/gemma-4-26B-A4B-it-GGUF",
                    "rev": "c099eb48e663fd284577b04978a94ffccb261841", "file": "gemma-4-26B-A4B-it-MXFP4_MOE.gguf",
                    "sha256": "a8908250f2a72a5824382d488158f12b65effa24e6c6b1244e5b4818ac0a1459", "bytes": 16551048928,
                    "role": "extraction", "needs_gb": 24,
                    "about": "Gemma 4 26B-A4B, MXFP4 (the comment extraction's default file name; runs with part of "
                             "it on the CPU on a 16 GB GPU)"},
    "whisper-large-v3": {"kind": "hf", "repo": "Systran/faster-whisper-large-v3",
                         "rev": "edaa852ec7e145841d8ffdb056a99866b5f0a478", "bytes": 3_100_000_000, "patterns": WHISPER_FILES,
                         "role": "speech", "needs_gb": 6, "about": "Whisper large-v3 for faster-whisper (transcription)"},
    # faster-whisper's name for it; Hugging Face redirects the repo to dropbox-dash/faster-whisper-large-v3-turbo
    "whisper-large-v3-turbo": {"kind": "hf", "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                               "rev": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", "bytes": 1_622_000_000,
                               "patterns": WHISPER_FILES,
                               "role": "speech", "needs_gb": 2,
                               "about": "Whisper large-v3-turbo: several times faster, a little less accurate "
                                        "(transcription without an NVIDIA card)"},
    "pyannote-community-1": {"kind": "hf", "repo": "pyannote/speaker-diarization-community-1",
                             "rev": "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee", "bytes": 34_000_000, "gated": True,
                             "patterns": ["config.yaml", "embedding/pytorch_model.bin", "plda/*.npz",
                                          "segmentation/pytorch_model.bin"],
                             "role": "speech", "needs_gb": 2,
                             "about": "pyannote's speaker diarization (who speaks when). Gated: accept its terms at "
                                      "https://huggingface.co/pyannote/speaker-diarization-community-1 first"},
    "whistress": {"kind": "hf", "repo": "slprl/WhiStress", "rev": "3641786c97df31786d2ae17bb9307f7776cf7138",
                  "patterns": ["classifier.pt", "additional_decoder_block.pt", "metadata.json"], "bytes": 42_600_000,
                  "role": "stress", "needs_gb": 2, "about": "WhiStress's stress head (the stress test stages)"},
    "whisper-small.en": {"kind": "hf", "repo": "openai/whisper-small.en", "rev": "e8727524f962ee844a7319d92be39ac1bd25655a",
                         "patterns": ["*.json", "*.txt", "model.safetensors"], "bytes": 970_000_000, "role": "stress",
                         "needs_gb": 2, "about": "The Whisper that WhiStress listens with (the stress test stages)"},
    # NVIDIA's ModelOpt FP8 export (repo made Oct 12, 2026; this revision Oct 29). It runs under vLLM 0.32.0 with
    # NeMo Speech's plugin, in its own environment (install/nemotron-vllm.sh); on a 16 GB card, with most of the
    # experts kept in RAM (--cpu-offload-params experts). The model card lists 48 GB and larger cards for FP8.
    "nemotron-3.5-transcribe-fp8": {
        "kind": "hf", "repo": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8",
        "rev": "2b6209f89bec0da576d69183bfdabb3b6c05ade2", "bytes": 34_689_160_218,
        "patterns": ["*.json", "*.jinja", "*.safetensors", "llm_backbone/config.json", "LICENSE", "licenses/*",
                     "THIRD_PARTY_NOTICES.md"],
        "role": "speech", "needs_gb": 48,
        "about": "NVIDIA Nemotron 3.5 Transcribe 30B-A4B, FP8: transcript, speaker labels and word times in one "
                 "model (vLLM; experts offloaded to RAM on smaller cards)"},
    # The same model as NVIDIA's ModelOpt NVFP4 export (repo made Oct 12, 2026; this revision Oct 29). Blackwell cards
    # only (the card lists the RTX 5090, B200, GB10...). 21.6 GB, so a 16 GB card keeps far fewer experts in RAM than
    # with FP8. The card: error rate 3.6% higher, relative, than the BF16 model's, averaged over its benchmarks.
    "nemotron-3.5-transcribe-nvfp4": {
        "kind": "hf", "repo": "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4",
        "rev": "66c6adc4020179567074d0399cda2a4eeb2bb347", "bytes": 21_576_957_938,
        "patterns": ["*.json", "*.jinja", "*.safetensors", "llm_backbone/config.json", "LICENSE", "licenses/*",
                     "THIRD_PARTY_NOTICES.md"],
        "role": "speech", "needs_gb": 16,
        "about": "NVIDIA Nemotron 3.5 Transcribe 30B-A4B, NVFP4 (Blackwell cards only): transcript, speaker labels "
                 "and word times in one model (vLLM; some experts offloaded to RAM on 16 GB cards)"},
}


# ---------------------------------------------------------------------------------------------- the machine
def home_dir() -> Path:
    """Where a desktop install keeps its things: one visible folder."""
    return Path(os.environ.get("ONTHERECORD_HOME", Path.home() / "OnTheRecord"))


def hardware() -> dict:
    """This machine: system, memory, processors, free disk where On the Record keeps its things, and every graphics
    card (`gpu` is the one the models use: the NVIDIA card with the most memory, or the Apple GPU)."""
    import psutil
    from . import hwdetect as hd
    hw = {"os": hd.os_name(), "arch": hd.arch(), "ram_gb": round(psutil.virtual_memory().total / 1e9),
          "cpus": os.cpu_count(), "gpus": hd.nvidia_gpus() + hd.other_gpus(), "gpu": None,
          "wsl": "microsoft" in platform.release().lower()}
    hw["gpu"] = hd.best_nvidia(hw["gpus"])
    if hw["os"] == "macos" and hw["arch"] == "arm64":
        hw["gpu"] = {"vendor": "apple", "name": "Apple GPU (shared memory)", "memory_gb": int(hw["ram_gb"] * 0.7)}
    home = home_dir()
    hw["disk_free_gb"] = round(shutil.disk_usage(home if home.exists() else Path.home()).free / 1e9)
    return hw


def accel(hw: dict, python: str | None = None) -> dict:
    """Which builds of PyTorch and llama.cpp suit this machine, what to do about the driver, and the PyTorch installed
    in `python` (the one that runs the audio stages; by default this one). Its version names the build: 2.11.0+cu128."""
    from . import hwdetect as hd
    flavor, flavor_why = hd.torch_flavor(hw["os"], hw["gpus"])
    builds, builds_why = hd.llama_build(hw["os"], hw["arch"], hw["gpus"])
    out = hd._run([python or sys.executable, "-c", "import importlib.metadata as m\ntry: print(m.version('torch'))\n"
                   "except Exception: pass"], timeout=60)
    torch = (out or "").strip() or None
    # 2.11.0+cu128 names its build; a bare 2.11.0 is PyPI's: CUDA 13 on Linux, the processor only on Windows
    have = (torch.split("+", 1)[1] if torch and "+" in torch else
            {"linux": "cu130 (PyPI)", "windows": "cpu (PyPI)"}.get(hw["os"], flavor) if torch else None)
    ok = have is None or have == flavor or hw["os"] == "macos"
    return {"torch": {"want": flavor, "why": flavor_why, "installed": torch, "installed_build": have, "ok": ok},
            "llama": {"want": builds[0], "why": builds_why, "chain": builds},
            "driver": hd.driver_advice(hw["os"], hw["gpus"])}


def fit(hw: dict) -> dict:
    """Which model for each job, and where the assistant runs, given this machine."""
    from .hwdetect import _ver
    g = hw["gpu"] or {}
    gpu_gb = g.get("memory_gb", 0)
    big = gpu_gb >= 16 or hw["ram_gb"] >= 32
    apple = g.get("vendor") == "apple"
    cc = _ver(g.get("compute_cap")) or (0, 0)
    # Whisper runs on CTranslate2: an NVIDIA card or the processor (it has no Apple GPU backend). large-v3 needs
    # about 4.5 GB in float16 and half that in int8; float16 needs compute capability 7.0. On the processor,
    # large-v3-turbo is several times faster for a little accuracy.
    if g.get("vendor") == "nvidia" and gpu_gb >= 4:
        whisper = "large-v3"
        compute = ("float16" if gpu_gb >= 6 else "int8_float16") if cc >= (7, 0) else "int8"
    else:
        whisper, compute = "large-v3-turbo", "int8"
    return {
        # Nothing runs beside the assistant, so it gets the whole card: E2B for a 4 GB laptop card, E4B from 6 GB.
        # With no card (or a smaller one) it runs on the processor, where the smaller E2B answers about twice as fast.
        "assistant": ("gemma-4-E4B" if (hw["ram_gb"] >= 16 if apple else gpu_gb >= 6) else "gemma-4-E2B"),
        "assistant_gpu_layers": "auto",  # llama-server puts as many layers on the GPU as fit
        "extraction": "gemma-4-26B" if big else "gemma-4-E4B",
        "extraction_offload": ("-ngl 999 --n-cpu-moe 8; -ngl 999 --n-cpu-moe 14; -ngl 999 --n-cpu-moe 22"
                               if gpu_gb >= 16 else "-ngl 999" if (hw["gpu"] or {}).get("vendor") == "apple"
                               else "-ngl 0"),
        "whisper": whisper,
        "whisper_compute": compute,
    }


def recommended_models(choice: dict) -> list[str]:
    """The catalog entries a machine with this fit should have."""
    return list(dict.fromkeys([choice["assistant"], choice["extraction"], f"whisper-{choice['whisper']}",
                               "pyannote-community-1"]))


# ---------------------------------------------------------------------------------------------- downloads
def download(url: str, dest: Path, sha256: str | None, size: int | None = None, headers: dict | None = None,
             log=print) -> Path:
    """Download to dest (resuming a partial download), and keep it only if its SHA-256 matches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": "ontherecord-setup",
                                               **({"Range": f"bytes={have}-"} if have else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        mode = "ab" if have and r.status == 206 else "wb"
        total = int(r.headers.get("Content-Length") or 0) + (have if mode == "ab" else 0)
        done, last = (have if mode == "ab" else 0), -1
        with open(part, mode) as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                pct = int(100 * done / total) if total else 0
                if pct // 10 != last // 10:
                    log(f"  {dest.name}: {done / 1e6:,.0f} MB" + (f" ({pct}%)" if total else ""))
                    last = pct
    if size and part.stat().st_size != size:
        raise OSError(f"{dest.name}: got {part.stat().st_size} bytes, expected {size}")
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as fh:
            while chunk := fh.read(1 << 22):
                h.update(chunk)
        if h.hexdigest() != sha256:
            part.unlink()
            raise OSError(f"{dest.name}: SHA-256 does not match the published one; the download was deleted")
    os.replace(part, dest)
    return dest


def _extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")
    if sys.platform != "win32":
        for p in into.rglob("*"):
            if p.is_file() and (p.name.startswith(("llama-", "ffmpeg", "ffprobe")) or p.suffix in (".so", ".dylib")):
                p.chmod(p.stat().st_mode | 0o111)


def llama_devices(server: Path) -> str | None:
    """What a llama-server build can run on (`--list-devices`), or None when it does not start."""
    try:
        r = subprocess.run([str(server), "--list-devices"], capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout + r.stderr if r.returncode == 0 else None


def _sees_card(build: str, devices: str) -> bool:
    if build.startswith("cuda"):
        return "CUDA0" in devices
    if build.startswith("vulkan"):
        return "Vulkan0" in devices
    return True  # the processor builds, and Metal (which lists itself only on Apple silicon)


def ensure_llama(bin_dir: Path, hw: dict, log=print, builds: list[str] | None = None, verify: bool = True) -> Path:
    """llama-server: the first build in the list (default: hwdetect's choice for this machine, best first) that
    starts and sees the graphics card. Each build gets its own folder, so the path names the build."""
    exe = "llama-server.exe" if hw["os"] == "windows" else "llama-server"
    if builds is None:
        from .hwdetect import llama_build
        builds = llama_build(hw["os"], hw["arch"], hw.get("gpus") or [])[0]
    for i, build in enumerate(builds):
        target = bin_dir / f"llama-{LLAMA_BUILD}-{build}"
        found = list(target.rglob(exe)) if target.is_dir() else []
        if not found:
            for name in LLAMA_BUILDS[hw["os"]][build]:
                sha, size = LLAMA_ASSETS[name]
                url = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/{name}"
                log(f"llama.cpp {LLAMA_BUILD}: {name} ({size / 1e6:.0f} MB)")
                arc = download(url, bin_dir / "downloads" / name, sha, size, log=log)
                _extract(arc, target)
                arc.unlink()
            found = list(target.rglob(exe))
            if not found:
                raise RuntimeError(f"no {exe} in the llama.cpp download")
            # the CUDA runtime libraries ship in a separate archive; put them beside the server
            for lib in target.rglob("*"):
                if (lib.is_file() and lib.parent != found[0].parent
                        and (lib.suffix in (".dll", ".so") or ".so." in lib.name)
                        and not (found[0].parent / lib.name).exists()):
                    shutil.copy2(lib, found[0].parent / lib.name)
        if not verify or i == len(builds) - 1:
            return found[0]
        devices = llama_devices(found[0])
        if devices is not None and _sees_card(build, devices):
            return found[0]
        log(f"llama.cpp's {build} build " + ("does not start here" if devices is None else "does not see the card")
            + f"; trying {builds[i + 1]}")
    raise RuntimeError("no llama.cpp build to try")


def ensure_ffmpeg(bin_dir: Path, hw: dict, log=print) -> dict[str, str]:
    """ffmpeg and ffprobe: the system's if both are on PATH, else a pinned download into bin_dir."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}
    exe = ".exe" if hw["os"] == "windows" else ""
    target = bin_dir / "ffmpeg"
    have = {n: next(iter(target.rglob(n + exe)), None) for n in ("ffmpeg", "ffprobe")} if target.is_dir() else {}
    if all(have.get(n) for n in ("ffmpeg", "ffprobe")):
        return {n: str(p) for n, p in have.items()}
    if hw["os"] == "macos":
        arch = "arm64" if hw["arch"] in ("arm64", "aarch64") else "x86_64"
        for n in ("ffmpeg", "ffprobe"):
            path, sha = MAC_FFMPEG[(arch, n)]
            log(f"ffmpeg 9.0.2: {n} for macOS {arch}")
            _extract(download(f"https://ffmpeg.martin-riedl.de/{path}", bin_dir / "downloads" / f"{n}.zip", sha,
                              log=log), target)
    else:
        name, sha, size = BTBN["win" if hw["os"] == "windows" else "linux"]
        log(f"ffmpeg 8.1.3: {name} ({size / 1e6:.0f} MB)")
        arc = download(f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{BTBN_TAG}/{name}",
                       bin_dir / "downloads" / name, sha, size, log=log)
        _extract(arc, target)
        arc.unlink()
    return {n: str(next(target.rglob(n + exe))) for n in ("ffmpeg", "ffprobe")}


def hf_cache() -> Path:
    return Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME", Path.home() / ".cache" /
                                                                      "huggingface")) / "hub")


def _file_hash(path: Path, kind: str) -> str:
    """sha256 of a file, or (kind 'git') git's blob id, which Hugging Face lists for files too small for LFS."""
    h = hashlib.sha256() if kind == "sha256" else hashlib.sha1(b"blob %d\0" % path.stat().st_size)
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def verify_model(name: str, models_root: Path, snapshot: Path | None = None, delete: bool = False) -> list[str]:
    """What is wrong with a downloaded model, checked against the digests written in this package; empty when it is
    exactly the pinned files. With delete, a Hugging Face file that does not match is removed (so the next download
    fetches it again); a GGUF file is only reported, as it may be one the person put there on purpose."""
    m = MODELS[name]
    if m["kind"] == "gguf":
        f = models_root / m["file"]
        if not f.is_file():
            return [f"{m['file']} is not downloaded"]
        return [] if _file_hash(f, "sha256") == m["sha256"] else [f"{m['file']} is not the pinned file"]
    from .model_hashes import FILES
    snap = snapshot or hf_cache() / ("models--" + m["repo"].replace("/", "--")) / "snapshots" / m["rev"]
    if not snap.is_dir():
        return [f"not downloaded at the pinned revision {m['rev'][:12]}"]
    problems = []
    for rel, want in FILES[name].items():
        kind, digest = want.split(":", 1)
        p = snap / rel
        if not p.is_file():
            problems.append(f"{rel} is missing")
        elif _file_hash(p, kind) != digest:
            problems.append(f"{rel} does not match its pinned {kind}")
            if delete:
                blob = p.resolve()
                p.unlink()
                blob.unlink(missing_ok=True)
    return problems


def model_status(models_root: Path) -> dict[str, str | bool]:
    """True when present; False when not; a note when a file has the name but not the size of the pinned one."""
    out: dict[str, str | bool] = {}
    for name, m in MODELS.items():
        if m["kind"] == "gguf":
            f = models_root / m["file"]
            out[name] = (f.is_file() and (f.stat().st_size == m["bytes"]
                                          or f"a different file by that name ({f.stat().st_size / 1e9:.1f} GB)"))
        else:
            snap = hf_cache() / ("models--" + m["repo"].replace("/", "--")) / "snapshots"
            out[name] = snap.is_dir() and any(snap.iterdir())
    return out


def get_model(name: str, models_root: Path, token: str | None = None, python: str | None = None, log=print) -> str:
    m = MODELS[name]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if m["kind"] == "gguf":
        url = f"https://huggingface.co/{m['repo']}/resolve/{m['rev']}/{m['file']}"
        log(f"{name}: {m['file']} ({m['bytes'] / 1e9:.1f} GB) from {m['repo']}")
        return str(download(url, models_root / m["file"], m["sha256"], m["bytes"], headers, log=log))
    if m.get("gated") and not token:
        raise PermissionError(f"{name} is gated: accept its terms on Hugging Face, then give a token (HF_TOKEN)")
    # faster-whisper and pyannote read the Hugging Face cache, so fill it with huggingface_hub, in the Python that
    # runs those stages (it has huggingface_hub, as both libraries depend on it)
    code = ("import json, sys; from huggingface_hub import snapshot_download; "
            "print(snapshot_download(sys.argv[1], revision=sys.argv[2], token=sys.argv[3] or None, "
            "allow_patterns=json.loads(sys.argv[4])))")
    r = subprocess.run([python or sys.executable, "-c", code, m["repo"], m["rev"], token or "",
                        json.dumps(m.get("patterns"))], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{name}: {r.stderr.strip()[-600:]}")
    path = r.stdout.strip().splitlines()[-1]
    bad = verify_model(name, models_root, Path(path), delete=True)
    if bad:
        raise OSError(f"{name}: {'; '.join(bad)}. The files that did not match were deleted; nothing was kept.")
    return path


# ---------------------------------------------------------------------------------------------- the config
def write_config(path: Path, hw: dict, tools: dict, choice: dict, home: Path) -> str:
    """A machine config for a fresh install (the paths a desktop install uses). An existing file is kept."""
    if path.exists():
        return f"kept the existing {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    a = MODELS[choice["assistant"]]
    text = f"""# Written by `ontherecord setup` for this machine ({hw['os']}, {hw['ram_gb']} GB RAM, {(hw['gpu'] or {}).get('name', 'no GPU')}).
workspace: {home / 'workspace'}
archive: {home / 'Archive'}
models_root: {home / 'models'}
tools:
  ffmpeg: {tools['ffmpeg']}
  ffprobe: {tools['ffprobe']}
  llama_server: {tools['llama_server']}
# Secrets (HF_TOKEN=...) live in this file, which is passed to stages and never written to a receipt.
env_file: {path.parent / 'secrets.env'}
assistant:
  model: {a['file']}
  gpu_layers: {choice['assistant_gpu_layers']}
  context: 16384
"""
    path.write_text(text, encoding="utf-8")
    return f"wrote {path}"


def save_token(secrets_file: Path, token: str) -> Path:
    """Keep the Hugging Face token in the secrets file (the machine config's env_file), readable by this user only."""
    p = Path(secrets_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in (p.read_text(encoding="utf-8").splitlines() if p.exists() else [])
             if not ln.startswith("HF_TOKEN=")]
    p.write_text("\n".join(lines + [f"HF_TOKEN={token}"]) + "\n", encoding="utf-8")
    if os.name != "nt":
        p.chmod(0o600)
    return p


def check_hf_token(token: str, model: str = "pyannote-community-1") -> dict:
    """Ask Hugging Face whether the token works and whether it may read the gated speaker model. This talks to the
    network, so it runs only when a person asks (the Setup tab's Check button). The token goes to Hugging Face only."""
    import urllib.error
    m = MODELS[model]
    page = f"https://huggingface.co/{m['repo']}"

    def ask(url: str, method: str = "GET"):
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}",
                                                                  "User-Agent": "ontherecord-setup"})
        return urllib.request.urlopen(req, timeout=20)
    try:
        user = json.loads(ask("https://huggingface.co/api/whoami-v2").read()).get("name")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"token": False, "access": False, "message": "Hugging Face does not accept this token. Make a "
                    "new one with Read access at https://huggingface.co/settings/tokens and paste it again."}
        raise
    try:
        ask(f"{page}/resolve/{m['rev']}/config.yaml", "HEAD")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        return {"token": True, "access": False, "user": user, "page": page,
                "message": f"The token works (signed in as {user}), but this account has not accepted the speaker "
                           f"model's terms yet. Open {page}, sign in as {user}, accept, then check again."}
    return {"token": True, "access": True, "user": user, "page": page,
            "message": f"The token works (signed in as {user}) and may download the speaker model."}


# ---------------------------------------------------------------------------------------------- the desktop
def make_shortcut(exe: Path, icons: Path, os_name: str) -> str:
    """A desktop (and, where there is one, applications menu) entry that starts the GUI and opens it."""
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    if os_name == "windows":
        lnk = desktop / "On the Record.lnk"
        ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
              f"$s.TargetPath = '{exe}'; $s.Arguments = 'serve --open'; $s.WorkingDirectory = '{exe.parent}'; "
              f"$s.IconLocation = '{icons / 'ontherecord.ico'}'; $s.Description = 'On the Record'; $s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        return str(lnk)
    if os_name == "macos":
        cmd = desktop / "On the Record.command"
        cmd.write_text(f'#!/bin/bash\n"{exe}" serve --open\n', encoding="utf-8")
        cmd.chmod(0o755)
        return str(cmd)
    entry = (f"[Desktop Entry]\nType=Application\nName=On the Record\nComment=Transcripts with receipts\n"
             f"Exec=\"{exe}\" serve --open\nIcon={icons / 'ontherecord.png'}\nTerminal=true\n"
             "Categories=AudioVideo;Office;\n")
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    for d in (apps, desktop):
        f = d / "ontherecord.desktop"
        f.write_text(entry, encoding="utf-8")
        f.chmod(0o755)
    subprocess.run(["gio", "set", str(desktop / "ontherecord.desktop"), "metadata::trusted", "true"],
                   capture_output=True)
    return str(desktop / "ontherecord.desktop")


def interactive_models(models_root: Path, choice: dict, secrets_file: Path, python: str, ask=input,
                       log=print) -> None:
    """Offer this machine's models, with sizes; ask for a Hugging Face token for the gated one."""
    status = model_status(models_root)
    todo = [n for n in recommended_models(choice) if status.get(n) is not True]
    if not todo:
        log("All the models this machine needs are here.")
        return
    gb = sum(MODELS[n]["bytes"] for n in todo) / 1e9
    log("\nModels to download:\n" + "\n".join(f"  {n:21} {MODELS[n]['bytes'] / 1e9:5.1f} GB  {MODELS[n]['about']}"
                                               for n in todo))
    if ask(f"Download them now (about {gb:.0f} GB)? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        log("Skipped. Later: `ontherecord models get NAME`.")
        return
    token = os.environ.get("HF_TOKEN")
    if "pyannote-community-1" in todo and not token:
        log("\nThe speaker model (pyannote) needs a free Hugging Face account:\n"
            "  1. Sign in at https://huggingface.co and accept the terms at\n"
            "     https://huggingface.co/pyannote/speaker-diarization-community-1\n"
            "  2. Make a token (Read access) at https://huggingface.co/settings/tokens and paste it here.")
        token = ask("Token (or press Enter to skip the speaker model for now): ").strip() or None
        if token:
            log(f"Saved in {save_token(secrets_file, token)} (readable only by you; never written to a receipt).")
    for n in todo:
        if MODELS[n].get("gated") and not token:
            log(f"Skipping {n}: no token.")
            continue
        try:
            log(f"{n}: {get_model(n, models_root, token, python, log=log)}")
        except Exception as e:  # one failed model should not lose the others
            log(f"{n} FAILED: {e}")
