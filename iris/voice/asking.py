"""Asking the user something when the only channel is the microphone.

The harness needs three answers from time to time: may I run this, what did
you mean, and you interrupted me so what now. Text mode reads them from the
keyboard and the panel puts them on screen; here they are spoken and listened
for.

These live in the voice package rather than in an entry point because both the
terminal and the tray app need them, and an entry point is the one place a
second entry point cannot import from without dragging in its whole main().
"""

import re

from iris.voice import cues, stt, tts

YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "affirmative"}
YES_PHRASES = ("go ahead", "do it", "that is fine", "sounds good")
NO_WORDS = {"no", "nope", "nah", "cancel", "negative"}
NO_PHRASES = ("don't", "do not", "no thanks", "never mind")
ALWAYS_PHRASES = ("always", "don't ask again", "stop asking")

# The old names, for anything still importing them.
YES = tuple(sorted(YES_WORDS)) + YES_PHRASES
ALWAYS = ALWAYS_PHRASES


def classify(reply: str) -> str | None:
    """"yes", "no" or "always" - or None when that was not an answer at all.

    Separate from confirm_asker so a caller can tell "they said something I did
    not understand" from "they said no", and ask again instead of taking a
    misheard sentence as a refusal.

    Whole words rather than substrings for the short ones: "I know" contains
    "no" and "nothing" starts with it, and neither of those is a refusal.
    """
    said = (reply or "").lower().strip(" .!?,")
    if not said:
        return None
    words = set(re.findall(r"[a-z']+", said))

    # Always first: "stop asking" would otherwise be read as a plain no.
    if any(phrase in said for phrase in ALWAYS_PHRASES):
        return "always"
    if words & YES_WORDS or any(phrase in said for phrase in YES_PHRASES):
        return "yes"
    if words & NO_WORDS or any(phrase in said for phrase in NO_PHRASES):
        return "no"
    return None


def confirm_asker(question: str, detail: str = "", on_print=print) -> str:
    """Ask for permission out loud and listen for the answer."""
    with cues.quiet():
        tts.speak(question)
        on_print(f"  [?] {question}")
        if detail:
            for line in detail.splitlines():
                on_print(f"      {line}")
        reply = stt.listen().lower().strip(" .!?")
    on_print(f"      heard: {reply!r}")
    # Anything unrecognised is a no here: with no screen to fall back on,
    # refusing is the safe reading of "I could not tell what you said".
    return classify(reply) or "no"


def question_asker(question: str, on_print=print) -> str:
    """Ask something open-ended out loud and transcribe the reply."""
    with cues.quiet():
        tts.speak(question)
        on_print(f"  [?] {question}")
        reply = stt.listen().strip()
    on_print(f"      heard: {reply!r}")
    return reply


def bargein_asker(on_print=print) -> str:
    """She was interrupted mid-task: ask what to do about it."""
    with cues.quiet():
        tts.speak("Yes?")
        on_print("  [interrupted - cancel, continue, or tell me what to change]")
        said = stt.listen().strip()
    on_print(f"      heard: {said!r}")
    return said


def silent(*_args, **_kwargs) -> None:
    """A drop-in for on_print when there is no console to print to."""
