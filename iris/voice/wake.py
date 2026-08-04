"""Wake word detection.

Whisper listens for whatever phrase you choose. There is no model to train, no
account to open, and no list of four names to pick from - if you want to be
answered by "iris", you are.

Transcribing continuously would cost about 16% of a core, but it does not have
to: measuring loudness is nearly free, so Whisper only runs on the second or
two after someone actually speaks. In a quiet room that is close to zero.

This replaced two other engines. openWakeWord was free but only recognised the
four phrases it ships with - hey_jarvis, alexa, hey_mycroft, hey_rhasspy - so
"iris" was never among them, and Porcupine wanted a Picovoice account and a
generated model file for the privilege. Neither earned its complexity next to
matching a phrase against text that is already being transcribed.
"""

import difflib
import re

import numpy as np

from iris import config

_wake_stt = None
_MAX_PHRASE = 2.0   # seconds of speech to transcribe
_FLOOR_MULTIPLE = 3.0
_TAIL = 0.25        # quiet needed before the phrase counts as finished

# Confidence gates on the transcription itself. Whisper will happily produce
# confident-looking words from a cough or a closing door, and a wake word only
# has to be wrong once to be annoying.
NOT_SPEECH = 0.6    # above this it probably was not speech at all
TOO_UNSURE = -1.0   # average log probability; below this it is guessing


def _get_wake_stt():
    """A second, smaller Whisper than the one used for commands.

    Only ever asked to recognise one short phrase, so tiny.en is plenty and
    keeps the always-listening path cheap.
    """
    global _wake_stt
    if _wake_stt is None:
        from faster_whisper import WhisperModel

        _wake_stt = WhisperModel(config.WAKE_STT_MODEL, device="cpu", compute_type="int8")
    return _wake_stt


def phrases() -> list[str]:
    """Every phrase she answers to, longest first.

    Comma-separated, like the hotkey. Worth having more than one: "hey iris"
    is much easier for Whisper to get right - more audio, more context, and a
    carrier word to absorb the clipping when speech detection starts a moment
    late - while plain "iris" still works when it does come through cleanly.
    Longest first so the stricter phrase is tried before the looser one.
    """
    found = [p.strip().lower() for p in config.WAKE_PHRASE.split(",") if p.strip()]
    return sorted(found, key=len, reverse=True) or ["iris"]


def heard_phrase(text: str) -> bool:
    """True if what was heard *opens* with one of the wake phrases.

    Position matters as much as the words. A wake phrase is the first thing
    you say - "iris, open chrome" - so anywhere else in the sentence is
    someone talking *about* her, not to her. Scanning the whole utterance is
    what lets "what about iris" and "the iris of your eye" set her off.
    """
    return any(_matches(text, want) for want in phrases())


def _matches(text: str, want: str) -> bool:
    """Fuzzy match, because "Iris" is regularly transcribed as "Irish"."""
    said = " ".join(re.sub(r"[^a-z ]", " ", text.lower()).split())
    want = " ".join(want.split())
    # startswith, not "in": a substring test anywhere in the sentence is
    # exactly the thing the position rule below exists to prevent, and it
    # would short-circuit past it.
    if said.startswith(want):
        return True

    words = said.split()
    target = want.split()

    # A short name has far less to match on, so an identical ratio is a much
    # harder test for "iris" than for "hey computer": one wrong letter in four
    # is 0.75 before anything else goes wrong. Give short phrases more room.
    threshold = config.WAKE_FUZZ
    if len(want) <= 5:
        threshold = min(threshold, 0.70)

    # A chunk much shorter than the phrase cannot be it, however well it
    # scores. "is" against "iris" rates 0.67 on shared letters alone, which at
    # a threshold this forgiving would wake her on "is this thing on".
    shortest = max(3, len(want) - 1)

    # Only the opening: the phrase's own length, and one word more because it
    # is often transcribed glued to what follows ("irisopen chrome").
    for span in (len(target), len(target) + 1):
        chunk = " ".join(words[:span])
        if len(chunk) < shortest:
            continue
        if difflib.SequenceMatcher(None, chunk, want).ratio() >= threshold:
            return True
        # ...and against the start of a longer word.
        if len(chunk) > len(want) and chunk.startswith(want[:3]):
            if difflib.SequenceMatcher(None, chunk[: len(want)], want).ratio() >= threshold:
                return True
    return False


