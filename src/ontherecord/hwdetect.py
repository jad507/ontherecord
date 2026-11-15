"""What graphics hardware this machine has, and which builds of PyTorch and llama.cpp suit it.

Standard library only, so the installers can run it before anything else is installed:

    uv run --no-project --python 3.12 src/ontherecord/hwdetect.py --torch-flavor    # cu128, cu126 or cpu
    python src/ontherecord/hwdetect.py --json                                       # everything, with the reasons

Nobody needs the CUDA Toolkit. PyTorch's wheels and llama.cpp's archives bring the CUDA libraries they use; what the
machine needs is an NVIDIA driver new enough for them. `nvidia-smi` reports the newest CUDA the driver supports and
each card's compute capability, and those two numbers decide everything here:

- PyTorch 2.11 for CUDA 12.8 has kernels for compute capability 7.5 to 12.0 (Turing to Blackwell); for CUDA 12.6,
  5.0 to 9.0 (Maxwell to Hopper, no Blackwell). So a GTX 10-series card needs cu126, and an RTX 50-series card
  needs cu128 and a driver that supports CUDA 12.8. (PyTorch's .ci/manywheel/build_cuda.sh at v2.11.0.)
- PyTorch's CUDA 13 build stays out for now: faster-whisper's CTranslate2 (4.8.3, the newest) loads CUDA 12's
  libcublas.so.12, and cuDNN's CUDA 12 and CUDA 13 wheels install the same files, so the two cannot share one
  environment. CUDA 12 builds run on CUDA 13 drivers, so a new driver loses nothing.
- llama.cpp (the assistant and comment extraction) does go to CUDA 13.4 when the driver supports it and the card is
  Turing or newer (CUDA 13 dropped older cards); else its CUDA 12 build; else Vulkan, which needs no CUDA at all.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys

DRIVER_URL = "https://www.nvidia.com/en-us/drivers/"


def os_name() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(m, m)


def _ver(text: str | None) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _run(cmd: list[str], timeout: float = 20) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_driver_cuda(smi_header: str) -> tuple[int, int] | None:
    """The newest CUDA the driver supports, from nvidia-smi's banner ("CUDA Version: 12.8", or since the 610
    drivers "CUDA UMD Version: 13.4")."""
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", smi_header or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_gpus(csv: str, cuda: tuple[int, int] | None) -> list[dict]:
    """Rows of `nvidia-smi --query-gpu=name,memory.total,memory.used,compute_cap,driver_version` (csv, no units)."""
    gpus = []
    for line in (csv or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name, total, used, cc, driver = parts[:5]
        try:
            total_mb, used_mb = float(total), float(used)
        except ValueError:
            continue
        gpus.append({"vendor": "nvidia", "name": name, "memory_gb": round(total_mb / 1024),
                     "free_gb": round((total_mb - used_mb) / 1024, 1), "compute_cap": cc if _ver(cc) else None,
                     "driver": driver, "cuda": f"{cuda[0]}.{cuda[1]}" if cuda else None})
    return gpus


def nvidia_gpus() -> list[dict]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    out = _run([smi, "--query-gpu=name,memory.total,memory.used,compute_cap,driver_version",
                "--format=csv,noheader,nounits"])
    if out is None:  # drivers older than about 510 have no compute_cap field: leave it empty
        out = _run([smi, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"])
        out = "\n".join(re.sub(r",([^,]*)$", r",,\1", r) for r in (out or "").splitlines())
    return parse_gpus(out, parse_driver_cuda(_run([smi]) or ""))


def other_gpus() -> list[dict]:
    """AMD and Intel graphics, by vendor (Linux from /sys, Windows from WMI). Names only where they are cheap."""
    found = []
    if os_name() == "linux":
        vendors = {"0x1002": "amd", "0x8086": "intel"}
        try:
            cards = sorted(os.listdir("/sys/class/drm"))
        except OSError:
            cards = []
        for c in cards:
            if not re.fullmatch(r"card\d+", c):
                continue
            try:
                with open(f"/sys/class/drm/{c}/device/vendor", encoding="ascii") as fh:
                    v = vendors.get(fh.read().strip())
            except OSError:
                continue
            if v and not any(g["vendor"] == v for g in found):
                found.append({"vendor": v, "name": {"amd": "AMD graphics", "intel": "Intel graphics"}[v]})
    elif os_name() == "windows":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"]) or ""
        for name in (n.strip() for n in out.splitlines()):
            if re.search(r"AMD|Radeon", name, re.I):
                found.append({"vendor": "amd", "name": name})
            elif re.search(r"Intel", name, re.I):
                found.append({"vendor": "intel", "name": name, "discrete": bool(re.search(r"\bArc\b", name))})
    return found


def best_nvidia(gpus: list[dict]) -> dict | None:
    nv = [g for g in gpus if g.get("vendor") == "nvidia"]
    return max(nv, key=lambda g: g.get("memory_gb") or 0) if nv else None


# ---------------------------------------------------------------------------------------------- what to install
def torch_flavor(system: str, gpus: list[dict]) -> tuple[str, str]:
    """Which PyTorch build (a dependency group in pyproject.toml) suits this machine, and why. macOS always uses
    PyPI's build, which runs on the Apple GPU, so its installer does not ask."""
    if system == "macos":
        return "cu128", "macOS: PyPI's PyTorch, which uses the Apple GPU (the group name does not matter here)"
    g = best_nvidia(gpus)
    if not g:
        return "cpu", "no NVIDIA graphics card: PyTorch for the processor (much smaller than the CUDA builds)"
    cc, cuda = _ver(g.get("compute_cap")), _ver(g.get("cuda"))
    if not cuda or cuda < (12, 0):
        return "cpu", (f"the NVIDIA driver ({g.get('driver')}) is too old for CUDA 12; update it and run the "
                       "installer again to use the card")
    if cc and cc < (7, 5):
        return "cu126", (f"{g['name']} (compute capability {g['compute_cap']}) is older than Turing, which only "
                         "PyTorch's CUDA 12.6 build still supports")
    if cuda >= (12, 8):
        return "cu128", f"{g['name']} with a driver that supports CUDA {g['cuda']}: PyTorch's CUDA 12.8 build"
    if cc and cc >= (10, 0):
        return "cpu", (f"{g['name']} needs a driver that supports CUDA 12.8 (this one supports {g['cuda']}); "
                       "update it and run the installer again")
    return "cu126", (f"the driver supports CUDA {g['cuda']}, not yet 12.8: PyTorch's CUDA 12.6 build "
                     "(a newer driver lets the installer use 12.8)")


