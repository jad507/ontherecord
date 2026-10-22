# Installing On the Record

You need: a computer with Windows 10 or 11, macOS 13 or later, or Linux; about 40 GB of free disk space; and an
internet connection for the first install. An NVIDIA graphics card (or a Mac with Apple silicon) makes it much
faster, but it also runs without one.

## 1. Get the files

Download On the Record as a ZIP (on its GitHub page: the green **Code** button, then **Download ZIP**) and unzip it
somewhere you will keep it, for example your Documents folder. Do not move the folder after installing.

## 2. Run the installer

- **Windows:** open the folder, then the `install` folder, and double-click **windows-install.bat**. If Windows
  says it protected your PC, click **More info**, then **Run anyway**.
- **Mac:** open the `install` folder, right-click **macos-install.command** and choose **Open** (double-clicking
  a downloaded script is blocked the first time).
- **Linux:** in a terminal, `bash install/linux-install.sh`.

The installer downloads Python and everything On the Record needs, checks that each download is exactly the
published file, and asks before downloading the models (several GB).

## 3. The speaker model needs a free account

Telling speakers apart uses pyannote's model, which asks you to accept its terms first:

1. Make a free account at https://huggingface.co and sign in.
2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the terms.
3. Open https://huggingface.co/settings/tokens, create a token with **Read** access, and copy it.
4. Paste it when the installer asks. It is kept on your computer only, and never written into results.

You can skip this and do it later: `ontherecord models get pyannote-community-1 --token YOUR_TOKEN --save-token`.

## 4. Start it

Double-click **On the Record** on your desktop. A window shows that it is running, and your browser opens the
program. Close that window to stop it. Everything stays on your computer.

## Updating, and for developers

To update, download the new ZIP into the same place and run the installer again; your recordings, archive and
settings are kept (they live in the `OnTheRecord` folder in your home folder). Developers: `uv sync --extra all`
then `uv run ontherecord --help`; see README.md and AGENTS.md. A Docker image is described in docker/README.md.
