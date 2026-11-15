# Status and handoff (October 24, 2026)

Where On the Record stands, what was decided and why, what is waiting on Jeff, and what to do next, for whoever
picks it up next (a person or an agent). Written Oct 22 at the end of the first build session and brought up to
date at the end of Oct 23's and Oct 24's. The Research-a-thon write-up notes, with every number and its source, are in
[researchathon-notes.md](researchathon-notes.md). The write-up (three pages) is due Dec 1, 11:59 pm ET.

## November 14: architecture audit

[audit-2026-11-14.md](audit-2026-11-14.md) audits how well the program generalizes beyond the council and stress
pipelines, and gives a numbered roadmap with the reasons and a done-when test per item. Read it before adding a
stage, a review kind or a screen. Nothing in it is built yet.

Jeff, Nov 14: the project is pre-launch, so anything that can be rerun may be broken for a better design, without
asking (stage outputs, defaults, the store, recipes). The audit was revised for that the same day: stages write
shared formats directly, `blocks` and `extract_comments` are replaced by `turns` and `code`, and there are no
adapters for old files. What may not break: people's answers in `<workspace>/reviews/`, the papers' code in
hands_on_dl and AITranscribe, and the decisions below. The work is ordered as step 0 on `main` (back up reviews,
the version test, annotators), a `formats` branch, then one rerun that must reproduce the recorded numbers.

His other answers that day: the time before Dec 1 goes to this, ahead of "Next, in order" below (with a guard he
confirmed: if the `formats` branch has not reproduced the numbers by Nov 24, the write-up uses `main`'s results and
the branch merges after Dec 1); the model reads `SPEAKER_02`, not names, by
default. The names `code`/`lens`/`context` and the tab names are deferred until he has used them.

## Against the original four-week plan

We are at about week 3.5: Jeff's additions took roughly a week of the plan's time.

| Week | Planned | State |
|---|---|---|
| 1 | Runner, store, receipts, checks, the council pipeline stages, from the command line | Done |
| 2 | The job queue and the GUI | Done |
| 3 | Compare-two-runs view; stress pipelines (StressTest, StressPresso, WildStressTest with clip and human-label stages, WhiStress to text-reader cascade); scoring against labels | Compare done. Stress set, WhiStress and reader stages done, scored against the labels inside each stage; the stress-benchmarks run is done but not yet read against the poster. Human labels: the Review tab (Oct 23) generalizes the gold and stress labelling pages, and the stress clips with their labels are a set there. Not done: WildStressTest's clip-cutting stage (the finished items.json is loaded instead), the classical detectors (Praat, wavelet), and the audio-LLM listening arm. |
| 4 | Slurm backend; Windows and macOS tests; backfill receipts for existing hands_on_dl outputs; more checks | Not started. Installers for all three systems are written (now picking the PyTorch and llama.cpp builds per machine, uv pinned); only Linux is tested. |
| 5-6 | About 10 days of writing | Notes started |

Added since the plan, at Jeff's request: Library and player; Import from cameras and drives (verified copies,
catalog, clearing the card); the Ask assistant with confirm buttons; the MCP server; the Setup tab and model
catalog; installers (uv) and a Docker image; one-job-at-a-time with `ontherecord exclusive`. On Oct 23: the guided
first run with machine detection and per-machine builds, picking up where you left off, transcript files, a
security review, compute time / tokens / API-price equivalents with time estimates, and reviews (checking by hand).

## November 2: Nemotron 3.5 Transcribe set up, not yet run

Jeff asked for NVIDIA's Nemotron 3.5 Transcribe, the FP8 checkpoint
(`nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-FP8`), set up for On the Record. His GPU was busy, so nothing was
run on it.

- **Downloaded:** revision `2b6209f8` (Oct 29), 34.7 GB, into the Hugging Face cache. Every file was checked against
  `model_hashes.py`. It is listed as `nemotron-3.5-transcribe-fp8` in `ontherecord models`.
