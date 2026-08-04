"""Speech to text, entirely on this machine.

faster-whisper runs locally, so transcription costs nothing per word and works
without internet. The only thing that ever hits the network is the agent loop.
"""

import queue
import re

import numpy as np

from iris import config

_model = None

# Recording stops after this much continuous quiet once you have started talking.
_SILENCE_TAIL = 1.1
_MAX_SECONDS = 25.0
_WAIT_FOR_SPEECH = 6.0  # how long to wait for you to start talking
_BLOCK = 1600  # 100 ms at 16 kHz


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        # int8 on CPU is the sweet spot: base.en transcribes a short command
        # faster than real time on any modern laptop.
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def warm_up() -> None:
    """Load the model now so the first real command is not slow."""
    _get_model().transcribe(np.zeros(config.SAMPLE_RATE, dtype=np.float32))


# Set on every record_command call so callers can explain a failed capture.
last_stats: dict = {}

# Speech is judged relative to the room, not against a fixed number.
# Microphone gain varies enormously - a laptop array mic can sit around
# 0.0005 RMS ambient where a headset sits 20x higher - so a hardcoded
# threshold either never triggers or triggers constantly. See Endpointer.
_THRESHOLD_FLOOR = 0.0006  # don't trust a near-silent calibration
_THRESHOLD_CEILING = 0.02  # in case the user starts talking during calibration


class Endpointer:
    """Decides when speech starts and stops, tracking the background live.

    A threshold fixed at the start of a recording fails as soon as the room
    changes: if background noise rises above it, the level never drops back
    down, so the recording never ends and Iris appears to listen forever.

    Instead the background is re-estimated every block from a rolling window,
    and both starting and stopping are judged relative to it. Ending also
    accepts a large drop from however loud the speech has been, so a room that
    rises to a fraction of your speaking volume cannot hold the turn open.
    """

    START_MULTIPLE = 3.5  # above the floor to begin
    STOP_MULTIPLE = 2.0  # below this to count as quiet (hysteresis)
    SPEECH_DROP = 0.18  # ...or this fraction of how loud the speech was
    WINDOW = 25  # 2.5 s of history used to find the background
    PERCENTILE = 0.10  # the quietest tenth of it is the background

    def __init__(self, fixed_threshold: float | None = None):
        from collections import deque

        self.fixed = fixed_threshold
        self.recent = deque(maxlen=self.WINDOW)
        self.speech_level = 0.0
        self.started = False
        self.quiet_for = 0.0

    @property
    def floor(self) -> float:
        """Background level: the quietest tenth of the last few seconds.

        A rolling low percentile rather than a decaying average. Speech is
        gappy - the pauses between words sit at background level - so the
        quietest slice of a window is a good estimate of the room even while
        someone is talking, and it moves within a couple of seconds when the
        room genuinely changes. An averaging filter cannot do both: tuned slow
        enough to ignore speech, it is far too slow to notice a rising room,
        which is exactly how a recording ends up never terminating.
        """
        if not self.recent:
            return 0.0
        ordered = sorted(self.recent)
        return ordered[min(int(len(ordered) * self.PERCENTILE), len(ordered) - 1)]

    def _update_floor(self, level: float) -> None:
        self.recent.append(level)

    @property
    def start_threshold(self) -> float:
        if self.fixed:
            return self.fixed
        return max(self.floor * self.START_MULTIPLE, _THRESHOLD_FLOOR)

    @property
    def stop_threshold(self) -> float:
        """Quiet enough to end on: near the floor, or well below the speech.

        Either test alone fails somewhere. Judged only against the speech, a
        room that rises to half your speaking volume never reads as quiet.
        Judged only against the floor, a trailing-off word ends the turn early.
        Taking the higher of the two ends the turn on whichever becomes true.
        """
        floor_based = max(self.floor * self.STOP_MULTIPLE, _THRESHOLD_FLOOR * 0.6)
        if self.started and self.speech_level:
            return max(floor_based, self.speech_level * self.SPEECH_DROP)
        return floor_based

    def feed(self, level: float, seconds: float) -> str:
        """Push one block. Returns "waiting", "speaking" or "done"."""
        if not self.started:
            self._update_floor(level)
            if level > self.start_threshold:
                self.started = True
                self.speech_level = level
                self.quiet_for = 0.0
                return "speaking"
            return "waiting"

        # Track how loud this speaker actually is, decaying so a single shout
        # does not set the bar for the rest of the utterance.
        self.speech_level = max(level, self.speech_level * 0.97)

        # Keep feeding the window during speech too: the gaps between words are
        # what tell us the room level, and a room that gets louder must be able
        # to move the floor up even while someone is still talking.
        self._update_floor(level)

        if level < self.stop_threshold:
            self.quiet_for += seconds
            if self.quiet_for >= _SILENCE_TAIL:
                return "done"
        else:
            self.quiet_for = 0.0
        return "speaking"


