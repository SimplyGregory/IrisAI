"""Iris as a panel: a chat window that slides in down the right of the screen.

    python panel/app.py

Press the hotkey (Win+Shift+J by default) to show or hide it. Escape hides it
too. It is the same agent as text mode - same tools, same memory, same .env -
just reached through a window instead of a terminal.

The window is a frameless WebView2 host: Python owns the frame and everything
Windows draws around it, the page owns everything inside. See chrome.py for
the first half and ui/style.css for the second.
"""

import ctypes
import os
import sys
import threading
try:
    from ctypes import wintypes
except ImportError:  # not Windows - see the note in panel/chrome.py
    wintypes = None
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Launched from the .vbs there is no console, and then print() raises on a
# None stdout. Swallow it before importing anything that might print.
if sys.stdout is None or sys.stderr is None:
    class _Null:
        def write(self, _text):
            pass

        def flush(self):
            pass

    sys.stdout = sys.stdout or _Null()
    sys.stderr = sys.stderr or _Null()

from iris import platform as _plat  # noqa: E402

# The two chrome modules expose the same names and are not interchangeable in
# any other sense: one drives a window through Win32 by its HWND, the other
# through pywebview on a Mac. Chosen here once so nothing below has to ask.
if _plat.is_macos():  # noqa: E402
    import chrome_macos as chrome  # noqa: E402
    import hotkey_macos as hotkey  # noqa: E402
else:  # noqa: E402
    import chrome  # noqa: E402
    import hotkey  # noqa: E402

# Before pywebview is imported, not after: DPI awareness can only be set once
# per process and importing pywebview sets it. Lose that race and every
# coordinate in chrome.py is measured in a different space than the one
# SetWindowPos writes to - which looks like a wrongly sized window, not an error.
chrome.set_dpi_aware()

import webview  # noqa: E402

from bridge import Api, wire  # noqa: E402
# From whichever hotkey module the platform check above selected. A plain
# `from hotkey import ...` here would quietly load the Windows one on a Mac.
Hotkey = hotkey.Hotkey  # noqa: E402
parse_hotkey = hotkey.parse  # noqa: E402
from iris import spawn, config, log, paths, selfcontrol  # noqa: E402

# Before anything starts a helper process. No-op from a terminal.
spawn.hide_console_children()

# Through paths.resource so it also works from inside a built exe,
# where the UI is unpacked to a temp folder rather than sitting next
# to this file.
UI = paths.resource("panel", "ui", "index.html")

# Win+Shift+J rather than Win+Shift+M: the shell refuses to give up the M key.
# hotkey.py explains what it would take to have it anyway.
DEFAULT_HOTKEY = "ctrl+alt+j"
HOTKEY = os.environ.get("IRIS_PANEL_HOTKEY", DEFAULT_HOTKEY)

# The panel's shape. Fixed rather than configurable: these were tuned against
# Windows' own Quick Settings flyout, and they are not settings anyone wants to
# reason about - a half-height panel 360 wide in the bottom right is what the
# thing *is*. Layout pixels, scaled to the display by chrome.dock_flyout.
WIDTH = 360           # matches the Quick Settings flyout
HEIGHT = 0.5          # share of the usable screen height
MARGIN = 16           # inset from the corner
NUDGE_X = 0           # real screen pixels, for lining it up by eye
NUDGE_Y = 0

# The window is found by this title, so it has to be unlikely to collide.
TITLE = f"{config.ASSISTANT_NAME} Panel"

# Normally the panel starts hidden and waits for the hotkey. Straight after
# setup that would look like nothing happened, so the installer passes --show
# and the first thing you see is Iris rather than an empty desktop.
SHOW_ON_START = "--show" in sys.argv


_mutex = None


