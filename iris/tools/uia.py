"""Reading and driving a native app through its accessibility tree.

The best way to work with a desktop application, and the one to try before
reaching for pixels: it gives real control names rather than coordinates, and
acting on a control does not steal the mouse.

Both platforms are served by one set of tools. What they return differs - UI
Automation on Windows is richer than what System Events exposes on a Mac - but
the shape is the same: inspect lists controls with an index, and the two act
tools take that index. The platform layer hands back an opaque handle with each
element, so nothing here has to know whether it is holding a live control
object or the names needed to find one again.

Some apps expose almost nothing either way:

    VS Code (Electron)        11 controls,   4 named, 0.1s   - window chrome only
    Slack (Electron)          the same
    File Explorer            hundreds, fully named

Electron hides its tree on both platforms, so for those fall back to the app's
command line if it has one, and to screenshots if it does not.
"""

from anthropic import beta_tool

from iris import platform
from iris.confirm import confirm
from iris.redact import scrubbed

# The last inspection, so an index means something to the act tools. Held here
# rather than passed around because the model only ever sees the number.
_snapshot: list[dict] = []


@beta_tool
@scrubbed
def ui_inspect(window_title: str, filter_text: str = "", max_results: int = 50) -> str:
    """List the controls inside a native application window, each with an index.

    This is the preferred way to work with a desktop app: it gives you real
    control names and types rather than pixels, and ui_click / ui_set_text act
    on them directly. Try this before taking a screenshot.

    If it returns almost nothing but the window buttons, the app is probably
    Electron-based (VS Code, Slack) and hides its accessibility tree.
    In that case use the app's command line if it has one, or fall back to
    screenshot and screen_click.

    Args:
        window_title: Part of the target window's title, e.g. "File Explorer".
        filter_text: Only show controls whose name contains this. Useful on
            busy windows.
        max_results: Maximum number of controls to list.
    """
    global _snapshot

    missing = platform.permissions_missing()
    if missing:
        return (
            "This needs a permission the system has not granted yet: "
            + "; ".join(missing)
            + ". Ask the user to grant it, then try again."
        )

    ok, elements = platform.ui_elements(window_title, max_results)
    if not ok:
        titles = ", ".join(platform.open_window_titles()[:8]) or "none"
        return f"Could not read window {window_title!r}. Open windows: {titles}"

    needle = filter_text.strip().lower()
    _snapshot = []
    lines = []
    for element in elements:
        if needle and needle not in element["name"].lower():
            continue
        index = len(_snapshot)
        _snapshot.append(element)
        mark = "" if element["actionable"] else "  (read-only)"
        lines.append(f"[{index}] {element['kind']}: {element['name'][:80]}{mark}")

    if needle and not lines:
        return f"No controls matching {filter_text!r} in {window_title!r}. Try without a filter."

    # An Electron app typically exposes its title-bar buttons and nothing else.
    # Finding only those means the real UI is invisible to us, which is a
    # different situation from a window that genuinely has no controls.
    chrome_only = not needle and lines and all(
        element["name"].strip().lower() in
        {"minimize", "maximize", "restore", "close", "system", "zoom", "full screen"}
        for element in _snapshot
    )
    if not lines or chrome_only:
        return (
            f"{window_title!r} exposes no usable controls - only window buttons. It is "
            "almost certainly an Electron app (VS Code, Slack) that hides its "
            "accessibility tree. Do not keep retrying this tool. Use the app's command "
            "line if it has one, or fall back to screenshot and screen_click."
        )
    return f"{len(lines)} control(s) in {window_title!r}:\n" + "\n".join(lines)


def _resolve(index: int):
    if not _snapshot:
        return None, "No controls have been inspected yet. Call ui_inspect first."
    if not 0 <= index < len(_snapshot):
        return None, f"No control [{index}]. ui_inspect listed {len(_snapshot)}."
    return _snapshot[index], None


@beta_tool
@scrubbed
@confirm("announce")
def ui_click(index: int) -> str:
    """Click a control by the index shown in the most recent ui_inspect.

    Invokes the control directly through the accessibility API where possible,
    which is more reliable than moving the mouse and does not steal the pointer.

    Args:
        index: The bracketed number from ui_inspect.
    """
    element, problem = _resolve(index)
    if problem:
        return problem
    ok, message = platform.ui_do(element, "click")
    return message if ok else f"Could not click [{index}]: {message}"


@beta_tool
@scrubbed
@confirm("announce")
def ui_set_text(index: int, text: str) -> str:
    """Type into a text control by the index shown in the most recent ui_inspect.

    Sets the control's value directly rather than sending keystrokes, so it
    cannot land in the wrong window and does not depend on focus.

    Args:
        index: The bracketed number from ui_inspect.
        text: What to put in the control, replacing whatever is there.
    """
    element, problem = _resolve(index)
    if problem:
        return problem
    if not element["actionable"]:
        return f"[{index}] {element['name']!r} is read-only; there is nothing to type into."
    ok, message = platform.ui_do(element, "set_text", text)
    return message if ok else f"Could not set [{index}]: {message}"


TOOLS = [ui_inspect, ui_click, ui_set_text]
