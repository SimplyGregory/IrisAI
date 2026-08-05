"""The macOS half of the platform interface.

Written on a Windows machine against Apple's documentation, so treat every
function here as unverified until it has run on a Mac. Where there was a choice
between a clever approach and one that fails loudly, this takes the loud one:
a first port is debugged by reading error messages, and silence is the enemy.

Three things behave differently from Windows in ways no code here can fix, and
they are worth knowing before debugging something that is not broken:

  - Screenshots need Screen Recording, and clicking or typing needs
    Accessibility. Until the user grants those in System Settings, the calls
    succeed and do nothing at all. permissions_missing() below exists so Iris
    can say which one is missing rather than appearing to ignore an instruction.
  - Controlling another application needs Automation permission, granted per
    application, the first time it is asked for.
  - An unsigned build is quarantined by Gatekeeper. Removing that means signing
    and notarising with a paid Apple developer account; without one, the first
    launch is right-click, Open, and a warning.
"""

import os
import shutil
import subprocess
from pathlib import Path

SHELL_TOOL_NAME = "run_shell"
SHELL_DISPLAY_NAME = "the shell"


def shell_argv(command: str) -> list[str]:
    """The argv that runs a command string through this system's shell.

    zsh has been the default since Catalina, and -l loads the user's profile so
    that tools installed by Homebrew are on PATH - which is where most of what
    anyone wants to run actually lives.
    """
    return ["/bin/zsh", "-l", "-c", command]


def quiet_process() -> dict:
    """No equivalent problem here: a child process opens no window of its own."""
    return {}


def speak_native(text: str) -> bool:
    """Speak through the built-in `say`, which every Mac has."""
    from iris import config

    argv = ["/usr/bin/say"]
    wanted = (config.VOICE or "").strip()
    if wanted:
        argv += ["-v", wanted]
    try:
        subprocess.run([*argv, text], capture_output=True, timeout=120)
        return True
    except Exception:
        return False


def list_voices() -> list[str]:
    try:
        listed = subprocess.run(
            ["/usr/bin/say", "-v", "?"], capture_output=True, text=True, timeout=20
        )
    except Exception:
        return []
    # Each line is "Name    en_GB    # a sample sentence", and only the name
    # can be passed back to `say -v`.
    return [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]


def default_install_dir() -> Path:
    """Under Application Support, not /Applications.

    The .app bundle goes to /Applications, but this is the writable folder
    beside it that holds .env and memory.json - and /Applications is not
    writable without authenticating, which a first run should not demand.
    """
    from iris.paths import APP_NAME

    return Path.home() / "Library" / "Application Support" / APP_NAME


def launch(target: str) -> subprocess.Popen | None:
    """Start an application or open a file.

    `open` takes both an app name and a path, and picks the registered handler
    the way double-clicking would - the closest thing to what Windows does with
    a Start menu shortcut.
    """
    argv = ["/usr/bin/open"]
    if not Path(target).exists():
        argv.append("-a")  # a name rather than a path, e.g. "Safari"
    return subprocess.Popen([*argv, target])


def open_url(url: str) -> None:
    subprocess.Popen(["/usr/bin/open", url])


def create_shortcut(target: Path, folder: Path, name: str = "Iris") -> str:
    """The Mac equivalent of a Start menu entry is being in /Applications.

    An installed .app is already in Spotlight and Launchpad, so there is
    nothing to create - except a symlink when the bundle lives somewhere else,
    which is what a developer run looks like.
    """
    applications = Path("/Applications")
    bundle = target if target.suffix == ".app" else target.parent
    if bundle.suffix != ".app":
        return "(no shortcut: not an .app bundle, so nothing to link)"
    link = applications / f"{name}.app"
    try:
        if link.exists() or link.is_symlink():
            return str(link)
        link.symlink_to(bundle)
        return str(link)
    except OSError as exc:
        return f"(no shortcut: {exc})"


def theme() -> dict:
    """The system accent colour and whether the Mac is in dark mode.

    Both come from the global preferences domain. AppleInterfaceStyle exists
    only in dark mode - light mode leaves the key absent rather than setting it
    to "Light", so a missing key is the answer, not a failure.
    """
    dark = _defaults_read("AppleInterfaceStyle") == "Dark"

    # AppleAccentColor is an index, not a colour, and is absent when the user
    # has left it on the default blue.
    accents = {
        "-1": "#8E8E93", "0": "#FF3B30", "1": "#FF9500", "2": "#FFCC00",
        "3": "#28CD41", "4": "#007AFF", "5": "#AF52DE", "6": "#FF2D55",
    }
    index = _defaults_read("AppleAccentColor")
    accent = accents.get(index or "", "#007AFF")
    return {"dark": dark, "accent": accent}


