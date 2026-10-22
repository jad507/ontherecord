"""Extract the audio every later stage reads, and measure whether the channel choice lost anything.

Why the channel is a setting: some YouTube streams carry phase-inverted stereo (right = -left). ffmpeg's `-ac 1`
averages the channels, so on those streams the speech cancels and what is left is a faint residue. In the
hands_on_dl corpus that silently collapsed diarization in 45 of 127 meetings (one or two "speakers", 5-30% of the
meeting counted as speech). The checks below measure the source channels before anyone has to notice it downstream.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .base import Check, Context, Item, Result, Setting, Stage

PAN = {
    "left": ["-af", "pan=mono|c0=FL"],
    "right": ["-af", "pan=mono|c0=FR"],
    "average": ["-ac", "1"],  # what hands_on_dl did before 22 May 2026
}


def probe(ffprobe: str, path: Path) -> dict:
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=codec_name,channels,channel_layout,sample_rate", "-of", "json", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    streams = json.loads(r.stdout or "{}").get("streams") or []
    if not streams:
        raise RuntimeError(f"no audio stream in {path.name}: {r.stderr.strip()[-300:]}")
    return streams[0]


def stream_pcm(ffmpeg: str, path: Path, channels: int, rate: int):
    """Yield float32 arrays of shape (n, channels), decoded by ffmpeg a few seconds at a time."""
    import numpy as np
    cmd = [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(rate),
           "-f", "f32le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame = 4 * channels
    chunk = rate * 10 * frame
    try:
        while True:
            buf = p.stdout.read(chunk)
            if not buf:
                break
            buf = buf[: len(buf) // frame * frame]
            yield np.frombuffer(buf, dtype=np.float32).reshape(-1, channels)
    finally:
        p.stdout.close()
        p.wait()


def choose_channel(cs: dict, dead_db: float = 20.0, max_loss_db: float = 6.0) -> tuple[str, str]:
    """'auto': average the two channels, unless one is dead (a mic unplugged or muted) or averaging would lose more
    than `max_loss_db` against the louder channel (phase-inverted copies cancel; two independent mics lose about 3 dB
    on average, identical ones nothing). Then take the louder channel. Returns the choice and the reason."""
    left, right, avg = cs["rms_left_dbfs"], cs["rms_right_dbfs"], cs["rms_average_dbfs"]
    loud = "left" if left >= right else "right"
    if abs(left - right) >= dead_db:
        quiet = "right" if loud == "left" else "left"
        return loud, f"the {quiet} channel is {abs(left - right):.0f} dB quieter (a dead or muted microphone)"
    loss = max(left, right) - avg
    if loss > max_loss_db:
        return loud, (f"averaging would lose {loss:.0f} dB (left/right r = {cs['lr_correlation']:+.2f}: the channels "
                      "cancel), so the louder channel alone")
    return "average", f"the channels agree well enough to average (r = {cs['lr_correlation']:+.2f}, {loss:.1f} dB lost)"


def db(x: float) -> float:
    return round(20 * math.log10(max(x, 1e-10)), 1)


def channel_stats(ffmpeg: str, path: Path) -> dict:
    """RMS of each channel and of their average, and the left/right correlation, over the whole file at 8 kHz."""
    sll = srr = slr = 0.0
    n = 0
    for x in stream_pcm(ffmpeg, path, 2, 8000):
        l, r = x[:, 0].astype("float64"), x[:, 1].astype("float64")
        sll += float(l @ l)
        srr += float(r @ r)
        slr += float(l @ r)
        n += len(l)
    if n == 0:
        raise RuntimeError(f"decoded no samples from {path.name}")
    rms_l, rms_r = math.sqrt(sll / n), math.sqrt(srr / n)
    rms_avg = math.sqrt(max(sll + srr + 2 * slr, 0.0) / 4 / n)
    corr = slr / math.sqrt(sll * srr) if sll > 0 and srr > 0 else 0.0
    return {"rms_left_dbfs": db(rms_l), "rms_right_dbfs": db(rms_r), "rms_average_dbfs": db(rms_avg),
            "lr_correlation": round(corr, 4), "seconds": round(n / 8000, 1)}


def level_stats(ffmpeg: str, path: Path, threshold_dbfs: float) -> dict:
    """Overall RMS of the output, and the share of 30 ms frames louder than `threshold_dbfs` (a crude speech proxy
    that needs no model)."""
    import numpy as np
    rate, hop = 16000, 480
    total = 0.0
    n = active = frames = 0
    thr = 10 ** (threshold_dbfs / 20)
    for x in stream_pcm(ffmpeg, path, 1, rate):
        m = x[:, 0].astype("float64")
        total += float(m @ m)
        n += len(m)
        k = len(m) // hop
        if k:
            fr = np.sqrt((m[: k * hop].reshape(k, hop) ** 2).mean(axis=1))
            active += int((fr > thr).sum())
            frames += k
    return {"rms_dbfs": db(math.sqrt(total / n)) if n else None,
            "active_fraction": round(active / frames, 3) if frames else 0.0, "seconds": round(n / rate, 1)}


class Audio(Stage):
    """Convert the media to the one audio file every later stage reads: mono, at a fixed sample rate, lossless.
    Measures the source's channels first, so a choice that cancels the speech is caught here."""
    name = "audio"
    label = "Extract audio"
    version = "1"
    group = "audio"
    needs = ("media",)
    makes = ("audio",)
    packages = ("numpy",)
    settings = {
        "channel": Setting("auto", "How stereo becomes mono. 'average' is ffmpeg's -ac 1; on phase-inverted "
                           "streams it cancels the speech. 'auto' averages unless that would cancel the speech or one "
                           "channel is dead, then takes the louder channel, and records why.",
                           choices=["auto", "left", "right", "average"]),
        "sample_rate": Setting(16000, "Hz. Whisper and pyannote both expect 16000.", choices=[16000, 22050, 44100, 48000]),
        "format": Setting("flac", "flac is lossless and about 40% smaller than wav.", choices=["flac", "wav"]),
        "active_threshold_dbfs": Setting(-40.0, "A 30 ms frame louder than this counts as active, for the "
                                         "active-fraction check."),
        "fail_below_db": Setting(-10.0, "Fail when the chosen mono is this many dB quieter than the louder single "
                                 "channel (the averaging-cancels-speech failure)."),
    }

    def run(self, item: Item, inputs, out: Path, settings: dict, state, ctx: Context) -> Result:
        ffmpeg, ffprobe = ctx.config.tool("ffmpeg"), ctx.config.tool("ffprobe")
        media = inputs["media"]
        src = probe(ffprobe, media)
        checks: list[Check] = []
        metrics: dict = {"source": src}
        channel = settings["channel"]
        if int(src.get("channels", 1)) >= 2:
            cs = channel_stats(ffmpeg, media)
            metrics["source_channels"] = cs
            corr = cs["lr_correlation"]
            if channel == "auto":
                channel, reason = choose_channel(cs)
                metrics["channel_chosen"], metrics["channel_reason"] = channel, reason
                checks.append(Check("auto_channel", "ok", channel, f"auto chose '{channel}': {reason}"))
            chosen = {"left": cs["rms_left_dbfs"], "right": cs["rms_right_dbfs"],
                      "average": cs["rms_average_dbfs"]}[channel]
            loss = round(chosen - max(cs["rms_left_dbfs"], cs["rms_right_dbfs"]), 1)
            metrics["mono_vs_loudest_channel_db"] = loss
            if corr < -0.5:
                msg = f"left and right are phase-inverted (r = {corr:+.2f})"
                checks.append(Check("phase", "warn" if channel == "average" else "ok", corr,
                                    msg + ("; averaging cancels them" if channel == "average"
                                           else f"; the {channel} channel alone avoids cancellation")))
            else:
                checks.append(Check("phase", "ok", corr, f"left/right correlation r = {corr:+.2f}"))
            status = "fail" if loss <= settings["fail_below_db"] else "ok"
            checks.append(Check("channel_loss", status, loss,
                                f"'{channel}' mono is {-loss + 0.0:.1f} dB below the loudest channel"
                                + (" — the speech is cancelling" if status == "fail" else "")))
        else:
            checks.append(Check("phase", "ok", None, "source is mono; the channel setting has no effect"))
        name = f"audio.{settings['format']}"
        pan = PAN[channel] if int(src.get("channels", 1)) >= 2 else ["-ac", "1"]
        cmd = [ffmpeg, "-y", "-v", "error", "-i", str(media), "-vn", *pan,
               "-ar", str(settings["sample_rate"]), "-f", settings["format"], str(out / name)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-500:]}")
        lv = level_stats(ffmpeg, out / name, settings["active_threshold_dbfs"])
        metrics["output"] = lv
        checks.append(Check("level", "warn" if (lv["rms_dbfs"] or -100) < -45 else "ok", lv["rms_dbfs"],
                            f"output RMS {lv['rms_dbfs']} dBFS"))
        checks.append(Check("active_fraction", "warn" if lv["active_fraction"] < 0.2 else "ok", lv["active_fraction"],
                            f"{lv['active_fraction']:.0%} of 30 ms frames above {settings['active_threshold_dbfs']} dBFS"))
        from ..envinfo import tool_version
        return Result(files={"audio": name}, metrics=metrics, checks=checks,
                      tools={"ffmpeg": tool_version([ffmpeg, "-version"]), "command": " ".join(cmd[1:-1])})
