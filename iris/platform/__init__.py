"""Everything that differs between Windows and macOS, behind one interface.

A second copy of the project for the Mac was the obvious move and the wrong
one. The interesting parts of Iris - the agent loop, memory, redaction, the
prompt - change constantly, and two copies would have diverged inside a week,
with every fix needing doing twice. So there is one codebase, and the handful
of things that genuinely cannot be shared live in windows.py and macos.py
behind the names below.

The rule for what belongs here: if the *answer* differs by platform but the
*question* does not. "Run this command" is the same question everywhere; the
argv that answers it is not. Anything a caller has to platform-check after
calling is a sign the interface is drawn in the wrong place.

Callers import from here and never from the implementations, so a module that
imports pywin32 is never even loaded on a Mac.
"""

import sys

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"


def name() -> str:
    if sys.platform == "darwin":
        return MACOS
    if sys.platform.startswith("win"):
        return WINDOWS
    return LINUX


def is_windows() -> bool:
    return name() == WINDOWS


def is_macos() -> bool:
    return name() == MACOS


if sys.platform == "darwin":
    from iris.platform import macos as _impl
else:
    # Linux is not a target. Windows is the closer fit of the two - both have
    # a real filesystem layout and a shell - so it fails on the first missing
    # import rather than pretending to be supported.
    from iris.platform import windows as _impl


# --- what the implementations must provide ---------------------------------
#
# Re-exported by hand rather than with a star import: this list is the
# interface, and it should be possible to read it without opening either
# implementation.

SHELL_TOOL_NAME = _impl.SHELL_TOOL_NAME      # what the shell tool is called
SHELL_DISPLAY_NAME = _impl.SHELL_DISPLAY_NAME  # what to call it when speaking

shell_argv = _impl.shell_argv                # a command string -> argv
quiet_process = _impl.quiet_process          # subprocess kwargs, no console
speak_native = _impl.speak_native            # the OS voice, as a fallback
list_voices = _impl.list_voices              # the OS voices, by name
default_install_dir = _impl.default_install_dir
launch = _impl.launch                        # start an app by name
open_url = _impl.open_url
create_shortcut = _impl.create_shortcut      # Start menu / Applications entry
theme = _impl.theme                          # accent colour and light/dark
bridge_address = _impl.bridge_address        # where the VS Code bridge listens

# Only macOS withholds permissions an app has to be granted before it can act,
# so on Windows this is always empty - but callers should not have to know
# that, or they would need a platform check at every call site.
permissions_missing = _impl.permissions_missing
kill_process_tree = _impl.kill_process_tree  # end a process and its children

# Windows and screen furniture.
list_windows = _impl.list_windows          # every visible window
window_action = _impl.window_action        # minimize / focus / close one
active_window_title = _impl.active_window_title
open_window_titles = _impl.open_window_titles
list_apps = _impl.list_apps                # installed applications

# The accessibility tree. Both sides return elements of the same shape, each
# carrying an opaque "ref" the caller never looks inside - a live control
# object on Windows, the names to find it again on a Mac. That is what lets one
# tool serve two models that otherwise have nothing in common.
ui_elements = _impl.ui_elements
ui_do = _impl.ui_do

__all__ = [
    "WINDOWS", "MACOS", "LINUX",
    "name", "is_windows", "is_macos",
    "SHELL_TOOL_NAME", "SHELL_DISPLAY_NAME",
    "shell_argv", "quiet_process", "speak_native", "list_voices",
    "default_install_dir", "launch", "open_url", "create_shortcut", "theme",
    "bridge_address", "permissions_missing", "kill_process_tree",
    "list_windows", "window_action", "active_window_title", "open_window_titles",
    "list_apps", "ui_elements", "ui_do",
]