- **Runtime:** `install/nemotron-vllm.sh` builds its own environment in `~/src/nemo-speech/.venv` (8.5 GB). It has
  vLLM 0.32.0 and NeMo Speech at `e5d79d5` (Oct 31), installed from NeMo's `uv.lock` with hashes, plus the card's two
  extras pinned by SHA-256. It is separate because it needs CUDA 13 PyTorch, and faster-whisper needs CUDA 12. NeMo's
  pyproject blocks OpenCV, so the extras install with `uv pip --no-config`. `machine.yaml` names it as
  `tools.nemotron_vllm`.
- **Stage:** `nemotron_transcribe` (`stages/nemotron.py`) takes the place of `diarize` and `transcribe`. It makes
  their artifacts (the transcript, both RTTMs and the stats), so `align` and everything after it work unchanged. It
  also makes `nemotron_raw`, every response exactly as the server sent it. It follows the card: the server command,
  the speaker-tag prompt, temperature 0 with no reasoning, word timestamps through NeMo's middleware, and the card's
  guard against loops (applied to the words). The recipe to try first is `recipes/eleven-left-nemotron.yaml`. Its
  downloads and audio are already cached.
- **Offloading experts:** the experts are 30.65 GB of the 34.7 GB, and everything else is 4.01 GB (read from the
  safetensors headers). The stage starts vLLM with `--cpu-offload-gb N --cpu-offload-params experts`, trying
  N = 22, 24, then 26 GiB until one loads. Offloaded weights are pinned in RAM and the GPU reads them in place
  (UVA). Pinned memory cannot swap, so the stage skips any N that would leave under 4 GiB of RAM free. With 30 GiB
  in shiro, the server will only start when little else is running. Its speed is unknown: no one has measured this
  on a 16 GB card.
- **Long recordings:** the model is trained on recordings up to an hour, and the server is told to accept no more.
  Longer meetings are cut near each even split, at the quietest second within 30 s of it. Speakers are numbered per
  piece (`P2_SPEAKER_00`), because the model does not carry voices across pieces. A `pieces` check warns when this
  happens.
- **Offline:** `config.LOCAL_ONLY` now also sets `VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK` and
  `FLASHINFER_NO_DOWNLOAD`. Without the last one, FlashInfer downloads a missing prebuilt kernel from NVIDIA's
  artifactory (`flashinfer/jit/cubin_loader.py`). With it, FlashInfer stops and names the missing file. If the first
  start stops that way, the fix is to allow the download or find a pinned kernel package; nothing is set up for
  either. FlashInfer also compiles kernels with `/usr/local/cuda/bin/nvcc` (13.3) on the first start, which takes
  several minutes.
