"""Tools that act on Iris rather than on the machine.

"Be quiet", "turn yourself down", "make yourself see-through", "close
yourself" - things you would naturally say to an assistant that are otherwise
impossible to ask for, because every other tool here points outward.

Anything belonging to whichever front-end is running is reached through
iris/selfcontrol.py, so these work the same from text mode, voice mode and
the panel, and say plainly when a mode cannot do something rather than
failing obscurely.

Not here, on purpose: changing the confirmation mode. Being able to switch off
the thing that asks your permission would make asking it meaningless.
"""

from anthropic import beta_tool

from iris import config, selfcontrol
from iris.confirm import confirm


@beta_tool
def speech_settings(speaking: str = "", volume: float = -1) -> str:
    """Change whether Iris speaks her replies aloud, and how loudly.

    Use this when the user asks you to be quiet, to speak up, to stop talking,
    to turn yourself down, or to start reading replies out again.

    speaking: "on" to read replies aloud, "off" to stay silent, "" to leave as is.
    volume: 0.0 to 1.0, where 1.0 is full volume. -1 leaves it as is.
    """
    changed = []

    wanted = speaking.strip().lower()
    if wanted in ("on", "true", "yes", "speak", "unmute"):
        config.SPEAK_REPLIES = True
        changed.append("speech on")
    elif wanted in ("off", "false", "no", "silent", "mute", "quiet"):
        config.SPEAK_REPLIES = False
        changed.append("speech off")
    elif wanted:
        return f"Did not understand speaking={speaking!r}. Use 'on', 'off', or leave it out."

    if volume >= 0:
        config.VOICE_VOLUME = max(0.0, min(1.0, volume))
        changed.append(f"volume {config.VOICE_VOLUME:.0%}")

    # Let the front-end refresh anything it shows about this, if it cares to.
    if selfcontrol.has("speech_changed"):
        try:
            selfcontrol.call("speech_changed")
        except Exception:
            pass

    state = (
        f"Speech is {'on' if config.SPEAK_REPLIES else 'off'}, "
        f"volume {config.VOICE_VOLUME:.0%}."
    )
    return f"{', '.join(changed)}. {state}" if changed else state


@beta_tool
def window_settings(transparency: int = -1, visible: str = "") -> str:
    """Change how the Iris panel window looks, or hide it.

    Only the panel has a window; in text or voice mode this reports that there
    is nothing to change rather than pretending it worked.

    transparency: 0 for fully opaque, up to 80 for very see-through. -1 leaves it.
    visible: "hide" to tuck the panel away, "" to leave it.
    """
    if not selfcontrol.has("set_transparency"):
        return (
            "There is no window to change - Iris is running in the terminal, "
            "not the panel. Tell the user that."
        )

    done = []
    if transparency >= 0:
        level = max(0, min(80, int(transparency)))
        try:
            selfcontrol.call("set_transparency", level)
        except Exception as exc:
            return f"Could not change transparency: {exc}"
        done.append(f"transparency {level}%")

    if visible.strip().lower() in ("hide", "hidden", "off", "away"):
        try:
            selfcontrol.call("hide")
        except Exception as exc:
            return f"Could not hide the panel: {exc}"
        done.append("panel hidden")

    return ", ".join(done) + "." if done else "Nothing changed."


@beta_tool
@confirm("silent")
def clear_conversation() -> str:
    """Wipe the current conversation: the messages on screen and what you recall of them.

    Use this when the user asks to clear the chat, start over, restart the
    conversation, or wipe the history. It is not the same as forgetting things
    about them: what you have saved with remember - their name, what they play,
    their preferences - survives this untouched. If they want *those* gone,
    that is forget, and it is worth checking which they meant.

    You will not remember this conversation afterwards, so say anything worth
    saying before you call it.
    """
    if not selfcontrol.has("clear_conversation"):
        return "This way of running Iris cannot clear its own history."

    try:
        selfcontrol.call("clear_conversation")
    except Exception as exc:
        return f"Could not clear the conversation: {exc}"
    return "Conversation cleared. Saved memories are untouched."


@beta_tool
@confirm("confirm")
def shut_down(purpose: str = "close Iris") -> str:
    """Stop Iris completely. The process exits and she is gone until restarted.

    Use this when the user says to close, quit, exit, shut down or turn you
    off. This is not the same as hiding the panel - use window_settings for
    that if they only want it out of the way.

    Say goodbye before calling this: nothing you say afterwards will be seen.
    """
    if not selfcontrol.has("quit"):
        return "This way of running Iris cannot close itself. Ask the user to close it."

    try:
        selfcontrol.call("quit")
    except Exception as exc:
        return f"Could not shut down: {exc}"
    return "Shutting down now."


TOOLS = [speech_settings, window_settings, clear_conversation, shut_down]
