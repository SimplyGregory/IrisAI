"""Speaking. Offline and free, and the same voice on either platform.

Piper first: a local neural model that sounds identical on every machine, so a
Mac and a PC running Iris sound like the same assistant rather than like two
different products. Only when no model is downloaded does this fall back to
whatever the operating system provides, which is where the platform layer takes
over - SAPI on Windows, `say` on a Mac. Nothing in this file knows which.
"""

import io
import re
import threading
import wave
from pathlib import Path

# Silence appended to every spoken line, so the audio device has something
# expendable to drop instead of the end of the last word.
_TAIL_PAD = 0.25  # seconds

# Piper is a local neural text-to-speech engine. It sounds far better than the
# Windows desktop voices (David, Zira), runs entirely offline on CPU, and
# synthesises a five second reply in about 0.2s once the model is loaded.
VOICE_DIR = Path.home() / ".iris" / "voices"
_piper = None
_piper_lock = threading.Lock()


def available_piper_voices() -> list[str]:
    return sorted(p.stem for p in VOICE_DIR.glob("*.onnx")) if VOICE_DIR.is_dir() else []


def _piper_model_path() -> Path | None:
    """The configured Piper voice, or any downloaded one as a fallback."""
    from iris import config

    voices = available_piper_voices()
    if not voices:
        return None
    wanted = (config.VOICE or "").strip().lower()
    if wanted:
        for name in voices:
            if wanted in name.lower():
                return VOICE_DIR / f"{name}.onnx"
        return None  # asked for a specific voice; fall through to SAPI
    return VOICE_DIR / f"{voices[0]}.onnx"


def _load_piper():
    """Load the model once. ONNX inference is thread-safe, the load is not."""
    global _piper
    with _piper_lock:
        if _piper is None:
            model = _piper_model_path()
            if model is None:
                return None
            from piper import PiperVoice

            _piper = PiperVoice.load(str(model))
    return _piper


def _speak_piper(text: str) -> bool:
    from iris import config

    if config.TTS_ENGINE not in ("piper", "auto"):
        return False
    try:
        voice = _load_piper()
        if voice is None:
            return False

        import numpy as np
        import sounddevice as sd

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            voice.synthesize_wav(text, out)
        buffer.seek(0)
        with wave.open(buffer, "rb") as data:
            rate = data.getframerate()
            audio = np.frombuffer(data.readframes(data.getnframes()), dtype=np.int16)

        # Piper has no volume control, so the samples are scaled instead.
        # In float, then back to int16: scaling int16 in place would wrap a
        # loud sample round to the opposite sign and click.
        if config.VOICE_VOLUME < 1.0:
            audio = (audio.astype(np.float32) * config.VOICE_VOLUME).astype(np.int16)

        # A tail of silence, because the last word was being clipped -
        # "flower" coming out as "flowe". The synthesis is complete; playback
        # is what cuts it. PortAudio closes the stream once its callback has
        # taken the last buffer, and whatever the device still had queued goes
        # with it. Padding means the part that gets dropped is silence.
        audio = np.concatenate([audio, np.zeros(int(rate * _TAIL_PAD), dtype=np.int16)])

        sd.play(audio, rate, blocking=True)
        sd.wait()  # belt and braces: do not return while it is still sounding
        return True
    except Exception:
        return False


def _spoken_form(text: str) -> str:
    """Strip anything that sounds wrong when read aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def list_voices() -> list[str]:
    """Names of every speech voice the operating system provides."""
    from iris import platform

    return platform.list_voices()


def preview_voices(sample: str = "Good morning. I waited five seconds for you, then opened Google.") -> None:
    """Speak the same line in every available voice so you can pick one."""
    import numpy as np
    import sounddevice as sd
    from piper import PiperVoice

    for model in sorted(VOICE_DIR.glob("*.onnx")) if VOICE_DIR.is_dir() else []:
        print(f"  [piper] {model.stem}")
        try:
            voice = PiperVoice.load(str(model))
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as out:
                voice.synthesize_wav(sample, out)
            buffer.seek(0)
            with wave.open(buffer, "rb") as data:
                rate = data.getframerate()
                audio = np.frombuffer(data.readframes(data.getnframes()), dtype=np.int16)
            sd.play(audio, rate, blocking=True)
        except Exception as exc:
            print(f"    failed: {exc}")

    # Then whatever the operating system offers, which is a different set of
    # names on each - Windows has David and Zira, a Mac has Daniel and Samantha.
    from iris import config, platform

    was = config.VOICE
    for name in platform.list_voices():
        print(f"  [{platform.name()}] {name}")
        config.VOICE = name
        platform.speak_native(sample)
    config.VOICE = was
    print("\n  Set your choice in .env, e.g.  IRIS_VOICE=Zira")


def speak(text: str) -> None:
    clean = _spoken_form(text)
    if not clean:
        return

    # Preferred: the local neural voice, which sounds the same on every
    # machine. Falls through to whatever the operating system provides - SAPI
    # on Windows, `say` on a Mac - when no Piper model is downloaded.
    if _speak_piper(clean):
        return

    from iris import platform

    if not platform.speak_native(clean):
        print(f"[tts failed, could not speak] {clean}")


def self_test() -> bool:
    """Speak a short phrase and report whether any path worked."""
    from iris import platform

    return platform.speak_native("Voice check.")