- **First run, when the GPU is free:** `ontherecord submit recipes/eleven-left-nemotron.yaml --wait`, or a single
  meeting. Things to watch: whether the server loads, and at which N (the receipt's `offload_gib_used`); RAM, in the
  scratch `vllm.log`; the `audio_minutes_per_minute` metric; and the speaker count. The card's diarization prompt
  names four speaker tags, and city council meetings have more speakers than that.

## October 25: the Check page after Jeff's first try; projects decided; the repository on GitHub

- Jeff tried the tutorials, Follow along, Key per word, Grid and One at a time. Changed from what he found: a tutorial
  step stays on screen when its key is pressed (marked done) until Next or Enter, with Back; a bar above the turn
  shows where the audio is and keeps moving through pauses in the speech (he thought long gaps were a freeze; he
  likes it in every way); the turns scroll as one transcript (neighbors whole, as checked where saved, click one to
  go there) instead of flipping pages; in every way a click plays from a word and a double-click types over it; Key
  per word: Space carries on from where it stopped (it restarted the turn), Tab hops to two words before the next
  doubtful word (Shift back), the keys follow it past 15; Grid: Tab skips to the next line with doubtful words and
  plays from two words before the first; One at a time: V for can't make out. Tested headless against a scratch copy.
- Projects: his three answers are in [projects-and-folders.md](projects-and-folders.md).
- **The gold sample holds 38 items (of 180) from the 11 phase-inverted meetings** (Oct 21 - Nov 25, 2025): drawn
  Sep 3 from the blocks of the May 15 diarization, so each shows one speaker label for the block and all its context,
  and the blocks are cut by pauses (median 12 s against 28 s). Jeff had coded items 1-12 when this came up; at his
  word his answers (13 rows) went to the desktop trash and coding starts over. Whether the 38 stay, under a second
  amendment written before any of them is coded, is his decision (Waiting on Jeff).
- Review tab: each grey context block has a play button, cut from the same local recording as the item (only the
  item's own neighbors can be cut).
- Git history rewritten to Jeffrey De Lucca <jad507@psu.edu> before the first push; old hashes (in earlier receipts)
  map to new ones in [commit-map.txt](commit-map.txt). Remote: github.com/jad507/ontherecord, pushed over a deploy
  key that only this repository uses. Still ask before pushing.

## October 24: checking transcripts by ear (word choices, the Check page), Penn State themes

Jeff asked for keyboard-first transcript checking and coding (WildStressTest's checking was mostly mouse work), with
Whisper's other likely words on left-hand keys, about five control schemes to try, tutorials a non-gamer can follow,
and light and dark themes from Penn State's colors. See [checking.md](checking.md).

- **`word_choices` stage** (`stages/word_choices.py`, needs `audio` and `align`, makes `words`): forced alignment of
  the transcript's own text (no new transcription), then for each word with a token below 0.8, Whisper's next-token
  distribution there and a short greedy completion of each alternative to find how many words it replaces. Words
  are the transcript split at spaces ("Balke-Hilditch." is one word; version 2 fixed that). 4 to 8 GPU minutes per
  hour of audio on shiro (transcription is about 9); 5 to 8% of words get choices. CTranslate2 4.8.3 quirks found:
  `generate()` returns logits for every forced prefix step, decodes a forced prefix one token at a time, and stops
  at about half the `max_length` given; each batch row holds about 245 MB of cross-attention for large-v3.
- **`check` review kind** (`reviews.py`): every speaker turn in order (parts of at most 60 s), WAV clips for exact
  word seeking, codes on keys 1-5 and Q-T (`--codes`), flags (wrong speaker, crosstalk). Answers record the corrected
  text, edits by how they were made, codes as word spans, the way of working, keys, plays and active seconds.
  `review score` reports post-editing WER (not blind), how often a typed word was one of the choices, and seconds per
  minute of audio per way of working.
- **The Check page** (`/check#SET`, `static/check.*`): five ways of working (Follow along, Key per word, Grid, One at a
  time, Type it), one engine (audio, edits, undo, drafts, saving, coding), an always-on map of what each left-hand key
  does, a start screen, and a tutorial for each on a practice turn (nothing saved).
- **Themes**: the whole GUI now uses Penn State's palette in light and dark (switch in the header; follows the system
  by default), every text color checked at 4.5:1 or better; speaker colors use the same ten accents. Font: Roboto,
  the brand's web font, bundled (151 KB, open license) so every machine shows the same; text boxes and key labels
  use it too, and monospace is kept for hashes, ids, paths and logs. Nothing is fetched (the Proxima Nova files are
  licensed, so they stay out of the repository). Static
  files are now served with `Cache-Control: no-cache`, so an update is never half-loaded.
- In shiro's workspace: run `20261024-104844-check-words` (the water mains meeting and the data center press
  conference) and the set `check-water-mains-and-press-conference-20261024` (64 turns, no answers). Tested: 90 tests;
  each scheme, coding, saving and all five tutorials driven headless in Chrome (`scripts/cdp.py`) against a scratch
  copy of the set (no answers in the real one), both themes. Not yet tried by a person, and not with a screen reader.

## October 23: first run, machine detection, picking up where you left off

Jeff asked for an easier first run (driver, token, first file), detection of the card, memory and system with
defaults to match, CUDA builds above 12.4/12.8 where the machine allows, and a better return to the program.

- **Detection** (`hwdetect.py`, standard library only, so the installers run it first): every NVIDIA card's memory,
  free memory, compute capability and driver, the newest CUDA the driver supports (both banner styles,
  `CUDA Version` and since the 610 drivers `CUDA UMD Version`), AMD and Intel graphics, WSL.
- **PyTorch build per machine**: dependency groups `cu128` (default, as before), `cu126`, `cpu`, chosen by the
  installers. Found while doing this: PyTorch 2.11's cu128 wheels have no kernels below compute capability 7.5
  (`.ci/manywheel/build_cuda.sh` at v2.11.0), so a GTX 10-series card got no GPU at all before; cu126 still has
  Maxwell to Hopper. CPU-only machines no longer download about 3 GB of CUDA libraries.
- **Not PyTorch's CUDA 13**: CTranslate2 4.8.3 (the newest) loads `libcublas.so.12`, and `nvidia-cudnn-cu12` and
  `-cu13` both install `nvidia/cudnn/lib/libcudnn.so.9`. CUDA 12 builds run on CUDA 13 drivers. Revisit when
  CTranslate2 ships CUDA 13 wheels, or with a torch upgrade (2.12 to 2.14 exist, with cu132).
- **llama.cpp goes higher**: its CUDA 13.4 builds (Windows and Linux, x64 and arm64; Windows was on CUDA 12.4) when
  the driver supports 13.4 and the card is Turing or newer, else CUDA 12, else Vulkan, else the CPU build. Each
  build is checked with `llama-server --list-devices` and setup falls down the list if it cannot see the card.
  Each build has its own folder (`bin/llama-b11249-<build>`). Docker stays on CUDA 12.8 (`--build-arg TORCH=`
  picks its PyTorch).
- **Whisper per machine**: large-v3 float16 on NVIDIA cards with 6 GB or more, int8_float16 below that, int8 on
  cards without fast float16 (compute capability below 7.0); large-v3-turbo int8 on the processor, including Macs
  (CTranslate2 has no Metal). Turbo is in the catalog, pinned (`mobiuslabsgmbh/faster-whisper-large-v3-turbo`,
  which Hugging Face redirects to `dropbox-dash/...`; rev `0a363e91...`, 1.6 GB, MIT). The transcribe stage now
  falls back to a compute type the card supports and to the CPU when CUDA 12's cuBLAS will not load, both recorded,
  and names turbo's revision correctly.
- **`ontherecord transcribe FILE|LINK...`** and the GUI's **Transcribe a recording** (Library, and Setup step 5)
  queue one ready-made recipe (`presets.py`) with this machine's settings written in. The assistant's transcribe
  tool uses it too (its description is unchanged, so no eval rerun is needed).
- **Setup is a guide**: five steps with status, opening by itself until the Library has a recording; the driver
  advice (needed, recommended, optional); what each part runs on; a warning when another program holds the GPU's
  memory; the installed PyTorch build against the one the card needs; a token **Check** (Hugging Face's whoami and
  the gated file at the pinned revision; the only setup call that goes online, on a click); where the folders are,
  with Open buttons.
- **Picking up where you left off** (per browser): the tab, the recording paused where it stopped, the Ask
  conversation (a confirm button that was waiting is not brought back), the Build draft, and a banner of jobs that
  finished or failed since the last visit. The player's **Save** writes .txt, .srt or .vtt.
- INSTALL.md is now the walkthrough. Projects and folders: a proposal waiting on Jeff,
  [projects-and-folders.md](projects-and-folders.md).
- Tested: 74 tests; the GUI headless in Chrome against a scratch workspace and, read-only, the real one (resume to
  the same second, paused; the banner; an .srt saved). Not tried: the installers with the new groups on a fresh
  machine (the three groups resolve in `uv sync --dry-run`), the CUDA 13.4 llama.cpp download (about 600 MB),
  Windows, a GTX 10-series card.

## October 23: security review (after a run of zero-days)

What goes online: setup's downloads, the YouTube download stage, the token Check, and an assistant endpoint if one
is configured. Everything else stays on 127.0.0.1. Found and fixed:

- **DNS rebinding on the GUI server** (since the GUI existed): it answered any Host header, so a web page whose
  domain was re-pointed at 127.0.0.1 could read transcripts, list folders, queue jobs (an `extract_comments`
  endpoint can send a transcript anywhere), replace the token, or clear a card after an import. Now only requests
  addressed to 127.0.0.1, localhost or ::1 (or an explicit `--host`) are answered, and a POST naming another origin
  is refused. Tested with curl and in the browser.
- **ffmpeg for Windows and Linux was "BtbN's latest"**, checked only against the digest the same release
  published. Now BtbN's month-end build of Sep 30, 2026 (8.1.3, kept long-term, uploaded by its Actions bot and
  never replaced), pinned by SHA-256.