def describe() -> str:
    """One line for a startup banner or a diagnostic."""
    listed = " or ".join(repr(p) for p in phrases())
    return f"listening for {listed} ({config.WAKE_STT_MODEL})"


def wait_for_wake(stop_event=None) -> bool:
    """Block until the wake phrase is heard. Returns True if it was.

    Pass stop_event to make this abandonable - the barge-in watcher has to be
    able to release the microphone promptly, because only one input stream can
    be open at a time and the reply needs it next.
    """
    import sounddevice as sd

    from iris.voice import stt as _stt

    model = _get_wake_stt()
    block = 1600  # 100 ms
    recent: list[float] = []

    with sd.InputStream(
        samplerate=config.SAMPLE_RATE, channels=1, dtype="float32", blocksize=block
    ) as stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                return False

            frame, _ = stream.read(block)
            level = float(np.sqrt(np.mean(frame**2)))

            # The background is re-estimated continuously from the quietest
            # tenth of recent history, so a room that gets louder does not
            # leave this either deaf or permanently triggered.
            recent.append(level)
            if len(recent) > 30:
                recent.pop(0)
            floor = sorted(recent)[len(recent) // 10] if len(recent) >= 10 else level

            # Respect the calibrated level. This used to take the hard floor
            # instead, which on a quiet microphone is louder than the person
            # using it - calibration would measure speech at 0.00024 and the
            # wake listener would then demand 0.0006, so it only heard shouting.
            quietest = config.SPEECH_THRESHOLD or _stt._THRESHOLD_FLOOR
            threshold = max(floor * _FLOOR_MULTIPLE, quietest)
            if level <= threshold:
                continue  # silence: costs nothing

            # Someone spoke. Capture the phrase, then transcribe just that.
            captured = [frame]
            quiet_for = 0.0
            while len(captured) * block / config.SAMPLE_RATE < _MAX_PHRASE:
                if stop_event is not None and stop_event.is_set():
                    return False
                frame, _ = stream.read(block)
                captured.append(frame)
                if float(np.sqrt(np.mean(frame**2))) <= threshold:
                    quiet_for += block / config.SAMPLE_RATE
                    if quiet_for >= _TAIL:
                        break
                else:
                    quiet_for = 0.0

            audio = np.concatenate(captured).flatten()
            # hotwords biases the decoder towards the phrase we are listening
            # for. Without it a short name is the hardest thing to catch: two
            # syllables with no context, which "Iris" loses to "Irish", "I
            # was", "arrows" and worse. Telling the model what to expect costs
            # nothing and is the single biggest improvement available here.
            segments, _info = model.transcribe(
                audio,
                language="en",
                beam_size=1,
                hotwords=" ".join(phrases()),
                initial_prompt=f"{phrases()[0]}.",
                condition_on_previous_text=False,
            )
            segments = list(segments)
            text = " ".join(s.text for s in segments).strip()
            if not text:
                continue

            # Whisper invents words when handed something that is not speech -
            # a door, a cough, a keyboard. It reports how sure it was, so ask.
            # Without this the wake word competes with the model's imagination.
            unsure = max((s.no_speech_prob for s in segments), default=0.0)
            weak = min((s.avg_logprob for s in segments), default=0.0)
            if unsure > NOT_SPEECH or weak < TOO_UNSURE:
                if config.WAKE_DEBUG:
                    print(f"    [wake ignored {text!r} - not confident "
                          f"(silence {unsure:.2f}, score {weak:.2f})]")
                continue

            if heard_phrase(text):
                return True
            if config.WAKE_DEBUG:
                print(f"    [wake heard {text!r} - no match]")
