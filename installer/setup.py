"""Creating the IrisAI folder and everything that goes in it.

Separate from the wizard on purpose: the wizard collects answers, this turns
answers into an install. That means the install can be tested without a window
in front of it, and re-run over an existing folder without harm.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from iris import paths

# What the wizard asks about. Everything else keeps the defaults in config.py -
# a settings screen with forty rows on it teaches nobody anything, and the .env
# it writes is commented for whoever goes looking later.
DEFAULTS = {
    "backend": "sdk",
    "api_key": "",
    "cli_path": "",
    "model": "claude-opus-5",
    "effort": "medium",
    "max_tokens": 8000,
    "history": 4,
    "confirm": "guarded",
    "redact_pii": True,
    "real_chrome_profile": False,
    "cdp_port": 9222,
    "vscode": False,
    "roku": False,
    "roku_ip": "",
    "gemini_key": "",
}

MODELS = [
    ("claude-opus-5", "Opus 5", "Best at picking the right tool. The default."),
    ("claude-sonnet-5", "Sonnet 5", "Faster and cheaper, still strong at tool use."),
    ("claude-haiku-4-5-20251001", "Haiku 4.5", "Quickest, for simple errands."),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SAFETY = [
    ("manual", "Manual", "Ask before every action. Good while you learn to trust it."),
    ("guarded", "Guarded", "Ask only before risky ones: commands, file edits."),
    ("auto", "Auto", "Never ask. Iris acts without checking."),
]
CONFIRM_FOR = {"manual": "all", "guarded": "risk", "auto": "off"}


def env_text(choices: dict) -> str:
    """The .env this install will run on, commented for a human reader."""
    c = {**DEFAULTS, **choices}
    sdk = c["backend"] == "sdk"

    return f"""# =============================================================================
#  Iris - written by the setup wizard. Safe to edit by hand afterwards.
# =============================================================================

# --- Backend ---------------------------------------------------------------
# sdk = Claude Agent SDK, drawing on your Claude subscription.
# api = Anthropic Messages API, billed per token against Console credits.
IRIS_BACKEND={c["backend"]}

# Path to the Claude Code CLI. Blank auto-detects, including the copy inside
# the VS Code extension.
IRIS_CLI_PATH={c["cli_path"] if sdk else ""}

# Only used when IRIS_BACKEND=api. Ignored entirely on the sdk backend.
ANTHROPIC_API_KEY={c["api_key"] if not sdk else ""}


# --- Model -----------------------------------------------------------------
IRIS_MODEL={c["model"]}

# How hard Claude thinks before acting: low | medium | high | xhigh | max
IRIS_EFFORT={c["effort"]}

# Ceiling on tokens per reply. Raise if long chains get cut off mid-task.
IRIS_MAX_TOKENS={c["max_tokens"]}

# How many past commands stay in context, so "minimize it" knows what "it" is.
IRIS_HISTORY={c["history"]}


# --- Safety ----------------------------------------------------------------
# all  = ask before every action      (Manual)
# risk = ask only before risky ones   (Guarded)
# off  = never ask                    (Auto)
# Changeable at any time from the chip under the message box.
IRIS_CONFIRM={CONFIRM_FOR[c["confirm"]]}

# 1 = replace emails, phone numbers and card numbers in tool output with
#     placeholders. The real values never leave this machine.
# Credentials are always redacted regardless of this setting.
IRIS_REDACT_PII={1 if c["redact_pii"] else 0}


# --- Browser ---------------------------------------------------------------
# 0 = drive a separate Chrome profile, so Iris never fights the browser you
#     already have open. Sign in once in that window and it stays signed in.
# 1 = use your everyday Chrome profile. All Chrome windows must be closed
#     before Iris launches it, because Chrome only opens its debugging port
#     at startup.
IRIS_CHROME_REAL_PROFILE={1 if c["real_chrome_profile"] else 0}

# DevTools port Iris attaches to. Change only if something else uses it.
IRIS_CDP_PORT={c["cdp_port"]}


# --- Editor ----------------------------------------------------------------
# 1 = the VS Code bridge extension is connected, so Iris can read the errors
#     your language server reports, see what you have open and selected, and
#     edit through the editor rather than underneath it.
# 0 = off, and the two VS Code tools are not loaded at all - so they cost
#     nothing on a machine that does not use them.
IRIS_VSCODE={1 if c["vscode"] else 0}


# --- Web search ------------------------------------------------------------
# A Google AI Studio key. With one, Iris can search the web and get an answer
# with its sources, instead of guessing at addresses. Free from
# aistudio.google.com. Blank means the web_search tool is not loaded at all -
# she can still read any page whose address she already knows.
IRIS_GEMINI_KEY={c["gemini_key"]}

