"""Talking to a Roku, over its External Control Protocol.

No application is installed on the television and nothing is sideloaded. Every
Roku already serves this: plain HTTP on port 8060, no authentication, on the
local network. The zip a Roku "app" would live in is not needed for any of what
Iris does here - a sideloaded channel can draw things on the screen, but the
control surface below is the built-in one and is strictly larger.

What it cannot do is worth knowing up front. There is no endpoint for settings;
changing one means walking the on-screen menus with arrow keys the way a person
would. And ECP's search was withdrawn in Roku OS 12, so finding a show means
deep-linking into the app that carries it rather than asking the box to look.

Nothing here raises at the caller. A television that is off, asleep or on
another network is an ordinary state, and the tools turn it into a sentence.
"""

import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PORT = 8060
TIMEOUT = 6.0

# Starting a channel is not like the other calls: the Roku holds the
# connection open while the app loads, and YouTube took longer than six
# seconds on real hardware - so launch reported "no answer, it may be off"
# about a television that was busy doing exactly what it was told.
LAUNCH_TIMEOUT = 25.0

# What a person says, and what the remote actually calls it. The right-hand
# side is ECP's spelling, which nobody would guess: rewind is "Rev", and replay
# is "InstantReplay".
KEYS = {
    "home": "Home", "back": "Back", "select": "Select", "ok": "Select",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "play": "Play", "pause": "Play", "resume": "Play",
    "rewind": "Rev", "forward": "Fwd", "fastforward": "Fwd",
    "replay": "InstantReplay", "info": "Info", "options": "Info",
    "star": "Info", "backspace": "Backspace", "enter": "Enter",
    "search": "Search", "findremote": "FindRemote",
    "volumeup": "VolumeUp", "volumedown": "VolumeDown", "mute": "VolumeMute",
    "channelup": "ChannelUp", "channeldown": "ChannelDown",
    "poweroff": "PowerOff", "poweron": "PowerOn",
    "tuner": "InputTuner", "hdmi1": "InputHDMI1", "hdmi2": "InputHDMI2",
    "hdmi3": "InputHDMI3", "hdmi4": "InputHDMI4", "av": "InputAV1",
}


class RokuUnavailable(Exception):
    """No Roku answered - off, asleep, or on a different network."""


class RokuRefused(Exception):
    """It answered and said no.

    A different thing entirely from silence, and worth its own type: reporting
    a refusal as "not reachable" sends someone to check cables and network
    settings for a television that is plainly online and talking.
    """


# --- finding it -------------------------------------------------------------