def _defaults_read(key: str) -> str | None:
    try:
        got = subprocess.run(
            ["/usr/bin/defaults", "read", "-g", key],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    return got.stdout.strip() if got.returncode == 0 else None


def bridge_address() -> str:
    """Where the VS Code bridge listens, per editor window.

    Windows uses a named pipe; here it is a Unix socket beside the window
    registry the extension already writes to. Node's net module serves both
    from the same call, so only the address shape differs.
    """
    return str(Path.home() / ".iris" / "vscode" / "sock-")


# --- the permissions that make everything else work ------------------------

def hide_offscreen_window():
    """No taskbar to clear here. An off-screen Chrome on a Mac is a dock and
    mission-control matter, left for when the port is actually run."""
    return None


def show_window(handle) -> None:
    return None


def kill_process_tree(pid: int) -> None:
    """End a process and its children. pkill -P gets the immediate children;
    the process itself is signalled directly."""
    import os
    import signal

    subprocess.run(["/usr/bin/pkill", "-TERM", "-P", str(pid)], capture_output=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def permissions_missing() -> list[str]:
    """Which macOS permissions are not granted, in words a user can act on.

    Worth checking before saying an action failed. Without Accessibility, a
    click is accepted and simply does not happen - so the honest report is
    "I need permission", not "I clicked it".
    """
    missing = []
    if not _has_accessibility():
        missing.append(
            "Accessibility (System Settings > Privacy & Security > Accessibility) "
            "- needed to click and type"
        )
    if not _has_screen_recording():
        missing.append(
            "Screen Recording (System Settings > Privacy & Security > Screen Recording) "
            "- needed to take screenshots"
        )
    return missing


def _has_accessibility() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        # Same reasoning as the screen check below: not being able to ask is
        # not the same as being refused, and reporting "missing" would send
        # someone to a settings pane to tick a box that is already ticked.
        return True


def _has_screen_recording() -> bool:
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        # The check itself needs pyobjc. Reporting "missing" when we simply
        # cannot tell would send the user to a settings pane for no reason.
        return True


def request_permissions() -> None:
    """Ask for the permissions, which is what makes them appear in Settings.

    An app that has never asked is not listed at all, so telling someone to
    tick a box they cannot see is worse than useless.
    """
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        from ApplicationServices import kAXTrustedCheckOptionPrompt

        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    try:
        from Quartz import CGRequestScreenCaptureAccess

        CGRequestScreenCaptureAccess()
    except Exception:
        pass


def has_command(name: str) -> bool:
    return shutil.which(name) is not None


# --- talking to the window server ------------------------------------------
#
# Through AppleScript rather than PyObjC. It is slower, but System Events is
# the same interface Apple's own automation uses, it needs no compiled bridge
# in the build, and when it fails it says why in a sentence - which matters far
# more on a platform none of this has been able to be tested on.

def _osascript(script: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if done.returncode != 0:
        message = (done.stderr or "").strip()
        # The one failure worth translating: everything else here is useless
        # without it, and Apple's wording does not say what to do about it.
        if "not allowed assistive access" in message or "-25211" in message:
            return False, (
                "macOS has not granted Accessibility permission. Open System Settings > "
                "Privacy & Security > Accessibility and switch Iris on."
            )
        return False, message or "AppleScript failed without saying why"
    return True, done.stdout.strip()


def list_windows() -> list[dict]:
    """Every visible window, as {app, title}."""
    script = (
        'tell application "System Events" to get {name, name of windows} of '
        '(every process whose visible is true and background only is false)'
    )
    ok, out = _osascript(script)
    if not ok:
        return []

    # AppleScript returns two flat comma-separated lists - the app names, then
    # each app's window names - which cannot be zipped back together reliably.
    # So each app is asked separately; slower, but the answer is correct.
    found = []
    for app in [n.strip() for n in out.split(",") if n.strip()]:
        ok, titles = _osascript(
            f'tell application "System Events" to get name of every window of process "{app}"'
        )
        if not ok:
            continue
        for title in [t.strip() for t in titles.split(",") if t.strip()]:
            found.append({"app": app, "title": title})
        if len(found) > 60:
            break
    return found


def window_action(title_contains: str, action: str) -> tuple[bool, str]:
    """minimize, maximize, restore, focus or close the first matching window."""
    wanted = title_contains.lower()
    match = next(
        (w for w in list_windows() if wanted in w["title"].lower() or wanted in w["app"].lower()),
        None,
    )
    if match is None:
        return False, f"No window matching {title_contains!r}."

    app, title = match["app"], match["title"]
    target = f'window "{title}" of process "{app}"'
    scripts = {
        "focus": f'tell application "{app}" to activate',
        "minimize": f'tell application "System Events" to set value of attribute "AXMinimized" of {target} to true',
        "restore": f'tell application "System Events" to set value of attribute "AXMinimized" of {target} to false',
        # There is no "maximize" on a Mac. Full screen is the closest thing the
        # user would recognise, and saying so beats silently doing nothing.
        "maximize": f'tell application "System Events" to set value of attribute "AXFullScreen" of {target} to true',
        "close": f'tell application "System Events" to click button 1 of {target}',
    }
    if action not in scripts:
        return False, f"Unknown action {action!r}."

    ok, message = _osascript(scripts[action])
    if not ok:
        return False, message
    return True, f"{action} on {title} ({app})."


def active_window_title() -> str:
    ok, out = _osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )
    return out if ok else ""


# --- applications -----------------------------------------------------------

_APP_FOLDERS = ("/Applications", "/System/Applications", str(Path.home() / "Applications"))


def list_apps() -> dict[str, str]:
    """Installed applications, as {name: path to the bundle}."""
    found: dict[str, str] = {}
    for folder in _APP_FOLDERS:
        base = Path(folder)
        if not base.is_dir():
            continue
        # One level down as well: Apple keeps Utilities in a subfolder, and
        # that is where Terminal and Activity Monitor live.
        for bundle in list(base.glob("*.app")) + list(base.glob("*/*.app")):
            found.setdefault(bundle.stem, str(bundle))
    return found


def ui_elements(window_title: str, limit: int = 50) -> tuple[bool, list[dict]]:
    """The controls of a window, read from the accessibility tree.

    Deliberately shallower than the Windows version. `entire contents` on a
    complex window can take tens of seconds, so this reads the top level and
    one level in, which is where buttons and text fields actually are.
    """
    wanted = window_title.lower()
    match = next(
        (w for w in list_windows() if wanted in w["title"].lower() or wanted in w["app"].lower()),
        None,
    )
    if match is None:
        return False, []

    script = (
        f'tell application "System Events" to tell process "{match["app"]}"\n'
        f'  set out to ""\n'
        f'  repeat with e in (UI elements of window "{match["title"]}")\n'
        f'    set out to out & (role of e) & "\\t" & (name of e as string) & "\\n"\n'
        f'    try\n'
        f'      repeat with c in (UI elements of e)\n'
        f'        set out to out & (role of c) & "\\t" & (name of c as string) & "\\n"\n'
        f'      end repeat\n'
        f'    end try\n'
        f'  end repeat\n'
        f'  return out\n'
        f'end tell'
    )
    ok, out = _osascript(script, timeout=45)
    if not ok:
        return False, []

    elements = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        role, _, label = line.partition("\t")
        label = label.strip()
        if label and label != "missing value":
            elements.append({"role": role.strip(), "name": label, "app": match["app"],
                             "window": match["title"]})
        if len(elements) >= limit:
            break
    return True, elements


_ACTIONABLE = {
    "Button", "CheckBox", "ComboBox", "TextField", "TextArea", "MenuItem",
    "RadioButton", "Slider", "Link", "Row", "PopUpButton",
}


def open_window_titles() -> list[str]:
    return [w["title"] for w in list_windows()]


def ui_do(element: dict, action: str, text: str = "") -> tuple[bool, str]:
    """Click a control, or type into it, by the name the tree gave it."""
    ref = element.get("ref") or {}
    where = (
        f'tell application "System Events" to tell process "{ref.get("app", "")}" '
        f'to tell window "{ref.get("window", "")}"'
    )
    name = str(ref.get("name", "")).replace('"', '\\"')
    if action == "click":
        ok, message = _osascript(f'{where} to click (first UI element whose name is "{name}")')
        return ok, message or f"Clicked {element['name']}."
    if action == "set_text":
        escaped = text.replace('"', '\\"')
        ok, message = _osascript(
            f'{where} to set value of (first UI element whose name is "{name}") to "{escaped}"'
        )
        return ok, message or f"Set {element['name']}."
    return False, f"Unknown action {action!r}."
