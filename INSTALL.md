# Installing On the Record

This walks you from nothing to your first transcript. Most of it is automatic; three parts need you: the graphics
driver (only if yours is old), a free Hugging Face account, and choosing your first recording. Allow about an hour,
most of it downloads.

You need: Windows 10 or 11, macOS 13 or later, or Linux; about 40 GB of free disk space; and an internet connection
for the install. An NVIDIA graphics card (or a Mac with Apple silicon) makes it several times faster, but it also
runs without one.

## 1. Check your graphics driver (NVIDIA cards only)

**You do not need the CUDA Toolkit.** On the Record brings the CUDA libraries it uses. What it needs from you is a
recent NVIDIA *driver*, the program that came with your card.

To see what you have, open a terminal (Windows: press Start, type `cmd`, press Enter) and type `nvidia-smi`. The top
of its answer shows the driver and the newest CUDA it supports, e.g. `CUDA Version: 12.8` (newer drivers write
`CUDA UMD Version: 13.4`).

| Your card | The driver should support | Below that |
|---|---|---|
| RTX 50-series (Blackwell) | CUDA 12.8 or later | the card is not used at all |
| RTX 20, 30 or 40-series, GTX 16-series | CUDA 12.8 or later (13.4 or later lets the language models use CUDA 13) | works, more slowly, down to CUDA 12.0 |
| GTX 10-series and older (Pascal, Maxwell) | CUDA 12.0 or later | the card is not used |

To update: on Windows, get the newest driver for your card at https://www.nvidia.com/en-us/drivers/ (or through the
NVIDIA app). On Ubuntu, `sudo ubuntu-drivers install`, then restart; on other Linux systems, your distribution's
NVIDIA driver package. If `nvidia-smi` is not found at all, the driver is not installed.

You can skip this step and come back: the installer and the Setup tab both check the driver and say exactly what to
do. If you update the driver later, run the installer again so it picks the faster builds.

## 2. Get the files

Download On the Record as a ZIP (on its GitHub page: the green **Code** button, then **Download ZIP**) and unzip it
somewhere you will keep it, for example your Documents folder. Do not move the folder after installing.

## 3. Run the installer

- **Windows:** open the folder, then the `install` folder, and double-click **windows-install.bat**. If Windows
  says it protected your PC, click **More info**, then **Run anyway**.
- **Mac:** open the `install` folder, right-click **macos-install.command** and choose **Open** (double-clicking
  a downloaded script is blocked the first time).
- **Linux:** in a terminal, `bash install/linux-install.sh`.

The installer looks at your graphics card and its driver first, and says what it picked, for example `PyTorch
build: cu128 (NVIDIA GeForce RTX 4070 with a driver that supports CUDA 12.8)`. Then it downloads Python and
everything On the Record needs (checking that each download is exactly the published file), and asks before
downloading the models, which take several GB.

## 4. Make a Hugging Face account (for telling speakers apart)

The model that tells speakers apart (pyannote) is free, but its makers ask each user to accept their terms. Once:

1. Make a free account at https://huggingface.co and sign in.
2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the terms.
3. Open https://huggingface.co/settings/tokens, create a token with **Read** access, and copy it (it starts with
   `hf_`).
4. Paste it when the installer asks, or later in the program: **Setup**, step 4, **Save**, then **Check**. Check
   asks Hugging Face whether the token works and whether your account accepted the terms; the two usual mistakes
   (a mistyped token, or terms not accepted) each get their own message.

The token is kept on your computer only, in a file only you can read, and is never written into results.

## 5. Start it, and transcribe your first recording

Double-click **On the Record** on your desktop. A window shows that it is running, and your browser opens the
program. Close that window to stop it. Everything stays on your computer.

The first time, the program opens on **Setup**: five steps, each turning green when done. Steps 1 to 4 are what you
just did. Step 5 is your first recording:

1. Press **Choose a file…** and pick an audio or video file (most formats work: MP4, MOV, MP3, WAV, M4A…), or
   paste a YouTube link instead.
2. Leave the language on English, or choose another, or **Detect the language**.
3. Press **Transcribe**. It runs in the background; the step shows its progress, and **Open it in the Library**
   when it is done. The first run takes longer while the models load.

In the **Library**, the recording plays beside its transcript, colored by speaker; click any line to jump there.
**Save** gives the transcript as a text file (.txt) or as subtitles (.srt, .vtt). **+ Transcribe a recording** at
the top of the Library adds more.

From a terminal the same thing is `ontherecord transcribe "C:\path\to\meeting.mp4" --wait` (Windows: run it from
the On the Record folder as `.venv\Scripts\ontherecord`; Mac and Linux: `.venv/bin/ontherecord`).

## Where your files are

**Setup** lists these with an **Open** button for each. A desktop install keeps everything in one folder,
`OnTheRecord` in your home folder:

| Folder | What is in it |
|---|---|
| `OnTheRecord/workspace` | Downloads, transcripts, receipts, the job list. Read it through the program; do not change files in it by hand. |
| `OnTheRecord/Archive` | Imports from cameras and cards (the Import tab), filed by project. An external drive is fine. |
| `OnTheRecord/models` | The language models. |
| your settings folder | `machine.yaml` (where the folders above are, which programs and models to use) and `secrets.env` (the token). Windows: `%APPDATA%\ontherecord`; Mac: `~/Library/Application Support/ontherecord`; Linux: `~/.config/ontherecord`. |

Your own recordings stay where they are: On the Record reads them in place and does not copy them. To move a folder,
change its line in `machine.yaml` and restart the program. Recordings of meetings can be sensitive; keep the
workspace off shared drives your ethics approval does not cover.

## Coming back later

The program reopens where you left it: the same tab, the last recording paused where you stopped, your Ask
conversation, and any recipe you were building. A banner says which jobs finished or failed while it was closed.

## When something goes wrong

- **Setup says the driver is too old**, or everything is slow on a machine with an NVIDIA card: update the driver
  (step 1), then run the installer again.
- **"no kernel image is available"** in a job's log: the PyTorch build does not match the card. Run the installer
  again; Setup's step 1 also says when the installed build is not the one the card needs.
- **The speaker step fails with "gated" or 403**: the account behind the token has not accepted pyannote's terms.
  Setup, step 4, **Check** says which.
- **A job runs out of graphics memory**: another program may be holding the card. Setup's step 1 says when it is
  (games, image generators and video editors are common culprits); close it and run the job again.
- **The models are missing** when you transcribe: download them in Setup, step 3. Runs never download anything by
  themselves.

`ontherecord setup --check` prints everything Setup's step 1 shows, for pasting into an email when asking for help.

## Updating, and for developers

To update, download the new ZIP into the same place and run the installer again; your recordings, archive and
settings are kept. Developers: `uv sync --extra all` then `uv run ontherecord --help`; see README.md and AGENTS.md.
The PyTorch build is a dependency group: `uv sync --extra all --no-default-groups --group cu126` (or `cpu`); the
default is `cu128`. A Docker image is described in docker/README.md.
