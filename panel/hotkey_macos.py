"""The panel's global hotkey on macOS.

Same shape as hotkey.py: parse a combo string, then a thread that calls back
when it is pressed anywhere on the system.

Windows hands this out through RegisterHotKey, which reserves the combination
system-wide. macOS has no equivalent a Python process can reach without Carbon,
so this watches for the keystroke instead, through a global event monitor.

Two consequences worth knowing, because both look like bugs:

  - The monitor needs Accessibility permission. Without it the callback never
    fires and nothing says why, so this checks and reports rather than sitting
    there silently.
  - Watching does not consume the keystroke. If the combination means something
    to the focused app, that app gets it too. Ctrl+Alt+J is unclaimed on macOS,
    which is why it remains the default.

Unverified: written against Apple's documentation on a Windows machine.
"""

import threading

# NSEvent modifier flags, which are what the monitor reports.
_SHIFT = 1 << 17
_CONTROL = 1 << 18
_OPTION = 1 << 19   # the Alt key
_COMMAND = 1 << 20

_NAMES = {
    "ctrl": _CONTROL, "control": _CONTROL,
    "alt": _OPTION, "option": _OPTION, "opt": _OPTION,
    "shift": _SHIFT,
    # "win" is what the .env says, and on a Mac the key in that position is
    # Command - so a combo written on Windows keeps working here.
    "cmd": _COMMAND, "command": _COMMAND, "win": _COMMAND, "super": _COMMAND,
}

_KEY_MASK = _SHIFT | _CONTROL | _OPTION | _COMMAND


# How a Mac writes a shortcut. "Ctrl+Alt+J" in the panel footer would look
# transplanted; every Mac app shows this as ⌃⌥J, and the symbols are ordered
# by convention rather than by how the user typed the combo.
_SYMBOLS = ((_CONTROL, "⌃"), (_OPTION, "⌥"), (_SHIFT, "⇧"), (_COMMAND, "⌘"))


def parse(combo: str) -> tuple[int, str, str]:
    """"ctrl+alt+j" -> (flags, "j", "⌃⌥J").

    Three values, like the Windows version, because app.py reads [2] for the
    label it shows. Raises ValueError if the combo cannot work.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("no hotkey given")

    flags = 0
    key = ""
    for part in parts:
        if part in _NAMES:
            flags |= _NAMES[part]
        elif len(part) == 1:
            key = part
        elif part == "space":
            key = " "
        else:
            raise ValueError(f"unrecognised key {part!r} in {combo!r}")

    if not key:
        raise ValueError(f"{combo!r} has modifiers but no key")
    if not flags:
        raise ValueError(f"{combo!r} needs at least one modifier")

    shown = "".join(symbol for bit, symbol in _SYMBOLS if flags & bit)
    label = "Space" if key == " " else key.upper()
    return flags, key, shown + label


class Hotkey(threading.Thread):
    """Calls `on_press` whenever the combination is pressed, anywhere."""

    def __init__(self, combo: str, on_press, on_problem=None):
        super().__init__(name="iris-hotkey", daemon=True)
        self.flags, self.key, self.name_shown = parse(combo)
        self.combo = combo
        self.error = ""
        self._on_press = on_press
        self._on_problem = on_problem or (lambda message: None)
        self._monitor = None
        # Mirrors the Windows class: app.py starts the thread and then waits to
        # be told whether the key was actually claimed, so it can report a
        # combination that was refused instead of one that silently never fires.
        self._settled = threading.Event()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """True once watching, False if it could not start."""
        self._settled.wait(timeout)
        return self._monitor is not None

    def _failed(self, message: str) -> None:
        self.error = message
        self._settled.set()
        self._on_problem(message)

    def run(self) -> None:
        try:
            from AppKit import NSEvent, NSKeyDownMask
        except Exception as exc:
            self._failed(
                f"The hotkey needs pyobjc, which is not installed ({exc}). "
                "The panel can still be opened from the menu bar."
            )
            return

        from iris import platform

        missing = platform.permissions_missing()
        if any("Accessibility" in item for item in missing):
            self._failed(
                "macOS has not granted Accessibility permission, so the hotkey "
                "cannot see keystrokes. System Settings > Privacy & Security > "
                "Accessibility, then switch Iris on and restart it."
            )
            return

        def handler(event):
            try:
                if (event.modifierFlags() & _KEY_MASK) != self.flags:
                    return
                if (event.charactersIgnoringModifiers() or "").lower() != self.key:
                    return
                self._on_press()
            except Exception:
                pass  # a raised exception here would kill the monitor for good

        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask, handler
        )
        if self._monitor is None:
            self._failed("macOS refused the keyboard monitor for the hotkey.")
        else:
            self._settled.set()

    def stop(self) -> None:
        if self._monitor is None:
            return
        try:
            from AppKit import NSEvent

            NSEvent.removeMonitor_(self._monitor)
        except Exception:
            pass
        self._monitor = None