def record_command(silence_threshold: float | None = None) -> np.ndarray | None:
    """Record from the microphone until the speaker stops.

    Returns mono float32 audio at 16 kHz, or None if nothing was said.
    Start and stop are decided by Endpointer, which re-estimates the background
    continuously so a room that gets louder mid-sentence still ends the turn.
    """
    import sounddevice as sd

    blocks: list[np.ndarray] = []
    audio_q: queue.Queue = queue.Queue()

    def on_audio(indata, _frames, _time, _status):
        audio_q.put(indata.copy())

    elapsed = 0.0
    peak = 0.0
    endpointer = Endpointer(silence_threshold or config.SPEECH_THRESHOLD)
    block_seconds = _BLOCK / config.SAMPLE_RATE

    with sd.InputStream(
        samplerate=config.SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=_BLOCK,
        callback=on_audio,
    ):
        while elapsed < _MAX_SECONDS:
            try:
                block = audio_q.get(timeout=1.0)
            except queue.Empty:
                break

            elapsed += block_seconds
            level = float(np.sqrt(np.mean(block**2)))
            peak = max(peak, level)

            state = endpointer.feed(level, block_seconds)
            if state == "waiting":
                if elapsed > _WAIT_FOR_SPEECH:
                    break  # nobody spoke
                continue

            blocks.append(block)
            if state == "done":
                break

    last_stats.update(
        threshold=round(endpointer.start_threshold, 5),
        peak=peak,
        ambient=round(endpointer.floor or 0.0, 5),
        seconds=round(elapsed, 1),
        captured=round(len(blocks) * block_seconds, 1),
    )
    if not blocks:
        return None
    return np.concatenate(blocks).flatten()


def record_until(stop, max_seconds: float = 180.0) -> np.ndarray | None:
    """Record until told to stop, rather than until you stop speaking.

    record_command ends the turn itself, which is right when the microphone is
    the only way in and nobody can press anything. With a button to press the
    decision is yours, and guessing on your behalf is worse than useless: a
    pause to think would cut you off mid-sentence.

    `stop` is a threading.Event. Returns mono float32 at 16 kHz, or None if
    the recording was too short to hold anything.
    """
    import sounddevice as sd

    blocks: list[np.ndarray] = []
    audio_q: queue.Queue = queue.Queue()

    def on_audio(indata, _frames, _time, _status):
        audio_q.put(indata.copy())

    elapsed = 0.0
    with sd.InputStream(
        samplerate=config.SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=_BLOCK,
        callback=on_audio,
    ):
        while not stop.is_set() and elapsed < max_seconds:
            try:
                # Short timeout so the button stays responsive in a silent room,
                # where no audio callback fires to wake this loop.
                blocks.append(audio_q.get(timeout=0.1))
                elapsed += _BLOCK / config.SAMPLE_RATE
            except queue.Empty:
                continue

    if not blocks:
        return None
    return np.concatenate(blocks).flatten()


