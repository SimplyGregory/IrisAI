"""Text mode. Type commands instead of speaking them.

Same agent, same tools, same behaviour as voice mode - just without the
microphone in the way. Get this working first.

Replies are printed and also spoken aloud, using the same voice as voice mode.
Set IRIS_SPEAK=0 in .env, or type /mute, for text only.

    python main.py
"""

import os
import sys
import threading

from iris import config, confirm, make_agent, selfcontrol
from iris.tools import browser

BANNER = f"""
  {config.ASSISTANT_NAME.upper()} - text mode
  backend {config.BACKEND} | model {config.MODEL} | effort {config.EFFORT} | confirm {config.CONFIRM_MODE}
  voice {'on' if config.SPEAK_REPLIES else 'off'}   ask her to be quiet, or use /mute

  Type a command in plain English. Try:
    what are the 5 most recently edited files in my downloads
    open chrome, go to youtube.com and click the first video
    minimize chrome then open notepad

  /reset  forget conversation history      /cost   session spend
  /mute   print replies without speaking   /speak  speak them again
  /quit   exit
"""


# --- speaking -------------------------------------------------------------
#
# Speaking runs on a background thread, so the "you >" prompt comes straight
# back and you can type the next command over the top of a long reply. The
# text is on screen either way, so there is nothing to miss by doing that.

_speech: threading.Thread | None = None
_tts = None


def _engine():
    """Load the speech module on first use, or None if speech is off.

    Imported here rather than at the top of the file because text mode is
    meant to be the thing that always works: a machine with none of the voice
    packages installed should still get a working prompt, just a silent one.

    Whether to speak is read from config every time, not cached: /mute, the
    panel's mute button and asking her to be quiet all set the same flag, and
    a copy kept here would go stale the moment one of them was used.
    """
    global _tts

    if not config.SPEAK_REPLIES:
        return None
    if _tts is None:
        try:
            from iris.voice import tts
        except Exception as exc:
            print(f"  (speech unavailable: {exc} - printing only)\n")
            config.SPEAK_REPLIES = False
            return None
        _tts = tts
    return _tts


def _wait_for_speech() -> None:
    """Let the current line finish, rather than cutting Iris off mid-sentence."""
    if _speech is not None and _speech.is_alive():
        try:
            _speech.join()
        except KeyboardInterrupt:
            pass


def _say(text: str) -> None:
    global _speech

    engine = _engine()
    if engine is None:
        return
    # Two replies can only overlap if you typed again while she was still
    # talking; queue rather than talk over herself.
    _wait_for_speech()
    _speech = threading.Thread(
        target=engine.speak, args=(text,), name="iris-speak", daemon=True
    )
    _speech.start()


def _spoken_question(question: str) -> str:
    """Iris asking something mid-task: say it aloud, then read the typed answer."""
    print(f"\n  [?] {question}")
    _say(question)
    return input("      > ").strip()


def _show_tool(name: str, args: dict) -> None:
    preview = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
    print(f"    -> {name}({preview})")


def _request_quit() -> None:
    """Close text mode from inside a tool call.

    The tool runs on a worker thread, and returning from it only ends that one
    turn - so the interrupt is aimed at the main thread, where it lands as the
    KeyboardInterrupt the loop already knows how to shut down cleanly on. The
    delay lets the reply reach the screen first.
    """
    import _thread

    threading.Timer(1.0, _thread.interrupt_main).start()


def main() -> int:
    # Page titles and filenames routinely contain emoji and non-Latin text. The
    # default Windows console codepage cannot encode those and would raise
    # UnicodeEncodeError mid-print, so force UTF-8 with replacement.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if config.BACKEND == "sdk":
        from iris.agent_sdk import find_cli

        if find_cli() is None and not config.CLI_PATH:
            print("Backend is sdk but the Claude Code CLI was not found.")
            print("Install Claude Code, or set IRIS_CLI_PATH in .env.")
            return 1
    elif not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key,")
        print("or set IRIS_BACKEND=sdk to run on your Claude subscription instead.")
        return 1

    # Questions Iris asks mid-task are her talking to you, so they get spoken
    # too. The y/n confirmation gate stays silent: with IRIS_CONFIRM=all it
    # fires on every single tool call, and you are already reading it.
    confirm.set_question_asker(_spoken_question)

    # "Close yourself" should work here as well as in the panel. Raising
    # KeyboardInterrupt in the main thread unwinds through the same path
    # Ctrl+C does, so the browser is shut down and totals still print.
    selfcontrol.provide("quit", _request_quit)

    iris = make_agent()

    def _clear() -> None:
        iris.reset()
        confirm.reset_session_approvals()
        print("\n  (conversation cleared)\n")

    selfcontrol.provide("clear_conversation", _clear)
    print(BANNER)

    try:
        while True:
            try:
                command = input("you > ").strip()
            except EOFError:
                break

            if not command:
                continue
            if command in ("/quit", "/exit"):
                break
            if command == "/reset":
                iris.reset()
                print("  (history cleared)\n")
                continue
            if command == "/cost":
                print(f"  session: {iris.session_usage.summary()}\n")
                continue
            if command in ("/mute", "/speak"):
                # Takes effect from the next reply; a line already being
                # spoken finishes rather than being cut off mid-word.
                config.SPEAK_REPLIES = command == "/speak"
                print(f"  (voice {'on' if config.SPEAK_REPLIES else 'off'})\n")
                continue

            try:
                reply = iris.send(command, on_tool=_show_tool)
            except KeyboardInterrupt:
                print("\n  (interrupted)\n")
                continue
            except Exception as exc:
                print(f"  ! {type(exc).__name__}: {exc}\n")
                continue

            print(f"\niris > {reply}")
            print(f"         [{iris.last_usage.summary()}]\n")
            _say(reply)
    except KeyboardInterrupt:
        pass
    finally:
        _wait_for_speech()
        browser.shutdown()

    print(f"\nSession total: {iris.session_usage.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
