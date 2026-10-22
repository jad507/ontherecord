# Research-a-thon write-up: notes and numbers

Raw material for the three-page submission (Penn State CSRAI Research-a-thon; Times New Roman 11 pt, single
column, single spaced; due Dec 1, 2026, 11:59 pm ET, via the Microsoft Form). Sections and lengths: Research
Problem (.25-.5 p), Generative AI Workflow (1-1.5 p), Impact and Value (.5 p), Innovation (.25-.5 p). Judged on
innovation, significance of AI's contribution, and impact on outcomes. Claude Code is to be named. Every number
below says where it comes from; recompute rather than copy if a run is redone.

## The story in one paragraph

A transcript pipeline fed a published study's coding of city council meetings (ISLS 2027 submission). For 11 of
its 50 meetings, the two channels of the YouTube audio are inverted copies of each other. Averaging them to mono,
the usual first step, cancels the speech. The speaker model (pyannote) read that averaged audio, while the speech
recognizer (Whisper) read a later left-channel fix. The scripts skipped any step whose output file already
existed, so nothing recorded that the two halves of each transcript came from different audio. Claude Code found
it while auditing quotes; the paper dropped those meetings. On the Record is the tool that would have caught it:
every output carries a receipt (input and output hashes, settings, versions, device, time), and a check fails a
run whose inputs trace back to different bytes of the same recording.

## The 11 meetings, run three ways in On the Record

Source: the runs `20261022-092723-eleven-left`, `20261022-095134-eleven-average` and
`20261022-092509-eleven-as-coded` in shiro's `~/ontherecord-workspace`. They are compared with hands_on_dl's files by the session script
`eleven/compare.py`: May 15 diarization at git `6360789~1`, Oct 19 re-diarization at HEAD, extractions
`gemma26b-2026-10-18` and `gemma26b-2026-10-19-rediarized`.

- 11 meetings, Oct 21 - Nov 25, 2025: 1,129 minutes (18.8 hours). Channel correlation r = -1.00 in every one;
  averaging loses 31.1-32.2 dB, and the audio check fails all 11.
- **Averaged (the trap):** pyannote finds 1 speaker in each meeting, and 176 minutes of speech in all. Whisper
  hears 0 words, so there are 0 blocks and 0 public comments. hands_on_dl's May 15 diarization also has 1 speaker
  in every meeting, with 210 minutes of speech.
- **As coded (pyannote on the average, Whisper on the left):** what the paper's Oct 17-18 runs read. On the Record
  makes 2,773 speech blocks and 98 public comments, against the paper's files' 2,703 and 102. The `same_source`
  check fails on all 11 meetings, so the error would have been flagged the first time it ran.
- **Left channel (as intended):** 189 speakers, the same as hands_on_dl's Oct 19 re-diarization in every meeting.
  896 minutes of speech (vs 901), 154,212 words (vs 154,100), 1,325 blocks (vs 1,324), 121 comments (vs 117).
- Fixing the input changes the analysis: from 98 to 121 comments (+23%), and from 2,773 speech blocks to 1,325.
- The small gaps between the left run and the Oct 19 files come from the download: audio-only is 13.5 ms shorter
  than the old merged mp4. pyannote's output was identical; Whisper gave 86 vs 90 segments on one meeting. The old
  transcript has no receipt, so this could only be measured by redoing it.
- One meeting close up (Jan 29, 2026, `Kr5f5_Rt90Q`, phase-inverted, r = -0.999). Averaged: 1 speaker, 20% speech,
  0 words. Left channel: 7 speakers, 66% speech, 3,838 words. The Compare view shows it side by side.

## The stress benchmarks, run again in On the Record

- WhiStress (vendored, MIT, with the transformers 5 fix) gives the ICDS poster's marks on every item: StressTest
  218/218 and StressPresso 202/202, both given the transcript and from audio alone. F1 is 0.884 and 0.835, against
  the paper's 0.883 and 0.835. Top-1 (the word it rates most stressed is a stressed word) is 98.2% and 94.1%.
- The 26B reader on the marked transcripts: run `20261022-132631-stress-benchmarks`, results pending.
- Provenance finding: the 26B file the poster and paper used cannot be downloaded any more. Its Hugging Face repo
  replaced the file under the same name six times in four months (STATUS.md). Pinning a file name does not pin a
  model; a receipt's hash does.

## Generative AI workflow (what each model did)

- **Claude Code** was the engineer and the auditor. It wrote the pipelines and the stress study, found the 11-meeting
  error by tracing quotes back through the pipeline, and built On the Record. That covered the runner, receipts,
  checks, GUI, queue, importer, assistant, MCP server, installers and tests, each change tested on the real data.
  Jeff set the direction, the constraints (local only, non-technical users, one job at a time, official models
  first) and the decisions. His turns are logged in `docs/private/user-turns.md`.
- **Local models doing the research work:**
  - Whisper large-v3 transcribes.
  - pyannote finds who spoke when.
  - Gemma 4 26B lists public comments from whole meetings, and reads stress marks.
  - WhiStress marks stressed words.
  - Gemma 4 E2B/E4B is the assistant that a non-technical researcher talks to. It looks things up, links answers
    to the moment in the recording, and proposes work; a person presses Go ahead.
- **Agents as users:** every GUI action is also a command with JSON output (AGENTS.md), and the assistant's tools
  are an MCP server. Claude Code can therefore drive the same queue a person watches.

## Impact and value

- Who it is for: social scientists who work with recordings (interaction analysis, public meetings, interviews)
  and use Word and Outlook, not a terminal. It needs one installer, a browser tab and plain-English questions.
  Nothing leaves the computer, which matters to ethics boards.
- What it changes: provenance is recorded, not reconstructed. A reviewer can ask which audio a speaker label came
  from, and the receipt answers. A redone step that changes nothing is reused; one that changes something is
  re-run, and the difference is visible (Compare).
- From the camera onwards: Import copies a card with hash checks, names files by date and device, keeps a catalog,
  and only clears the card once every file is verified.

## Innovation

- Receipts and lineage checks for qualitative-research pipelines. The `same_source` check catches mixed inputs,
  the failure that went unnoticed in a real study.
- A local assistant that small models can drive reliably: tools written for a 4B model, actions written by the
  program rather than the model, and a test set of plain-English requests (`evals/`; E4B passed every scored case after one tool description was
  reworded).
- One machine, one job at a time, so every step gets all the memory a laptop has and every timing means something.

## Still to measure before writing

- The stress reader results (queued).
- Google's vs Unsloth's 4-bit E2B/E4B on the assistant test and the stress reader (downloads awaiting approval).
- The Windows install on a work laptop: how long it takes, and what a first-time user trips on.