def already_running() -> bool:
    """True if another panel already owns the single-instance mutex.

    Worth the twenty lines: a hotkey can only belong to one process, so a
    second launch loses the race and reports the combo as "taken by something
    else" - naming, confusingly, the copy you forgot was running. That is
    especially easy when the first was started from the .vbs and has no
    console to notice.
    """
    global _mutex

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    # Held for the life of the process; Windows releases it when we exit.
    _mutex = kernel32.CreateMutexW(None, True, "Local\\IrisPanelSingleInstance")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def hotkey_test(combos: str) -> int:
    """Register the combo and report each press. No window, no agent.

    Worth having because a hotkey is the one part that cannot be tested
    without a human finger: Windows ignores injected Win-key presses for
    hotkey purposes, so synthetic input proves nothing here.
    """
    import time

    seen = []
    live = []
    for candidate in combos.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            keys = Hotkey(candidate, lambda k=candidate: seen.append(k))
        except ValueError as exc:
            print(f"  ! {exc}")
            continue
        keys.start()
        if keys.wait_until_ready():
            live.append(keys)
            print(f"  registered {keys.name_shown}")
        else:
            print(f"  ! {keys.error}")

    if not live:
        return 1

    print("  press them now. ctrl+c to stop.")
    count = 0
    try:
        while True:
            time.sleep(0.2)
            while len(seen) > count:
                count += 1
                print(f"  [{time.strftime('%H:%M:%S')}] {seen[count - 1]} detected")
    except KeyboardInterrupt:
        print(f"\n  {count} press(es) detected")
    return 0


def voice_check(flag: str) -> int:
    """The microphone and speech diagnostics.

    Dictation and the wake word still depend on the microphone being audible
    and the threshold being right, so these are worth having somewhere - and
    the panel is now the entry point everything else runs through.
    """
    from iris.voice import stt, tts

    if flag == "--voices":
        tts.preview_voices()
    elif flag == "--mic-test":
        stt.mic_check()
    elif flag == "--calibrate":
        result = stt.calibrate(say=tts.speak)
        print(f"\n  room noise floor : {result['ambient']:.5f}")
        print(f"  your voice       : {result['speech_low']:.5f} typical, "
              f"{result['speech_peak']:.5f} peak")
        print(f"  separation       : {result['separation']:.0f}x above the room")
        print(f"  new threshold    : {result['threshold']:.5f}")
        print(f"  {result['verdict']}")
        print(f"  saved to {stt.save_threshold(result['threshold'])}")
        tts.speak("Calibration saved.")
    return 0


