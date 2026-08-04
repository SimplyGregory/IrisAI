"""The Windows half of the platform interface.

Nothing new is invented here. Each function is the behaviour Iris already had,
moved behind a name that macos.py can also answer to, so the callers stopped
knowing which platform they are on.
"""

import os
import subprocess
import threading
from pathlib import Path

SHELL_TOOL_NAME = "run_powershell"
SHELL_DISPLAY_NAME = "PowerShell"

CREATE_NO_WINDOW = 0x08000000


def shell_argv(command: str) -> list[str]:
    """The argv that runs a command string through this system's shell."""
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]


def quiet_process() -> dict:
    """subprocess kwargs that keep a console window from appearing.

    A Windows-only problem: a GUI build has no console to inherit, so every
    child process without this opens a blank black window for as long as it
    runs. On a Mac there is nothing to suppress and this is empty.
    """
    return {"creationflags": CREATE_NO_WINDOW}


_local = threading.local()
_SAPI_RATE = 1  # SAPI range is -10..10; slightly quicker than default


def _sapi_voice():
    """One SAPI voice per thread, with COM initialised on that thread.

    SAPI objects are apartment-threaded, so this cannot be shared: confirmations
    are spoken from the tool worker thread while replies come from the main one.
    """
    voice = getattr(_local, "voice", None)
    if voice is None:
        import pythoncom
        import win32com.client

        try:
            pythoncom.CoInitialize()
        except Exception:
            pass  # already initialised on this thread
        voice = win32com.client.Dispatch("SAPI.SpVoice")

        from iris import config

        wanted = (config.VOICE or "").strip().lower()
        if wanted:
            for candidate in voice.GetVoices():
                if wanted in candidate.GetDescription().lower():
                    voice.Voice = candidate
                    break
        voice.Rate = _SAPI_RATE
        _local.voice = voice

    # Set every time rather than once: the voice object is cached per thread,
    # so a volume changed mid-session would otherwise never reach it.
    from iris import config

    try:
        voice.Volume = int(round(config.VOICE_VOLUME * 100))
    except Exception:
        pass
    return voice


def _speak_via_shell(text: str) -> bool:
    """Last resort: a separate process, so no in-process COM state can break it."""
    escaped = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {_SAPI_RATE}; $s.Speak('{escaped}')"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=120,
            **quiet_process(),
        )
        return True
    except Exception:
        return False


def speak_native(text: str) -> bool:
    """Speak through the OS voice. Only used when Piper is unavailable."""
    try:
        _sapi_voice().Speak(text)
        return True
    except Exception:
        _local.voice = None  # force a rebuild next time

    try:
        _sapi_voice().Speak(text)
        return True
    except Exception:
        return _speak_via_shell(text)


def list_voices() -> list[str]:
    """Names of every speech voice installed on this machine."""
    try:
        import pythoncom
        import win32com.client

        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        return [v.GetDescription() for v in speaker.GetVoices()]
    except Exception:
        return []


def default_install_dir() -> Path:
    from iris.paths import APP_NAME

    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / APP_NAME


def launch(target: str) -> subprocess.Popen | None:
    """Start an application or open a file by path."""
    return subprocess.Popen(["cmd", "/c", "start", "", target], **quiet_process())


def open_url(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def create_shortcut(target: Path, folder: Path, name: str = "Iris") -> str:
    """A Start menu entry, so it is launchable like anything else."""
    try:
        import win32com.client

        start_menu = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs"
        start_menu.mkdir(parents=True, exist_ok=True)
        link = start_menu / f"{name}.lnk"

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(link))
        shortcut.TargetPath = str(target)
        # Clicking a Start menu entry and having nothing happen is not a
        # behaviour anyone expects, so the shortcut always asks to be seen -
        # whether that starts her or brings up the copy already running.
        shortcut.Arguments = "--show"
        shortcut.WorkingDirectory = str(folder)
        shortcut.Description = "Iris - your assistant"
        icon = folder / "claude-logo.ico"
        if icon.is_file():
            shortcut.IconLocation = str(icon)
        shortcut.save()
        return str(link)
    except Exception as exc:  # noqa: BLE001 - reported to the wizard, not raised
        return f"(no shortcut: {exc})"


def theme() -> dict:
    """The system accent colour and whether the shell is in dark mode."""
    import sys

    from iris import paths

    sys.path.insert(0, str(paths.resource("panel")))
    import chrome

    return chrome.theme()


