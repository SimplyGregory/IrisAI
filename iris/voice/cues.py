"""A quiet heartbeat while Iris is working.

Long chains can run twenty seconds or more with nothing audible happening, so
there is no way to tell "thinking" from "crashed". This plays a soft, short
tone every few seconds - easy to ignore, but there if you listen for it.

Deliberately quiet and low: a loud or bright cue becomes maddening within a
minute. It also pauses itself whenever Iris needs to hear you, since anything
playing through the speakers is something the microphone can pick up.
"""

import threading
import time

_SAMPLE_RATE = 44100
_FREQ = 520.0  # low enough not to be piercing
_INTERVAL = 2.2  # seconds between one ding-ding and the next
_FIRST_DELAY = 0.6  # quiet grace period so quick replies do not ding

_thread: threading.Thread | None = None
_stop = threading.Event()
_paused = threading.Event()
_blip = None


def _ding(freq: float, ms: int, volume: float):
    """One bell-like note: fast attack, exponential decay, a few harmonics."""
    import numpy as np

    samples = int(_SAMPLE_RATE * ms / 1000)
    t = np.linspace(0.0, ms / 1000.0, samples, endpoint=False)
    # Harmonics give it a struck-bell timbre rather than a flat test tone.
    tone = (
        np.sin(2 * np.pi * freq * t)
        + 0.5 * np.sin(2 * np.pi * freq * 2 * t)
        + 0.25 * np.sin(2 * np.pi * freq * 3 * t)
    )
    tone /= np.abs(tone).max()
    decay = np.exp(-t * 14.0)  # rings out quickly, like a chime
    attack = np.minimum(1.0, np.linspace(0.0, 1.0, samples) * 60)  # no click
    return tone * decay * attack * volume


def _waveform(volume: float | None = None):
    """Two quick dings - "ding-ding" - then silence until the next repeat.

    A pair reads as a deliberate signal rather than an incidental noise, which
    makes it easier to ignore while still being obvious when you listen for it.
    """
    global _blip
    if volume is None and _blip is not None:
        return _blip

    import numpy as np

    from iris import config

    level = config.CUE_VOLUME if volume is None else volume
    first = _ding(_FREQ, 150, level)
    second = _ding(_FREQ, 200, level)  # second rings slightly longer
    gap = np.zeros(int(_SAMPLE_RATE * 0.09), dtype="float64")
    wave = np.concatenate([first, gap, second]).astype("float32")

    if volume is None:
        _blip = wave
    return wave


_ready_tone = None


def acknowledge() -> None:
    """One short rising note, played without waiting for it to finish.

    This is what answers the wake word. Saying "Yes?" out loud costs a full
    second of speech synthesis and playback before the microphone can even
    open, which is most of the delay between calling her name and being able
    to speak. A note is instant, and it is what every other assistant does for
    the same reason.
    """
    global _ready_tone
    try:
        import numpy as np
        import sounddevice as sd

        from iris import config

        if _ready_tone is None:
            level = min(1.0, max(0.0, config.CUE_VOLUME * 1.6))
            # Two notes a fifth apart, rising: reads as "go ahead" rather than
            # as the working ding, which is the same tone twice.
            first = _ding(_FREQ, 90, level)
            second = _ding(_FREQ * 1.5, 130, level)
            _ready_tone = np.concatenate([first, second]).astype("float32")

        sd.play(_ready_tone, _SAMPLE_RATE, blocking=False)
    except Exception:
        pass  # a missing sound device must not stop her listening


def preview(times: int = 3) -> None:
    """Play the cue a few times so you can judge the volume."""
    import sounddevice as sd

    from iris import config

    print(f"  cue volume {config.CUE_VOLUME} - playing {times} pulses...")
    wave = _waveform()
    for _ in range(times):
        sd.play(wave, _SAMPLE_RATE, blocking=True)
        time.sleep(0.6)
    print("  if that was too quiet or too loud, set IRIS_CUE_VOLUME in .env (0.0 - 1.0)")


def _loop():
    import sounddevice as sd

    wave = _waveform()
    # A short grace period so an instant answer does not ding pointlessly.
    # Measured: a 3 second task gets one ding-ding, an 8 second task gets three.
    if _stop.wait(_FIRST_DELAY):
        return
    while not _stop.is_set():
        # Checked again here, not just at the top: stop() aborts whatever is
        # sounding, and without this a thread that had just passed the loop
        # test would start a fresh ding immediately afterwards.
        if not _paused.is_set() and not _stop.is_set():
            try:
                sd.play(wave, _SAMPLE_RATE, blocking=True)
            except Exception:
                return  # no output device, or it is busy; stay silent
        _stop.wait(_INTERVAL)


def start() -> None:
    global _thread
    from iris import config

    if not config.SOUND_CUES or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _paused.clear()
    _thread = threading.Thread(target=_loop, name="iris-cue", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread and _thread.is_alive():
        # Cut a ding still sounding rather than waiting out its half second.
        # stop() runs the instant the work finishes, immediately before the
        # reply is shown and spoken, so anything blocking here is delay the
        # user feels - and a tone still ringing over the first word of the
        # answer is the thing the cue is meant to prevent, not cause.
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass
        _thread.join(timeout=1.0)
    _thread = None


class thinking:
    """Context manager: pulse quietly for as long as the block runs."""

    def __enter__(self):
        start()
        return self

    def __exit__(self, *_exc):
        stop()
        return False


class quiet:
    """Context manager: silence the pulse, e.g. while listening for an answer.

    Anything coming out of the speakers can be picked up by the microphone, so
    the cue must not run while Iris is waiting for you to speak.
    """

    def __enter__(self):
        _paused.set()
        time.sleep(0.12)  # let any in-flight pulse finish
        return self

    def __exit__(self, *_exc):
        _paused.clear()
        return False
