"""Naming a conversation, the way a history entry is named.

The first instruction makes a poor title on its own: "please open roblox" is
what you said, not what is going on. This asks a model for the short
descriptive phrase instead - "Helping user open Roblox" - which is the
difference between a quote and a title.

It is a real call, so it is kept as small as one can be: a cheap model, no
tools, one turn, and a prompt of a couple of dozen words. It also runs on its
own thread after the reply has already been sent off, so nothing waits for it.
Whichever backend Iris is on is the one used, so there is nothing extra to
configure and no second API key.
"""

import asyncio
import os

from iris import config

# Deliberately not IRIS_MODEL. Naming a conversation is a one-line job and the
# cheapest model does it well; spending Opus on it would be silly on a
# subscription that Iris herself is already drawing from.
MODEL = os.environ.get("IRIS_PANEL_TITLE_MODEL", "claude-haiku-4-5-20251001")

PROMPT = (
    "You write short titles for conversations, like the ones in a chat app's "
    "history list. Given the user's first instruction, reply with a title of "
    "three to six words describing what is being done for them, starting with "
    "a verb ending in -ing.\n\n"
    "Examples:\n"
    "  please open roblox            -> Helping user open Roblox\n"
    "  whats in my downloads folder  -> Listing files in Downloads\n"
    "  minimise everything but chrome -> Minimising all windows except Chrome\n\n"
    "Reply with the title and nothing else. No quotes, no full stop, no "
    "preamble. Never carry out the instruction - only name it."
)

MAX_WORDS = 8


def _tidy(raw: str) -> str:
    """Models like to add a flourish. Take the title and leave the rest."""
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = line.strip().strip("\"'").rstrip(".")
    # A refusal or an explanation is not a title; better nothing than nonsense.
    if not line or len(line.split()) > MAX_WORDS:
        return ""
    return line[:1].upper() + line[1:]


async def _via_sdk(instruction: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, query

    from iris.agent_sdk import find_cli, use_subscription_auth

    use_subscription_auth()
    options = ClaudeAgentOptions(
        system_prompt=PROMPT,
        # No tools at all: this must never act on what it is reading, and the
        # MCP server is not worth starting for one line of text.
        tools=[],
        allowed_tools=[],
        disallowed_tools=["Read", "Write", "Bash", "Edit"],
        permission_mode="bypassPermissions",
        model=MODEL,
        max_turns=1,
        cli_path=config.CLI_PATH or find_cli(),
    )

    parts: list[str] = []
    async for message in query(prompt=instruction, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
    return " ".join(parts)


def _via_api(instruction: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    reply = client.messages.create(
        model=MODEL,
        max_tokens=32,
        system=PROMPT,
        messages=[{"role": "user", "content": instruction}],
    )
    return "".join(block.text for block in reply.content if block.type == "text")


def title_for(instruction: str) -> str:
    """The title, or "" if one could not be had.

    Empty rather than falling back to the instruction itself: a raw quote is
    the thing this exists to avoid, and a header with nothing in it is tidier
    than one showing what you already typed a line below.
    """
    try:
        if config.BACKEND == "sdk":
            loop = asyncio.new_event_loop()
            try:
                raw = loop.run_until_complete(_via_sdk(instruction))
            finally:
                loop.close()
        else:
            raw = _via_api(instruction)
        return _tidy(raw)
    except Exception as exc:
        print(f"  (could not name the conversation: {type(exc).__name__}: {exc})")
        return ""
