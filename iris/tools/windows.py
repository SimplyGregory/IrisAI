"""Window management: minimize, focus, close, and so on."""

from anthropic import beta_tool

from iris.confirm import confirm
from iris.redact import scrubbed

_ACTIONS = ("minimize", "maximize", "restore", "focus", "close")


def _all_windows():
    from iris import platform

    return platform.list_windows()


@beta_tool
@scrubbed
def list_windows() -> str:
    """List the titles of all open application windows.

    Call this when you need to know what is currently open, or to find the exact
    title of a window before acting on it.
    """
    try:
        windows = _all_windows()
    except Exception as exc:  # pygetwindow raises bare Exceptions on some systems
        return f"Could not enumerate windows: {exc}"

    if not windows:
        return "No open windows."

    lines = []
    for w in windows:
        # The owning application is only known on macOS, where a window title
        # is often just a document name with no hint of what opened it. Shown
        # when there is one rather than padded out when there is not.
        owner = f"  ({w['app']})" if w.get("app") else ""
        lines.append(f"  {w['title']}{owner}")
    return f"{len(windows)} open window(s):\n" + "\n".join(lines)


@beta_tool
@scrubbed
def wait_for_window(title_contains: str, timeout_seconds: float = 30, focus: bool = False) -> str:
    """Wait until a window with this title appears, then return immediately.

    Use this after launching an app instead of guessing a duration with wait.
    A fixed pause is always wrong: too short and the window is not there yet,
    too long and you sit doing nothing. This returns the moment the window
    actually exists, so a fast app costs a fraction of a second.

    Args:
        title_contains: Part of the window title to wait for, e.g. "Roblox".
        timeout_seconds: Give up after this long. Capped at 120.
        focus: Bring the window to the front once it appears.
    """
    import time

    from iris import interrupt

    needle = title_contains.strip().lower()
    if not needle:
        return "Say which window to wait for."

    limit = max(0.5, min(float(timeout_seconds), 120.0))
    started = time.monotonic()

    while True:
        try:
            match = next(
                (w for w in _all_windows() if needle in w["title"].lower()), None
            )
        except Exception as exc:
            return f"Could not enumerate windows: {exc}"

        if match is not None:
            waited = time.monotonic() - started
            note = ""
            if focus:
                from iris import platform

                ok, _ = platform.window_action(match["title"], "focus")
                note = " and brought it to the front" if ok else " (could not bring it to the front)"
            return f"'{match['title']}' appeared after {waited:.1f}s{note}."

        if time.monotonic() - started > limit:
            try:
                titles = ", ".join(w["title"] for w in _all_windows()[:10]) or "none"
            except Exception:
                titles = "unknown"
            return (
                f"No window matching {title_contains!r} appeared within {limit:g}s. "
                f"Open windows: {titles}. The app may have failed to start, or its "
                "window may be titled differently than you expect."
            )

        # Stay interruptible: waiting for a slow app is exactly when someone
        # changes their mind.
        held = interrupt.interruptible_sleep(0.3, "wait_for_window")
        if held is not None:
            return held


@beta_tool
@scrubbed
@confirm("announce")
def window_control(title_contains: str, action: str) -> str:
    """Minimize, maximize, restore, focus, or close a window.

    Matching is a case-insensitive substring of the window title, so "chrome"
    matches "YouTube - Google Chrome". If several windows match, the first is
    used; call list_windows first if you need to be precise.

    Args:
        title_contains: Part of the target window's title.
        action: One of "minimize", "maximize", "restore", "focus", "close".
    """
    action = action.strip().lower()
    if action not in _ACTIONS:
        return f"action must be one of {list(_ACTIONS)}, got {action!r}"

    try:
        windows = _all_windows()
    except Exception as exc:
        return f"Could not enumerate windows: {exc}"

    needle = title_contains.strip().lower()
    matches = [w for w in windows if needle in w["title"].lower()]
    if not matches:
        titles = ", ".join(w["title"] for w in windows[:10]) or "none"
        return f"No window matching {title_contains!r}. Open windows: {titles}"

    from iris import platform

    target = matches[0]
    ok, message = platform.window_action(target["title"], action)
    if not ok:
        # Focus in particular can fail when another process holds foreground.
        return f"Could not {action} '{target['title']}': {message}"

    extra = f" ({len(matches)} windows matched; acted on the first)" if len(matches) > 1 else ""
    return f"Did {action} on '{target['title']}'.{extra}"


TOOLS = [list_windows, wait_for_window, window_control]
