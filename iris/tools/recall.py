"""Tools for remembering things across sessions."""

from anthropic import beta_tool

from iris import memory
from iris.confirm import confirm
from iris.redact import scrubbed


@beta_tool
@confirm("silent")
def remember(fact: str, topic: str = "") -> str:
    """Save something about the user so it survives a restart, or correct it.

    Use this whenever you learn something durable, without being asked. If they
    say "my name is Bob", remember it and use it from then on. If they ask you
    to load Roblox, that tells you Roblox is a game they play. Preferences,
    where their files live, which apps they use, how they want to be spoken to,
    what a nickname refers to: all worth keeping.

    Standing instructions belong here too, not just facts. "From now on always
    reply with X", "call me by my first name", "stop explaining what you did" -
    save it as you obey it, under a topic like "reply-style". An instruction you
    followed but did not save is one the user has to give you again tomorrow.

    This is for how you speak and what you know, not for what you are allowed to
    do. A memory cannot switch off a confirmation, so do not save one that
    claims to; if they want fewer prompts, the safety mode under the message box
    is the thing that changes it.

    The topic is how a memory gets corrected later. Saving a topic that already
    exists REPLACES it, so use the same topic when something you were told
    before turns out to be out of date or wrong. You are shown the topics you
    already have in your instructions - reuse one rather than inventing a
    near-duplicate.

    Do not use it for one-off details that will not matter tomorrow, and never
    for passwords or keys; it refuses those anyway.

    Args:
        fact: The thing to remember, as one short third-person sentence.
        topic: A short handle for what this is about, e.g. "name",
            "games", "work-hours". Reuse an existing topic to correct it.
    """
    return memory.remember(fact, topic)


@beta_tool
@confirm("silent")
def forget(about: str) -> str:
    """Remove remembered things that mention a word, phrase or topic.

    Use this when something is no longer true and there is nothing to replace
    it with. If there *is* a replacement, call remember with the same topic
    instead, which keeps the history tidier.

    Args:
        about: A word, phrase or topic; every memory matching it is deleted.
    """
    return memory.forget(about)


@beta_tool
@confirm("silent")
@scrubbed
def list_memories() -> str:
    """List everything currently remembered about the user, with topics.

    You are already given these in your instructions, so only call this when
    the user explicitly asks what you remember.
    """
    return memory.listing()


TOOLS = [remember, forget, list_memories]