def llama_build(system: str, machine_arch: str, gpus: list[dict]) -> tuple[list[str], str]:
    """Which llama.cpp builds to try, best first, and why the first. Setup falls back down the list when a build
    cannot see the card (see bootstrap.ensure_llama)."""
    if system == "macos":
        return [f"macos-{'arm64' if machine_arch == 'arm64' else 'x64'}"], "macOS: Metal on Apple silicon"
    g = best_nvidia(gpus)
    arm = machine_arch == "arm64"
    cpu = "cpu-arm64" if arm else "cpu"
    if g:
        cc, cuda = _ver(g.get("compute_cap")), _ver(g.get("cuda"))
        chain, why = [], ""
        if cuda and cuda >= (13, 4) and (cc is None or cc >= (7, 5)):
            chain.append("cuda-13.4-arm64" if arm else "cuda-13.4")
            why = f"{g['name']} with a driver that supports CUDA {g['cuda']}: llama.cpp's CUDA 13.4 build"
        cuda12 = (12, 4) if system == "windows" else (12, 8)
        if not arm and cuda and cuda >= cuda12:
            chain.append("cuda-12")
            why = why or (f"{g['name']}: llama.cpp's CUDA {cuda12[0]}.{cuda12[1]} build"
                          + (" (CUDA 13 needs a Turing or newer card)" if cc and cc < (7, 5) else
                             f" (its CUDA 13.4 build needs a driver that supports 13.4; this one supports {g['cuda']})"))
        if not (system == "windows" and arm):
            chain.append("vulkan-arm64" if arm else "vulkan")
            why = why or f"{g['name']}: llama.cpp's Vulkan build (the driver is too old for its CUDA builds)"
        return chain + [cpu], why
    # Integrated Intel graphics are left to the processor: llama.cpp is rarely faster on them
    if any(o["vendor"] == "amd" or o.get("discrete") for o in gpus) and not (system == "windows" and arm):
        return ["vulkan-arm64" if arm else "vulkan", cpu], "AMD or Intel Arc graphics: llama.cpp's Vulkan build"
    return [cpu], "no graphics card it can use: llama.cpp for the processor"


