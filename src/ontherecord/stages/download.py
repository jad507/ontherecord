"""Fetch the media for an item: a URL through yt-dlp, or a local file used where it is."""
from __future__ import annotations

from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage

MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".m4a", ".opus", ".ogg", ".mp3", ".wav", ".flac", ".aac"}


class Download(Stage):
    """Download the video or audio for each URL with yt-dlp, keeping its metadata (title, upload date, channel) and
    any captions. A local file is not copied: it is hashed and used in place."""
    name = "download"
    label = "Download media"
    version = "1"
    group = "audio"
    makes = ("media", "info")
    packages = ("yt-dlp",)
    settings = {
        "format": Setting("bestaudio/best", "yt-dlp format selector. The hands_on_dl corpus used "
                          "'bestvideo*+bestaudio/best' (merged mp4); audio-only is the same audio stream at a "
                          "fraction of the size."),
        "captions": Setting(True, "Also save YouTube's own captions (English), as a reference transcript."),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        if item.path:
            return self._local(item, out)
        import yt_dlp

        cfg = ctx.config
        opts = {
            "paths": {"home": str(out)},
            "outtmpl": {"default": "media.%(ext)s", "subtitle": "captions.%(ext)s"},
            "format": settings["format"],
            "writeinfojson": False,
            "writesubtitles": settings["captions"], "writeautomaticsub": settings["captions"],
            "subtitleslangs": ["en.*", "en"], "subtitlesformat": "srt/vtt/best",
            "noplaylist": True, "socket_timeout": 60, "retries": 10, "fragment_retries": 10,
            "quiet": True, "no_warnings": True, "noprogress": True,
        }
        if cfg.js_runtime:
            opts["js_runtimes"] = {k: {"path": v} for k, v in cfg.js_runtime.items()}
        if cfg.cookies_file:
            opts["cookiefile"] = str(cfg.cookies_file)
            opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(item.url, download=True)
            info = ydl.sanitize_info(info)
        media = next((p for p in sorted(out.iterdir()) if p.stem == "media" and p.suffix.lower() in MEDIA_EXTS), None)
        if media is None:
            raise RuntimeError(f"yt-dlp finished but left no media file in {out}: {[p.name for p in out.iterdir()]}")
        keep = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "upload_date", "duration",
                                          "webpage_url", "format_id", "format", "acodec", "asr", "audio_channels",
                                          "ext", "filesize", "chapters", "description")}
        import json
        (out / "info.json").write_text(json.dumps(keep, indent=1, ensure_ascii=False), encoding="utf-8")
        checks = []
        if info.get("duration"):
            checks.append(Check("duration", "ok", info["duration"], f"{info['duration'] / 60:.1f} min"))
        return Result(files={"media": media.name, "info": "info.json"},
                      metrics={"format_id": info.get("format_id"), "acodec": info.get("acodec"),
                               "audio_channels": info.get("audio_channels"), "bytes": media.stat().st_size},
                      checks=checks, tools={"yt-dlp": yt_dlp.version.__version__})

    def _local(self, item: Item, out: Path) -> Result:
        import json
        src = Path(item.path).resolve()
        if not src.is_file():
            raise FileNotFoundError(src)
        (out / "info.json").write_text(json.dumps({"title": item.title or src.stem, "source_path": str(src)},
                                                  indent=1, ensure_ascii=False), encoding="utf-8")
        return Result(files={"media": str(src), "info": "info.json"}, metrics={"bytes": src.stat().st_size})
