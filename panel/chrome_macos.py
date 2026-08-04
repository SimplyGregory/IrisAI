"""The panel's window handling on macOS.

Same function names as chrome.py, so panel/app.py does not know which one it
is talking to. What they operate on is completely different: chrome.py drives
a window by its HWND through Win32, and there is no HWND here. This drives
pywebview's own window object, and lets the page draw its own corners, border
and material - which is why the macOS block in style.css exists.

The "handle" passed around is therefore the pywebview Window itself rather than
an integer. Nothing outside this module looks inside it.

Unverified: written against Apple's and pywebview's documentation on a Windows
machine. The animation and the geometry are the parts most likely to need
adjusting on real hardware.
"""

import time

# Matches chrome.py so the two behave alike where they can.
_ANIMATION_STEPS = 12


def set_dpi_aware() -> bool:
    """Nothing to do. macOS is point-based and scales for us."""
    return True


def dpi_scale(window=None) -> float:
    """Always 1. Points are already logical units; Retina is invisible here."""
    return 1.0


def work_area(window=None) -> tuple[int, int, int, int]:
    """The usable screen - display minus menu bar and Dock - as (l, t, r, b).

    Same four numbers in the same order as the Windows version, because
    dock_flyout below does the same arithmetic with them.

    visibleFrame is exactly the right rectangle, but its origin is bottom-left:
    macOS measures up from the bottom of the screen where everything here
    measures down from the top. Converted once, at this boundary, so nothing
    downstream has to remember which way is up.
    """
    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame()
        full = screen.frame()
        left = int(visible.origin.x)
        right = int(visible.origin.x + visible.size.width)
        top = int(full.size.height - (visible.origin.y + visible.size.height))
        bottom = top + int(visible.size.height)
        return left, top, right, bottom
    except Exception:
        # A sane guess beats crashing: the panel lands roughly right and the
        # user can see something is off, rather than seeing nothing at all.
        return 0, 25, 1440, 900


def dock_flyout(
    width: int = 360,
    height_fraction: float = 0.5,
    margin: int = 16,
    window=None,
    nudge_x: int = 0,
    nudge_y: int = 0,
) -> tuple[int, int, int, int]:
    """Geometry for the flyout: bottom right, part height.

    Argument for argument, and line for line, the same as the Windows version -
    because app.py calls both the same way and the panel is meant to land in
    the same place on either machine. Only the material was to change.

    No DPI scaling, unlike Windows: macOS window coordinates are already in
    points, and Retina is handled below this layer. So the layout pixels the
    caller passes are used as they are, which is what the Windows version
    achieves by multiplying by its scale factor.
    """
    left, top, right, bottom = work_area(window)

    usable = bottom - top
    tall = max(int(usable * height_fraction), 280)
    tall = min(tall, usable - margin * 2)

    # Anchored to the bottom right, so it grows upwards out of the corner it
    # appears from rather than away from it.
    x = right - width - margin + nudge_x
    y = bottom - tall - margin + nudge_y
    return x, y, width, tall


# How far the panel travels on its way in and out. The same distance as the
# Windows side, for the same reason: shorter reads as a flash, not a movement.
TRAVEL = 140


# The window app.py created. Windows looks its handle up by title through the
# window manager; there is no such lookup here, so it is simply remembered.
_window = None


def attach(window) -> None:
    global _window
    _window = window


def find_hwnd(title: str, timeout: float = 10.0):
    """The window we were handed. Nothing to search for."""
    return _window


def place(window, x: int, y: int, width: int, height: int) -> None:
    if window is None:
        return
    try:
        window.resize(width, height)
        window.move(x, y)
    except Exception:
        pass


def window_rect(window) -> tuple[int, int, int, int]:
    try:
        return int(window.x), int(window.y), int(window.width), int(window.height)
    except Exception:
        return 0, 0, 0, 0


def is_visible(window) -> bool:
    try:
        return bool(window.shown)
    except Exception:
        return False


def show(window) -> None:
    try:
        window.show()
    except Exception:
        pass


def hide(window) -> None:
    try:
        window.hide()
    except Exception:
        pass


def slide_up(window, x: int, y: int, width: int, height: int, duration: float = 0.26) -> None:
    """Rise into place, matching the Windows animation.

    pywebview can only be asked to move the whole window, so this steps it
    rather than animating a layer. Fewer, larger steps than the Win32 version:
    each move crosses into Cocoa, and a smooth-looking sixty of them is slower
    than the animation it is trying to draw.
    """
    start_y = y + TRAVEL
    place(window, x, start_y, width, height)
    show(window)
    for step in range(1, _ANIMATION_STEPS + 1):
        # Ease-out: quick away, settling at the end. The same curve the Windows
        # side uses, so the two read as one product.
        progress = 1 - (1 - step / _ANIMATION_STEPS) ** 3
        place(window, x, int(start_y + (y - start_y) * progress), width, height)
        time.sleep(duration / _ANIMATION_STEPS)
    place(window, x, y, width, height)


def slide_down(window, duration: float = 0.16) -> None:
    x, y, width, height = window_rect(window)
    end_y = y + TRAVEL
    for step in range(1, _ANIMATION_STEPS + 1):
        progress = (step / _ANIMATION_STEPS) ** 2  # ease-in, mirroring the rise
        place(window, x, int(y + (end_y - y) * progress), width, height)
        time.sleep(duration / _ANIMATION_STEPS)
    hide(window)
    place(window, x, y, width, height)  # back where it belongs for next time


# --- the things the page does for itself -----------------------------------
#
# On Windows these are Win32 calls against the window. Here the web view is
# transparent and the page draws its own rounded, blurred, shadowed card, so
# these have nothing to do - see the macOS block at the end of style.css.

def apply_style(window, dark: bool) -> None:
    return None


def round_corners(window, width: int, height: int, radius: int = 8) -> bool:
    return True  # the page's border-radius already did it


def set_opacity(window, percent_transparent: int) -> None:
    """Transparency, which here is a property of the window, not a region."""
    try:
        from AppKit import NSApp

        native = _native_window(window)
        if native is not None:
            native.setAlphaValue_(max(0.2, 1.0 - percent_transparent / 100))
    except Exception:
        pass


def set_icon(window, icon) -> bool:
    """The .app bundle carries the icon; a window has none of its own."""
    return True


def hide_from_taskbar(window) -> None:
    """Handled by LSUIElement in the bundle, which keeps Iris out of the Dock."""
    return None


def _native_window(window):
    """The NSWindow behind a pywebview window, if this backend exposes one."""
    try:
        from webview.platforms.cocoa import BrowserView

        return BrowserView.instances[window.uid].window
    except Exception:
        return None


def uses_dark_theme() -> bool:
    from iris import platform

    return platform.theme()["dark"]


def accent(dark: bool) -> str:
    from iris import platform

    return platform.theme()["accent"]


def theme() -> dict:
    from iris import platform

    return platform.theme()