# Which model does the searching. A free key does not reach every model, and
# Google says "no quota for this" with the same 429 it uses for real rate
# limiting - so if search is refused immediately rather than after a burst,
# try another here.
IRIS_GEMINI_MODEL=gemini-3.5-flash-lite


# --- Television ------------------------------------------------------------
# 1 = a Roku on the local network is connected, so Iris can open channels,
#     play and pause, change the volume and press remote keys. Nothing is
#     installed on the television; every Roku already answers on port 8060.
# 0 = off, and the two Roku tools are not loaded at all.
IRIS_ROKU={1 if c["roku"] else 0}

# Which one, found by the wizard. If the router later hands it a different
# address, run setup again rather than guessing.
IRIS_ROKU_IP={c["roku_ip"]}


# --- Panel -----------------------------------------------------------------
# Modifiers plus one key. Anything with M is refused by the shell.
IRIS_PANEL_HOTKEY=ctrl+alt+j


# --- Voice -----------------------------------------------------------------
IRIS_SPEAK=1
IRIS_VOICE_VOLUME=1.0
IRIS_WAKE_PHRASE=hey iris, iris
IRIS_WAKE_FUZZ=0.75

# The soft ding-ding while Iris is working, so a long silence does not look
# like a crash. It waits half a second first, so quick answers stay silent,
# and pauses itself whenever she needs to hear you. 0 turns it off.
IRIS_SOUND_CUES=1
IRIS_CUE_VOLUME=0.15

# How loud counts as speech. Blank measures the room on every recording, which
# is usually right. Set by `IrisAI.exe --calibrate` if the wake word is missing
# you, or triggering on nothing.
IRIS_SPEECH_THRESHOLD=
"""


def validate(choices: dict) -> str:
    """Why this cannot be installed yet, or "" if it can."""
    c = {**DEFAULTS, **choices}

    if c["backend"] == "api" and not c["api_key"].strip():
        return "An API key is needed for the Anthropic API backend."
    if c["backend"] == "sdk" and c["cli_path"].strip():
        if not Path(c["cli_path"]).expanduser().is_file():
            return f"No file at {c['cli_path']}. Leave it blank to auto-detect."
    try:
        port = int(c["cdp_port"])
    except (TypeError, ValueError):
        return "The DevTools port has to be a number."
    if not 1024 <= port <= 65535:
        return "The DevTools port has to be between 1024 and 65535."
    if not 1000 <= int(c["max_tokens"]) <= 64000:
        return "Max tokens per reply should be between 1,000 and 64,000."
    if not 0 <= int(c["history"]) <= 50:
        return "History should be between 0 and 50 commands."
    return ""


def _shortcut(target_exe: Path, folder: Path, name: str = "Iris") -> str:
    """Make Iris launchable the way anything else on this system is.

    A Start menu entry on Windows, a place in /Applications on a Mac. The two
    have nothing in common beyond the intention, so the intention is what lives
    here and the platform layer does the rest.
    """
    from iris import platform

    return platform.create_shortcut(target_exe, folder, name)


def install(choices: dict, target: Path) -> dict:
    """Create the folder and fill it. Safe to run again over an existing one."""
    target = Path(target).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    done = []

    (target / ".env").write_text(env_text(choices), encoding="utf-8")
    done.append(".env")

    # Pre-created so the first save has somewhere to go and the file is there
    # to look at from the start.
    store = target / "memory.json"
    if not store.is_file():
        store.write_text(
            json.dumps({"version": 1, "memories": [], "transcript": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        done.append("memory.json")

    # A built copy installs itself. It is a one-folder build, so the exe alone
    # is not enough - the runtime sits beside it in _internal and has to come
    # too. Anything the user owns is skipped, so re-running setup over an
    # existing install replaces the program and leaves the memories alone.
    launcher = target / "IrisAI.exe"
    if paths.is_frozen():
        source = Path(sys.executable).resolve().parent
        if source != target:
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".env", "memory.json"),
            )
            done.append("IrisAI.exe and its runtime")
    else:
        launcher = Path(sys.executable)

    # The editor extension, if it was asked for. Deliberately last and
    # deliberately not fatal: VS Code refusing it should leave a working Iris
    # that cannot see the editor, not a failed install.
    editor = None
    if {**DEFAULTS, **choices}["vscode"]:
        from installer import vsix

        editor = vsix.install(target)
        done.append("VS Code extension" if editor["ok"] else f"VS Code extension failed: {editor['problem']}")

    link = _shortcut(launcher, target)
    return {"folder": str(target), "created": done, "shortcut": link, "editor": editor}
