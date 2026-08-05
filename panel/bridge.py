"""The link between the page and the agent.

Everything the page can ask for is a method on Api, reachable from JavaScript
as pywebview.api.<name>(). Everything the agent has to say goes the other way
through _push, which runs a line of JS in the page.

The agent runs on a worker thread. It has to: a command takes seconds to tens
of seconds, and the GUI thread is what keeps the window painting and the
typing responsive. That also means confirmations arrive on the worker thread
and block it while the page decides - exactly as the console asker blocks on
input() in text mode.
"""

import inspect
import json
import re
import threading
import time
from pathlib import Path

import main as text_mode  # the text-mode wiring: agent setup, /commands, speech
from iris import config, confirm, interrupt, make_agent, redact
from iris import platform as iris_platform

# What the mode selector offers. The names are the ones shown in the menu; the
# values are what iris/confirm.py already understands.
MODES = {
    "manual": ("Manual", "Ask before every action"),
    "guarded": ("Guarded", "Ask only before risky ones - commands, file edits"),
    "auto": ("Auto", "Never ask. Iris acts without checking"),
}
MODE_TO_CONFIRM = {"manual": "all", "guarded": "risk", "auto": "off"}
CONFIRM_TO_MODE = {value: key for key, value in MODE_TO_CONFIRM.items()}

# How many times a spoken question is asked before it is left to the screen.
# Three survives missing the moment once without becoming badgering.
_ASK_ATTEMPTS = 3

