"""Discovering and launching installed applications."""

import difflib
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from anthropic import beta_tool

from iris.redact import scrubbed

_START_MENU_ROOTS = [
    Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    Path(os.environ.get("USERPROFILE", "")) / "Desktop",
    Path(os.environ.get("PUBLIC", "")) / "Desktop",
]


def _appsfolder_entries() -> dict[str, str]:
    """Map lowercase app name -> AppUserModelID, via the shell's Apps folder.

    Start Menu shortcuts miss Store and system apps entirely: Settings,
    Calculator and Photos have no .lnk file anywhere, so a shortcut-only index
    cannot launch them. This enumeration covers those (~150 apps, ~0.5s).
    """
    script = (
        "@((New-Object -ComObject Shell.Application).NameSpace('shell:AppsFolder').Items()"
        " | ForEach-Object { [PSCustomObject]@{ Name = $_.Name; Id = $_.Path } })"
        " | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=25,
            # Ours to read, not the user's to watch: without this a blank
            # console appears for as long as the listing takes.
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        entries = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}

    if isinstance(entries, dict):  # a single result does not come back as a list
        entries = [entries]
    return {
        e["Name"].strip().lower(): e["Id"]
        for e in entries
        if isinstance(e, dict) and e.get("Name") and e.get("Id")
    }


@lru_cache(maxsize=1)
def _app_index() -> dict[str, tuple[str, str]]:
    """Map lowercase app name -> (kind, target), kind being "file" or "appid"."""
    index: dict[str, tuple[str, str]] = {}
    for root in _START_MENU_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() in (".lnk", ".url") and path.stem.lower() not in index:
                index[path.stem.lower()] = ("file", str(path))

    # Added second so a real shortcut wins where both exist.
    for name, app_id in _appsfolder_entries().items():
        index.setdefault(name, ("appid", app_id))
    return index


@beta_tool
@scrubbed
def list_installed_apps(filter_text: str = "", limit: int = 40) -> str:
    """List applications installed on this machine.

    Use this when you are unsure of an app's exact name before launching it, or
    when the user asks what is available.

    Args:
        filter_text: Only show apps whose name contains this text. Empty shows all.
        limit: Maximum number of names to return.
    """
    names = sorted(_app_index())
    needle = filter_text.strip().lower()
    if needle:
        names = [n for n in names if needle in n]
    if not names:
        return f"No installed apps matching {filter_text!r}."
    shown = names[: max(1, limit)]
    suffix = f"\n[{len(names) - len(shown)} more not shown]" if len(names) > len(shown) else ""
    return "\n".join(f"  {n}" for n in shown) + suffix


@beta_tool
def launch_app(name: str) -> str:
    """Launch an application by name.

    Matches against Start Menu and Desktop shortcuts, then against executables
    on PATH. Names do not need to be exact: "valorant", "chrome", "notepad",
    "spotify" all work. If the match is wrong, call list_installed_apps to see
    the real names and try again.

    Args:
        name: The application name as a person would say it.
    """
    query = name.strip().lower()
    if not query:
        return "No application name given."

    index = _app_index()

    # Exact, then substring, then fuzzy.
    key = None
    if query in index:
        key = query
    else:
        contains = [k for k in index if query in k]
        if contains:
            key = min(contains, key=len)
        else:
            close = difflib.get_close_matches(query, list(index), n=1, cutoff=0.6)
            if close:
                key = close[0]

    if key:
        kind, target = index[key]
        try:
            if kind == "appid":
                # Store and system apps are launched through the shell by their
                # AppUserModelID; they have no executable path to start.
                subprocess.Popen(  # noqa: S603
                    ["explorer.exe", f"shell:AppsFolder\\{target}"],
                    # The user asked for this app; naming a console flag opts
                    # out of the blanket hiding in iris/spawn.py.
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                os.startfile(target)  # noqa: S606 - launching a user's own shortcut
            return f"Launched {key}."
        except OSError as exc:
            return f"Found {key} but could not launch it: {exc}"

    # Fall back to something on PATH (notepad, calc, cmd, ...).
    exe = shutil.which(query) or shutil.which(f"{query}.exe")
    if exe:
        try:
            subprocess.Popen([exe], creationflags=subprocess.CREATE_NEW_CONSOLE)
            return f"Launched {Path(exe).name}."
        except OSError as exc:
            return f"Could not start {exe}: {exc}"

    suggestions = difflib.get_close_matches(query, list(index), n=5, cutoff=0.35)
    hint = f" Closest installed names: {', '.join(suggestions)}." if suggestions else ""
    return f"Could not find an app called {name!r}.{hint} Try list_installed_apps."


@lru_cache(maxsize=1)
def _protocol_handlers() -> list[tuple[str, str]]:
    """Every registered URL scheme on this machine, as (scheme, command).

    These are the deep links: roblox://, steam://, vscode://, spotify://. They
    are the difference between opening an app and opening it *on the thing the
    user asked for*, and they are discoverable rather than something to recall.
    """
    import winreg

    found: list[tuple[str, str]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "")
    except OSError:
        return found

    for i in range(winreg.QueryInfoKey(root)[0]):
        try:
            scheme = winreg.EnumKey(root, i)
        except OSError:
            break
        if scheme.startswith("."):  # file associations, not protocols
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
                winreg.QueryValueEx(key, "URL Protocol")  # absent -> not a protocol
        except OSError:
            continue
        try:
            path = rf"{scheme}\shell\open\command"
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as key:
                command = str(winreg.QueryValueEx(key, "")[0])
        except OSError:
            command = ""
        found.append((scheme, command))
    return found


@beta_tool
@scrubbed
def app_interfaces(name: str) -> str:
    """Find programmatic ways to drive an app: URL schemes and command lines.

    Call this before clicking anything. Driving an app through a deep link or
    its command line is faster and far more reliable than finding a button in a
    screenshot, and this tells you which of those exist rather than leaving you
    to guess. "roblox" finds the roblox: scheme, which opens a specific game
    directly; "code" finds the VS Code CLI.

    Args:
        name: The app or scheme to look for, e.g. "roblox", "spotify", "code".
    """
    needle = name.strip().lower()
    if not needle:
        return "Say which app to look up."

    lines: list[str] = []

    try:
        schemes = [
            (s, c) for s, c in _protocol_handlers() if needle in s.lower() or needle in c.lower()
        ]
    except Exception as exc:
        schemes = []
        lines.append(f"Could not read the registry: {exc}")

    if schemes:
        lines.append("URL schemes (open with run_shell: Start-Process 'scheme:...'):")
        for scheme, command in schemes[:8]:
            exe = command.split('"')[1] if command.startswith('"') else command.split(" ")[0]
            lines.append(f"  {scheme}:  -> {Path(exe).name if exe else 'unknown'}")

    exe = shutil.which(needle) or shutil.which(f"{needle}.exe")
    if exe:
        lines.append(f"Command line: {needle} is on PATH at {exe}")
        lines.append(f"  Run `{needle} --help` with run_shell to see what it accepts.")

    if not lines:
        return (
            f"No URL scheme or command line found for {name!r}. Try the accessibility "
            "tree with ui_inspect, or the app's own web API with fetch_url, before "
            "falling back to screenshots and mouse clicks."
        )

    lines.append(
        "Prefer these over clicking. A deep link or one command does in a single step "
        "what takes several screenshots and clicks, and cannot miss the target."
    )
    return "\n".join(lines)


TOOLS = [list_installed_apps, launch_app, app_interfaces]
