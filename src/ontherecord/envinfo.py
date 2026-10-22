"""What machine and software produced an output, for the receipt.

Float reductions are not bit-identical across GPU architectures or library versions, so the same audio transcribed
on two machines need not give the same text. A receipt that names the device and the package versions makes that
difference visible in the data instead of in someone's memory.
"""
from __future__ import annotations

import platform
import socket
import subprocess
from importlib.metadata import PackageNotFoundError, version


def package_versions(names: list[str]) -> dict[str, str | None]:
    out = {}
    for n in names:
        try:
            out[n] = version(n)
        except PackageNotFoundError:
            out[n] = None
    return out


def gpu() -> dict | None:
    """The first NVIDIA GPU via nvidia-smi (no torch import needed), else Apple Silicon, else None."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            name, mem, driver = [s.strip() for s in r.stdout.strip().splitlines()[0].split(",")][:3]
            return {"vendor": "nvidia", "name": name, "memory": mem, "driver": driver}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return {"vendor": "apple", "name": "Apple Silicon (Metal)", "memory": "unified", "driver": None}
    return None


def tool_version(cmd: list[str]) -> str | None:
    """First line of `<tool> --version` (or whatever argument list is given)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        lines = (r.stdout or r.stderr).strip().splitlines()
        return lines[0].strip() if lines else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def code_version() -> dict:
    """This package's version and, in a git checkout, the commit and whether it had uncommitted changes."""
    from pathlib import Path

    from . import __version__
    root = Path(__file__).resolve().parents[2]
    out = {"version": __version__, "commit": None, "dirty": None}
    if (root / ".git").exists():
        def git(*args: str) -> str | None:
            try:
                r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
                return r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        commit = git("rev-parse", "--short=12", "HEAD")
        status = git("status", "--porcelain", "--untracked-files=no")
        out.update(commit=commit, dirty=bool(status) if commit else None)
    return out


def environment(packages: list[str]) -> dict:
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "ontherecord": code_version(),
        "packages": package_versions(packages),
        "gpu": gpu(),
    }
