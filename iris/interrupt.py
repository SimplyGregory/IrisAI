"""Barge-in: saying the wake word mid-task pauses Iris so you can steer.

While a command runs, nothing is listening - the loop is busy executing tools.
So a background watcher listens for the wake word during the run. When it
fires, the *next* tool call is held before it executes and you are asked what
you want. Between tool calls is the right place to pause: the machine is in a
consistent state there, whereas stopping halfway through a click or a file
write is not something that can be undone cleanly.

What you say is classified into three outcomes:

    "cancel" / "stop"        -> abandon the rest of the task
    "continue" / "carry on"  -> resume as if nothing happened
    anything else            -> passed to Iris as a correction to apply first

Only one microphone stream can be open at a time, so the watcher releases the
microphone before the question is asked, and is restarted afterwards.
"""

import threading
import time

_wake_heard = threading.Event()
_cancelled = threading.Event()
_stop_watch = threading.Event()
_watcher: threading.Thread | None = None
_ask = None  # set by the voice entry point: speak a prompt, return what was said

CANCEL_WORDS = ("cancel", "stop", "abort", "forget it", "don't", "do not", "quit")
CONTINUE_WORDS = ("continue", "carry on", "go ahead", "keep going", "nothing",
                  "never mind", "nevermind", "sorry", "ignore", "resume")


_watch = True


def set_asker(fn, watch: bool = True) -> None:
    """fn() should return what the user wants to do about the interruption.

    In voice mode that means speaking a prompt and listening, and a background
    watcher listens for the wake word to know when to ask. Pass watch=False
    when the interruption arrives some other way - the panel is interrupted by
    a typed message, and starting a wake-word watcher there would open the
    microphone for no reason.
    """
    global _ask, _watch
    _ask = fn
    _watch = watch


def barge_in() -> None:
    """Interrupt from something other than the wake word.

    Sets the same flag the watcher would, so the next tool call is held and
    the asker is consulted exactly as it is in voice mode.
    """
    _wake_heard.set()


def _loop():
    from iris.voice import wake

    while not _stop_watch.is_set():
        try:
            if wake.wait_for_wake(stop_event=_stop_watch):
                _wake_heard.set()
                return  # release the microphone; the handler needs it
        except Exception:
            return


def start_watching() -> None:
    global _watcher
    from iris import config

    if _ask is None or not config.BARGE_IN or not _watch:
        return
    stop_watching()
    _wake_heard.clear()
    _cancelled.clear()
    _stop_watch.clear()
    _watcher = threading.Thread(target=_loop, name="iris-bargein", daemon=True)
    _watcher.start()


def stop_watching() -> None:
    global _watcher
    _stop_watch.set()
    if _watcher and _watcher.is_alive():
        _watcher.join(timeout=2.0)
    _watcher = None


def cancelled() -> bool:
    return _cancelled.is_set()


def pending() -> bool:
    """True if you have barged in and it has not been dealt with yet."""
    return _wake_heard.is_set() or _cancelled.is_set()


def run_interruptible(work, tool_name: str, timeout: float = 120.0):
    """Run a blocking call on a worker thread while staying responsive.

    Returns (held_message, result). If held_message is not None the user barged
    in and it should be returned to the model instead of the result.

    The worker thread is abandoned rather than killed - Python cannot safely
    kill a thread, and for a read-only fetch letting it finish and discarding
    the answer costs nothing. Anything that changes state needs its own
    handling; see run_shell, which really does stop the process.
    """
    import concurrent.futures as futures

    pool = futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(work)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return None, future.result(timeout=0.15)
            except futures.TimeoutError:
                if pending():
                    held = check(tool_name)
                    if held is not None:
                        return held, None
                if time.monotonic() > deadline:
                    return f"{tool_name} timed out after {timeout:g}s.", None
    finally:
        pool.shutdown(wait=False)


def interruptible_sleep(seconds: float, tool_name: str) -> str | None:
    """Sleep, but react the moment the user barges in.

    Checking only between tool calls leaves a gap exactly where you most want
    to interrupt: a long deliberate pause. Sleeping in short steps and testing
    the flag closes it, so "wait thirty seconds" can be cut short after two.

    Returns a message to hand back to the model instead of finishing the
    sleep, or None if the wait completed (or you said "continue").
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        if pending():
            held = check(tool_name)
            if held is not None:
                return held  # cancelled, or a correction to apply
            # said "continue" - keep waiting out the remaining time
        time.sleep(min(0.15, remaining))


def check(tool_name: str) -> str | None:
    """Called before each tool runs. Returns a message to send instead, or None.

    Returning a string means the tool did NOT run, and the string goes back to
    the model as that tool's result.
    """
    if _cancelled.is_set():
        return (
            "The user cancelled this task. Do not run any more tools. "
            "Tell them briefly that you stopped, and what you had done so far."
        )

    if not _wake_heard.is_set():
        return None

    _wake_heard.clear()
    stop_watching()  # free the microphone for the question
    try:
        said = (_ask() or "").strip()
    except Exception:
        said = ""
    lowered = said.lower().strip(" .!?,")

    if not said:
        start_watching()
        return None  # heard nothing, carry on

    if any(word in lowered for word in CANCEL_WORDS):
        _cancelled.set()
        return (
            f"The user interrupted and cancelled: {said!r}. The {tool_name} call was "
            "NOT run. Stop here, run nothing further, and tell them you have stopped."
        )

    if any(lowered.startswith(word) or lowered == word for word in CONTINUE_WORDS):
        start_watching()
        return None  # resume, tool runs normally

    start_watching()
    return (
        f"The user interrupted before {tool_name} ran, and said: {said!r}. "
        "That call was NOT performed. Treat what they said as a correction: do it "
        "first, then carry on with the original task if it still makes sense."
    )
