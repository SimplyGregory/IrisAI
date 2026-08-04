"""Native Windows dressing for the panel window.

pywebview gives us a window with a browser in it. Everything that makes it read
as a Windows app rather than a web page lives here: where it sits on screen,
its rounded corners, and the colours the user actually picked in Settings.

None of this touches the page content - see ui/style.css for that.
"""

import ctypes
import time
try:
    from ctypes import wintypes
except ImportError:
    # Not Windows. Everything below drives a window by its HWND through Win32,
    # which has no macOS equivalent - a Mac port does the same jobs through
    # pywebview's own window object instead. Importing has to survive so that
    # the rest of the panel can load and fail with something readable, rather
    # than an ImportError on line twelve that says nothing about what is
    # missing. This is the largest piece of the macOS port still outstanding.
    wintypes = None
from pathlib import Path

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32

HANDLE = ctypes.c_void_p  # HWND, HMONITOR and HRGN are all pointer-sized


def _declare() -> None:
    """Give ctypes the real signatures before calling anything.

    Undeclared return types default to a 32-bit int, which truncates every
    64-bit handle Windows hands back. Handles usually fit in 32 bits, so this
    appears to work for a long time and then fails as a window that will not
    show or a region that clips away the whole panel - never as an error that
    points at the cause.
    """
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = HANDLE
    user32.GetDpiForWindow.argtypes = [HANDLE]
    user32.GetDpiForWindow.restype = ctypes.c_uint
    user32.GetDpiForWindow.argtypes = [HANDLE]
    user32.GetDpiForWindow.restype = ctypes.c_uint
    user32.MonitorFromWindow.argtypes = [HANDLE, wintypes.DWORD]
    user32.MonitorFromWindow.restype = HANDLE
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = HANDLE
    user32.GetMonitorInfoW.argtypes = [HANDLE, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [HANDLE, HANDLE] + [ctypes.c_int] * 4 + [wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [HANDLE, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [HANDLE]
    user32.IsWindowVisible.argtypes = [HANDLE]
    user32.SetWindowRgn.argtypes = [HANDLE, HANDLE, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [HANDLE, ctypes.c_void_p]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetLayeredWindowAttributes.argtypes = [
        HANDLE, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD
    ]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    # LONG_PTR, so 64-bit. The non-Ptr names are the 32-bit fallback.
    for name, setter in (("GetWindowLongPtrW", False), ("SetWindowLongPtrW", True)):
        fn = getattr(user32, name, None)
        if fn is None:
            continue
        fn.argtypes = [HANDLE, ctypes.c_int] + ([ctypes.c_ssize_t] if setter else [])
        fn.restype = ctypes.c_ssize_t
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi32.CreateRoundRectRgn.restype = HANDLE
    gdi32.DeleteObject.argtypes = [HANDLE]
    dwmapi.DwmSetWindowAttribute.argtypes = [HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


_declare()

# DwmSetWindowAttribute keys. The last two are Windows 11 only; on 10 they
# fail harmlessly, which is why nothing here checks the build number.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
DWMSBT_TRANSIENTWINDOW = 3  # the backdrop Windows uses for flyouts

SW_HIDE, SW_SHOW = 0, 5
SPI_GETWORKAREA = 0x0030

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

# Windows' own default accent, for when the palette cannot be read.
DEFAULT_ACCENT = {True: "#4CC2FF", False: "#005FB8"}


# --- process setup --------------------------------------------------------


def set_dpi_aware() -> bool:
    """Declare per-monitor DPI awareness. Call before importing pywebview.

    Awareness can only be set once per process, and whichever call gets there
    first wins - importing pywebview sets it, so doing this afterwards fails
    silently and leaves the process in a different coordinate space than this
    module assumes. That mismatch is not visible as an error; it shows up as
    a window of the wrong size in the wrong place on a scaled display.

    Returns whether we were the ones who set it. False is not fatal: the
    geometry below is measured in whatever space the process ended up in.
    """
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(-4):  # per-monitor v2
            return True
    except (AttributeError, OSError):
        pass
    try:
        return ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
    except (AttributeError, OSError):
        return bool(user32.SetProcessDPIAware())


# --- where the window goes ------------------------------------------------


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def work_area(hwnd=None) -> tuple[int, int, int, int]:
    """The usable screen - monitor minus taskbar - as (left, top, right, bottom).

    Measured from the window's own monitor rather than via SPI_GETWORKAREA,
    which only ever describes the primary one. The important part is that
    GetMonitorInfo answers in the same coordinate space SetWindowPos expects,
    so the numbers cannot disagree about DPI however the process was set up.
    Given a handle this also means the panel docks to whichever screen it is
    already on instead of jumping to the primary.
    """
    if hwnd:
        monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest
    else:
        monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # primary
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        rect = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom


def dpi_scale(hwnd=None) -> float:
    """Physical pixels per layout pixel: 1.25 on a display set to 125%."""
    if hwnd:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96
    try:
        return user32.GetDpiForSystem() / 96
    except (AttributeError, OSError):
        return 1.0


def dock_flyout(
    width: int = 360,
    height_fraction: float = 0.5,
    margin: int = 16,
    hwnd=None,
    nudge_x: int = 0,
    nudge_y: int = 0,
) -> tuple[int, int, int, int]:
    """Geometry for a Quick Settings-style flyout: bottom right, part height.

    Sizes are given in layout pixels and scaled here, because that is the unit
    Windows designs in - its own Quick Settings flyout is 360 wide whatever the
    display is set to. Passing a raw pixel count instead makes the panel shrink
    as the scaling goes up, which is what "420" was quietly doing: 336 layout
    pixels on a 125% display, noticeably narrower than the real thing.
    """
    left, top, right, bottom = work_area(hwnd)
    scale = dpi_scale(hwnd)

    wide = round(width * scale)
    gap = round(margin * scale)
    usable = bottom - top
    tall = max(round(usable * height_fraction), round(280 * scale))
    tall = min(tall, usable - gap * 2)

    # Anchored to the bottom right, so it grows upwards out of the corner it
    # appears from rather than away from it.
    #
    # The nudges are applied last and deliberately not scaled: they are for
    # lining the panel up by eye against what is already on screen, and what
    # you are matching is real pixels, not layout ones.
    x = right - wide - gap + nudge_x
    y = bottom - tall - gap + nudge_y
    return x, y, wide, tall


# How far the panel travels on its way in and out, in layout pixels. Short
# distances read as a flash rather than a movement no matter how long they are
# given: at 56px the eye only ever caught the last few pixels of it.
TRAVEL = 140


def _animate(hwnd, x, width, height, start_y, end_y, duration, ease) -> None:
    """Walk the window between two positions on a real clock.

    Driven by elapsed time rather than a fixed step count, so the motion keeps
    its timing when a frame runs late instead of stretching out. The timer
    resolution is raised for the duration because Windows' default 15.6ms tick
    quantises sleeps into about a dozen visible jumps.
    """
    winmm = ctypes.windll.winmm
    winmm.timeBeginPeriod(1)
    try:
        started = time.perf_counter()
        while True:
            fraction = min(1.0, (time.perf_counter() - started) / duration)
            place(hwnd, x, round(start_y + (end_y - start_y) * ease(fraction)), width, height)
            if fraction >= 1.0:
                return
            time.sleep(0.006)
    finally:
        winmm.timeEndPeriod(1)


def slide_up(hwnd, x: int, y: int, width: int, height: int, duration: float = 0.26) -> None:
    """Show the window rising into place, the way Windows' own flyouts arrive.

    Done by moving the window rather than animating inside the page: the whole
    panel travels, including its background and border, which is what makes it
    read as a flyout instead of a box whose contents happen to slide.
    """
    rise = round(TRAVEL * dpi_scale(hwnd))
    place(hwnd, x, y + rise, width, height)
    show(hwnd)
    # Decelerating: quick off the mark, settling gently into place.
    _animate(hwnd, x, width, height, y + rise, y, duration, lambda t: 1 - (1 - t) ** 3)


def slide_down(hwnd, duration: float = 0.16) -> None:
    """Drop the window out of sight, then hide it properly.

    Accelerating rather than decelerating, which is the pairing Windows uses:
    things ease in when they arrive and speed up as they leave. Hiding only
    once the movement is finished is what stops it vanishing mid-slide.
    """
    x, y, width, height = window_rect(hwnd)
    fall = round(TRAVEL * dpi_scale(hwnd))
    _animate(hwnd, x, width, height, y, y + fall, duration, lambda t: t**3)
    hide(hwnd)


# --- the window itself ----------------------------------------------------


def attach(window) -> None:
    """Nothing to remember. Windows finds the panel by title through the window
    manager, so the pywebview object is of no use here - but the macOS side has
    no such lookup and needs the object, and app.py should not have to know
    which of those it is talking to."""
    return None


def find_hwnd(title: str, timeout: float = 10.0):
    """Wait for the window to exist and return its handle.

    pywebview does not hand out the handle, and the window is built on the GUI
    thread a moment after create_window returns, so it is looked up by title.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return None


def _set_attribute(hwnd, key: int, value: int) -> bool:
    data = ctypes.c_int(value)
    result = dwmapi.DwmSetWindowAttribute(hwnd, key, ctypes.byref(data), ctypes.sizeof(data))
    return result == 0


def apply_style(hwnd, dark: bool) -> None:
    """Dark mode, rounded corners and the flyout backdrop.

    Only some of this lands on a frameless window - see round_corners below -
    but asking costs nothing and a framed variant gets it all.
    """
    _set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    _set_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)


def round_corners(hwnd, width: int, height: int, radius: int = 8) -> bool:
    """Clip the window to rounded corners.

    The DWM corner preference above only rounds a frame, and this window has
    none, so the corners are cut out of the window region instead. That is a
    hard-edged clip rather than the antialiased curve of a real frame, but at
    an 8px radius against the desktop it is not something you notice.

    Cosmetic, and deliberately failure-tolerant: a bad region would clip the
    whole panel away, and an invisible window is far worse than a square one.
    """
    if width <= 0 or height <= 0:
        return False
    region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
    if not region:
        return False
    if not user32.SetWindowRgn(hwnd, region, True):  # on success the window owns it
        gdi32.DeleteObject(region)
        return False
    return True


def place(hwnd, x: int, y: int, width: int, height: int) -> None:
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)


def is_visible(hwnd) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def window_rect(hwnd) -> tuple[int, int, int, int]:
    """Where the window is right now, as (x, y, width, height)."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def set_opacity(hwnd, percent_transparent: int) -> None:
    """Fade the whole window, 0 being fully solid.

    WS_EX_LAYERED has to be on the window before the alpha means anything, and
    it is left on afterwards: taking it off again to go back to solid would
    make the window flicker as it is recreated, and a layered window at full
    alpha costs nothing.
    """
    get = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    put = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    style = get(hwnd, GWL_EXSTYLE)
    if not style & WS_EX_LAYERED:
        put(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

    alpha = max(0, min(255, round(255 * (1 - max(0, min(90, percent_transparent)) / 100))))
    user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)


def set_icon(hwnd, ico: Path) -> bool:
    """Give the window the Claude mark.

    A tool window shows no icon in the taskbar or Alt+Tab, so this is not
    about those - it is what Windows uses anywhere else the window is named,
    and what stops a stray default Python icon appearing there instead.
    """
    if not ico or not Path(ico).is_file():
        return False

    LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

    user32.LoadImageW.argtypes = [
        HANDLE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT
    ]
    user32.LoadImageW.restype = HANDLE
    user32.SendMessageW.argtypes = [HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]

    loaded = False
    for which, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
        handle = user32.LoadImageW(
            None, str(ico), 1, size, size, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        if handle:
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
            loaded = True
    return loaded


def hide_from_taskbar(hwnd) -> None:
    """Keep the panel out of the taskbar and out of Alt+Tab.

    A flyout is furniture, not a document you switch to - Quick Settings has
    no taskbar button either, and a stray python icon appearing next to it
    gives the game away. WS_EX_TOOLWINDOW is the flag that says so; it is set
    while the window is still hidden, because the taskbar decides whether to
    add a button at the moment the window is first shown.
    """
    get = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    put = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    style = get(hwnd, GWL_EXSTYLE)
    put(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def show(hwnd) -> None:
    """Show and focus.

    SetForegroundWindow is normally refused for a background process, but a
    process whose hotkey was just pressed is explicitly allowed to steal focus
    - which is the only reason this panel can appear over a full-screen app.
    """
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)


def hide(hwnd) -> None:
    user32.ShowWindow(hwnd, SW_HIDE)


# --- the user's colours ---------------------------------------------------


def uses_dark_theme() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def accent(dark: bool) -> str:
    """The user's own accent colour, shaded the way WinUI shades it.

    Windows keeps eight tints of the accent in one 32-byte blob, ordered
    lightest to darkest. WinUI fills controls with Light2 on a dark surface and
    Dark1 on a light one, so the colour holds its contrast against the page
    either way; picking the base accent for both is what makes home-made
    Fluent themes look slightly wrong.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
        ) as key:
            palette = winreg.QueryValueEx(key, "AccentPalette")[0]
        offset = 4 if dark else 16  # Light2 / Dark1
        red, green, blue = palette[offset : offset + 3]
        return f"#{red:02X}{green:02X}{blue:02X}"
    except (OSError, IndexError, ValueError):
        return DEFAULT_ACCENT[dark]


def theme() -> dict:
    """Everything the page needs to match the system, in one object."""
    dark = uses_dark_theme()
    return {"dark": dark, "accent": accent(dark)}
