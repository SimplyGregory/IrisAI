"""A record of everything that crosses to the model.

Kept inside memory.json, under "transcript", alongside what Iris remembers -
one file holding everything she carries between sessions rather than two that
have to be found and moved together.

    {"at": "2026-08-03 18:47:40", "who": "user",  "text": "what time is it"}
    {"at": "2026-08-03 18:47:41", "who": "iris",  "text": "Twelve past seven."}

What gets written is what the model actually receives - after redaction. So the
record shows "[secret DB_PASSWORD]", never the password itself. That keeps it
safe to read, share and keep, and it doubles as a way to audit exactly what
left the machine.

Append-only, deliberately. Nothing here removes or rewrites an entry, and the
file tools refuse to write to the store at all, so asking Iris to amend the
record gets a refusal rather than an edit. A log that could be revised on
request would not be worth keeping.
"""

from pathlib import Path

from iris import memory


def path() -> Path | None:
    """The file the record lives in - the same one the memories live in."""
    from iris import config

    return memory.path() if config.TRANSCRIPT else None


def write(kind: str, text: str) -> None:
    """Append one entry. Never raises - logging must not break a command."""
    from iris import config

    if not config.TRANSCRIPT:
        return
    memory.append_transcript(kind, text)


def separator(command: str) -> None:
    """Kept for the callers that mark a turn boundary.

    A blank line was how the old text file showed where one command ended and
    the next began. Entries carry a timestamp and a speaker now, so the
    boundary is already visible and there is nothing to write.
    """


def recent(limit: int = 40) -> list[dict]:
    """The last few entries, oldest first."""
    return memory.transcript_entries(limit)