def main() -> int:
    if "--hotkey-test" in sys.argv:
        return hotkey_test(HOTKEY)

    for flag in ("--voices", "--mic-test", "--calibrate"):
        if flag in sys.argv:
            return voice_check(flag)

    if already_running():
        # A stray double-launch does nothing, deliberately: summoning the
        # existing panel used to show it wherever it had last been left, which
        # after a dismissal is part-way through its slide off the bottom of the
        # screen - a half-drawn panel hanging off the edge.
        #
        # An explicit --show is different. That comes from finishing setup or
        # from the Start menu, where the whole point is to see her, and being
        # silently swallowed by the guard looks exactly like the launch failing.
        # Re-docking first is what the old version missed.
        if SHOW_ON_START:
            existing = chrome.find_hwnd(TITLE, timeout=3.0)
            if existing:
                at = chrome.dock_flyout(
                    WIDTH, HEIGHT, MARGIN, existing, nudge_x=NUDGE_X, nudge_y=NUDGE_Y
                )
                chrome.round_corners(existing, at[2], at[3])
                chrome.slide_up(existing, *at)
                log.write("already running - showed the copy that was already up")
                return 0
        print("  Already running. Press the hotkey to show it.")
        return 0

    x, y, width, height = chrome.dock_flyout(
        WIDTH, HEIGHT, MARGIN, nudge_x=NUDGE_X, nudge_y=NUDGE_Y
    )
    theme = chrome.theme()

    # Validate now, so a typo in .env is a one-line message at startup rather
    # than an exception once the GUI loop is already running.
    combos = []
    for candidate in HOTKEY.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            combos.append((candidate, parse_hotkey(candidate)[2]))
        except ValueError as exc:
            print(f"  ! {exc} - ignoring it")
    if not combos:
        combos = [(DEFAULT_HOTKEY, parse_hotkey(DEFAULT_HOTKEY)[2])]

    api = Api(theme, " or ".join(name for _, name in combos))
    wire(api)

    window = webview.create_window(
        TITLE,
        str(UI),
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        frameless=True,
        easy_drag=False,  # it is docked; dragging it would only misplace it
        resizable=False,
        on_top=True,
        hidden=True,  # the hotkey brings it up, so no flash at startup
        # Windows clips the corners from outside with SetWindowRgn and fills
        # the rest, so the window is opaque. macOS has no such call, so the
        # page draws its own rounded, blurred card instead - which only works
        # if the window behind it is see-through.
        transparent=_plat.is_macos(),
        background_color=(
            "#00000000" if _plat.is_macos()
            else ("#202020" if theme["dark"] else "#F3F3F3")
        ),
    )
    api.attach(window)
    # macOS has nothing to look the window up by, so it is handed over.
    chrome.attach(window)

    def setup():
        """Runs once the GUI loop is up and the window actually exists.

        Wrapped, because pywebview runs this on its own thread and swallows
        whatever it raises - so a failure here leaves a window that exists,
        was never shown, and never says why.
        """
        try:
            _setup()
        except Exception as exc:
            log.failure("panel setup failed", exc)
            raise

    def _setup():
        hwnd = chrome.find_hwnd(TITLE)
        if hwnd is None:
            log.write("could not find the panel window; styling skipped")
            return

        chrome.apply_style(hwnd, theme["dark"])
        chrome.set_icon(hwnd, paths.resource("claude-logo.ico"))
        # Set while still hidden: the taskbar decides whether to give a window
        # a button at the moment it is first shown.
        chrome.hide_from_taskbar(hwnd)

        def geometry():
            """Re-measured on every appearance, so a resolution change, a moved
            taskbar or a different monitor all sort themselves out."""
            return chrome.dock_flyout(
                WIDTH, HEIGHT, MARGIN, hwnd, nudge_x=NUDGE_X, nudge_y=NUDGE_Y
            )

        spot = geometry()
        chrome.place(hwnd, *spot)
        chrome.round_corners(hwnd, spot[2], spot[3])

        log.write(f"panel ready; show on start = {SHOW_ON_START}")
        if SHOW_ON_START:
            chrome.slide_up(hwnd, *spot)
            try:
                window.evaluate_js("window.panel.focusInput()")
            except Exception:
                pass

        def dismiss():
            """Drop it out of sight. Shared by the hotkey, Esc and the X."""
            chrome.slide_down(hwnd)

        def toggle():
            # Announced, because a hotkey that appears to do nothing is
            # otherwise indistinguishable from one that never arrived.
            if chrome.is_visible(hwnd):
                print("  [hotkey] hiding")
                dismiss()
                return
            print("  [hotkey] showing")
            at = geometry()
            # The region is in window coordinates so it survives the slide,
            # but it has to match the current size before anything is shown.
            chrome.round_corners(hwnd, at[2], at[3])
            chrome.slide_up(hwnd, *at)
            try:
                window.evaluate_js("window.panel.focusInput()")
            except Exception as exc:
                # Cosmetic only - the panel is already on screen by now.
                print(f"  (focus failed: {exc})")

        api.on_hide = dismiss
        api.on_quit = window.destroy

        # What Iris is allowed to do to herself while the panel is what is
        # running. Registered here rather than reached for from the tools, so
        # the same tools behave sensibly in text and voice mode too.
        selfcontrol.provide("quit", lambda: threading.Timer(1.2, window.destroy).start())
        selfcontrol.provide("hide", dismiss)
        selfcontrol.provide("clear_conversation", api.reset_conversation)
        selfcontrol.provide("set_transparency", lambda level: chrome.set_opacity(hwnd, level))
        selfcontrol.provide("speech_changed", lambda: api.refresh_speech())

        # Every combo listed gets registered, and any one of them toggles the
        # panel. More than one is worth having when a combo turns out to be
        # swallowed upstream: a hotkey the OS accepts can still be intercepted
        # by a keyboard driver or utility before it ever reaches us.
        registered = []
        for combo, _ in combos:
            keys = Hotkey(combo, toggle)
            keys.start()
            if keys.wait_until_ready():
                registered.append(keys)
            else:
                print(f"  ! {keys.error}")

        if registered:
            print(f"  {TITLE}: press {' or '.join(k.name_shown for k in registered)}")
        else:
            # No hotkey means no way to summon it, so show it now rather than
            # leaving an invisible process running.
            print("  ! no hotkey could be registered")
            print("    set IRIS_PANEL_HOTKEY in .env to something else")
            chrome.show(hwnd)

    print(f"  {TITLE} - {config.BACKEND} backend, {config.MODEL}")
    print(f"  theme: {'dark' if theme['dark'] else 'light'}, accent {theme['accent']}")
    webview.start(setup, gui="edgechromium", debug=bool(os.environ.get("IRIS_PANEL_DEBUG")))

    from iris.tools import browser

    browser.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
