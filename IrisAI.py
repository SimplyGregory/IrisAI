"""Iris. The one thing you run.

    IrisAI.exe                  setup on the first run, the panel after that
    python IrisAI.py            the same, from source

    --setup                     run setup again over an existing install
    --hotkey-test               check the hotkey reaches us
    --mic-test / --voices / --calibrate      microphone and speech checks

Whether setup has run is decided by one thing: does a .env exist in the install
folder. No registry key, no marker file. If you delete the folder it is gone;
if you copy it somewhere it works there.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Launched from a shortcut with no console, print() would raise on a None
# stdout. Swallow it before anything imported below writes a line.
if sys.stdout is None or sys.stderr is None:
    class _Null:
        def write(self, _text):
            pass

        def flush(self):
            pass

    sys.stdout = sys.stdout or _Null()
    sys.stderr = sys.stderr or _Null()

from iris import spawn, log, paths  # noqa: E402

# Before anything starts a helper process. No-op from a terminal.
spawn.hide_console_children()


def main() -> int:
    log.startup("IrisAI")

    if "--setup" in sys.argv or not paths.is_installed():
        from installer import wizard

        folder = wizard.run()
        if folder is None:
            return 0  # closed without finishing; nothing was written

        # The panel reads .env at import time, so it has to start in the
        # folder setup just wrote - which for a built copy means handing over
        # to the exe that now lives there.
        os.environ["IRIS_HOME"] = str(folder)
        if paths.is_frozen() and Path(sys.executable).resolve().parent != folder:
            wizard.launch_installed(folder)
            return 0

        # Running from source the panel starts in this process, so the same
        # request has to be made here rather than on a command line.
        if "--show" not in sys.argv:
            sys.argv.append("--show")

    sys.path.insert(0, str(paths.resource("panel")))
    import app as panel

    return panel.main()


if __name__ == "__main__":
    # Built windowed there is no console for a traceback to reach, so an
    # unhandled exception would end the process with no trace of why.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        log.failure("IrisAI stopped", exc)
        raise
