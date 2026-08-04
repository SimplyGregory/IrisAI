"""Talking to VS Code, through the bridge extension.

The extension serves one connection point per window - a named pipe on Windows,
a Unix socket on a Mac - and announces itself in ~/.iris/vscode, stamping the
file whenever that window takes focus. This picks the most recently focused
one, so "fix this file" means the window you are looking at rather than
whichever happened to start first.

Which transport it is comes from the address the extension recorded, not from
checking the platform here. The extension is the one that chose it.

Nothing here raises at the caller: a closed editor is an ordinary state, not a
failure, and the tools turn it into something Claude can act on - usually
"open VS Code first".
"""

import contextlib
import itertools
import json
import socket
import threading
from pathlib import Path

REGISTRY = Path.home() / ".iris" / "vscode"
TIMEOUT = 8.0  # seconds; every operation is a local API call, so this is generous

_counter = itertools.count(1)


class EditorUnavailable(Exception):
    """VS Code is not running, or the bridge is not installed in it."""


def windows() -> list[dict]:
    """Every VS Code window serving the bridge, most recently focused first."""
    if not REGISTRY.is_dir():
        return []
    found = []
    for record in REGISTRY.glob("*.json"):
        try:
            found.append(json.loads(record.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # half-written or gone; the next scan will settle it
    return sorted(found, key=lambda w: w.get("focused", 0), reverse=True)


def _forget(pid) -> None:
    """Drop a window that is no longer answering.

    VS Code closing normally removes its own file, but a crash or a killed
    process leaves one behind, and a stale entry at the top of the focus order
    would shadow the window that is actually open.
    """
    try:
        (REGISTRY / f"{pid}.json").unlink()
    except OSError:
        pass


@contextlib.contextmanager
def _connected(address: str):
    """A read/write byte stream to the extension, whatever it is listening on.

    Windows serves a named pipe, which opens like a file. Everywhere else it is
    a Unix socket, which does not - but makefile() gives it the same read and
    write methods, so only this function knows the difference.

    Told apart by the address rather than by asking which platform we are on:
    the extension chose it and put it in the registry, and that record is the
    honest answer even if this process is somehow wrong about itself.
    """
    if address.startswith("\\\\"):
        with open(address, "r+b", buffering=0) as pipe:
            yield pipe
        return

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT)
    try:
        connection.connect(address)
        # Closed with the socket below, so the stream is not given ownership.
        stream = connection.makefile("rwb", buffering=0)
        try:
            yield stream
        finally:
            stream.close()
    finally:
        connection.close()


def _exchange(pipe: str, payload: bytes) -> dict:
    """One request, one reply, with a ceiling on how long it may take.

    Neither transport has a timeout of its own that covers a wedged extension
    host, which would otherwise block the tool - and with it the whole turn -
    indefinitely. The worker is a daemon, so abandoning it cannot keep the
    program alive.
    """
    outcome: dict = {}

    def talk():
        try:
            with _connected(pipe) as connection:
                connection.write(payload)
                line = bytearray()
                while not line.endswith(b"\n"):
                    chunk = connection.read(1)
                    if not chunk:
                        break
                    line += chunk
            outcome["reply"] = json.loads(bytes(line))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=talk, name="iris-vscode", daemon=True)
    worker.start()
    worker.join(TIMEOUT)

    if worker.is_alive():
        raise EditorUnavailable(f"VS Code did not answer within {TIMEOUT:.0f}s.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["reply"]


def call(op: str, **args) -> dict:
    """Run one operation in the focused VS Code window.

    Raises EditorUnavailable if there is no window to run it in, and ValueError
    if the window ran it and said no.
    """
    payload = json.dumps({"id": next(_counter), "op": op, "args": args}).encode("utf-8") + b"\n"

    nothing_open = (
        "No VS Code window has the Iris bridge running. Open VS Code, or - "
        "if it is already open - reload the window so the extension starts."
    )

    available = windows()
    if not available:
        raise EditorUnavailable(nothing_open)

    problems = []
    for window in available:
        try:
            reply = _exchange(window["pipe"], payload)
        except (FileNotFoundError, ConnectionRefusedError):
            # Announced, but gone. Missing is what a dead named pipe looks
            # like; refused is what a Unix socket file looks like once the
            # window that made it has crashed - the file outlives the process.
            # Both mean the same thing, and both have to clear the entry, or a
            # dead window sits at the top of the focus order shadowing a live
            # one for as long as VS Code stays open.
            _forget(window.get("pid"))
            continue
        except EditorUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{window.get('name', '?')}: {exc}")
            continue

        if not reply.get("ok"):
            raise ValueError(reply.get("error", "VS Code refused the request."))
        return reply.get("result", {})

    # Every window we knew about was a leftover from one that has since closed,
    # which is the same situation as none being open - and saying "reload the
    # window" about a window that is not there sends the user looking for it.
    if not problems:
        raise EditorUnavailable(nothing_open)
    raise EditorUnavailable(
        "VS Code is open but the bridge is not answering; reload the window. "
        + "; ".join(problems)
    )


def is_connected() -> bool:
    """Whether a live window is there, without making a fuss if it is not."""
    try:
        call("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


def enabled() -> bool:
    """Whether the user asked for the VS Code connection during setup.

    Through config rather than os.environ directly: config is what loads the
    .env, and reading the variable before that has happened would silently
    report "off" for everybody.
    """
    from iris import config

    return config.VSCODE
