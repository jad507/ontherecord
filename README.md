# On the Record

Run AI transcription and coding pipelines where **every result carries a receipt** of how it was made: which
source, which audio conversion, which versions of Whisper and pyannote, which model, which prompt, which settings,
on which machine, and how long it took.

Qualitative researchers have long treated a transcript as a theoretical artifact rather than a recording. When a
lab adopts AI tools, much of that theory gets written by software defaults instead: silently, inconsistently, and
without a record. On the Record makes each of those choices a visible, versioned setting, and keeps the evidence.

## Why: a real failure this would have caught

In the Lancaster council corpus behind the ISLS 2027 paper, some YouTube streams carry phase-inverted stereo (right
= −left). The original pipeline made mono with ffmpeg's `-ac 1`, which averages the channels, so on those meetings
the speech cancelled. The extraction was fixed on May 22, 2026, and Whisper was re-run, but diarization had already
run on May 15 and its script skipped any meeting whose output file existed. For months, many meetings had
transcripts made from one audio file and speaker turns from another (11 of the 50 in the paper's corpus), and
nothing recorded it. It surfaced only in October, when an audit of quotes found meetings with a single "speaker".

Here, that cannot happen silently:

- **The audio stage measures the channels.** For the January 29, 2026 council meeting, left and right correlate at
  r = −0.999; averaging them loses 32 dB. With `channel: average` the check fails and the meeting's later stages stop.
- **Outputs are keyed on the bytes of their inputs**, not on a file existing. Re-extract the audio and everything
  downstream of it runs again, unless the new audio is byte-identical, in which case it is reused.
- **Inputs are traced to their sources.** A stage whose inputs come from different bytes of the same artifact (speaker
  turns from one audio file, words from another) fails its `same_source` check. `recipes/eleven-as-coded.yaml`
  rebuilds the corpus mistake on purpose, wiring pyannote to the averaged audio and Whisper to the left channel, and
  the check catches it at the alignment step.
- **Every output has a receipt**: settings (defaults included), input and output hashes, package versions, model
  revisions, GPU, timings, and the results of its checks.

## Install

Non-technical users: see [INSTALL.md](INSTALL.md). Double-click the installer for your system; it installs
Python, the packages (pinned by `uv.lock`), ffmpeg and llama.cpp (pinned and checked), offers the models this
computer can run, and puts **On the Record** on the desktop. Linux servers: see [docker/README.md](docker/README.md).

## Quick start (developers)

```bash
python -m venv .venv && .venv/bin/pip install -e ".[server]"      # Windows: .venv\Scripts\pip
cp machine.example.yaml machine.yaml                               # then edit: workspace, tools, environments
ontherecord doctor                                                 # what this machine has
ontherecord plan recipes/jan29-left.yaml                           # what would be reused, what would run
ontherecord run  recipes/jan29-left.yaml                           # Ctrl+C cancels cleanly
ontherecord show                                                   # the last run, items x stages, with checks
ontherecord receipt RUN_ID ITEM STAGE                              # everything behind one output
ontherecord serve                                                  # the GUI at http://127.0.0.1:8765
```

The GUI has seven tabs:

- **Library**: every recording, opened in a player beside its transcript. The player plays the audio the
  transcript was made from, with a timeline of who spoke when; click a line to jump there. Search across every
  transcript at once, and see the public comments and any failed checks for that run.
- **Ask**: plain-English questions ("where did people talk about the data center?", "transcribe this link") to a
  small model running on this computer. It answers from the transcripts, gives times you can click, and shows what
  it looked at. Anything that starts work waits for you to press a button.
- **Import**: copy a camera card or drive into an archive. Every copy is verified by hash, files are named by date
  and camera, and a manifest opens in Excel. The card is cleared only when you ask, and only of files that still
  match their archived copies.
- **Build a run**: a form for a recipe (sources, stages, every setting with its default and help), a plan of what
  will be reused, submit and cancel.
- **Results**: each run as a grid of items × stages; click a cell for its receipt and a preview of its output.
  **Compare with** lines two runs up: for each item and stage, whether the outputs are byte-identical, the headline
  number on each side (dB lost, speakers, words, comments), and which settings or inputs made the difference.
- **Setup**: this computer and what fits it, the helper programs, the models (download buttons; every file is
  checked against its published fingerprint), and the Hugging Face token the speaker model needs.

The same queue works from the command line, for scripts and coding agents:

```bash
ontherecord submit RECIPE --wait --json    # queue a run and follow it; exit 0 done, 1 failed, 3 cancelled
ontherecord jobs                           # what is queued, running and finished
ontherecord cancel RUN_ID                  # stop it cleanly
ontherecord dispatch                       # run the queue on a machine without the GUI
```

Imports, the assistant and MCP work from the command line too:

```bash
ontherecord devices                        # cards and drives plugged in
ontherecord ingest /media/me/EOS_DIGITAL --project "Reverse Telescope" --wait
ontherecord clear IMPORT_RUN_ID            # delete the verified files from the card
ontherecord ask "what did people say about parking?"
claude mcp add ontherecord -- ontherecord mcp   # the assistant's tools in Claude Code (or Claude Desktop)
```

[AGENTS.md](AGENTS.md) has the details an agent needs: JSON output, where results live, the HTTP API, the rules.

## How it is put together

| Piece | What it is |
|---|---|
| **Recipe** (`recipes/*.yaml`) | *What* to run: the sources (URLs or files) and the stages with their settings. Plain text, so it can be diffed, reviewed and cited. Each input comes from the latest earlier stage that makes it, or from a named one (`as:` / `from:`). |
| **Machine config** (`machine.yaml`) | *Where things are* on this machine: workspace, binaries, model weights, which Python runs which stages, secrets. Recipes never contain paths that only work on one machine. |
| **Stage** (`src/ontherecord/stages/`) | One step, declaring its inputs, outputs and settings (with defaults and help text, so the GUI can draw a form). |
| **Runner** | Computes each output's cache key, reuses what is unchanged, runs the rest stage by stage in a separate worker process, and records the run. |
| **Job queue** (`<workspace>/jobs.db`) | One job at a time per workspace, each in its own process, detached from the GUI that started it. Cancel stops the whole process tree, llama-server included, and removes partial outputs. |
| **Store** (`<workspace>/store/`) | One directory per stage × item × key, holding the outputs and `receipt.json`. Written to a temporary directory and renamed into place, so a cancelled stage never leaves something that looks finished. |

Stages so far: `download` (yt-dlp) → `audio` (ffmpeg, channel policy and checks) → `diarize` (pyannote) →
`transcribe` (faster-whisper) → `align` → `blocks` → `extract_comments` (a long-context LLM through llama-server or
any OpenAI-compatible server). The stages port `hands_on_dl`'s scripts with the same defaults.

## Runs anywhere

The same repository runs on Linux, Windows and macOS: Python only, no shell scripts, binaries and weights located
through the machine config. Stages that need torch can run in a different Python environment from the core. On a
Mac, pyannote uses the Apple GPU and Whisper the CPU; the receipt records which device computed each output, so
results from different machines are never mistaken for each other.

## Status

Built in October 2026 for Penn State CSRAI's Research-a-thon. Working on Linux: the runner, store, receipts and
checks; the council pipeline stages; the job queue (one job at a time, the assistant included); the Library and player; imports
with verified copies; the assistant and its MCP server (`pytest` covers caching, wiring, the queue, cancel,
imports, clearing, the library and the MCP server). Next: installers for Windows and macOS and a Docker image, a
first-run setup page, a view comparing two runs, the stress-test pipelines, and a Slurm backend.

Built by Jeffrey De Lucca (jad507@psu.edu) with Claude Code.
MIT licensed (see [LICENSE](LICENSE)). To cite it, see [CITATION.cff](CITATION.cff).
