"""First-run setup: what this machine can run, the helper programs it needs, the models, and a machine config.

    ontherecord setup              check, fetch ffmpeg and llama-server if missing, write the machine config
    ontherecord setup --check      only report
    ontherecord models             the model catalog, what is downloaded, what fits this machine
    ontherecord models get NAME    download one (the speech models need a Hugging Face token; see below)

Every download is pinned and checked: llama.cpp at build b11249 and the macOS ffmpeg at 9.0.2 against SHA-256
digests written below, Windows and Linux ffmpeg against the digest GitHub publishes for the file, and each model at
a fixed Hugging Face revision against the SHA-256 Hugging Face stores for it. A file that does not match is deleted.

The hardware-fit rule is deliberately simple: which model sizes fit in GPU memory (or, on a Mac, in shared memory),
with the CPU as the fallback. The choice is written to the machine config, and every receipt records the model.
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
    "cudart-llama-bin-win-cuda-12.4-x64.zip": ("8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6", 391443627),
    "llama-b11249-bin-macos-arm64.tar.gz": ("d5fb90058f8d0c73c1e687b406dc1a6f53c64de725d3e642ae210db90c0b6613", 11763877),
    "llama-b11249-bin-macos-x64.tar.gz": ("8f446f75e176b25580faeba7f7d78dc624a7b5360a931655f2d0eb1befad5096", 11316747),
    "llama-b11249-bin-ubuntu-cuda-12.8-x64.tar.gz": ("a550c6157cc587d7c5845669d66f3f57b856c6fd4987fa706393d0d9f7e0b66b", 172238409),
    "llama-b11249-bin-ubuntu-vulkan-x64.tar.gz": ("f8d39878bcc9144b3bda8cbd75d8703b1d6466ab9b8f25e14840c746960b75c8", 31348947),
    "llama-b11249-bin-ubuntu-x64.tar.gz": ("d1aad0580a1f48cb55a9e8f7de0d6eed5e6415acfce65b677023f1fcd5c06083", 17405122),
    "llama-b11249-bin-win-cpu-x64.zip": ("ba0beb2076250970c113b90c0e60611909bc1e7e973ee92a416756a64dd0763c", 19163703),
    "llama-b11249-bin-win-cuda-12.4-x64.zip": ("500ea1076423d729b9537083b045c6f2dbeedb97f3bb02661022c75251e5c3c3", 264527344),
    "llama-b11249-bin-win-vulkan-x64.zip": ("32cf3b0e3477263c959ae17a08882f9b1e091c4d6d93ccc0631efd2185f5f87d", 33077226),
}
MAC_FFMPEG = {  # ffmpeg 9.0.2 from ffmpeg.martin-riedl.de, with the digests it publishes
    ("arm64", "ffmpeg"): ("download/macos/arm64/1789931890_9.0.2/ffmpeg.zip", "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
    ("arm64", "ffprobe"): ("download/macos/arm64/1789931890_9.0.2/ffprobe.zip", "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"),
    ("x86_64", "ffmpeg"): ("download/macos/amd64/1789931006_9.0.2/ffmpeg.zip", "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
    ("x86_64", "ffprobe"): ("download/macos/amd64/1789931006_9.0.2/ffprobe.zip", "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd"),
}
BTBN = {"win": "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip", "linux": "ffmpeg-n8.1-latest-linux64-lgpl-8.1.tar.xz"}

# The models On the Record offers, each pinned. gguf models are single files in models_root; hf models go to the
# Hugging Face cache, where faster-whisper and pyannote look for them.
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
                         "rev": "edaa852ec7e145841d8ffdb056a99866b5f0a478", "bytes": 3_100_000_000,
                         "role": "speech", "needs_gb": 6, "about": "Whisper large-v3 for faster-whisper (transcription)"},
    "pyannote-community-1": {"kind": "hf", "repo": "pyannote/speaker-diarization-community-1",
                             "rev": "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee", "bytes": 40_000_000, "gated": True,
                             "role": "speech", "needs_gb": 2,
                             "about": "pyannote's speaker diarization (who speaks when). Gated: accept its terms at "
                                      "https://huggingface.co/pyannote/speaker-diarization-community-1 first"},
}


# ---------------------------------------------------------------------------------------------- the machine
def home_dir() -> Path:
    """Where a desktop install keeps its things: one visible folder."""
    return Path(os.environ.get("ONTHERECORD_HOME", Path.home() / "OnTheRecord"))


def hardware() -> dict:
    import psutil
    hw = {"os": {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"), "arch": platform.machine().lower(),
          "ram_gb": round(psutil.virtual_memory().total / 1e9), "gpu": None}
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
            if out:
                name, mem, drv = [x.strip() for x in out[0].split(",")]
                hw["gpu"] = {"vendor": "nvidia", "name": name, "memory_gb": round(int(mem) / 1024), "driver": drv}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    if hw["os"] == "macos" and hw["arch"] in ("arm64", "aarch64"):
        hw["gpu"] = {"vendor": "apple", "name": "Apple GPU (shared memory)", "memory_gb": int(hw["ram_gb"] * 0.7)}
    return hw


def fit(hw: dict) -> dict:
    """Which model for each job, and where the assistant runs, given this machine."""
    gpu_gb = (hw["gpu"] or {}).get("memory_gb", 0)
    big = gpu_gb >= 16 or hw["ram_gb"] >= 32
    return {
        "assistant": "gemma-4-E4B" if hw["ram_gb"] >= 12 else "gemma-4-E2B",
        # on a GPU shared with Whisper and the 26B, the assistant stays on the CPU; a Mac shares one memory anyway
        "assistant_gpu_layers": 999 if (hw["gpu"] or {}).get("vendor") == "apple" else 0,
        "extraction": "gemma-4-26B" if big else "gemma-4-E4B",
        "extraction_offload": ("-ngl 999 --n-cpu-moe 8; -ngl 999 --n-cpu-moe 14; -ngl 999 --n-cpu-moe 22"
                               if gpu_gb >= 16 else "-ngl 999" if (hw["gpu"] or {}).get("vendor") == "apple"
                               else "-ngl 0"),
        "whisper": "large-v3" if gpu_gb >= 6 else "large-v3-turbo",
        "whisper_compute": "float16" if (hw["gpu"] or {}).get("vendor") == "nvidia" else "int8",
    }


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


def llama_assets(hw: dict) -> list[str]:
    nvidia = (hw["gpu"] or {}).get("vendor") == "nvidia"
    if hw["os"] == "windows":
        return ([f"llama-{LLAMA_BUILD}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
                if nvidia else [f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip"])
    if hw["os"] == "macos":
        return [f"llama-{LLAMA_BUILD}-bin-macos-{'arm64' if hw['arch'] in ('arm64', 'aarch64') else 'x64'}.tar.gz"]
    return ([f"llama-{LLAMA_BUILD}-bin-ubuntu-cuda-12.8-x64.tar.gz",
             f"cudart-llama-{LLAMA_BUILD}-bin-ubuntu-cuda-12.8-x64.tar.gz"] if nvidia
            else [f"llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"])


def ensure_llama(bin_dir: Path, hw: dict, log=print, assets: list[str] | None = None) -> Path:
    exe = "llama-server.exe" if hw["os"] == "windows" else "llama-server"
    target = bin_dir / f"llama-{LLAMA_BUILD}"
    found = list(target.rglob(exe)) if target.is_dir() else []
    if found:
        return found[0]
    for name in assets or llama_assets(hw):
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
        if lib.is_file() and lib.parent != found[0].parent and lib.suffix in (".dll", ".so") or ".so." in lib.name:
            if lib.is_file() and not (found[0].parent / lib.name).exists():
                shutil.copy2(lib, found[0].parent / lib.name)
    return found[0]


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
        name = BTBN["win" if hw["os"] == "windows" else "linux"]
        rel = json.loads(urllib.request.urlopen("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest",
                                                timeout=60).read())
        asset = next(a for a in rel["assets"] if a["name"] == name)
        log(f"ffmpeg 8.1: {name} ({asset['size'] / 1e6:.0f} MB)")
        arc = download(asset["browser_download_url"], bin_dir / "downloads" / name,
                       asset["digest"].split(":", 1)[1], asset["size"], log=log)
        _extract(arc, target)
        arc.unlink()
    return {n: str(next(target.rglob(n + exe))) for n in ("ffmpeg", "ffprobe")}


def model_status(models_root: Path) -> dict[str, str | bool]:
    """True when present; False when not; a note when a file has the name but not the size of the pinned one."""
    out: dict[str, str | bool] = {}
    for name, m in MODELS.items():
        if m["kind"] == "gguf":
            f = models_root / m["file"]
            out[name] = (f.is_file() and (f.stat().st_size == m["bytes"]
                                          or f"a different file by that name ({f.stat().st_size / 1e9:.1f} GB)"))
        else:
            cache = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME", Path.home() / ".cache" /
                                                                                 "huggingface")) / "hub")
            snap = cache / ("models--" + m["repo"].replace("/", "--")) / "snapshots"
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
    code = ("import sys; from huggingface_hub import snapshot_download; "
            "print(snapshot_download(sys.argv[1], revision=sys.argv[2], token=sys.argv[3] or None))")
    r = subprocess.run([python or sys.executable, "-c", code, m["repo"], m["rev"], token or ""],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{name}: {r.stderr.strip()[-600:]}")
    return r.stdout.strip().splitlines()[-1]


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


def save_token(cfg_dir: Path, token: str) -> Path:
    """Keep the Hugging Face token in the secrets file, readable by this user only."""
    p = cfg_dir / "secrets.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in (p.read_text(encoding="utf-8").splitlines() if p.exists() else [])
             if not ln.startswith("HF_TOKEN=")]
    p.write_text("\n".join(lines + [f"HF_TOKEN={token}"]) + "\n", encoding="utf-8")
    if os.name != "nt":
        p.chmod(0o600)
    return p


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


def interactive_models(models_root: Path, choice: dict, cfg_dir: Path, python: str, ask=input, log=print) -> None:
    """Offer this machine's models, with sizes; ask for a Hugging Face token for the gated one."""
    want = [choice["assistant"], choice["extraction"], "whisper-large-v3", "pyannote-community-1"]
    status = model_status(models_root)
    todo = [n for n in dict.fromkeys(want) if status.get(n) is not True]
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
            log(f"Saved in {save_token(cfg_dir, token)} (readable only by you; never written to a receipt).")
    for n in todo:
        if MODELS[n].get("gated") and not token:
            log(f"Skipping {n}: no token.")
            continue
        try:
            log(f"{n}: {get_model(n, models_root, token, python, log=log)}")
        except Exception as e:  # one failed model should not lose the others
            log(f"{n} FAILED: {e}")
