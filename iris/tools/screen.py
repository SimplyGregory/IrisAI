"""Direct screen control: the fallback tier for native apps with no DOM.

Slower and less reliable than the browser tools, so the system prompt tells
Claude to reach for these only when nothing else fits.
"""

import base64
import io

from anthropic import beta_tool

from iris.confirm import confirm

# Screenshots are downscaled before being sent to the model: full-resolution
# images cost a lot of tokens. click_at takes coordinates in *screenshot* space
# and scales them back up here, so Claude never has to do the arithmetic.
#
# The encoded size is also capped. A busy desktop can produce a PNG whose
# base64 exceeds the Agent SDK's 1 MB message limit, which kills the whole
# session rather than failing one tool. Encoding steps down through widths, and
# falls back to a 256-colour palette (flat UI colours compress far better that
# way, and text stays sharp) until the result fits.
_WIDTH_STEPS = (1536, 1280, 1024, 800)
_MAX_B64_CHARS = 400_000
_last_scale = 1.0


def _encode_bounded(image, full_width: int, full_height: int):
    """Return (base64, (w, h), scale) for the largest encoding that fits."""
    from PIL import Image

    data = ""
    size = image.size
    scale = 1.0
    for width in _WIDTH_STEPS:
        if full_width <= width:
            scaled, scale = image, 1.0
        else:
            scale = width / full_width
            scaled = image.resize((width, round(full_height * scale)), Image.LANCZOS)

        for palette in (False, True):
            candidate = (
                scaled.convert("P", palette=Image.ADAPTIVE, colors=256) if palette else scaled
            )
            buffer = io.BytesIO()
            candidate.save(buffer, format="PNG", optimize=True)
            data = base64.standard_b64encode(buffer.getvalue()).decode()
            size = scaled.size
            if len(data) <= _MAX_B64_CHARS:
                return data, size, scale
    return data, size, scale  # smallest attempt, even if still over


def _grab():
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary display
        raw = sct.grab(monitor)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"), monitor


@beta_tool
def screenshot():
    """Take a screenshot of the primary monitor and look at it.

    Use this before screen_click or screen_type so you can see where things are.
    Coordinates you read off this image are the ones those tools expect.
    """
    global _last_scale
    try:
        image, monitor = _grab()
    except Exception as exc:
        return f"Could not capture the screen: {exc}"

    full_w, full_h = image.size
    encoded, (shot_w, shot_h), _last_scale = _encode_bounded(image, full_w, full_h)

    return [
        {
            "type": "text",
            "text": (
                f"Screenshot is {shot_w}x{shot_h} "
                f"(actual screen {full_w}x{full_h}). "
                "Give screen_click coordinates as they appear in this image."
            ),
        },
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": encoded},
        },
    ]


@beta_tool
@confirm("announce")
def screen_click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at a point on screen, in screenshot coordinates.

    Take a screenshot first. Only use this for native application windows;
    for anything inside Chrome use the browser tools instead.

    Args:
        x: Horizontal position as seen in the most recent screenshot.
        y: Vertical position as seen in the most recent screenshot.
        button: "left", "right", or "middle".
        double: Double-click instead of single.
    """
    import pyautogui

    real_x, real_y = int(x / _last_scale), int(y / _last_scale)
    try:
        pyautogui.click(real_x, real_y, button=button, clicks=2 if double else 1)
    except Exception as exc:
        return f"Could not click at ({x}, {y}): {exc}"
    return f"Clicked at ({x}, {y})."


def _active_title() -> str:
    try:
        import pygetwindow as gw

        window = gw.getActiveWindow()
        return window.title if window else ""
    except Exception:
        return ""


def _wrong_window(expect_window: str) -> str | None:
    """Guard against typing into the wrong application."""
    if not expect_window:
        return None
    active = _active_title()
    if expect_window.lower() in active.lower():
        return None
    return (
        f"Refused: the focused window is {active or 'unknown'!r}, which does not match "
        f"{expect_window!r}. The app may still be starting. Use wait, then focus it with "
        "window_control before typing."
    )


@beta_tool
def wait(seconds: float, reason: str = "") -> str:
    """Pause before continuing.

    Applications take a moment to appear after launching, and pages and dialogs
    take a moment to render. Use this between launching something and acting on
    it, rather than assuming it is ready.

    Args:
        seconds: How long to wait. Capped at 30.
        reason: What you are waiting for, e.g. "VS Code to finish starting".
    """
    from iris import interrupt

    duration = max(0.0, min(float(seconds), 30.0))
    # A long wait is exactly when someone wants to barge in, so this sleep
    # watches for it rather than blocking straight through.
    held = interrupt.interruptible_sleep(duration, "wait")
    if held is not None:
        return held
    return f"Waited {duration:g}s{f' for {reason}' if reason else ''}. Focused window is now {_active_title()!r}."


@beta_tool
@confirm("announce")
def screen_type(text: str, press_enter: bool = False, expect_window: str = "") -> str:
    """Type text into whatever window currently has focus.

    Always pass expect_window. Keystrokes go wherever focus happens to be, so if
    the app you meant is still starting, your text lands in whatever was
    underneath it - possibly an editor or a chat box.

    Args:
        text: The text to type.
        press_enter: Press Enter afterwards.
        expect_window: Part of the title of the window that should have focus.
            Typing is refused if it does not match.
    """
    import pyautogui

    mismatch = _wrong_window(expect_window)
    if mismatch:
        return mismatch

    try:
        pyautogui.write(text, interval=0.01)
        if press_enter:
            pyautogui.press("enter")
    except Exception as exc:
        return f"Could not type: {exc}"
    return f"Typed {len(text)} characters."


@beta_tool
@confirm("announce")
def screen_key(keys: str, expect_window: str = "") -> str:
    """Press a key or a keyboard shortcut.

    Shortcuts go to the focused window, so pass expect_window whenever the
    shortcut only makes sense in a particular app (ctrl+shift+x in VS Code
    would do something quite different elsewhere).

    Args:
        keys: A single key like "enter", "escape", "tab", or a combination
            joined by "+" such as "ctrl+s", "alt+tab", "win+d".
        expect_window: Part of the title of the window that should have focus.
            The keypress is refused if it does not match.
    """
    import pyautogui

    mismatch = _wrong_window(expect_window)
    if mismatch:
        return mismatch

    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not parts:
        return "No keys given."
    try:
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
    except Exception as exc:
        return f"Could not press {keys}: {exc}"
    return f"Pressed {keys}."


TOOLS = [screenshot, screen_click, screen_type, screen_key, wait]
