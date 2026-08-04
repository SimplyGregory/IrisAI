"""System tray presence, so Iris is a background app rather than a terminal job.

The icon is the whole point: without it there is no way to tell whether she is
running, listening, or wedged. Colour follows state, so a glance at the tray
answers "is she alive and what is she doing".

    grey    idle, waiting for the wake word
    blue    listening to you
    amber   working on it
    green   speaking
    dim     paused - not listening at all
"""

import threading

_STATES = {
    "idle": ((120, 124, 130), "Iris - waiting for the wake word"),
    "listening": ((40, 130, 220), "Iris - listening"),
    "thinking": ((225, 160, 40), "Iris - working on it"),
    "speaking": ((60, 170, 90), "Iris - speaking"),
    "paused": ((70, 72, 76), "Iris - paused"),
}

_icon = None
_state = "idle"
_paused = threading.Event()
_on_quit = None


def _project_root():
    """The folder holding .env - not STATE_DIR, which is ~/.iris."""
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _image(state: str):
    """A filled circle in the state colour, with a soft ring around it."""
    from PIL import Image, ImageDraw

    colour, _ = _STATES.get(state, _STATES["idle"])
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    ring = tuple(min(255, c + 45) for c in colour)
    draw.ellipse((2, 2, size - 3, size - 3), outline=ring + (170,), width=3)
    draw.ellipse((13, 13, size - 14, size - 14), fill=colour + (255,))
    return img


def set_state(state: str) -> None:
    """Called from the voice loop as Iris moves between activities."""
    global _state
    if _paused.is_set() and state != "paused":
        return
    _state = state
    if _icon is not None:
        try:
            _icon.icon = _image(state)
            _icon.title = _STATES.get(state, _STATES["idle"])[1]
        except Exception:
            pass  # the tray is cosmetic; never let it break the assistant


def is_paused() -> bool:
    return _paused.is_set()


def _toggle_pause(icon, item):
    if _paused.is_set():
        _paused.clear()
        set_state("idle")
    else:
        _paused.set()
        set_state("paused")


def _open(path):
    import os

    def handler(icon, item):
        try:
            os.startfile(str(path))
        except OSError:
            pass

    return handler


def _quit(icon, item):
    if _on_quit:
        _on_quit()
    icon.stop()


def run(on_quit=None) -> None:
    """Show the tray icon and block. Must be called on the main thread."""
    global _icon, _on_quit
    import pystray

    from iris import config, memory

    _on_quit = on_quit
    menu = pystray.Menu(
        pystray.MenuItem(
            lambda item: "Resume listening" if _paused.is_set() else "Pause listening",
            _toggle_pause,
        ),
        pystray.Menu.SEPARATOR,
        # Memories and the transcript share one file, so one item opens both.
        pystray.MenuItem("Open memory", _open(memory.path() or "")),
        pystray.MenuItem("Open settings (.env)", _open(_project_root() / ".env")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Quit {config.ASSISTANT_NAME}", _quit),
    )
    _icon = pystray.Icon(
        config.ASSISTANT_NAME.lower(),
        _image("idle"),
        _STATES["idle"][1],
        menu,
    )
    _icon.run()


def stop() -> None:
    if _icon is not None:
        try:
            _icon.stop()
        except Exception:
            pass
