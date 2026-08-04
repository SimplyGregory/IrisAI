"""Console windows, and how not to have them.

A console program started by a process that has no console of its own gets a
fresh window from Windows. Run from a terminal that never happens - children
inherit ours. Built windowed, or under pythonw, there is nothing to inherit, so
every helper we start flashes up a black rectangle: the Claude Code CLI, the
PowerShell we use to enumerate apps, the speech fallback.

Rather than remember the flag at every call site - and it was forgotten at all
eight - the default is set once, here, and only when we have no console to
share. Anything launched *for* the user opts out by passing creationflags of
its own, because a window they asked for should appear.
"""

import ctypes
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008

# Any of these means the caller has already decided how this child relates to a
# console, and we leave it alone.
DECIDED = CREATE_NO_WINDOW | CREATE_NEW_CONSOLE | DETACHED_PROCESS

_installed = False


def _hidden():
    """A STARTUPINFO that asks for the window to be hidden.

    Belt and braces, and on Windows 11 the braces are load-bearing. When the
    default terminal application is Windows Terminal rather than the old
    console host, CREATE_NO_WINDOW on its own is not honoured: the ConPTY
    handoff still puts a Terminal window on screen for as long as the child
    lives. Asking for SW_HIDE as well is what actually keeps it off screen.
    """
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def has_console() -> bool:
    """True when this process owns a console - i.e. it was run from a terminal."""
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True  # if in doubt, change nothing


def hide_console_children() -> bool:
    """Default child processes to no console window. Returns whether it applied.

    A no-op when we have a console, so running from a terminal behaves exactly
    as it always did and output still goes where you can see it.
    """
    global _installed
    if _installed or os.name != "nt" or has_console():
        return False

    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        # Judged on the flags themselves, not on whether the argument was
        # passed. anyio hands subprocess.Popen an explicit creationflags=0 -
        # so a check for "did the caller supply it" skips every spawn the
        # Claude Code CLI makes, which is exactly the one that has to be quiet.
        #
        # Saying "show this one" therefore means naming a console flag:
        # CREATE_NEW_CONSOLE, which is what launching an app for the user does.
        flags = kwargs.get("creationflags") or 0
        if not flags & DECIDED:
            kwargs["creationflags"] = flags | CREATE_NO_WINDOW
            if kwargs.get("startupinfo") is None:
                kwargs["startupinfo"] = _hidden()
        return original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched
    _installed = True
    return True