def list_windows() -> list[dict]:
    """Every visible window, as {app, title}."""
    import pygetwindow as gw

    return [{"app": "", "title": w.title} for w in gw.getAllWindows() if w.title.strip()]


def window_action(title_contains: str, action: str) -> tuple[bool, str]:
    import pygetwindow as gw

    wanted = title_contains.lower()
    match = next(
        (w for w in gw.getAllWindows() if w.title.strip() and wanted in w.title.lower()),
        None,
    )
    if match is None:
        return False, f"No window matching {title_contains!r}."

    try:
        if action == "minimize":
            match.minimize()
        elif action == "maximize":
            match.maximize()
        elif action == "restore":
            match.restore()
        elif action == "focus":
            match.activate()
        elif action == "close":
            match.close()
        else:
            return False, f"Unknown action {action!r}."
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return False, f"Could not {action} {match.title}: {exc}"
    return True, f"{action} on {match.title}."


def active_window_title() -> str:
    import pygetwindow as gw

    try:
        window = gw.getActiveWindow()
        return window.title if window else ""
    except Exception:
        return ""


def list_apps() -> dict[str, str]:
    """Installed applications, as {name: how to launch it}."""
    from iris.tools import apps

    return {name: target for name, (target, _kind) in apps._app_index().items()}


_INTERESTING = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
    "RadioButton", "Slider", "TabItem", "Text", "TreeItem", "Document",
}
_ACTIONABLE = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
    "RadioButton", "Slider", "TabItem", "TreeItem",
}
_desktop = None


def _get_desktop():
    global _desktop
    if _desktop is None:
        from pywinauto import Desktop

        _desktop = Desktop(backend="uia")
    return _desktop


def ui_elements(window_title: str, limit: int = 50) -> tuple[bool, list[dict]]:
    """The controls of a window, from UI Automation.

    Each element carries a "ref" the caller does not look inside - here the
    pywinauto control itself, on a Mac the names needed to find it again. That
    is what lets one accessibility tool serve two platforms whose models have
    nothing in common: an index into this list means the same thing on both.
    """
    try:
        window = _get_desktop().window(title_re=f".*{window_title}.*")
        controls = window.descendants(depth=7)
    except Exception:
        return False, []

    found = []
    for control in controls:
        try:
            kind = control.element_info.control_type
            name = control.window_text().strip()
            auto_id = (getattr(control.element_info, "automation_id", "") or "").strip()
        except Exception:
            continue
        if kind not in _INTERESTING:
            continue

        # An empty text box has no name - which is exactly the control you most
        # want to find, since an empty search field is the one you are about to
        # type into. Fall back to its automation id, which is usually
        # descriptive ("CommandSearchTextBox").
        label = name or (f"(unlabelled) id={auto_id}" if kind in _ACTIONABLE and auto_id else "")
        if not label:
            continue

        found.append({
            "kind": kind,
            "name": label,
            "actionable": kind in _ACTIONABLE,
            "ref": control,
        })
        if len(found) >= limit:
            break
    return True, found


def open_window_titles() -> list[str]:
    try:
        return [w.window_text() for w in _get_desktop().windows() if w.window_text().strip()]
    except Exception:
        return []


def ui_do(element: dict, action: str, text: str = "") -> tuple[bool, str]:
    control = element.get("ref")
    if control is None:
        return False, "That control is no longer available; inspect the window again."
    try:
        if action == "click":
            try:
                control.invoke()
            except Exception:
                # Not everything implements the invoke pattern; a real click on
                # the control's own coordinates is the honest fallback.
                control.click_input()
            return True, f"Clicked {element['name']}."
        if action == "set_text":
            control.set_edit_text(text)
            return True, f"Set {element['name']}."
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return False, f"Could not {action} {element['name']}: {exc}"
    return False, f"Unknown action {action!r}."


def permissions_missing() -> list[str]:
    """Nothing to grant. Windows lets a program click and screenshot freely.

    Present so callers never have to ask which platform they are on before
    checking - the macOS answer is the interesting one.
    """
    return []


def bridge_address() -> str:
    """Where the VS Code bridge listens, per editor window.

    A named pipe on Windows; the macOS build uses a socket file in the same
    folder the window registry already lives in.
    """
    return r"\\.\pipe\iris-vscode-"