def transcribe(audio: np.ndarray) -> str:
    """Turn recorded audio into text."""
    segments, _ = _get_model().transcribe(
        audio,
        language="en",
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def mic_check(seconds: float = 6.0) -> None:
    """Live level meter, to see whether the microphone is actually picking you up.

    Run with: python panel/app.py --mic-test
    """
    import sounddevice as sd

    device = sd.query_devices(kind="input")
    print(f"  input device : {device['name']}")
    print(f"  speak now for {seconds:.0f} seconds - the bar should move clearly\n")

    levels: list[float] = []
    with sd.InputStream(
        samplerate=config.SAMPLE_RATE, channels=1, dtype="float32", blocksize=_BLOCK
    ) as stream:
        for _ in range(int(seconds * config.SAMPLE_RATE / _BLOCK)):
            frame, _ = stream.read(_BLOCK)
            level = float(np.sqrt(np.mean(frame**2)))
            levels.append(level)
            bars = min(int(level * 400), 50)
            print(f"\r  |{'#' * bars:<50}| {level:.4f}", end="", flush=True)

    quiet = sorted(levels)[: max(1, len(levels) // 4)]
    ambient = sum(quiet) / len(quiet)
    loudest = max(levels)
    print("\n")
    print(f"  ambient (quietest quarter): {ambient:.5f}")
    print(f"  loudest                   : {loudest:.5f}")
    print(f"  ratio                     : {loudest / max(ambient, 1e-9):.0f}x")
    print()
    if loudest < 0.004:
        print("  PROBLEM: your speech barely registers. Raise the microphone level in")
        print("  Windows Settings > System > Sound > Input, or move closer to the mic.")
    elif loudest / max(ambient, 1e-9) < 5:
        print("  PROBLEM: speech is not much louder than the room. Reduce background")
        print("  noise, or use a headset mic.")
    else:
        print("  Looks fine - speech is well above the noise floor.")


def _record_levels(seconds: float, on_tick=None) -> list[float]:
    """Record and return the per-block RMS levels, discarding the audio."""
    import sounddevice as sd

    levels: list[float] = []
    with sd.InputStream(
        samplerate=config.SAMPLE_RATE, channels=1, dtype="float32", blocksize=_BLOCK
    ) as stream:
        for i in range(int(seconds * config.SAMPLE_RATE / _BLOCK)):
            frame, _ = stream.read(_BLOCK)
            levels.append(float(np.sqrt(np.mean(frame**2))))
            if on_tick and i % 10 == 0:
                on_tick(len(levels) * _BLOCK / config.SAMPLE_RATE)
    return levels


def calibrate(say=None, phrase: str = "Hello Iris", repeats: int = 3) -> dict:
    """Measure this room and this voice, and work out the right threshold.

    Records silence to find the noise floor, then recorded speech to find how
    loud you actually are, and puts the trigger between the two. Beats guessing
    at a constant, which is how the original 0.012 ended up above this
    microphone's entire dynamic range.

    Args:
        say: Optional callable used to speak each instruction aloud.
        phrase: What to ask the speaker to repeat.
        repeats: How many times to repeat it.

    Returns a dict with ambient, speech and the recommended threshold.
    """

    def announce(text: str) -> None:
        print(f"  {text}")
        if say:
            say(text)

    announce(f"Calibrating. Please stay silent for 5 seconds, starting now.")
    ambient = _record_levels(5.0)

    announce(f"Thank you. Now please say, {phrase}, {repeats} times.")
    speech = _record_levels(3.0 * repeats + 1.0)

    ambient_sorted = sorted(ambient)
    # 95th percentile of room tone: ignore the odd click, keep the real floor.
    ambient_high = ambient_sorted[int(len(ambient_sorted) * 0.95)]

    # The recording is mostly gaps between repetitions, so take the loudest
    # fifth as "this is what speech looks like", then its quiet end.
    loud = sorted(speech)[int(len(speech) * 0.80) :]
    speech_low = loud[0] if loud else 0.0
    speech_peak = max(speech) if speech else 0.0

    if speech_low > ambient_high * 2:
        # Geometric midpoint sits proportionally between the two distributions.
        threshold = (ambient_high * speech_low) ** 0.5
    else:
        threshold = max(ambient_high * 2.5, _THRESHOLD_FLOOR)
    threshold = round(min(max(threshold, 0.0002), _THRESHOLD_CEILING), 5)

    separation = speech_low / ambient_high if ambient_high else 0.0
    if speech_peak < 0.002:
        verdict = "Your microphone is very quiet. Raise its level in Windows sound settings."
    elif separation < 3:
        verdict = "Your voice is close to the background noise. A headset mic would help."
    else:
        verdict = "Good separation between your voice and the room."

    return {
        "ambient": ambient_high,
        "speech_low": speech_low,
        "speech_peak": speech_peak,
        "separation": separation,
        "threshold": threshold,
        "verdict": verdict,
    }


def save_threshold(value: float, env_path=None) -> str:
    """Persist a calibrated threshold to .env so it survives a restart."""
    from pathlib import Path

    # The install folder, not wherever this file happens to live. Frozen,
    # parents[2] is the temporary folder PyInstaller unpacks into and deletes
    # on exit, so a calibration written there is gone the moment it finishes.
    if env_path:
        path = Path(env_path)
    else:
        from iris import paths

        path = paths.env_file()
    line = f"IRIS_SPEECH_THRESHOLD={value}"
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return str(path)

    text = path.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*#?\s*IRIS_SPEECH_THRESHOLD\s*=.*$", text):
        text = re.sub(r"(?m)^\s*#?\s*IRIS_SPEECH_THRESHOLD\s*=.*$", line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n\n# Set by voice calibration.\n{line}\n"
    path.write_text(text, encoding="utf-8")
    return str(path)


def listen() -> str:
    """Record one spoken command and return its transcript ('' if silence)."""
    audio = record_command()
    if audio is None or audio.size < config.SAMPLE_RATE // 4:
        return ""
    return transcribe(audio)
