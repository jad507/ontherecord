# On the Record: From Recording to Coded Talk on the Researcher's Own Computer

Draft 1 of the Research-a-thon write-up (Nov 16, 2026). Due Dec 1, 11:59 pm ET. Limit: three pages, Times New Roman
11 pt, single spaced (about 1,500 words; the four sections below are about 1,350). Numbers come from
[researchathon-notes.md](researchathon-notes.md). `[BRACKETS]` mark what is still to be measured or decided; the
list is at the end, with what to do if a result does not arrive.

---

## Research Problem

Last spring I went to a talk by [a Penn State research team: NAME THE LAB, WITH THEIR PERMISSION] about what
residents said in public comment while their city council considered an AI data center. The team's method is the
normal one for research on recorded talk. They took the council's meeting videos, got automatic transcripts,
corrected them against the recordings, read every transcript to find the public comments, and then read and coded
every comment under the four themes they developed. With this method, a team that has a new idea and wants to
code the comments a different way reads every comment again.

That is months of skilled work, and the most expensive step is the last: each new idea costs a full re-read, so a
team can afford only a few. Interviews, focus groups and classroom recordings are studied the same way. AI can
already help, but the well-known tools run on company servers, and recordings of students and research
participants often may not be sent there.

Sitting in that talk I thought: every one of these steps can now run on a desktop computer. **On the Record** is
what I set out to build: free software that takes recordings and a researcher's own codebook, and does the
transcribing, the finding and the coding on the researcher's machine, where they can check it against the audio.
When I described it to a sociology graduate student who works with focus group recordings, she asked whether she
could use it on hers. This write-up reports how it was built, what works today, and what is not yet tested.

## Generative AI Workflow

Generative AI did two different jobs: it built the software, and it does the research work inside it.

**Building it: Claude Code as engineer and auditor.** I am a mechanical engineer, not a software developer. Claude Code
(Anthropic) wrote On the Record over four weeks, working from my descriptions of what a researcher should see and
my rules: nothing leaves the computer, no terminal needed, one job at a time, published models before modified
ones. I made the decisions and tried each feature; Claude wrote the code, the [N] automated tests and the
installers, and checked each change against real meetings.

Claude also found the mistake that shaped the design. Asked to trace 17 quoted comments back through my earlier
scripts, it found meetings where the software had labeled the whole meeting as one speaker. In those recordings
the two stereo channels are mirror images, so mixing them to one channel, the usual first step, cancels the
speech. I had fixed the audio and transcribed again months earlier, but the speaker-labeling step had already run
on the canceled audio, and the scripts skipped it because its output file existed. Eleven of the 50 meetings in a
paper I was writing had the right words and the wrong speakers, and nothing had flagged it.

**Doing the research: models that run on the researcher's computer.** A researcher gives On the Record links or
files. From there:

1. **Speakers.** pyannote labels who spoke when.
2. **Words.** Whisper writes the transcript (about 9 minutes per hour of recording on a $750 graphics card).
   Words Whisper was unsure of are marked, with its second and third guesses one key press away.
