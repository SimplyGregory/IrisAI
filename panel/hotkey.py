"""A global hotkey, on its own thread with its own message loop.

RegisterHotKey posts WM_HOTKEY to the *thread* that registered it, not to a
window, so this cannot borrow the GUI thread's pump - it needs one of its own.

On Win+M: the shell reserves the M key and refuses to hand it over. Win+M,
Win+Shift+M, Win+Alt+M and Win+Ctrl+M are all rejected by RegisterHotKey,
while Win+Shift+I and Win+Shift+J are granted, so it is the key rather than
the modifier. Taking M anyway needs a WH_KEYBOARD_LL hook that swallows the
keystroke ahead of the shell, at the cost of the built-in "minimize all".
Win+Shift+J is the documented, side-effect-free option and is the default.
"""

import ctypes
import threading
try:
    from ctypes import wintypes
except ImportError:  # not Windows - see the note in panel/chrome.py
    wintypes = None

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Declared rather than left to ctypes' 32-bit int default, so nothing is
# truncated on the way in or out. See chrome._declare for why that matters.
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p
]

MOD = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000  # one event per press, not a stream while held
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

# Keys worth naming. Anything else is taken as a single character.
NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def parse(combo: str) -> tuple[int, int, str]:
    """'win+shift+j' -> (modifier flags, virtual key, tidy name)."""
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")

    modifiers = 0
    for part in parts[:-1]:
        if part not in MOD:
            raise ValueError(f"unknown modifier {part!r} in {combo!r}")
        modifiers |= MOD[part]

    key = parts[-1]
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1:
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r} in {combo!r}")

    pretty = "+".join(p.capitalize() for p in parts)
    return modifiers, vk, pretty


class Hotkey(threading.Thread):
    """Calls `callback` whenever the combo is pressed, from this thread."""

    def __init__(self, combo: str, callback):
        super().__init__(name="panel-hotkey", daemon=True)
        self.modifiers, self.vk, self.name_shown = parse(combo)
        self._callback = callback
        self._thread_id = None
        self._registered = threading.Event()
        self.error: str | None = None
        self.presses = 0  # counted before the handler runs, so a broken
        # handler still shows the key itself is arriving

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """Block until registration has been attempted. False if it failed."""
        self._registered.wait(timeout)
        return self.error is None

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, 1, self.modifiers | MOD_NOREPEAT, self.vk):
            # Almost always means another program - or the shell itself - got
            # there first. Say which combo, because the message is otherwise
            # impossible to act on.
            self.error = f"{self.name_shown} is already taken by something else"
            self._registered.set()
            return
        self._registered.set()

        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    self.presses += 1
                    try:
                        self._callback()
                    except Exception:
                        # A bad handler must not kill the hotkey, but it must
                        # not vanish either: silently swallowing this turns
                        # "the window does not open" into an unfindable bug.
                        import traceback

                        print(f"  ! {self.name_shown} handler failed:")
                        traceback.print_exc()
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
