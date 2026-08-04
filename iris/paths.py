"""Where Iris keeps things, running from source or from a built exe.

Two different questions, and conflating them is the usual way a frozen app
breaks:

    resource()  what ships *inside* the program - the panel's HTML, the logo.
                Read-only. PyInstaller unpacks these to a temp folder that is
                deleted on exit, so nothing written there survives.

    data_dir()  what belongs to the *install* - .env, memory.json. Written to
                constantly, and must outlive the process.

From source both answer "the project folder", which is why the distinction can
sit unnoticed until the first build.

On the install location: Program Files needs administrator rights, and would
still be unwritable afterwards for a normally-launched process - which is fatal
here, because the memory file is rewritten on every saved memory. So the
default is under LOCALAPPDATA, where VS Code and Discord put themselves for the
same reason.
"""

import os
import sys
from pathlib import Path

APP_NAME = "IrisAI"


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than the source."""
    return bool(getattr(sys, "frozen", False))


def resource(*parts) -> Path:
    """A file that ships with the program. Read-only."""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parents[1]
    return base.joinpath(*parts)


def default_install_dir() -> Path:
    """Where a fresh install goes. Programs on Windows, Application Support on a Mac.

    Deferred to the platform layer rather than decided here: the two are not
    variations on one path, they are different conventions, and putting the
    Mac's under LOCALAPPDATA-with-a-fallback would produce a folder no Mac user
    would think to look in.
    """
    from iris import platform

    return platform.default_install_dir()


def data_dir() -> Path:
    """Where .env and memory.json live.

    IRIS_HOME overrides everything, which is how the wizard points a
    not-yet-installed copy at the folder it is about to fill, and how a
    portable install can keep its data beside the exe.
    """
    override = os.environ.get("IRIS_HOME")
    if override:
        return Path(override)

    if is_frozen():
        beside_exe = Path(sys.executable).resolve().parent
        if (beside_exe / ".env").is_file():
            return beside_exe  # an installed copy, or a portable one
        return default_install_dir()

    return Path(__file__).resolve().parents[1]


def env_file() -> Path:
    return data_dir() / ".env"


def is_installed() -> bool:
    """Has setup been run? The presence of a config is the only test that matters."""
    return env_file().is_file()
