"""The CLI's config handling: a file, the defaults, and a workspace override."""
from argparse import Namespace

from ontherecord.cli import config_from


def test_config_from_file_defaults_and_override(tmp_path):
    f = tmp_path / "machine.yaml"
    f.write_text(f"workspace: {tmp_path / 'ws'}\ntools:\n  ffmpeg: /opt/ffmpeg/bin/ffmpeg\n", encoding="utf-8")
    cfg = config_from(Namespace(config=str(f), workspace=None))
    assert cfg.workspace == tmp_path / "ws" and cfg.tool("ffmpeg") == "/opt/ffmpeg/bin/ffmpeg"
    cfg = config_from(Namespace(config="none", workspace=str(tmp_path / "other")))
    assert cfg.source is None and cfg.workspace == tmp_path / "other"
