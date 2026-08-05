"""Sending a Discord message.

One tool, and a deliberately narrow one. Claude passes a name and a message and
gets back names and status - never a page, never a history, never an id. That
boundary is the whole point of the design, and it lives here, in what this
function chooses to return.

Registered only when the Discord connection is turned on, like the others.
"""

from anthropic import beta_tool

from iris import discord
from iris.confirm import confirm


@beta_tool
@confirm("confirm")
def discord_send(to: str, message: str, channel: str = "") -> str:
    """Send a Discord message, as the user, through their logged-in browser.

    This types into the real Discord web app the way the user would - it does
    not use the API or any token. If the user is not signed in, a window opens
    for them to log in once, and then it continues.

    You choose two things and nothing else: who to send to, by name, and what
    to say. You are given names to pick from when a name is unclear; you never
    see message history, other people's messages, or anything else on Discord.
    That is by design - do not try to work around it.

    Before sending, tell the user exactly what you are about to send and to
    whom, and only send if they agree - this reaches another person.

    Args:
        to: Who to send to, by name. A friend's name for a direct message, or a
            server's name. Say it the way the user did; close is fine, it is
            matched against what is actually there.
        message: What to send. This is the user's words, so send what they
            meant, not a paraphrase, unless they asked you to write it.
        channel: For a server, which channel, by name - e.g. "general". Not
            needed for a direct message to a friend.
    """
    if not to.strip():
        return "Say who to send it to."
    if not message.strip():
        return "There is nothing to send - the message is empty."

    try:
        discord.ensure_login(on_note=lambda _m: None)
        page = discord._page()
        where = discord.resolve(page, to, channel)
    except discord.NotLoggedIn as exc:
        return str(exc)
    except discord.DiscordUnavailable as exc:
        return f"Discord is not reachable. {exc}"

    # Everything below returns names or status only. No id, no page content,
    # no history ever crosses back to where Claude can read it.
    if "choices" in where:
        return (
            f"More than one thing matches {to!r}: "
            + ", ".join(where["choices"])
            + ". Which one?"
        )
    if "channels" in where:
        return (
            f"{where['guild']} has these channels: "
            + ", ".join(where["channels"])
            + f". Which should the message go in? Ask again with channel set."
        )
    if "error" in where:
        return where["error"]

    try:
        discord.send(page, where["url"], message)
    except discord.DiscordUnavailable as exc:
        return f"Could not send it. {exc}"

    target = f"{to} in #{channel}" if channel else to
    return f"Sent to {target}."


TOOLS = [discord_send]
