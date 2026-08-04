"""A log file, because a built copy has nowhere else to say anything.

Run from source there is a console and print() is enough. Built windowed -
which it must be, or every launch flashes a terminal - there is no console at
all: no stdout, no stderr, and an unhandled exception kills the process in
silence. Every problem looks identical from outside, which is "I double-clicked
it and nothing happened".

So the same lines go to a file next to the install. It is small, it is plain
text, and it is the first thing to ask for when something does not start.
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path

MAX_BYTES = 256_000  # a couple of thousand lines; older ones roll off


def path() -> Path | None:
    from iris import paths

    try:
        return paths.data_dir() / "iris.log"
    except Exception:
        return None


def write(message: str) -> None:
    """Append one line. Never raises - logging must not break the program."""
    target = path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Roll rather than grow forever. Keeping the tail is what matters:
        # whatever went wrong did so at the end.
        if target.is_file() and target.stat().st_size > MAX_BYTES:
            kept = target.read_text(encoding="utf-8", errors="replace")[-MAX_BYTES // 2:]
            target.write_text(f"[earlier lines dropped]\n{kept}", encoding="utf-8")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {message}\n")
    except OSError:
        pass


def failure(what: str, exc: BaseException) -> None:
    """Log an exception with its traceback, then carry on failing."""
    write(f"{what}: {type(exc).__name__}: {exc}")
    write(textwrap_indent(traceback.format_exc()))


def textwrap_indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip().splitlines())


def startup(where: str) -> None:
    """What was running, and how it was invoked. The first questions to ask."""
    from iris import paths

    write("-" * 60)
    write(f"start: {where}")
    write(f"  frozen   : {paths.is_frozen()}")
    write(f"  argv     : {sys.argv}")
    write(f"  exe      : {sys.executable}")
    write(f"  data dir : {paths.data_dir()}")
    write(f"  installed: {paths.is_installed()}")

    # Whether child processes will get console windows. When this says the
    # patch did not apply, any blank terminal that appears is explained.
    try:
        from iris import spawn

        write(f"  console   : has={spawn.has_console()} hiding={spawn._installed}")
    except Exception:
        pass
