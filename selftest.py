#!/usr/bin/env python3
"""Check every part of Iris on this machine, then build it.

    python3 selftest.py              run the checks, then build if they all pass
    python3 selftest.py --no-build   checks only
    python3 selftest.py --speak      also say something out loud
    python3 selftest.py --panel      also open the panel window (needs a screen)

Written for the macOS port, where nothing has ever run and the useful question
is not "does it work" but "which part does not". So every section is caught
separately and reported on its own line: one failure does not stop the rest,
because the second failure is usually the informative one.

Python rather than a shell script for the same reason it is one file and not
two - it runs on both machines, and a check that only exists on the platform it
was written for is a check nobody runs.
"""

import argparse
import io
import platform as host
import subprocess
import sys
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "panel"))

# ANSI, which macOS Terminal and Windows Terminal both understand. Falls back
# to plain text when the output is piped to a file, where escape codes would be
# noise in the log someone pastes back.
_TTY = sys.stdout.isatty()
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""

results: list[tuple[str, str, str]] = []  # (state, name, detail)


def _printable(text) -> str:
    """Text this console can actually print.

    Found by this script failing on itself: a weather report came back with a
    sun in it, and the Windows console is cp1252, so printing the result of a
    passing check raised UnicodeEncodeError and reported it as a failure. A
    diagnostic that breaks while describing what it found is worse than no
    diagnostic, so anything on its way to the screen goes through here.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(text).encode(encoding, errors="replace").decode(encoding, errors="replace")


class Skip(Exception):
    """Not applicable here, which is not a failure."""


def check(name: str, fn) -> bool:
    """Run one section. Never raises: that is the entire point of this file."""
    print(f"  {name} ... ", end="", flush=True)
    started = time.time()
    try:
        # Some of the code under test prints; it would break up the report.
        captured = io.StringIO()
        with redirect_stdout(captured):
            detail = fn()
        took = time.time() - started
        print(f"{GREEN}ok{OFF}  {DIM}{took:.1f}s{OFF}")
        if detail:
            for line in _printable(detail).splitlines():
                print(f"      {DIM}{line}{OFF}")
        results.append(("ok", name, str(detail or "")))
        return True
    except Skip as exc:
        print(f"{YELLOW}skipped{OFF}  {DIM}{_printable(exc)}{OFF}")
        results.append(("skip", name, str(exc)))
        return True
    except Exception as exc:
        print(f"{RED}FAILED{OFF}")
        # The last frame is where it actually broke; the rest is this file.
        lines = _printable(traceback.format_exc()).strip().splitlines()
        for line in lines[-4:]:
            print(f"      {RED}{line.strip()}{OFF}")
        results.append(("fail", name, _printable(f"{type(exc).__name__}: {exc}")))
        return False


# --- the sections ----------------------------------------------------------


def python_version():
    if sys.version_info < (3, 11):
        raise RuntimeError(f"Python 3.11+ needed, this is {host.python_version()}")
    return f"Python {host.python_version()} on {host.system()} {host.release()} ({host.machine()})"


def dependencies():
    missing = []
    for module, why in (
        ("anthropic", "the API client"),
        ("dotenv", "reading .env"),
        ("webview", "the panel window"),
        ("mss", "screenshots"),
        ("pyautogui", "clicking and typing"),
        ("PIL", "image handling"),
    ):
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module} ({why}): {exc}")
    if missing:
        raise RuntimeError("; ".join(missing))
    return "core packages import"


def platform_layer():
    from iris import platform as plat

    name = plat.name()
    argv = plat.shell_argv("echo hi")
    where = plat.default_install_dir()
    if name == "macos" and argv[0] != "/bin/zsh":
        raise RuntimeError(f"macOS should use zsh, got {argv[0]}")
    if name == "windows" and "powershell" not in argv[0].lower():
        raise RuntimeError(f"Windows should use PowerShell, got {argv[0]}")
    return f"detected {name}\nshell: {' '.join(argv[:3])}\ninstall dir: {where}"


def permissions():
    from iris import platform as plat

    if not plat.is_macos():
        raise Skip("Windows grants these by default")
    missing = plat.permissions_missing()
    if missing:
        # A real state of the machine, not a broken build - but the user has to
        # act on it, so it is louder than a pass and quieter than a failure.
        return "STILL TO GRANT:\n  " + "\n  ".join(missing)
    return "Accessibility and Screen Recording granted"


def configuration():
    from iris import config, paths

    env = paths.env_file()
    if not Path(env).is_file():
        raise Skip(f"no .env yet at {env} - run: python3 IrisAI.py --setup")
    return (
        f"backend: {config.BACKEND}\nmodel: {config.MODEL}\n"
        f"confirm: {config.CONFIRM_MODE}\nvscode bridge: {'on' if config.VSCODE else 'off'}"
    )


def memory_store():
    from iris import memory

    path = memory.path()
    listing = memory.listing()
    count = len([line for line in listing.splitlines() if line.strip()])
    return f"{path}\n{count} memory line(s) readable"


def redaction():
    from iris import redact

    redact.reset()
    secret = "https://site.com/cb?access_token=ya29AbCdEf1234567890XyZwVuT"
    hidden = redact.scrub(secret)
    if "ya29" in hidden:
        raise RuntimeError("a credential was not redacted")
    if redact.resolve(hidden) != secret:
        raise RuntimeError("a redacted value did not survive the round trip")

    public = "https://www.youtube.com/feeds/videos.xml?channel_id=UCBJycsmduvYEL83R_U4JriQ"
    if redact.scrub(public) != public:
        raise RuntimeError("a public channel id was redacted")
    return "credentials hidden, round trip exact, public ids left alone"


def tool_registry():
    from iris import tools

    names = [t.name for t in tools.ALL_TOOLS]
    if "run_shell" not in names:
        raise RuntimeError(f"run_shell is missing; found {len(names)} tools")
    return f"{len(names)} tools registered"


def system_prompt():
    from iris.agent import Iris
    from iris import platform as plat

    text = Iris.system_text()
    expect = "Mac" if plat.is_macos() else "Windows PC"
    if expect not in text:
        raise RuntimeError(f"the prompt does not mention {expect!r}")
    return f"{len(text)} characters, describes a {expect}"


def shell_execution():
    from iris import platform as plat

    argv = plat.shell_argv('echo "hello world"')
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          **plat.quiet_process())
    out = done.stdout.strip()
    if out != "hello world":
        raise RuntimeError(f"expected 'hello world', got {out!r} (rc={done.returncode})")
    return "a quoted phrase came back as one string"


def window_listing():
    from iris import platform as plat

    found = plat.list_windows()
    if not found:
        raise RuntimeError("no windows found at all, which should never happen")
    titles = [w["title"] for w in found[:3]]
    return f"{len(found)} window(s), e.g. " + "; ".join(titles)


def application_listing():
    from iris import platform as plat

    apps = plat.list_apps()
    if len(apps) < 5:
        raise RuntimeError(f"only {len(apps)} applications found, which looks wrong")
    return f"{len(apps)} applications"


def screenshot_capture():
    from iris.tools import screen

    image, _monitor = screen._grab()
    if image.size[0] < 100:
        raise RuntimeError(f"the screen came back as {image.size}")
    return f"captured {image.size[0]}x{image.size[1]}"


def speech_voices():
    from iris.voice import tts

    piper = tts.available_piper_voices()
    system = tts.list_voices()
    if not piper and not system:
        raise RuntimeError("no voices at all: neither a Piper model nor a system voice")
    return f"piper models: {piper or 'none'}\nsystem voices: {len(system)}"


def speech_out_loud():
    from iris.voice import tts

    tts.speak("Self test. Speech is working.")
    return "spoke a line (you should have heard it)"


def speech_recognition():
    from iris import config

    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:
        raise Skip(f"faster-whisper not installed ({exc})")
    import sounddevice as sd

    inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    if not inputs:
        raise RuntimeError("no microphone found")
    return f"model: {config.WHISPER_MODEL}\n{len(inputs)} input device(s), default: {inputs[0]['name']}"


def network_fetch():
    from iris.tools import info

    text = info.fetch_url.func(url="https://wttr.in/London?format=3")
    if "London" not in text:
        raise RuntimeError(f"unexpected reply: {text[:120]}")
    # Past the wrapper. Every fetch is framed with a "this came from the
    # internet" banner that names the URL, so the first line mentioning London
    # is that banner rather than the weather.
    weather = [
        line.strip() for line in text.splitlines()
        if "London" in line and "Content from" not in line and "UNTRUSTED" not in line
    ]
    if not weather:
        raise RuntimeError("the reply had no weather line in it")
    return weather[0]


def browser_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:
        raise Skip(f"playwright not installed ({exc})")
    from iris import config

    # No platform special case here on purpose: chrome_path() knows where
    # Chrome lives on both, and a check that works around the code under test
    # is a check that passes while the real thing is broken.
    chrome = config.chrome_path()
    if not chrome:
        raise Skip("Chrome is not installed")
    return f"chrome: {chrome}"


def vscode_bridge():
    from installer import vsix

    cli = vsix.find_cli()
    if not cli:
        raise Skip("VS Code is not installed")

    from iris import editor

    live = editor.windows()
    installed = vsix.installed_version()
    if not installed:
        return f"cli: {cli}\nextension not installed yet - the wizard installs it"
    if not live:
        return f"extension {installed} installed, but no window is serving it yet\n(quit VS Code fully and reopen - a reload is not enough)"
    got = editor.call("ping")
    return f"extension {installed}, {len(live)} window(s), ping -> VS Code {got.get('vscode')}"


def web_search_ready():
    from iris import config, gemini

    if not config.GEMINI_KEY:
        raise Skip("no Gemini key - web search is off; fetch_url still works")

    found = gemini.search("What year is it? Answer in one word.")
    answer = found["answer"].strip().splitlines()[0][:60]
    return "\n".join([
        f"key works, Google answered: {answer}",
        f"{len(found['sources'])} source(s) cited",
    ])


def roku_connection():
    from iris import config, roku

    if not config.ROKU:
        raise Skip("no Roku connected - the wizard can find one")
    if not config.ROKU_IP:
        raise Skip("connected but no address saved; run setup again")

    info = roku.device_info(config.ROKU_IP)
    channels = roku.apps(config.ROKU_IP)
    name = info.get("user-device-name") or info.get("model-name", "?")
    return "\n".join([
        f"{name} at {config.ROKU_IP}",
        f"{info.get('model-name', '?')}, software {info.get('software-version', '?')}",
        f"{len(channels)} channel(s) installed, screen {info.get('power-mode', '?')}",
    ])


def panel_geometry():
    import chrome

    x, y, width, height = chrome.dock_flyout(360, 0.5, 16)
    if width < 100 or height < 100:
        raise RuntimeError(f"nonsense geometry: {width}x{height} at {x},{y}")
    theme = chrome.theme()
    return f"{width}x{height} at {x},{y}\ntheme: {'dark' if theme['dark'] else 'light'}, accent {theme['accent']}"


def panel_modules():
    import app  # noqa: F401
    import bridge  # noqa: F401
    import hotkey

    from iris import config

    combo = config._env("IRIS_PANEL_HOTKEY", "ctrl+alt+j")
    label = hotkey.parse(combo)[2]
    return f"panel imports; hotkey {combo} shows as {label}"


def panel_window():
    """Actually open it. Needs a screen, so it is behind --panel."""
    launcher = [sys.executable, str(ROOT / "panel" / "app.py"), "--show"]
    process = subprocess.Popen(launcher)
    time.sleep(6)
    if process.poll() is not None:
        raise RuntimeError(f"the panel exited immediately (code {process.returncode})")
    process.terminate()
    return "the panel started and stayed up for 6s (look at it before it closes)"


# --- building --------------------------------------------------------------


def _run_showing_output(argv: list[str]) -> int:
    """Run a command and print what it says, line by line as it says it.

    Captured explicitly rather than letting the child inherit this terminal,
    which looks like the simpler option and does not work here. Checking that
    the panel imports means importing panel/app.py, and that calls
    spawn.hide_console_children() at module level - which patches
    subprocess.Popen process-wide to add CREATE_NO_WINDOW so the app never
    flashes a blank console. A child started afterwards then prints nowhere.

    Right for the app, fatal here: it silently swallowed every line PyInstaller
    produced, so a failed build would have reported nothing at all - in the one
    script whose entire job is saying what went wrong.
    """
    process = subprocess.Popen(
        argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )
    for line in process.stdout:
        print(line.rstrip())
    return process.wait()


def build() -> int:
    """Build for whichever machine this is."""
    if host.system() == "Darwin":
        script = ROOT / "build_macos.sh"
        if not script.is_file():
            print(f"  {RED}build_macos.sh is missing{OFF}")
            return 1
        print("\n  building the macOS app - this takes several minutes\n")
        script.chmod(0o755)
        return _run_showing_output(["/bin/bash", str(script)])

    script = ROOT / "build.py"
    if not script.is_file():
        print(f"  {RED}build.py is missing{OFF}")
        return 1
    print("\n  building the Windows exe - this takes a few minutes\n")
    return _run_showing_output([sys.executable, str(script)])


# --- the run ---------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Iris on this machine, then build it.")
    parser.add_argument("--no-build", action="store_true", help="run the checks only")
    parser.add_argument("--speak", action="store_true", help="also say a line out loud")
    parser.add_argument("--panel", action="store_true", help="also open the panel window")
    args = parser.parse_args()

    print(f"\n{DIM}Iris self test - {host.system()} {host.release()} ({host.machine()}){OFF}\n")

    print("The machine")
    check("python version        ", python_version)
    check("dependencies          ", dependencies)
    check("platform layer        ", platform_layer)
    check("macos permissions     ", permissions)

    print("\nThe install")
    check("configuration         ", configuration)
    check("memory store          ", memory_store)
    check("redaction             ", redaction)

    print("\nThe agent")
    check("tool registry         ", tool_registry)
    check("system prompt         ", system_prompt)

    print("\nControlling the machine")
    check("shell execution       ", shell_execution)
    check("window listing        ", window_listing)
    check("application listing   ", application_listing)
    check("screen capture        ", screenshot_capture)

    print("\nVoice")
    check("voices available      ", speech_voices)
    if args.speak:
        check("speaking out loud     ", speech_out_loud)
    check("microphone and model  ", speech_recognition)

    print("\nThe outside world")
    check("fetching a url        ", network_fetch)
    check("browser               ", browser_available)
    check("vs code bridge        ", vscode_bridge)
    check("web search (gemini)   ", web_search_ready)
    check("roku                  ", roku_connection)

    print("\nThe panel")
    check("panel modules         ", panel_modules)
    check("panel geometry        ", panel_geometry)
    if args.panel:
        check("opening the window    ", panel_window)

    # --- the verdict --------------------------------------------------------

    failed = [name.strip() for state, name, _ in results if state == "fail"]
    skipped = [name.strip() for state, name, _ in results if state == "skip"]
    passed = sum(1 for state, _, _ in results if state == "ok")

    print(f"\n{'-' * 62}")
    print(f"  {GREEN}{passed} passed{OFF}"
          + (f"   {YELLOW}{len(skipped)} skipped{OFF}" if skipped else "")
          + (f"   {RED}{len(failed)} failed{OFF}" if failed else ""))

    if skipped:
        print(f"  {DIM}skipped: {', '.join(skipped)}{OFF}")

    if failed:
        print(f"\n  {RED}not building, because these failed:{OFF}")
        for state, name, detail in results:
            if state == "fail":
                print(f"    {RED}{name.strip()}{OFF}: {_printable(detail)}")
        print(f"\n  {DIM}Send this whole output back and it can be fixed.{OFF}\n")
        return 1

    print(f"\n  {GREEN}everything works.{OFF}")
    if args.no_build:
        print(f"  {DIM}--no-build given, so stopping here.{OFF}\n")
        return 0

    return build()


if __name__ == "__main__":
    sys.exit(main())