- **uv came from `curl https://astral.sh/uv/install.sh | sh`** (and `irm ... | iex`), always the newest. Now
  `install/uv-pinned.sh` and the PowerShell equivalent fetch uv 0.11.25 (June 27) from its GitHub release and keep
  it only if its SHA-256 matches (GitHub's digest, which uv's own installer also carries). uv's install.ps1 does not
  check its download, and its install.sh skips the check on macOS (no `sha256sum`). The Dockerfile's uv image is
  pinned by digest too. Tested on Linux, including a mismatch stopping the install; the PowerShell is untested.
- **The speech models were pinned by revision only.** `model_hashes.py` now holds every file's hash (sha256, or
  git's blob id for small files) at the pinned revision; setup checks each download and deletes a file that does
  not match, and `ontherecord models verify` checks what is on disk. shiro's cached speech models and the Oct 22
  Gemma files all match. Downloads are now only the files the programs load (faster-whisper's own list).
- **yt-dlp with bun runs YouTube's JavaScript unsandboxed** (yt-dlp's bun provider: "No sandboxing options
  available"; deno runs with no permissions). `machine.example.yaml` now names deno, and `setup --check` warns.
  shiro still uses bun: deno is not installed.

Checked and fine: no LiteLLM anywhere (lock or environments). Every PyPI release in the lock published since Sep 23
either has PyPI provenance from its own GitHub repository, or comes from a project that has never published with
provenance (no break in habit). The one break, SQLAlchemy 2.0.55, is the 2.0 branch's norm, and only used on Python
below 3.11. Yesterday's re-lock added only PyTorch and NVIDIA packages, the newest being cuda-bindings 13.4.4 (Oct
14, NVIDIA's provenance), reachable only without a PyTorch group. uv verifies every package against uv.lock's
hashes, and managed Pythons against its own. llama-server binds 127.0.0.1; WhiStress loads with
`weights_only=True`; no `shell=True`, `eval` or unsafe YAML; file writes from the GUI are slugged.

Still depending on TLS: a forged certificate for huggingface.co (the post claims a CA's keys were cracked) would
expose the Hugging Face token during a download or Check (it is read-only; revoke it if in doubt), but could not
change a model file. Keep the system's root certificates updated.

## October 23: compute time, tokens and their API price (Jeff's Oct 22 item 3)

- `usage.py` and `ontherecord usage` (and the Jobs tab's line and details): compute hours from the store's receipts
  (each one a piece of work done once; a batch's model loading counted once), by stage, device and month; the
  queue's busy time; tokens; their price at six API models (Claude Sonnet 5.5 and GPT-5.6 Terra headline; Opus 5.5
  and Sol, Fable 5.1 and Astra too), read Oct 23 from Anthropic's and OpenAI's pricing pages and written in
  `usage.py` with that date; speeds by software version (Whisper and pyannote as times real time; llama.cpp prompt
  and generation tok/s).
- Tokens now recorded by every language-model stage (stress_read too) with llama-server's own speeds (its `timings`,
  checked in tools/server/server-common.cpp), in metrics only: timings vary per run, and comments.json must stay
  byte-identical. The assistant logs each call to `<workspace>/usage/assistant.jsonl`.
- Estimates from this machine's receipts (per minute of audio, per stage and model, plus model loading), in Jeff's
  yardsticks (a coffee break, a lunch break, a long meeting, overnight): `plan` and its `--json`, `transcribe`, the
  Build tab, the Transcribe form, the Jobs tab's time left, and the assistant's transcribe and jobs answers (their
  outputs; no tool description changed, so no eval rerun).
- shiro so far: 5.66 h since Oct 22; 468,867 tokens in and 39,879 out (comment extraction), $1.34 at Sonnet 5.5,
  $1.42 at GPT-5.6 Terra. The 10 stress_read receipts from before Oct 23 carry no token counts and are reported as
  not counted. Whisper large-v3 float16 runs at 7.5x real time and diarization at 58x on the 5070 Ti; this machine
  takes about 9 minutes per hour of audio for a transcript.
- Not done: tok/s has no data until the next language-model run; transcription is not priced at API speech-to-text
  rates (only tokens are); hands_on_dl and AITranscribe work before On the Record is not in the ledger.

## October 23: reviews, checking by hand (generalized from the gold and stress labelling pages)

- `reviews.py`, the Review tab, `ontherecord review`, and two assistant tools (`review_progress`, and
  `make_review`, which waits for the person's button). A set is items answered one per screen: choice (gold's
  yes / no / unsure, with flags such as fused), words (which are stressed) or text (a corrected line, a speaker's
  name), each kind with extra options that may need a reason (bad: cut off). What the two pages did, every set
  does: blind by construction (hidden fields never served; the machine's answer after answering, or first and
  recorded as seen, or never), one seeded shuffle, an append-only log with seconds per answer (time left at the
  person's pace), the latest answer winning, audio cut on first play, keys for everything, Back to revise.
  Scores: agreement and Cohen's kappa with a confusion table, exact match and top-word hit, word error rate; on the
  first unseen answers; with the set's and answers' sha256 for citing.
- Sets from runs: comments (blocks half from those the extractor flagged, the gold question, the extractor's
  answer revealed after), speakers (one item per label per recording, answered with a name), transcript (lines to
  correct, scored as Whisper's WER; `--blind` writes them from scratch).
- `scripts/review_gold_sample.py` made the gold sample's 180 blocks a set in shiro's workspace (no model output in
  it at all; `--write-back` fills gold_coding.csv for hands_on_dl's analysis). `scripts/review_stress_clips.py`
  made the stress clips a set (170 after exclusions) with the 172 labels so far; its blind score, WhiStress 47 of
  49 (95.9%), is score_corpus_labels.py's own. Neither original page is changed or retired.
- Not done: the assistant eval with the two new tools (three cases added to evals/assistant_cases.yaml; run it
  before relying on the small model to pick them); several annotators' agreement with each other (each answer
  records `by`, so the data is there); the stress page's video-level Review mode (keep/exclude per video, with
  re-cutting), which stays in AITranscribe. The GUI listens on 127.0.0.1 only, so labelling from another machine
  goes through `ssh -L 8765:127.0.0.1:8765 shiro`, where the old pages listened on the tailnet address.

## Built and tested (shiro, Linux, RTX 5070 Ti)

- 90 tests pass (`pytest`; 55 before Oct 23, 85 before Oct 24). Commits on `main`; pushed to github.com/jad507/ontherecord when Jeff says.
- The 11 meetings, three ways (left channel, averaged, as coded in May), each compared with hands_on_dl's files:
  see researchathon-notes.md.
- WhiStress inside On the Record gives the poster's marks on every item (StressTest 218/218, StressPresso
  202/202).
- Linux installer in a fresh clone; Docker image on the GPU as the host user. Oct 23: the GUI driven headless in
  Chrome (Setup, Library, resume, Jobs usage, Review), the pinned uv step on Linux (and a mismatch stopping it).

## Decisions

With Jeff, Oct 22:

- Name **On the Record**, package `ontherecord`, licence **MIT**.
- Web GUI on localhost, not PyQt (a pywebview window could wrap it later). uv installers for laptops, Docker for
  Linux servers.
- PewDiePie's Odysseus is AGPL-3.0: ideas only, no code.
- **One thing at a time.** Every job (a recipe, an import, a download, a queued command) waits for whatever is
  running, and the assistant switches off while a job runs or waits. "We just assume we're always resource
  constrained." Agents use `ontherecord exclusive -- CMD` for heavy work. Nothing heavy runs outside the queue.
- Models: official sources first. When an official file cannot run, the compromise is named and measured on our
  own tasks. Assistant tiers: E2B for a 4 GB laptop card, E4B from 6 GB; with nothing else running, it takes as
  much of the GPU as fits.

Oct 23:

- **Commit finished work without asking** ("that's what the commit history is for"). Still ask before pushing.
- PyTorch stays on CUDA 12 builds (cu128 default, cu126 for pre-Turing or older drivers, cpu); llama.cpp goes to
  CUDA 13.4 where the driver allows. Nobody needs the CUDA Toolkit. (Why: hwdetect.py's docstring.)
- Every download is pinned by SHA-256 in the code, never "the latest"; the GUI server answers only requests
  addressed to 127.0.0.1, localhost or ::1.
- API prices are read once from the providers' pages and written in usage.py with the date; the program never looks
  them up. Headline: Claude Sonnet 5.5 and GPT-5.6 Terra (Jeff's tiers: Terra ~ Sonnet, Sol ~ Opus, Astra ~ Fable).
- Reviews keep the old pages' rules: blind by construction, machine answers after answering (or never: the gold
  sample holds no model output, as pre-registered), seeded order, append-only log. The old pages stay as they are.

## Waiting on Jeff

0. **The gold sample's 38 items from the 11 phase-inverted meetings** (Oct 25, above): leave them out of phase-1
   accuracy under Amendment 2 (recommended; weights recomputed for the remaining meetings), or code them and report
   them separately. Decide before coding them; coding restarts from item 1.
1. ~~Projects and folders~~: answered Oct 25 (labels in one workspace, `Transcripts/` written automatically, speaker
   names in `project.yaml`); see [projects-and-folders.md](projects-and-folders.md).
2. **The extraction model.** Agreed in outline Oct 22: try Unsloth's QAT `gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf`
   (fetched Oct 22, 14.25 GB) and make it the catalog's extraction default if it runs well; keep noctrex's MXFP4
   file (the paper's; provenance below) listed as the paper's; rerun the stress benchmarks on it when he says.
   Until then the catalog pins Unsloth's July MXFP4 file, so Setup's step 3 shows shiro's 26B as "a different file
   by that name" and `transcribe --comments` refuses on shiro.
3. **The assistant eval** (`evals/assistant_eval.py`, now 25 cases): on the four 4-bit E2B/E4B files ("we're not
   running it right now", Oct 22), and now also because two tools were added (review_progress, make_review).
4. **deno on shiro**: yt-dlp runs YouTube's JavaScript with bun, unsandboxed; deno would run it with no
   permissions. A download, so his call; then `js_runtime: {deno: ~/.deno/bin/deno}` in machine.yaml.
5. **What to cut** to protect Dec 1: WildStressTest's clip-cutting stage, the classical detectors, the audio-LLM
   arm, Slurm. Whatever is cut goes in the write-up as future work.
6. **The Check page**: try the five ways (each tutorial takes a few minutes) and say which to keep, change or drop;
   the codes to use instead of the placeholders (`review new RUN --kind check --codes "..."`); which recordings get
   `word_choices` next (4 to 8 GPU minutes per hour of audio).
7. Optional downloads, only if asked: Google's full-precision E2B (10.2 GB, for our own KL check); llama.cpp's CUDA
   13.4 build on shiro (about 600 MB, to test the new install path on real hardware).

The 26B provenance (settled Oct 22): shiro's `gemma-4-26B-A4B-it-MXFP4_MOE.gguf` (15,419,239,968 bytes, sha256
`9d46651aecae9701e7ec7c9dd48418f1d7bdecbb0adc41199eff79684c48ed6c`) came from
`noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF` (main = `ba1408cd8891f9d3af97595ec838779ae8d0ec9b`), downloaded Aug 3,
2026 (Chrome Profile 2's download record). It is in none of the 142 revisions of `unsloth/gemma-4-26B-A4B-it-GGUF`.

## Next, in order

1. Read the stress-benchmarks run (`20261022-132631-stress-benchmarks`) against the poster's cascade files
   (`AITranscribe/stress_search/{stresstest,stresspresso}_repro/gemma-4-26B-A4B-it-mxfp4/`). On StressTest the
   poster had 36.7% unmarked, 76.6% top word, 72.9% WhiStress marks, 74.3% true words, and 86.2% WhiStress marks
   with the paper's prompt. Expect close agreement, not identical: our llama-server calls set `cache_prompt: false`.
2. The QAT 26B trial for comment extraction (Waiting on Jeff, item 2), through the queue, then the catalog change.
3. Receipts backfilled for the existing hands_on_dl outputs (the corpus as the paper used it).
4. Windows test on Jeff's work machine following INSTALL.md (the walkthrough; the PowerShell uv step and the
   per-machine PyTorch group are untested), then macOS.
5. Projects, as decided Oct 25 ([projects-and-folders.md](projects-and-folders.md)).
6. Slurm backend (ROAR), if there is time; otherwise say so in the write-up.
7. After Jeff has tried the Check page: keep the ways he likes, and consider giving the assistant's `make_review` the
   `check` kind (a tool description change, so rerun the assistant eval).
8. The write-up, from about Nov 15. Numbers it can use now: `ontherecord usage` (compute hours, API-price
   equivalent), the stress review's blind WhiStress score (47 of 49), researchathon-notes.md.

## Pitfalls

- Stop the GUI with `kill <serve pid>`. TaskStop kills the whole process tree. Jobs survive either way now, but the
  assistant's llama-server would not be stopped cleanly.
- shiro's system CUDA hides library-path bugs. Test installs in Docker or a fresh machine. Example, found Oct 23:
  hands_on_dl's `.venv-audio` has PyPI's `torch 2.11.0`, which on Linux is the CUDA 13 build; faster-whisper works
  there only because the system CUDA 12 supplies `libcublas.so.12`. `ontherecord setup --check` now says so.
- Stages run offline everywhere now (`config.LOCAL_ONLY`, Oct 22): `HF_HUB_OFFLINE=1`, and pyannote's usage
  metrics off. pyannote 4.0 sends them to otel.pyannote.ai by default (version, session id, audio duration,
  speaker counts), and `HF_HUB_OFFLINE` does not stop it, so shiro's diarization runs before Oct 22 sent them.
  A model or dataset must be in the cache before a run.
- On Oct 22, WhiStress was run outside the queue for about 7 minutes while the left-channel run diarized. That broke
  the rule above. Outputs are unaffected (the extraction started after it and used its usual offload), but two
  diarization timings in that run are inflated.
- `docs/private/` is gitignored: Jeff's turns, regenerated with `python docs/private/user_turns.py >
  docs/private/user-turns.md`.
- Claude Code deletes session logs after about 30 days, so "which session downloaded this?" can only be answered
  for recent work. Older provenance: file dates, Chrome Profile 2's download history (query it for the file name
  only), and the memory folders.
- Other programs share the GPU (ComfyUI held 11 GB on Oct 23). On the Record cannot see them; Setup's step 1 warns
  when one holds a lot of memory. Check `nvidia-smi` before timing anything.
- Testing the GUI: `scripts/cdp.py` drives headless Chrome (its docstring has the commands). Use a scratch workspace
  holding a copy of the set: answers posted while testing would otherwise land in Jeff's set.
- Do not answer review items, and do not show Jeff a machine answer some other way: the answers are the yardstick.
  Review item ids may contain "/", which is why their routes use `{item_id:path}`.
- Setup's guide shows "to do" on shiro for step 3 until the extraction-model decision (Waiting on Jeff, item 2).

## Resuming

```bash
cd ~/src/ontherecord
.venv/bin/ontherecord jobs                 # what is queued or running (the GUI runs the queue: serve on :8765)
.venv/bin/ontherecord show RUN_ID          # a run's grid and checks
.venv/bin/ontherecord setup --check        # this machine: GPUs, driver, the builds it suits, models, warnings
.venv/bin/ontherecord usage                # compute hours, tokens, the API-price equivalent, speeds
.venv/bin/ontherecord review list          # the hand-checking sets and how far each has got
.venv/bin/python -m pytest -q              # 85 tests; run it through `ontherecord exclusive` if a job is running
```