MODELS = [
    ("claude-opus-5", "Opus 5", "Best at choosing the right tool"),
    ("claude-sonnet-5", "Sonnet 5", "Faster and cheaper, still strong"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5", "Quickest, for simple errands"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Mentions typed into the message, expanded on the way to Iris. The tokens
# stay visible in the thread, so what you sent still reads back as what you
# wrote rather than as the wall of file content it turned into.
FILE_TOKEN = re.compile(r'@file:("[^"]+"|\S+)')
BROWSER_TOKEN = re.compile(r"@browser:\s*", re.I)

# Enough for a source file or a config; past this the point is to have Iris
# read it with the tool, which can seek about rather than swallowing it whole.
MAX_ATTACHED = 60_000

# A non-zero exit code is the one unambiguous failure signal a tool gives us.
# The rest is guesswork, so it stays narrow: matching the word "error" anywhere
# would flag a directory listing that happens to contain error.log.
_EXIT_CODE = re.compile(r"\[exit code (\d+)\]")
_TROUBLE = ("[stderr]", "Traceback (most recent call last)", "Exception calling")


def _failed(text: str) -> bool:
    exit_code = _EXIT_CODE.search(text)
    if exit_code:
        return exit_code.group(1) != "0"
    return any(marker in text for marker in _TROUBLE)


def _first_problem(text: str, limit: int = 160) -> str:
    """The one line worth showing out of a wall of stack trace.

    A COM error arrives as twenty lines of PowerShell scaffolding wrapped
    around one sentence that says what went wrong. That sentence is the only
    part anyone reads.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line in ("[stderr]",) or line.startswith(("+", "At line:", "    +")):
            continue
        if line.startswith("[exit code"):
            continue
        return line[:limit] + ("..." if len(line) > limit else "")
    return "failed"


BROWSER_NOTE = (
    "Use the browser for this - browser_open and the other browser tools - "
    "rather than answering from memory.\n\n"
)

# Openers that say nothing about what the conversation turned out to be about.
# One of these gets skipped and the next message names the conversation instead.
_PLEASANTRIES = {
    "hi", "hii", "hiya", "hello", "hey", "heya", "yo", "sup", "howdy",
    "test", "testing", "ping", "hi there", "hello there", "hey there",
    "good morning", "good afternoon", "good evening", "thanks", "thank you",
    "ok", "okay", "you there", "are you there", "wake up",
}



class _Either:
    """Reads as set when any of the events it wraps is.

    wake.wait_for_wake takes one stop event, and the wake loop has to answer to
    two things: being switched off, and being asked to give the microphone up
    for a moment so a spoken question can be heard.
    """

    def __init__(self, *events):
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class Api:
    def __init__(self, theme: dict, hotkey: str = ""):
        self._window = None
        self._agent = None
        self._busy = threading.Lock()
        self._pending: dict[int, list] = {}
        self._next_id = 0
        self._theme = theme
        self._hotkey = hotkey
        self._title = None
        self._naming = False
        self._streamed: list[str] = []
        self._last_flush = 0.0
        self._interruption = ""
        self._dictating = False
        self._stop_dictation = threading.Event()
        self._voice_on = False
        self._voice_stop = threading.Event()
        # Only one input stream can be open at a time, so the wake listener has
        # to let go before anything else can hear you.
        self._wake_pause = threading.Event()
        self._expecting_reply = False
        self.on_hide = lambda: None

    # --- plumbing ---------------------------------------------------------

    def attach(self, window) -> None:
        self._window = window

    def _push(self, **payload) -> None:
        """Hand one event to the page. Safe to call from any thread."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(f"window.panel.receive({json.dumps(payload)})")
        except Exception:
            pass  # the window went away mid-reply; nothing to do about it

    def _ensure_agent(self):
        # Built on first use rather than at startup, so the window appears
        # instantly and the SDK backend only spins up if you actually talk.
        if self._agent is None:
            self._push(type="note", text="Starting up...")
            self._agent = make_agent()
        return self._agent

    # --- asking the user something ---------------------------------------

    def _ask_blocking(self, kind: str, question: str, detail: str = "") -> str:
        """Put a question on screen and wait for the answer.

        With voice control on it is also asked out loud, and either channel can
        answer it - whichever comes first wins. The card stays on screen
        regardless, so a question that was misheard can still be clicked, and
        so you can read exactly what is about to run.
        """
        self._next_id += 1
        request = self._next_id
        answered = threading.Event()
        # The kind travels with it so that speech arriving later - by wake word,
        # after the spoken asking has given up - can be read the right way:
        # yes/no/always for a confirmation, free text for a question.
        self._pending[request] = [answered, "", kind]

        self._push(type=kind, id=request, question=question, detail=detail)

        if self._voice_on:
            threading.Thread(
                target=self._ask_aloud, args=(request, question, kind),
                name="panel-ask-aloud", daemon=True,
            ).start()

        answered.wait()
        return self._pending.pop(request)[1]

    def _ask_aloud(self, request: int, question: str, kind: str) -> None:
        """Speak a question and listen for the answer, asking again if none comes.

        Racing the on-screen card rather than replacing it: this thread only
        ever calls answer(), which is the same thing a click does, and the
        first to arrive is the one that counts.

        Silence used to end this quietly, which was the worst of both: the card
        stayed up, the microphone closed, and there was no way back in by voice
        - saying her name only queued a fresh command behind the turn that was
        still waiting for this very answer. So it says it did not hear anything
        and asks again.
        """
        try:
            from iris.voice import asking, cues, stt, tts

            # Take the microphone off the wake listener first, and give its
            # stream a moment to actually close.
            self._wake_pause.set()
            time.sleep(0.35)

            options = question if kind == "question" else "Yes, no, or always?"
            say = question

            for attempt in range(_ASK_ATTEMPTS):
                if request not in self._pending:
                    return  # answered on screen while we were talking

                # Silence the working cue: it would talk over the question, and
                # the microphone would hear it as an answer.
                with cues.quiet():
                    tts.speak(say)
                    self._push(type="dictating", stage="recording")
                    heard = stt.listen().strip()
                    self._push(type="dictating", stage="idle")

                if request not in self._pending:
                    return

                if kind == "question":
                    if heard:
                        self.answer(request, heard, spoken=True)
                        return
                else:
                    # None means it was speech, but not an answer to this -
                    # which is worth asking again about rather than taking as a
                    # refusal the way a no would be.
                    decided = asking.classify(heard)
                    if decided:
                        self.answer(request, decided, spoken=True)
                        return

                if attempt == _ASK_ATTEMPTS - 1:
                    break
                say = (
                    f"I did not catch that. {options}" if heard
                    else f"I did not get a response. {options}"
                )

            tts.speak(
                "I did not get an answer, so I have left it on screen. Say my "
                "name when you are ready and I will ask again."
            )
        except Exception as exc:
            self._push(type="error", text=f"Could not ask aloud: {exc}")
        finally:
            self._wake_pause.clear()

    def _confirm_asker(self, question: str, detail: str = "") -> str:
        answer = self._ask_blocking("confirm", question, detail)
        return answer if answer in ("yes", "no", "always") else "no"

    def _question_asker(self, question: str) -> str:
        return self._ask_blocking("question", question)

    def _answer_waiting(self, heard: str) -> bool:
        """Give what was just said to a question still waiting on an answer.

        Without this, calling her name while a card is up starts a new command,
        and that command blocks behind the turn the card is holding up - so
        nothing happens at all, which is exactly what it looks like from the
        outside. A question on screen is almost certainly what you are answering.
        """
        from iris.voice import asking

        waiting = sorted(self._pending)
        if not waiting:
            return False

        request = waiting[-1]  # the newest is the one still on screen
        pending = self._pending.get(request)
        kind = pending[2] if pending and len(pending) > 2 else "confirm"

        if kind == "question":
            self.answer(request, heard, spoken=True)
            return True

        decided = asking.classify(heard)
        if decided is None:
            return False  # not an answer; let it be treated as a new command
        self.answer(request, decided, spoken=True)
        return True

    def answer(self, request: int, value: str, spoken: bool = False) -> None:
        """Record an answer, from a click or from what was said out loud."""
        pending = self._pending.get(request)
        if not pending:
            return
        pending[1] = value
        pending[0].set()
        if spoken:
            # A click updates the card itself; an answer that arrived by voice
            # has to say so, or the buttons sit there as though nothing was
            # heard while the action goes ahead behind them.
            self._push(type="answered", id=request, value=value)

    # --- what the page calls ---------------------------------------------

    def ready(self) -> dict:
        """First call from the page: header text and the system colours.

        The theme is handed over here rather than pushed at startup because
        the page asks for it once it has loaded, which sidesteps the race of
        pushing JS at a document that may not have parsed app.js yet.
        """
        return {
            "name": config.ASSISTANT_NAME,
            "model": config.MODEL,
            "backend": config.BACKEND,
            "confirm": config.CONFIRM_MODE,
            "speaking": config.SPEAK_REPLIES,
            "hotkey": self._hotkey,
            "theme": self._theme,
            # The page styles itself to match the operating system. Sent once
            # here rather than sniffed from the user agent, which reports the
            # web view's platform and would be right by accident at best.
            "platform": iris_platform.name(),
        }

    def hide_panel(self) -> None:
        self.on_hide()

    def reset_conversation(self) -> None:
        """Forget this conversation. Saved memories are untouched.

        Shared by /reset and by the clear_conversation tool, so asking her to
        clear the chat and typing the command do exactly the same thing.
        """
        if self._agent is not None:
            self._agent.reset()
        confirm.reset_session_approvals()
        self._title = None  # a new conversation gets to be named afresh
        self._naming = False
        self._push(type="wiped")

    def refresh_speech(self) -> None:
        """Re-read the mute state into the header icon.

        Called by /mute and by the speech_settings tool alike, so asking her to
        be quiet crosses the speaker icon out just as pressing the button does.
        """
        self._push(type="speaking", value=config.SPEAK_REPLIES)

    # --- the controls under the input -------------------------------------

    def settings(self) -> dict:
        """Everything the three menus need to draw themselves."""
        return {
            "mode": CONFIRM_TO_MODE.get(config.CONFIRM_MODE, "guarded"),
            "modes": [
                {"id": key, "name": name, "hint": hint}
                for key, (name, hint) in MODES.items()
            ],
            "model": config.MODEL,
            "models": [
                {"id": mid, "name": name, "hint": hint} for mid, name, hint in MODELS
            ],
            "effort": config.EFFORT,
            "efforts": EFFORTS,
            "thinking": config.THINKING,
            "voice_control": self._voice_on,
            "wake_phrase": config.WAKE_PHRASE,
        }

    def set_mode(self, mode: str) -> dict:
        """Confirmation mode, live.

        iris/confirm.py reads config.CONFIRM_MODE at the moment each tool
        runs, so this takes effect on the very next tool call - no restart and
        no new conversation.
        """
        if mode in MODE_TO_CONFIRM:
            config.CONFIRM_MODE = MODE_TO_CONFIRM[mode]
            confirm.reset_session_approvals()  # "always" answers were given under the old mode
        return self.settings()

    def set_model(self, model: str) -> dict:
        if any(model == mid for mid, _, _ in MODELS):
            if self._agent is not None and hasattr(self._agent, "set_model"):
                self._agent.set_model(model)  # keeps the conversation
            else:
                config.MODEL = model
        return self.settings()

    def set_effort(self, effort: str) -> dict:
        if effort in EFFORTS and effort != config.EFFORT:
            config.EFFORT = effort
            self._restart_session("Effort")
        return self.settings()

    def set_thinking(self, on: bool) -> dict:
        if bool(on) != config.THINKING:
            config.THINKING = bool(on)
            self._restart_session("Thinking")
        return self.settings()

    def _restart_session(self, what: str) -> None:
        """Effort and thinking are fixed when the session is built.

        Unlike the model there is no way to change them in place, so the only
        honest options are to start a new conversation or to pretend the
        setting did not apply. This does the former and says so.
        """
        if self._agent is not None:
            self._agent.reset()
            self._push(type="note", text=f"{what} changed - starting a fresh conversation.")
            self._title = None
            self._naming = False
            self._push(type="retitle")

    # --- voice control ----------------------------------------------------

    def set_voice_control(self, on: bool) -> dict:
        """Listen for the wake phrase and act on whatever is said next.

        The same two steps voice mode uses: waiting for the phrase costs almost
        nothing while the room is quiet, and the recorder only opens once it has
        been heard.
        """
        on = bool(on)
        if on == self._voice_on:
            return self.settings()

        self._voice_on = on
        if on:
            self._voice_stop = threading.Event()
            threading.Thread(
                target=self._wake_loop, name="panel-wake", daemon=True
            ).start()
            self._push(type="note", text=f'Listening for "{config.WAKE_PHRASE}"')
        else:
            self._voice_stop.set()
            self._push(type="note", text="Voice control off")

        self._push(type="voice_control", value=on)
        return self.settings()

    def _warm_up(self) -> None:
        """Load what the first wake word would otherwise wait for."""
        try:
            from iris.voice import stt, wake

            wake._get_wake_stt()
            stt.warm_up()
        except Exception:
            pass  # it will just be slow the first time instead

    def _wake_loop(self) -> None:
        try:
            from iris.voice import cues, stt, tts, wake
        except Exception as exc:
            self._push(type="error", text=f"Voice control needs the audio packages: {exc}")
            self._voice_on = False
            self._push(type="voice_control", value=False)
            return

        # Load the speech models now, not on the first wake word. Cold they
        # cost about two and a half seconds, and that lands entirely on the
        # first thing you say - which is the moment it most looks broken.
        threading.Thread(target=self._warm_up, name="panel-warm", daemon=True).start()

        while not self._voice_stop.is_set():
            try:
                if self._expecting_reply:
                    # She just asked you something, so listen straight away
                    # rather than making you say her name to answer a question
                    # she only just put to you.
                    #
                    # Said plainly in the thread as well as lighting the mic:
                    # an open microphone you did not ask for is the one thing
                    # here that should never be a surprise.
                    self._expecting_reply = False
                    self._push(type="note", text="Listening for your answer")
                    self._push(type="dictating", stage="recording")
                    heard = stt.listen().strip()
                    self._push(type="dictating", stage="idle")
                    if heard:
                        self._push(type="said", text=heard)
                        self.send(heard)
                        while self._busy.locked() and not self._voice_stop.is_set():
                            time.sleep(0.2)
                        text_mode._wait_for_speech()
                    continue

                if not wake.wait_for_wake(
                    stop_event=_Either(self._voice_stop, self._wake_pause)
                ):
                    # Either switched off, or the microphone was wanted for a
                    # spoken question. Wait that out rather than fighting for it.
                    while self._wake_pause.is_set() and not self._voice_stop.is_set():
                        time.sleep(0.1)
                    continue
                if self._voice_stop.is_set():
                    break

                # A note rather than a spoken "Yes?": speaking it blocks for a
                # second before the microphone opens, which is most of the wait
                # between calling her and being able to say anything.
                cues.acknowledge()
                self._push(type="dictating", stage="recording")
                heard = stt.listen().strip()
                self._push(type="dictating", stage="idle")
                if not heard:
                    continue

                # A card still waiting for an answer takes precedence: what you
                # just said is that answer, not a new errand. send() would only
                # queue behind the turn the card is blocking.
                if self._answer_waiting(heard):
                    self._push(type="said", text=heard)
                    continue

                # Shown as though it had been typed, because as far as the
                # conversation is concerned it was.
                self._push(type="said", text=heard)
                self.send(heard)

                # One at a time. Listening again while she is still answering
                # would have her hear her own reply and treat it as a command.
                while self._busy.locked() and not self._voice_stop.is_set():
                    time.sleep(0.2)
                text_mode._wait_for_speech()
            except Exception as exc:
                self._push(type="error", text=f"Voice control: {type(exc).__name__}: {exc}")
                break

        self._voice_on = False
        self._push(type="voice_control", value=False)

    # --- dictation --------------------------------------------------------

    def dictate(self) -> None:
        """Start recording, or stop a recording already running.

        The same button does both, so the second press is what ends it - you
        decide when you have finished rather than a silence timer deciding for
        you. Whatever was captured up to that press is what gets transcribed.

        Speech goes into the box rather than straight to Iris, so a misheard
        word can be fixed before it is acted on - the whole point of having
        typing available at all.
        """
        if self._dictating:
            self._stop_dictation.set()
            return
        self._dictating = True
        self._stop_dictation = threading.Event()
        threading.Thread(target=self._listen, name="panel-dictate", daemon=True).start()

    def _listen(self) -> None:
        try:
            from iris.voice import cues, stt

            # Dictating during a turn is how you barge in, so the cue may well
            # be running - and the microphone would record it along with you.
            with cues.quiet():
                self._push(type="dictating", stage="recording")
                audio = stt.record_until(self._stop_dictation)

            self._push(type="dictating", stage="transcribing")
            heard = stt.transcribe(audio).strip() if audio is not None else ""
            # Nothing heard is not worth a message. It is obvious from the
            # empty box, and a line of microphone levels in the conversation
            # is noise in the literal and the ordinary sense.
            self._push(type="dictated", text=heard)
        except Exception as exc:
            self._push(type="error", text=f"Microphone: {type(exc).__name__}: {exc}")
        finally:
            self._dictating = False
            self._push(type="dictating", stage="idle")

    # --- the + menu -------------------------------------------------------

    def pick_file(self) -> str:
        """Native file picker. Returns the chosen path, or "" if cancelled.

        The path is put in the input rather than the file being read here:
        Iris already has read_file and can decide how to handle it, and a
        path in the box is something you can still edit or explain around.
        """
        try:
            import win32con
            import win32gui

            path, _, _ = win32gui.GetOpenFileNameW(
                InitialDir=str(Path.home()),
                Title="Add a file for Iris",
                Flags=win32con.OFN_EXPLORER | win32con.OFN_FILEMUSTEXIST,
            )
            return path or ""
        except Exception:
            return ""  # cancelled, or no picker available

    def send(self, text: str) -> None:
        """Take a command. Returns at once; the reply arrives via _push."""
        text = (text or "").strip()
        if not text:
            return

        if text.startswith("/"):
            self._command(text)
            return

        if self._busy.locked():
            # A confirmation waiting on screen is ANSWERED by what you say next,
            # not cancelled by it. Saying "yes" or re-asking for the thing
            # answers the card; a real "no" cancels the action. Anything that is
            # not an answer leaves the card up with a nudge, rather than letting
            # it be silently rejected - which is what turned "shut down" followed
            # by "shut yourself down please" into a self-cancelling loop: the
            # second message barged in and declined the first one's confirmation.
            if self._pending:
                if self._answer_waiting(text):
                    self._push(type="said", text=text)
                    return
                self._push(
                    type="note",
                    text="There's a confirmation waiting - answer yes, no, or always.",
                )
                self._push(type="said", text=text)
                return
            self._barge_in(text)
            return

        # Named from what you wrote, run from what it expands to - a title
        # taken from 2,000 lines of attached file would be useless.
        self._name_conversation(self._readable(text))

        threading.Thread(
            target=self._run, args=(self._expand(text),), name="panel-agent", daemon=True
        ).start()

    # --- @mentions --------------------------------------------------------

    def _read_attachment(self, path: Path) -> str:
        """One file, ready to sit in the prompt.

        A credentials file is named, never inlined. The scrubber works on
        `KEY=value` lines, so a bare secret sitting on a line of its own in a
        .env survives it untouched - and unlike tool output, text attached
        here reaches the model without passing the confirmation gate on the
        way. Iris can still be asked to read one; that route is gated and
        redacted, and going through it is a decision rather than a slip.
        """
        if redact.is_sensitive_file(path):
            return (
                f"[{path} looks like a credentials file, so it was not attached. "
                "Ask me to read it if you actually need what is in it.]"
            )

        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[could not read {path.name}: {exc}]"

        clipped = ""
        if len(raw) > MAX_ATTACHED:
            raw = raw[:MAX_ATTACHED]
            clipped = f"\n[... truncated at {MAX_ATTACHED:,} characters]"

        # Same treatment tool output gets: credentials always, personal
        # details when IRIS_REDACT_PII is on.
        raw = redact.scrub(raw) if config.REDACT_PII else redact.scrub_sensitive_file(raw)

        return f"--- {path} ---\n{raw}{clipped}\n--- end of {path.name} ---"

    def _expand(self, text: str) -> str:
        """Turn the mentions into something Iris can actually act on."""
        attachments: list[str] = []

        def swap(match: re.Match) -> str:
            path = Path(match.group(1).strip('"'))
            attachments.append(self._read_attachment(path))
            return path.name  # the sentence keeps reading naturally

        body = FILE_TOKEN.sub(swap, text)

        browsing = bool(BROWSER_TOKEN.search(body))
        body = BROWSER_TOKEN.sub("", body).strip()

        parts = []
        if browsing:
            parts.append(BROWSER_NOTE)
        parts.append(body)
        if attachments:
            parts.append(
                "\n\nThe user attached "
                + ("this file" if len(attachments) == 1 else "these files")
                + ":\n\n"
                + "\n\n".join(attachments)
            )
        return "".join(parts)

    @staticmethod
    def _readable(text: str) -> str:
        """The message without its mentions, for naming the conversation."""
        return BROWSER_TOKEN.sub(
            "", FILE_TOKEN.sub(lambda m: Path(m.group(1).strip('"')).name, text)
        ).strip()

    # --- interrupting -----------------------------------------------------

    def _barge_in(self, text: str) -> None:
        """A message sent while Iris is still working.

        Two things have to stop, and they stop differently. The reply being
        written is cut off through the SDK; the *task* is stopped between tool
        calls by the same gate voice mode uses, because halting halfway
        through a click or a file write is not something that can be undone
        cleanly. What you said then reaches Iris as a correction to apply.
        """
        self._interruption = text
        interrupt.barge_in()

        agent = self._agent
        if agent is not None and hasattr(agent, "interrupt"):
            # Stops mid-sentence if she is talking. If she is inside a tool
            # this does nothing and the gate below catches her instead.
            agent.interrupt()

        self._push(type="interrupted", text=text)

    def _typed_interruption(self) -> str:
        """What iris/interrupt.py asks for when the gate trips."""
        said, self._interruption = self._interruption, ""
        return said

    def _name_conversation(self, text: str) -> None:
        """Have the conversation named from its first real instruction, once.

        Settled early and then left alone, the way a history entry is: a title
        that kept rewriting itself as the conversation went on would be worse
        than no title, because you would never learn to recognise it.

        The naming itself is a model call and takes a few seconds, so it goes
        on its own thread - the reply is already on its way and must not wait
        behind a cosmetic label.
        """
        if self._title is not None or self._naming:
            return
        # Drop her name before judging it, so "hey Iris" is recognised as the
        # greeting it is rather than as a two-word instruction.
        opener = re.sub(
            rf"\b{re.escape(config.ASSISTANT_NAME)}\b", "", text.strip(), flags=re.I
        )
        # Both ends: removing the name from "Iris, hi" leaves the comma behind.
        opener = " ".join(opener.split()).strip(" ,.!?:;-").lower()
        if opener in _PLEASANTRIES or len(opener) < 4:
            return  # says nothing yet - let the next message name it

        self._naming = True
        threading.Thread(
            target=self._fetch_title, args=(text,), name="panel-title", daemon=True
        ).start()

    def _fetch_title(self, text: str) -> None:
        import titler

        name = titler.title_for(text)
        if name:
            self._title = name
            self._push(type="title", text=name)
        else:
            # Nothing usable came back. Leave the header blank and let the next
            # message try again rather than showing the raw instruction.
            self._naming = False

    def _command(self, text: str) -> None:
        """The same slash commands text mode has, so the two behave alike."""
        if text in ("/reset",):
            self.reset_conversation()
            self._push(type="cleared", text="History cleared.")
        elif text in ("/cost",):
            usage = self._agent.session_usage.summary() if self._agent else "nothing yet"
            self._push(type="note", text=f"Session: {usage}")
        elif text in ("/mute", "/speak"):
            config.SPEAK_REPLIES = text == "/speak"
            self._push(type="note", text=f"Voice {'on' if config.SPEAK_REPLIES else 'off'}")
            self.refresh_speech()
        elif text in ("/quit", "/exit"):
            self._push(type="note", text="Shutting down.")
            self.on_quit()
        else:
            self._push(
                type="note",
                text=f"Unknown command {text}. Try /reset, /cost, /mute, /speak, /quit.",
            )

    def on_quit(self) -> None:
        pass  # replaced by app.py

    # --- the agent turn ---------------------------------------------------

    def _run(self, command: str) -> None:
        with self._busy:
            self._push(type="thinking", value=True)
            self._streamed = []
            self._last_flush = 0.0
            try:
                from iris.voice import cues

                agent = self._ensure_agent()
                # Only the SDK backend can stream. Ask for it where it exists
                # rather than pretending both backends behave alike.
                extra = {}
                accepts = inspect.signature(agent.send).parameters
                if "on_text" in accepts:
                    extra["on_text"] = self._on_text
                if "on_result" in accepts:
                    extra["on_result"] = self._on_result
                # The working ding-ding, for as long as the turn runs. It stops
                # before the reply is spoken, so the two never share the output
                # device - sounddevice plays one thing at a time, and whichever
                # started last cuts off the other.
                with cues.thinking():
                    reply = agent.send(command, on_tool=self._on_tool, **extra)
            except Exception as exc:
                self._push(type="error", text=f"{type(exc).__name__}: {exc}")
                return
            finally:
                self._push(type="thinking", value=False)

            # Replaces whatever streamed in, so the settled bubble is the real
            # reply with its markdown rendered - and is right even when nothing
            # streamed at all. Per-turn usage is gone; /cost still has it.
            # A reply ending in a question is waiting for an answer, so voice
            # control listens again rather than making you say her name to
            # reply to something she just asked you.
            self._expecting_reply = reply.rstrip().endswith("?")
            self._push(type="reply", text=reply)
            # Speech runs on its own thread and outlives this turn, so waiting
            # for it here would hold the busy lock and make barging in during
            # a long spoken reply impossible - the one moment you most want to.
            threading.Thread(
                target=self._speak, args=(reply,), name="panel-speak", daemon=True
            ).start()

    def _speak(self, reply: str) -> None:
        """Say it, and flag when she starts and stops so the mascot can react."""
        if not config.SPEAK_REPLIES:
            return
        text_mode._say(reply)  # same voice, same setting as text mode
        self._push(type="talking", value=True)
        text_mode._wait_for_speech()
        self._push(type="talking", value=False)

    def _on_text(self, fragment: str) -> None:
        """The reply arriving a fragment at a time.

        Batched rather than passed straight through: fragments land dozens of
        times a second, and each one crossing into the page separately would
        cost far more than it shows. A 60ms cadence still reads as typing.
        """
        self._streamed.append(fragment)
        now = time.monotonic()
        if now - self._last_flush >= 0.06:
            self._last_flush = now
            self._push(type="delta", text="".join(self._streamed))
            self._streamed.clear()

    def _on_tool(self, name: str, args: dict) -> None:
        preview = ", ".join(f"{key}={str(value)[:40]}" for key, value in list(args.items())[:3])
        self._push(type="tool", name=name, args=preview)

    def _on_result(self, name: str, text: str, is_error: bool) -> None:
        """Show a tool that failed. Successes stay quiet.

        Claude reads every result and adapts, which is how a failed approach
        becomes a working one - but the person who approved the action saw
        none of that. A command that errored twice before working looked
        exactly like one that sat there doing nothing, which is the opposite
        of what a confirmation prompt is for.

        Only failures are surfaced. Showing every result would bury the
        conversation in output nobody asked to read.
        """
        if not (is_error or _failed(text)):
            return
        self._push(type="tool-failed", name=name, text=_first_problem(text))


def wire(api: Api) -> None:
    """Route the agent's questions - and interruptions - into the panel."""
    confirm.set_asker(api._confirm_asker)
    confirm.set_question_asker(api._question_asker)
    # watch=False: the panel is interrupted by a typed message, so there is no
    # wake-word watcher to run and no reason to hold the microphone open.
    interrupt.set_asker(api._typed_interruption, watch=False)