def driver_advice(system: str, gpus: list[dict]) -> dict | None:
    """What to do about the NVIDIA driver, if anything: 'needed' (the card cannot be used), 'recommended' (it is
    used, but by an older build), or 'optional' (llama.cpp's CUDA 13.4 build waits for a newer driver)."""
    g = best_nvidia(gpus)
    if not g or system == "macos":
        return None
    cc, cuda = _ver(g.get("compute_cap")), _ver(g.get("cuda"))
    how = (f"Install the newest driver for your card from {DRIVER_URL}" if system == "windows" else
           f"Install the newest driver for your card (Ubuntu: `sudo ubuntu-drivers install`, then restart; others: "
           f"your distribution's NVIDIA driver package, or {DRIVER_URL})")
    tail = ". You do not need the CUDA Toolkit. Afterwards, run the installer again so it picks the faster builds."
    have = f"Your driver ({g.get('driver')}) supports CUDA {g.get('cuda') or 'older than 12'}"
    if not cuda or cuda < (12, 0) or (cc and cc >= (10, 0) and cuda < (12, 8)):
        return {"level": "needed", "text": f"{have}, which is too old for {g['name']}. {how}{tail}"}
    if cuda < (12, 8):
        return {"level": "recommended", "text": f"{have}; 12.8 or later is better. {how}{tail}"}
    if cuda < (13, 4) and (cc is None or cc >= (7, 5)):
        return {"level": "optional", "text": f"{have}. A driver that supports CUDA 13.4 lets the language models "
                                             f"use llama.cpp's CUDA 13 build. {how}{tail}"}
    return None


def ct2_compute_types(compute_cap: str | None) -> list[str]:
    """The compute types CTranslate2 offers on a CUDA card of this compute capability. From CTranslate2 4.8.3
    (read Nov 14, 2026): python/cpp/module.cc get_supported_compute_types, with src/cuda/utils.cc deciding float16
    (gpu_has_fp16_tensor_cores: major >= 7) and int8 (gpu_supports_int8: major > 6, or exactly 6.1). bfloat16 and
    int16 are left out: no stage here offers them."""
    cc = _ver(compute_cap) or (0, 0)
    fp16, int8 = cc[0] >= 7, cc[0] > 6 or cc == (6, 1)
    return (["float16"] if fp16 else []) + (["int8_float16"] if fp16 and int8 else []) \
        + (["int8", "int8_float32"] if int8 else []) + ["float32"]


_MACHINE: dict | None = None


def machine() -> dict:
    """What stages settle `auto` against before a key is made (Stage.resolve): the NVIDIA cards, in CUDA's order,
    and whether this is an Apple Silicon Mac (PyTorch's mps). Detected once per process."""
    global _MACHINE
    if _MACHINE is None:
        _MACHINE = {"cuda": [{"name": g["name"], "compute_cap": g["compute_cap"]} for g in nvidia_gpus()],
                    "mps": os_name() == "macos" and arch() == "arm64"}
    return _MACHINE


def detect() -> dict:
    system, a = os_name(), arch()
    gpus = nvidia_gpus() + other_gpus()
    flavor, flavor_why = torch_flavor(system, gpus)
    builds, build_why = llama_build(system, a, gpus)
    return {"os": system, "arch": a, "gpus": gpus, "torch_flavor": flavor, "torch_why": flavor_why,
            "llama_builds": builds, "llama_why": build_why, "driver": driver_advice(system, gpus)}


if __name__ == "__main__":
    d = detect()
    if "--torch-flavor" in sys.argv:
        print(d["torch_flavor"])
        print(f"PyTorch build: {d['torch_flavor']} ({d['torch_why']})", file=sys.stderr)
        if d["driver"] and d["driver"]["level"] != "optional":
            print(f"NOTE: {d['driver']['text']}", file=sys.stderr)
    else:
        print(json.dumps(d, indent=2))
