"""The machine config: everything that differs between machines, so the repository itself never has to.

Recipes say *what* to run (stages and settings) and travel between machines unchanged. The machine config says
*where things are on this machine*: the workspace, which Python runs which group of stages, where ffmpeg and
llama-server live, where model weights are, and which file holds secrets such as a Hugging Face token. Secret values
are passed to stage processes and never written into a receipt.

Looked up, first match wins: $ONTHERECORD_CONFIG, ./machine.yaml, then the per-user config directory
(~/.config/ontherecord on Linux, ~/Library/Application Support/ontherecord on macOS, %APPDATA%\\ontherecord on
Windows). With no file at all, everything runs in the current Python and the workspace is ./workspace.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def user_config_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ontherecord"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ontherecord"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ontherecord"


def find_config_file() -> Path | None:
    env = os.environ.get("ONTHERECORD_CONFIG")
    candidates = [Path(env)] if env else []
    candidates += [Path.cwd() / "machine.yaml", user_config_dir() / "machine.yaml"]
    return next((p for p in candidates if p.is_file()), None)


def _path(value) -> Path | None:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))) if value else None


@dataclass
class MachineConfig:
    source: Path | None = None
    workspace: Path = field(default_factory=lambda: Path.cwd() / "workspace")
    envs: dict[str, Path] = field(default_factory=dict)       # stage group -> python interpreter
    tools: dict[str, str] = field(default_factory=dict)       # ffmpeg, llama_server, ...
    js_runtime: dict[str, str] = field(default_factory=dict)  # yt-dlp's YouTube challenge solver, {bun: path}
    models_root: Path | None = None
    env_file: Path | None = None                              # KEY=VALUE lines passed to stage processes
    stage_env: dict[str, str] = field(default_factory=dict)   # plain environment settings for stage processes
    cookies_file: Path | None = None                          # yt-dlp cookies; never recorded

    def python_for(self, group: str) -> str:
        """The interpreter for a stage group; the current one when the config names none."""
        p = self.envs.get(group)
        return str(p) if p else sys.executable

    def tool(self, name: str) -> str:
        """A binary named in the config, else found on PATH, else the bare name (so the error names it)."""
        return self.tools.get(name) or shutil.which(name) or name

    def model_path(self, value: str) -> Path:
        """A model file: absolute, or relative to models_root."""
        p = _path(value)
        if not p.is_absolute() and self.models_root:
            p = self.models_root / p
        return p

    def secret_env(self) -> dict[str, str]:
        """Values from env_file. Read only to hand to a stage process."""
        out: dict[str, str] = {}
        if self.env_file and self.env_file.is_file():
            for line in self.env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.removeprefix("export ").split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out

    def describe(self) -> dict:
        """What a receipt may say about the machine config: paths and names, never secret values."""
        return {
            "config_file": str(self.source) if self.source else None,
            "envs": {k: str(v) for k, v in self.envs.items()},
            "tools": dict(self.tools), "models_root": str(self.models_root) if self.models_root else None,
            "env_file": "set" if self.env_file else None, "stage_env": dict(self.stage_env),
        }


def load_config(path: str | Path | None = None) -> MachineConfig:
    src = Path(path) if path else find_config_file()
    raw = yaml.safe_load(src.read_text(encoding="utf-8")) if src else {}
    raw = raw or {}
    cfg = MachineConfig(source=src)
    if raw.get("workspace"):
        cfg.workspace = _path(raw["workspace"])
    cfg.envs = {k: _path(v) for k, v in (raw.get("envs") or {}).items()}
    cfg.tools = {k: str(_path(v)) if ("/" in str(v) or "\\" in str(v)) else str(v)
                 for k, v in (raw.get("tools") or {}).items()}
    cfg.js_runtime = {k: str(_path(v)) for k, v in (raw.get("js_runtime") or {}).items()}
    cfg.models_root = _path(raw.get("models_root"))
    cfg.env_file = _path(raw.get("env_file"))
    cfg.stage_env = {k: str(v) for k, v in (raw.get("stage_env") or {}).items()}
    cfg.cookies_file = _path(raw.get("cookies_file"))
    return cfg
