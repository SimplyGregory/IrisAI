"""Iris as a background app.

Launched with pythonw (via Start Iris.vbs, or the .pyw association) there is no
console at all - just a tray icon. The voice loop runs on a worker thread
because pystray must own the main thread on Windows.

    Start Iris.vbs        start it silently
    tray right-click      pause, open the transcript or memories, quit

This entry point is deliberately silent. To watch what it is doing, run the
panel instead, which shows the same tool calls on screen.
"""

import sys
import threading

from iris import config, spawn, tray

# No console at all under pythonw, so helpers would each open one.
spawn.hide_console_children()

_stopping = threading.Event()


def _voice_loop() -> None:
    """The voice loop, with tray state instead of printing."""
    from iris import confirm, interrupt, make_agent
    from iris.tools import browser
    from iris.voice import asking, cues, stt, tts, wake

    # Launched with pythonw there is no console, so the askers are told to
    # print nowhere rather than left to write to a stdout that does not exist.
    interrupt.set_asker(lambda: asking.bargein_asker(asking.silent))
    confirm.set_asker(
        lambda question, detail="": asking.confirm_asker(question, detail, asking.silent)
    )
    confirm.set_question_asker(lambda question: asking.question_asker(question, asking.silent))

    iris = make_agent()
    stt.warm_up()
    tray.set_state("idle")
    tts.speak(f"{config.ASSISTANT_NAME} online.")

    expecting_reply = False
    try:
        while not _stopping.is_set():
            if tray.is_paused():
                _stopping.wait(0.5)
                continue

            if not expecting_reply:
                tray.set_state("idle")
                if not wake.wait_for_wake(stop_event=_stopping):
                    continue
                if _stopping.is_set():
                    break
                tray.set_state("speaking")
                tts.speak("Yes?")

            tray.set_state("listening")
            command = stt.listen()
            if not command:
                expecting_reply = False
                continue

            if any(w in command.lower() for w in ("goodbye iris", "shut down")):
                tray.set_state("speaking")
                tts.speak("Goodbye.")
                break

            try:
                tray.set_state("thinking")
                interrupt.start_watching()
                with cues.thinking():
                    reply = iris.send(command)
            except Exception:
                tray.set_state("speaking")
                tts.speak("Something went wrong with that one.")
                expecting_reply = False
                continue
            finally:
                interrupt.stop_watching()

            tray.set_state("speaking")
            tts.speak(reply)
            expecting_reply = reply.rstrip().endswith("?")
    finally:
        browser.shutdown()
        tray.stop()


def main() -> int:
    worker = threading.Thread(target=_voice_loop, name="iris-voice", daemon=True)
    worker.start()
    # pystray owns the main thread on Windows, so the tray runs here and the
    # assistant runs behind it.
    tray.run(on_quit=_stopping.set)
    _stopping.set()
    worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
