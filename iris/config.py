"""Central configuration. Everything is overridable via environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

from iris import paths

# From the install folder, not the working directory: launched from a shortcut
# or the Start menu, the cwd is anybody's guess.
load_dotenv(paths.env_file())


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- backend -------------------------------------------------------------
# api = Anthropic Messages API, billed per token against Console credits.
# sdk = Claude Agent SDK, which drives the Claude Code CLI and draws on your
#       Pro/Max subscription's usage limits instead. Same tools either way.
BACKEND = _env("IRIS_BACKEND", "api").lower()

# Path to the Claude Code CLI, only used by the sdk backend. Left blank it is
# auto-detected, including the copy bundled with the VS Code extension.
CLI_PATH = _env("IRIS_CLI_PATH", "") or None

# What the assistant calls itself. The wake word is separate and limited to
# the models openWakeWord ships, so this is the spoken persona, not the trigger.
ASSISTANT_NAME = _env("IRIS_NAME", "Iris")

# --- model ---------------------------------------------------------------
MODEL = _env("IRIS_MODEL", "claude-opus-5")
EFFORT = _env("IRIS_EFFORT", "medium")

# Whether Claude reasons before answering, and how much room it gets. Worth
# having off for "minimize chrome" and on for anything multi-step. Fixed when
# the session starts, so the panel restarts the conversation when you change it.
THINKING = _env("IRIS_THINKING", "1") == "1"
THINKING_BUDGET = int(_env("IRIS_THINKING_BUDGET", "4000"))
MAX_TOKENS = int(_env("IRIS_MAX_TOKENS", "8000"))

# How many previous user commands (with their full tool traces) stay in
# context. Keeps "minimize it" working without re-sending the whole day.
HISTORY_EXCHANGES = int(_env("IRIS_HISTORY", "4"))

# --- safety --------------------------------------------------------------
# off  = never ask
# risk = ask only for tools declared as destructive (default)
# all  = ask before every single tool call
CONFIRM_MODE = _env("IRIS_CONFIRM", "risk")

# Replace emails, phone numbers and card numbers in tool output with
# placeholders like [email 1]. The real values stay on this machine and can be
# revealed or used via reveal_redacted / copy_to_clipboard.
REDACT_PII = _env("IRIS_REDACT_PII", "1") == "1"

# Whether to keep a record of everything sent to the model. It lives inside
# the memory file below, under "transcript", so there is one file to find
# rather than two. Blank = off.
#
# What is written is the redacted text, so the record holds no real secrets -
# and it is append-only: the file tools refuse to write to it, so asking Iris
# to amend the record gets a refusal rather than an edit.
TRANSCRIPT = _env("IRIS_TRANSCRIPT", "1")

# Things Iris remembers between sessions, kept beside the program. Loaded into
# her instructions on every request. Blank = no memory.
#
# JSON rather than a markdown list because each memory carries a topic, and the
# topic is what lets her correct one later instead of saving a second, slightly
# different version of the same thing.
MEMORY_FILE = _env("IRIS_MEMORY", "memory.json")

# --- the editor ----------------------------------------------------------
# Whether the VS Code bridge extension is connected. Off means the two VS Code
# tools are not registered at all, so their schemas cost nothing on machines
# that do not use it. Turned on by the setup wizard.
VSCODE = _env("IRIS_VSCODE", "0") == "1"

# --- Discord ---------------------------------------------------------------
# Whether the discord_send tool is registered. It drives the real Discord web
# app in Iris's own Chrome profile - no API, no token. Off means the tool does
# not exist. Claude, when it is on, only ever chooses a name and a message;
# everything else is handled in Python and never reaches the model.
DISCORD = _env("IRIS_DISCORD", "0") == "1"

# Whether Discord's window is hidden off-screen while Iris works in it. On means
# it lives past the edge of the screen - a real, undetectable Chrome, unlike
# headless - and only comes into view for the one-time login. Off means you see
# it work. If window positioning ever misbehaves it falls back to visible, so
# this never stops a message being sent.
DISCORD_HIDDEN = _env("IRIS_DISCORD_HIDDEN", "1") == "1"

# --- the television --------------------------------------------------------
# A Roku on the local network, reached over its External Control Protocol.
# Nothing is installed on the television: every Roku serves this already. Off
# means the two Roku tools are not registered, so they cost nothing here.
ROKU = _env("IRIS_ROKU", "0") == "1"

# Found by the setup wizard, which asks the network rather than the user. Worth
# re-running setup if the router hands it a different address later.
ROKU_IP = _env("IRIS_ROKU_IP", "")

# --- paths ---------------------------------------------------------------
STATE_DIR = Path.home() / ".iris"
STATE_DIR.mkdir(exist_ok=True)

# --- browser -------------------------------------------------------------
CDP_PORT = int(_env("IRIS_CDP_PORT", "9222"))
CHROME_PROFILE_DIR = STATE_DIR / "chrome-profile"
USE_REAL_CHROME_PROFILE = _env("IRIS_CHROME_REAL_PROFILE", "0") == "1"

# Where Chrome installs itself, which is nothing alike on the two systems: an
# .exe under Program Files, or the binary buried inside an .app bundle. Both
# lists are always defined and only the matching one can hit, so this needs no
# platform check - a Windows path simply never exists on a Mac.
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe",
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Chromium, for a Mac that has it instead. Iris drives it over the same
    # debugging port, so which of the two it is makes no difference past here.
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
]


def chrome_path() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


# --- voice ---------------------------------------------------------------
# Fixed speech-detection threshold. Blank means calibrate from room tone on
# every recording, which is what you want unless that is misbehaving. Lower =
# more sensitive. Compare against the numbers from `panel/app.py --mic-test`.
SPEECH_THRESHOLD = float(_env("IRIS_SPEECH_THRESHOLD", "0") or 0) or None

# Soft periodic tone while Iris is working, so silence does not look like
# a crash. Quiet by design; set to 0 for no sound at all.
# Which speech voice to use; partial name match, e.g. "Zira" or "David".
# Blank uses the Windows default. List them: python panel/app.py --voices
# piper = local neural voice (natural, offline, needs a downloaded model)
# sapi  = the built-in Windows voices (David / Zira) - robotic but always there
# auto  = piper if a model is present, otherwise sapi
TTS_ENGINE = _env("IRIS_TTS", "auto").lower()

# Which voice. For piper, part of a model name such as "jenny" or "amy".
# For sapi, part of a Windows voice name such as "Zira".
# List them with: python panel/app.py --voices
VOICE = _env("IRIS_VOICE", "")

# Text mode (main.py) and the panel speak their replies aloud as well as
# printing them, using the same voice as voice mode. Toggle mid-session with
# /mute and /speak, or just ask her to be quiet.
SPEAK_REPLIES = _env("IRIS_SPEAK", "1") == "1"

# How loud she is, 0.0 to 1.0, applied to whichever speech engine is in use.
# Separate from the Windows volume mixer: this is her level, not the system's.
VOICE_VOLUME = max(0.0, min(1.0, float(_env("IRIS_VOICE_VOLUME", "1.0"))))

# Say the wake word while Iris is working to pause her and steer: cancel,
# continue, or give a correction to apply first.
BARGE_IN = _env("IRIS_BARGE_IN", "1") == "1"

SOUND_CUES = _env("IRIS_SOUND_CUES", "1") == "1"

# How loud that tone is, 0.0 to 1.0. Roughly a quarter of speech volume by
# default. Preview it with: python -c "from iris.voice import cues; cues.preview()"
CUE_VOLUME = max(0.0, min(1.0, float(_env("IRIS_CUE_VOLUME", "0.15"))))

# --- the wake word --------------------------------------------------------
# Whisper listens for whatever you choose, so this is free text rather than a
# menu. Comma-separate several and any of them wakes her: "hey iris" is much
# easier for a speech model to get right - more audio, more context, and a
# carrier word to absorb the clipping when speech detection starts a moment
# late - while plain "iris" still works when it comes through cleanly.
WAKE_PHRASE = _env("IRIS_WAKE_PHRASE", "hey iris, iris")
WAKE_FUZZ = float(_env("IRIS_WAKE_FUZZ", "0.75"))
WAKE_STT_MODEL = _env("IRIS_WAKE_STT_MODEL", "tiny.en")
WAKE_DEBUG = _env("IRIS_WAKE_DEBUG", "0") == "1"

WHISPER_MODEL = _env("IRIS_WHISPER_MODEL", "base.en")
SAMPLE_RATE = 16000
