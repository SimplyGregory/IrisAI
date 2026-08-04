"""Microphone calibration, runnable as a spoken command.

Speech detection compares loudness against a threshold. Get it wrong and either
nothing is ever heard, or the room triggers it constantly. Rather than guess at
a constant, this measures the actual room and the actual voice and puts the
trigger between them.
"""

from anthropic import beta_tool


@beta_tool
def calibrate_microphone(phrase: str = "Hello Iris", repeats: int = 3) -> str:
    """Run an interactive microphone calibration and save the result.

    Speaks instructions aloud, records five seconds of silence to measure the
    room, then records the user repeating a short phrase to measure their
    voice. Saves the resulting threshold to .env so it survives a restart.

    Call this when the user asks to calibrate their microphone or voice, or
    when speech is repeatedly not being detected. Tell them beforehand that it
    takes about twenty seconds and they will be asked to stay quiet and then to
    speak.

    Args:
        phrase: The phrase to ask the user to repeat.
        repeats: How many times to ask them to repeat it.
    """
    try:
        from iris.voice import stt, tts
    except Exception as exc:
        return f"Voice support is not available: {exc}"

    try:
        result = stt.calibrate(say=tts.speak, phrase=phrase, repeats=max(1, repeats))
    except Exception as exc:
        return f"Calibration failed: {type(exc).__name__}: {exc}"

    try:
        saved_to = stt.save_threshold(result["threshold"])
        saved = f"Saved to {saved_to}; it applies from the next restart."
    except Exception as exc:
        saved = f"Could not save it automatically ({exc}); set IRIS_SPEECH_THRESHOLD manually."

    return (
        f"Calibration complete.\n"
        f"  room noise floor : {result['ambient']:.5f}\n"
        f"  your voice       : {result['speech_low']:.5f} typical, "
        f"{result['speech_peak']:.5f} peak\n"
        f"  separation       : {result['separation']:.0f}x above the room\n"
        f"  new threshold    : {result['threshold']:.5f}\n"
        f"  {result['verdict']}\n"
        f"  {saved}"
    )


TOOLS = [calibrate_microphone]