def discover(timeout: float = 4.0) -> list[dict]:
    """Every Roku that answers on this network, as {name, ip, id}.

    SSDP: a multicast question that Rokus answer with their address. Worth
    having rather than asking the user to find an IP in a settings menu, and
    worth re-running later because a lease can move.
    """
    question = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "ST: roku:ecp",
        f"MX: {int(timeout)}",
        "", "",
    ]).encode()

    found: dict[str, dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(question, ("239.255.255.250", 1900))
        while True:
            try:
                data, _sender = sock.recvfrom(4096)
            except socket.timeout:
                break
            reply = data.decode("utf-8", errors="replace")
            where = re.search(r"LOCATION:\s*http://([\d.]+):(\d+)", reply, re.I)
            if not where:
                continue
            ip = where.group(1)
            if ip in found:
                continue
            serial = re.search(r"USN:\s*uuid:roku:ecp:(\S+)", reply, re.I)
            found[ip] = {"ip": ip, "id": serial.group(1) if serial else "", "name": ""}
    except OSError:
        # No network, or multicast is refused. An empty list says the same
        # thing to the caller as finding nothing, which is the truth here.
        return []
    finally:
        sock.close()

    # Ask each one its name, so the user picks "Living Room" and not an address.
    for ip, entry in found.items():
        try:
            info = device_info(ip)
            entry["name"] = info.get("user-device-name") or info.get("model-name", "")
        except Exception:  # noqa: BLE001 - it answered SSDP; a name is a bonus
            pass
    return list(found.values())


# --- the protocol -----------------------------------------------------------

def _url(ip: str, path: str) -> str:
    return f"http://{ip}:{PORT}/{path.lstrip('/')}"


def _request(ip: str, path: str, method: str = "GET", timeout: float = TIMEOUT) -> str:
    request = urllib.request.Request(_url(ip, path), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RokuRefused(
                "The Roku answered but refused the command. It is online and "
                "reachable - only control is being turned away, which is a "
                "setting on the television and not a network problem.\n"
                "On the Roku: Settings > System > Advanced system settings > "
                "Control by mobile apps > Network access. Set it to Enabled "
                "(older software calls it Permissive). Recent Roku software "
                "ships this as Limited, which refuses exactly this.\n"
                "The user's phone controlling the Roku does not mean it is "
                "already set: Limited still allows Roku's own app and turns "
                "away everything else. Say that, rather than concluding the "
                "setting must be fine.\n"
                "This needs the physical remote - the menus cannot be walked "
                "with key presses when key presses are what is being refused."
            ) from exc
        if exc.code == 400:
            raise RokuRefused(
                f"The Roku did not understand {path}. The key name or the deep "
                "link is not one it recognises."
            ) from exc
        raise RokuRefused(f"The Roku refused {path}: HTTP {exc.code}.") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise RokuUnavailable(
            f"no answer from {ip}. It may be off, asleep, or on another network. "
            "Check that 'Control by mobile apps' is enabled in its network settings."
        ) from exc


def _xml(ip: str, path: str) -> ET.Element:
    text = _request(ip, path)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise RokuUnavailable(f"{path} did not return XML: {text[:120]}") from exc


# --- reading ----------------------------------------------------------------

def device_info(ip: str) -> dict:
    """Everything the box will say about itself, as a flat dictionary."""
    root = _xml(ip, "query/device-info")
    return {child.tag: (child.text or "").strip() for child in root}


def apps(ip: str) -> list[dict]:
    """Installed channels, as {id, name, version}."""
    root = _xml(ip, "query/apps")
    return [
        {"id": app.get("id", ""), "name": (app.text or "").strip(),
         "version": app.get("version", "")}
        for app in root.findall("app")
    ]


def active_app(ip: str) -> dict:
    """What is on screen now. An empty id means the home screen."""
    root = _xml(ip, "query/active-app")
    app = root.find("app")
    if app is None:
        return {"id": "", "name": "Home", "version": ""}
    return {
        "id": app.get("id", ""),
        "name": (app.text or "").strip(),
        "version": app.get("version", ""),
    }


def media_player(ip: str) -> dict:
    """Playback state: whether something is playing, and how far in.

    Positions come back as "1234 ms", which is not a number anyone wants to do
    arithmetic on, so they are converted to seconds here.
    """
    root = _xml(ip, "query/media-player")
    state = {"state": root.get("state", "unknown"), "error": root.get("error", "false")}
    for tag in ("position", "duration", "runtime"):
        node = root.find(tag)
        if node is not None and node.text:
            digits = re.sub(r"[^\d]", "", node.text)
            if digits:
                state[tag] = int(digits) // 1000  # milliseconds -> seconds
    for tag in ("plugin", "format"):
        node = root.find(tag)
        if node is not None:
            state[tag] = node.get("name") or node.get("video") or ""
    return state


# --- acting -----------------------------------------------------------------

def press(ip: str, key: str, times: int = 1) -> str:
    """Press a remote key. Accepts what a person calls it, not ECP's spelling."""
    wanted = KEYS.get(key.strip().lower().replace(" ", "").replace("_", ""))
    if wanted is None:
        # A literal ECP name is accepted too, so a key added to the protocol
        # later is reachable without waiting for the table above to catch up.
        if key in KEYS.values():
            wanted = key
        else:
            raise ValueError(
                f"unknown key {key!r}. Try one of: " + ", ".join(sorted(KEYS)[:14]) + " ..."
            )
    for _ in range(max(1, min(times, 50))):
        _request(ip, f"keypress/{wanted}", method="POST")
    return wanted


def type_text(ip: str, text: str) -> int:
    """Type into whatever on-screen keyboard is showing.

    One request per character, which is how ECP works - there is no bulk entry.
    Capped, because a long string is almost always a mistake and each keystroke
    is a round trip to the television.
    """
    sent = 0
    for character in text[:120]:
        encoded = urllib.parse.quote(character, safe="")
        _request(ip, f"keypress/Lit_{encoded}", method="POST")
        sent += 1
    return sent


def launch(ip: str, app_id: str, content_id: str = "", media_type: str = "") -> None:
    """Open a channel, optionally jumping straight to something inside it.

    contentId and mediaType are the deep link. What they mean is the channel's
    own business - Netflix wants its title id, YouTube wants a video id - so a
    wrong one usually opens the app at its home screen rather than failing.
    """
    query = {}
    if content_id:
        query["contentId"] = content_id
    if media_type:
        query["mediaType"] = media_type
    path = f"launch/{app_id}"
    if query:
        path += "?" + urllib.parse.urlencode(query)
    _request(ip, path, method="POST", timeout=LAUNCH_TIMEOUT)


def find_app(ip: str, name: str) -> dict | None:
    """The installed channel whose name best matches what was asked for."""
    wanted = name.strip().lower()
    installed = apps(ip)
    for app in installed:
        if app["name"].lower() == wanted:
            return app
    for app in installed:
        if wanted in app["name"].lower():
            return app
    return None


BLOCKED_ADVICE = (
    "Control is turned off on this Roku. On the television:\n"
    "  Settings > System > Advanced system settings > Control by mobile apps\n"
    "  > Network access > Enabled   (older software calls it Permissive)\n"
    "Recent Roku software ships this as Limited, which refuses outside control.\n"
    "Your phone controlling the Roku does not mean it is already on: Limited "
    "still allows Roku's own app and turns everything else away.\n"
    "This needs the physical remote - the menus cannot be walked with key "
    "presses while key presses are the thing being refused."
)


def can_control(ip: str) -> tuple[bool, str]:
    """Whether this Roku will accept commands, and what to do if it will not.

    Worth knowing before the user finds out mid-sentence. Queries always work,
    so a Roku that answers everything and then refuses every action looks
    broken rather than switched off - which is exactly how it was reported.

    Probes with Lit_ and a space: a real keypress, so it goes through the same
    permission check a command would, but it only types anything when an
    on-screen keyboard happens to be focused, and a space at that. Testing with
    Home would answer the question by yanking someone out of their programme,
    and an invalid key name is no test at all - the name is validated first, so
    it comes back 400 without the permission ever being consulted.
    """
    try:
        _request(ip, "keypress/Lit_%20", method="POST")
        return True, "control accepted"
    except RokuRefused as exc:
        return False, str(exc)
    except RokuUnavailable as exc:
        return False, str(exc)


def is_awake(ip: str) -> bool:
    """Whether the screen is on. Roku TVs report this; players do not."""
    try:
        return device_info(ip).get("power-mode", "PowerOn") == "PowerOn"
    except RokuUnavailable:
        return False


# --- configuration ----------------------------------------------------------

def address() -> str:
    """The Roku the user chose during setup."""
    from iris import config

    return config.ROKU_IP


def enabled() -> bool:
    from iris import config

    return config.ROKU and bool(config.ROKU_IP)