3. **Finding.** A language model (Google's Gemma 4) reads each transcript and marks the passages the researcher
   asked for: here, the public comments.
4. **Coding.** The same model labels each passage under the researcher's codebook, written in plain English.
5. **Asking.** A small Gemma model answers questions such as "where did people talk about water?" with links to
   the moment in the recording. It can propose work, but a person presses the button.

Changing the codebook means running step 4 again. Steps 1 to 3 are reused, not redone.

**Keeping it honest: receipts and checking.** Every output carries a receipt: the input file's fingerprint, every
setting, the software and model versions, the machine and the time taken. A check compares where a transcript's
words and its speaker labels came from, and stops the run if they came from different audio. Rebuilding my old
mistake on purpose, that check failed on all 11 meetings. Run correctly, the same 11 meetings (18.8 hours) have
189 speakers and 1,323 speaker turns; run the old way they had about 2,770 "turns" that often merged several
people. The model's list of public comments was 121 long on the correct turns and 79 to 102 on the wrong ones,
depending on the version of the coding step. Nobody has yet checked by hand which list is closer to right.

A Review page lets a person check the machine: listen to a passage, answer, and only then see what the model
said. The software reports how often they agreed. That is how accuracy will be measured: [GOLD CODING: I am
coding 180 passages by hand; result to come. If not finished by Nov 28, say so here in one sentence].

**What works today.** Steps 1 to 3 and 5 run from a browser window on one Linux desktop. Step 4, as the second
pass of a two-pass study (find the comments, then label each with themes), [THEMES RUN: has been run on the 11 meetings; N comments
labeled in M minutes / is built and tested on one meeting]. Installers for Windows and Mac are written;
[WINDOWS INSTALL: tested on one university laptop, taking N minutes / not yet tested].

## Impact and Value

**Time.** Transcribing and speaker-labeling the 11 meetings takes about [N] hours of unattended computer time,
and a coding pass takes [N] minutes. The hand equivalent is reading 154,000 words once per codebook. The
researcher's time moves from reading everything to checking a sample and reading what the model flags.

**New questions become cheap.** A team that can re-code in an afternoon can try the codebook it was curious about
but could not afford, or code the same comments under two theories and compare.

**Privacy.** Recordings, transcripts and codes stay on the computer. After setup the program does not go online
except to fetch a public video the researcher asks for. That fits data that ethics boards will not allow on outside services,
and it costs nothing per use.

**Trust.** A reviewer or a collaborator can ask of any coded passage: which recording, which transcript, which
model, which instructions? The receipt answers. The 11-meeting mistake would have been caught on the day it
happened, not five months later.

**Who can use it.** People who use Word and a browser. The first run is a guided setup; after that it is a
library of recordings with a player, a transcript and the codes beside it.

It has had one user so far: me. The next step is two or three Penn State groups trying it on recordings they
have already coded, which tests the software and gives them a second look at their own coding.

## Innovation

**The whole path, locally, under the researcher's own codes.** [CHECK AGAINST NVivo, ATLAS.ti, MAXQDA AND
OTTER BEFORE CLAIMING A DIFFERENCE: which transcribe or code in the cloud, which suggest their own themes.] On
the Record runs published open models on a desktop and codes under the codebook the researcher wrote.

**Re-coding as a rerun.** Because each step is stored with a fingerprint of its inputs and settings, changing the
codebook redoes only the coding. Changing how the audio is read redoes everything that depends on it, and the
program says which results are now out of date.

**Receipts for qualitative research.** I know of no qualitative analysis software that records how each
transcript line and each code was produced, or that refuses to combine steps made from different audio. This came
from a real error in my own work, which generative AI both helped cause (by making it easy to run steps I did not
inspect) and found.

**AI checked by people, blind.** The Review page hides the model's answer until the person has given theirs, so
the agreement it reports is not flattered by suggestion.

**Built by one person with an AI engineer.** A staff engineer with a research idea and no software team produced
installable, tested research software in a month. That is itself a result about what generative AI now makes
possible for a university lab.

---

## Not part of the submission: what the brackets are waiting on

| Bracket | What it needs | If it does not arrive |
|---|---|---|
| Lab's name | Jeff asks the lab whether they are happy to be named as the motivation, and whether the description of their method is fair (their talk says they corrected YouTube's automatic captions; it does not mention re-coding) | "a research team's talk", unnamed |
| `[N]` tests | `pytest -q` on the day | drop the number |
| Gold coding | Jeff codes the 180-item set on the Review page (not started; 38 items are from the 11 meetings and wait on his decision in STATUS.md) | one sentence: accuracy is not yet measured, and how it will be |
| Themes run | `recipes/eleven-left-themes.yaml` through the queue (about 15 GPU minutes; never run) | "built and tested on one meeting" only if that is true by then; otherwise "built, not yet run on the full set" |
| Hours and minutes in Impact | `ontherecord usage` and the receipts of the 11-meeting runs | "about 9 minutes per hour of recording" for transcription, no total |
| Windows install | one try on the work laptop following INSTALL.md | "written, not yet tested" |

Checked against the notes on Nov 16: 11 of 50 meetings; 18.8 hours; 189 speakers; 1,323 turns against 2,765
as coded; comments 121 (left channel) and 79 (as coded), which are counts from two runs, not accuracy; 154,205
words; 9 minutes per hour of audio on the RTX 5070 Ti; 17 traced quotes. To check before submitting: the card's
price ("$750"), "four weeks", "last spring" (the talk was recorded March 23, 2026), and "five months" (May 15 to
the October audit).

Left out on purpose: the stress benchmarks and the batching experiment (they are the ISLS paper's subject and
would cost half a page to explain), Slurm, the camera importer and the MCP server. One sentence on each could go
in Innovation if there is room.
