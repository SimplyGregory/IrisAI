"""Controlling a Roku television or player.

Two tools rather than a dozen, for the reason given in tools/__init__.py: every
schema is re-sent on every request, and a machine with no Roku should pay
nothing for one. They are only registered when the connection was turned on
during setup.

The split is where the confirmation gate wants it. Asking what is playing is
free; turning the television off in front of someone is not.
"""

from anthropic import beta_tool

from iris import roku
from iris.confirm import confirm
from iris.redact import scrubbed

_LOOK = ("device", "apps", "playing", "power")
_DO = ("launch", "key", "type", "power", "volume")


def _unavailable(exc: Exception) -> str:
    return f"The Roku is not reachable. {exc}"


def _seconds(value) -> str:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "?"
    return f"{value // 60}:{value % 60:02d}"


@beta_tool
@scrubbed
def roku_inspect(op: str, contains: str = "") -> str:
    """Look at what the user's Roku is doing, without changing it.

    Operations:
      device   Model, serial, software version, network and whether the screen
               is on. Use it for any question about the Roku itself.
      apps     Every channel installed, with the id needed to launch one. Call
               this before launching something, rather than guessing an id -
               they are arbitrary numbers and a wrong one does nothing.
      playing  What is on screen now, and if something is playing, how far
               through it is. This is the honest answer to "what am I
               watching" and "how long is left".
      power    Whether the screen is on. Roku TVs report this; streaming
               players always say on, because they have no screen to sleep.

    Args:
        op: One of device, apps, playing, power.
        contains: For apps, only list channels whose name contains this.
    """
    op = (op or "").strip().lower()
    if op not in _LOOK:
        return f"Unknown operation {op!r}. Use one of: {', '.join(_LOOK)}."

    where = roku.address()
    if not where:
        return "No Roku is configured. Run setup again to connect one."

    try:
        if op == "device":
            info = roku.device_info(where)
            return "\n".join(
                f"  {label}: {info.get(key, '?')}"
                for label, key in (
                    ("name", "user-device-name"), ("model", "model-name"),
                    ("type", "device-type"), ("software", "software-version"),
                    ("serial", "serial-number"), ("network", "network-name"),
                    ("connection", "network-type"), ("power", "power-mode"),
                )
                if info.get(key)
            ) or "The Roku answered but told us nothing about itself."

        if op == "apps":
            installed = roku.apps(where)
            wanted = contains.strip().lower()
            if wanted:
                installed = [a for a in installed if wanted in a["name"].lower()]
            if not installed:
                return f"No installed channel matches {contains!r}." if wanted else "No channels installed."
            lines = [f"{len(installed)} channel(s):"]
            lines += [f"  {a['name']}  (id {a['id']})" for a in installed[:60]]
            return "\n".join(lines)

        if op == "power":
            info = roku.device_info(where)
            mode = info.get("power-mode", "unknown")
            return f"The screen is {'on' if mode == 'PowerOn' else 'off (' + mode + ')'}."

        # playing
        app = roku.active_app(where)
        if not app["id"]:
            return "Nothing is open; the Roku is on its home screen."
        media = roku.media_player(where)
        state = media.get("state", "unknown")
        if state not in ("play", "pause"):
            return f"{app['name']} is open, but nothing is playing."
        position, duration = media.get("position"), media.get("duration")
        progress = ""
        if position is not None and duration:
            progress = f", {_seconds(position)} of {_seconds(duration)}"
        return f"{app['name']} is {'playing' if state == 'play' else 'paused'}{progress}."

    except roku.RokuUnavailable as exc:
        return _unavailable(exc)
    except roku.RokuRefused as exc:
        return str(exc)


@beta_tool
@confirm("confirm")
def roku_control(
    op: str,
    app: str = "",
    content_id: str = "",
    media_type: str = "",
    key: str = "",
    text: str = "",
    times: int = 1,
    state: str = "",
) -> str:
    """Control the user's Roku: open things, press remote keys, power, volume.

    This acts on a television someone may be watching, so it is as visible as
    an action gets - treat it accordingly.

    Operations:
      launch  Open a channel by `app` name, e.g. "Netflix". Call
              roku_inspect("apps") first for the exact name. To jump straight
              to something inside it, pass `content_id` and `media_type` - the
              channel decides what those mean, so a wrong one usually just
              opens the app rather than failing.
      key     Press a remote key `times` times. This is how you move around
              inside an app: up, down, left, right, select, back, home, play,
              pause, rewind, forward, replay, info. There is no API for
              settings, so changing one means walking the menus with these.
      type    Type `text` into an on-screen keyboard that is already showing.
              One keystroke per character, so keep it short.
      power   `state` "on" or "off". Off works on every Roku; on only reaches
              Roku TVs, since a player has no screen to wake.
      volume  `state` "up", "down" or "mute", `times` steps. Only works where
              the Roku drives the audio - a TV, or a player over HDMI-ARC. On
              a player feeding an external amplifier it does nothing.

    Args:
        op: One of launch, key, type, power, volume.
        app: For launch, the channel name as roku_inspect("apps") lists it.
        content_id: For launch, what to open inside the channel.
        media_type: For launch, what that content is - "movie", "episode",
            "series", "live". Needed with content_id by most channels.
        key: For key, the remote button to press.
        text: For type, what to enter.
        times: How many presses or volume steps. Defaults to one.
        state: For power, on or off. For volume, up, down or mute.
    """
    op = (op or "").strip().lower()
    if op not in _DO:
        return f"Unknown operation {op!r}. Use one of: {', '.join(_DO)}."

    where = roku.address()
    if not where:
        return "No Roku is configured. Run setup again to connect one."

    try:
        if op == "launch":
            if not app:
                return "launch needs the name of a channel."
            found = roku.find_app(where, app)
            if found is None:
                names = ", ".join(a["name"] for a in roku.apps(where)[:12])
                return f"No channel called {app!r} is installed. There is: {names}"
            roku.launch(where, found["id"], content_id, media_type)
            if content_id:
                return (
                    f"Opened {found['name']} at {content_id}. If it landed on the "
                    "channel's home screen instead, the id or media type was not "
                    "one it recognises - say so rather than assuming it worked."
                )
            return f"Opened {found['name']}."

        if op == "key":
            if not key:
                return "key needs the button to press."
            pressed = roku.press(where, key, times)
            return f"Pressed {pressed}" + (f" {times} times." if times > 1 else ".")

        if op == "type":
            if not text:
                return "type needs something to enter."
            sent = roku.type_text(where, text)
            return f"Typed {sent} character(s). Press select or enter to submit it."

        if op == "power":
            wanted = state.strip().lower()
            if wanted not in ("on", "off"):
                return "power needs state 'on' or 'off'."
            roku.press(where, "poweron" if wanted == "on" else "poweroff")
            if wanted == "on":
                return (
                    "Sent power on. Only Roku TVs answer this; a streaming player "
                    "has no screen of its own and will ignore it."
                )
            return "Sent power off."

        # volume
        wanted = state.strip().lower()
        if wanted not in ("up", "down", "mute"):
            return "volume needs state 'up', 'down' or 'mute'."
        roku.press(where, {"up": "volumeup", "down": "volumedown", "mute": "mute"}[wanted], times)
        if wanted == "mute":
            return "Toggled mute."
        return f"Volume {wanted} {times} step(s)."

    except roku.RokuUnavailable as exc:
        return _unavailable(exc)
    except roku.RokuRefused as exc:
        # Deliberately not prefixed with "not reachable": it answered.
        return str(exc)
    except ValueError as exc:
        return str(exc)


TOOLS = [roku_inspect, roku_control]
