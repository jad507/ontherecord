"""What a stage is.

A stage turns named input artifacts into named output artifacts for one item (one meeting, one clip) at a time.
It declares its settings with defaults and help text, so the GUI can draw a form for it and the runner can fill in
and record every default. Nothing a stage does may depend on a value that is not either a setting or an input:
that is what makes the cache key complete.

Stage classes are imported by the runner just to read these declarations, so heavy imports (torch, pyannote,
yt_dlp) belong inside setup() and run(), never at module level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What an artifact is, so a stage can say "any segments" and readers can find data without knowing stage names.
# segments and annotations are JSON formats checked on write (artifacts.py); speakers is RTTM. media, audio, info,
# table (CSV) and other are not checked. table and other are opaque: they are wired by name, not by type.
TYPES = ("media", "audio", "info", "segments", "speakers", "annotations", "table", "other")
OPAQUE = ("table", "other")


class UserError(Exception):
    """A failure the person can act on: what went wrong, in plain words, and what to do about it."""
    def __init__(self, message: str, fix: str = ""):
        super().__init__(f"{message} {fix}".strip())
        self.message, self.fix = message, fix


def torch_device(want: str, machine: dict) -> str:
    """Settle a PyTorch stage's device: auto is cuda, then mps, then cpu, from what the machine has."""
    if want != "auto":
        return want
    return "cuda" if machine.get("cuda") else ("mps" if machine.get("mps") else "cpu")


@dataclass
class Setting:
    default: Any
    help: str
    choices: list | None = None
    kind: str = "value"          # "value", or "file": a path whose *contents* go into the cache key and the receipt
    in_key: bool = True          # False only for settings that cannot change any output byte


@dataclass
class Check:
    """A measured property of an output, judged ok / warn / fail. A fail stops this item's later stages."""
    name: str
    status: str                  # ok | warn | fail
    value: Any
    message: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "value": self.value, "message": self.message}


@dataclass
class Item:
    """One unit of data moving through a run: a video URL or a local file."""
    id: str
    url: str | None = None
    path: str | None = None
    title: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Result:
    """What run() returns. `files` maps each output artifact to a file name inside the output directory, or to an
    absolute path outside it (a local source file the stage only points at)."""
    files: dict[str, str]
    metrics: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    tools: dict = field(default_factory=dict)     # external programs and models used, with versions or hashes
    device: str | None = None                     # what actually computed it: "cuda:0", "cpu", "mps", "server"


@dataclass
class Context:
    """Handed to setup() and run(): the machine config, a scratch directory for this stage in this run (server
    logs and the like, never outputs), and somewhere to print progress."""
    config: Any
    scratch: Path
    log: Any = print


class Stage:
    name: str = ""
    version: str = "1"           # bump whenever a change to the code could change an output
    label: str = ""              # for people: "Diarize speakers"
    group: str = "core"          # which Python environment runs it (machine config `envs`)
    gpu: bool = False
    needs: dict[str, str] = {}   # input name -> artifact type; filled from the latest earlier step making that type
    makes: dict[str, str] = {}   # output name -> artifact type
    headline: tuple[str, str] | None = None   # (metric, wording) that Compare shows for this stage, e.g. ("words", "words")
    packages: tuple[str, ...] = ()   # distributions whose versions the receipt records
    settings: dict[str, Setting] = {}

    @classmethod
    def resolve(cls, settings: dict, machine: dict) -> dict:
        """Settle values that depend on the machine (device: auto, a compute type the card lacks) before the key is
        made, so the key and the receipt say what will really run. `machine` is hwdetect.machine(). Raise UserError
        when the settings cannot run here. The default changes nothing."""
        return settings

    def setup(self, settings: dict, ctx: Context):
        """Load models once for a batch of items. Returns whatever run() needs as `state`."""
        return None

    def run(self, item: Item, inputs: dict[str, Path], out: Path, settings: dict, state, ctx: Context) -> Result:
        raise NotImplementedError

    def teardown(self, state) -> None:
        pass

    @classmethod
    def describe(cls) -> dict:
        """The declaration, as the GUI reads it."""
        return {
            "name": cls.name, "label": cls.label or cls.name, "version": cls.version, "doc": (cls.__doc__ or "").strip(),
            "group": cls.group, "gpu": cls.gpu, "needs": dict(cls.needs), "makes": dict(cls.makes),
            "settings": {k: {"default": s.default, "help": s.help, "choices": s.choices, "kind": s.kind,
                             "in_key": s.in_key} for k, s in cls.settings.items()},
        }
