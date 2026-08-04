"""Things Iris remembers between sessions.

Without this she starts every conversation knowing nothing about you - not your
name, not which games you play, not where your projects live. The sliding
context window covers the last few commands and then forgets, so anything you
told her yesterday is gone.

Stored as JSON next to the program rather than as prose in a markdown list,
because a memory has to be *findable* to be correctable. "The user's name is
Bob" replacing "The user's name is Clara" only works if the two are
recognisably about the same thing, and a bullet list gives nothing to match on.
So every entry carries a short topic, and saving the same topic again replaces
what was there. That is the whole difference between a memory and a log.

The file is still yours to open and edit; JSON is about as readable as markdown
for a list of short sentences, and it survives being written by both of us.

Credentials are deliberately refused. A memory file is long-lived plaintext on
disk and is read into the model's context on every single request, which is the
worst possible place to keep a password.
"""

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path

MAX_MEMORIES = 200
MAX_CHARS = 8000
VERSION = 1

# The transcript lives in the same file, so every entry rewrites it. That is
# fine at this size and would not be at ten times it, which is what the cap is
# for: old turns fall off the front rather than the file growing forever.
MAX_TRANSCRIPT = 1200
MAX_ENTRY = 4000

# One file, written from the main thread (replies), the tool worker thread
# (tool results) and the memory tools. Every change is read-modify-write, so
# they have to take turns or one will overwrite another's.
_lock = threading.RLock()

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the a an is are was were be been his her their my your our its of to for in on at "
    "and or but that this these those it they he she we i you user users prefers likes "
    "has have had do does did with from by as if then than so".split()
)


def path() -> Path | None:
    """Where the memories live: in the install folder, unless told otherwise."""
    from iris import config, paths

    if not config.MEMORY_FILE:
        return None
    target = Path(config.MEMORY_FILE)
    if not target.is_absolute():
        target = paths.data_dir() / target
    return target


# --- the store ------------------------------------------------------------


def is_protected(candidate) -> bool:
    """True if this path is the store itself.

    Used by the file and shell tools to refuse writing to it. Memories change
    through remember and forget; the transcript is append-only and changes
    through neither.
    """
    target = path()
    if target is None or not candidate:
        return False
    try:
        return Path(str(candidate)).expanduser().resolve() == target.resolve()
    except (OSError, ValueError):
        return False


def _read() -> dict:
    target = path()
    if target is None or not target.is_file():
        return {"version": VERSION, "memories": [], "transcript": []}
    try:
        # utf-8-sig, not utf-8: Notepad and PowerShell both write a byte order
        # mark, and json.loads treats those three bytes as a syntax error. The
        # file is meant to be editable by hand, so it has to survive the
        # editors people actually have.
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        # A corrupt file must not take the assistant down with it, and must
        # not be silently overwritten either - it is the only copy.
        return {"version": VERSION, "memories": [], "transcript": [], "unreadable": True}
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        return {"version": VERSION, "memories": [], "transcript": [], "unreadable": True}
    data.setdefault("transcript", [])
    if not isinstance(data["transcript"], list):
        data["transcript"] = []
    return data


def _write(data: dict) -> None:
    target = path()
    if target is None:
        return
    data["version"] = VERSION
    data["memories"] = data["memories"][-MAX_MEMORIES:]
    data["transcript"] = data.get("transcript", [])[-MAX_TRANSCRIPT:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- the transcript -------------------------------------------------------


def append_transcript(kind: str, text: str) -> None:
    """Add one entry. Append-only by design, and never raises.

    Nothing in the codebase edits or removes an entry, and the tools are
    blocked from writing to this file at all, so what is here is what actually
    crossed to the model. A record that could be revised on request would not
    be worth keeping.
    """
    if path() is None or text is None:
        return

    body = str(text)
    if len(body) > MAX_ENTRY:
        body = body[:MAX_ENTRY] + f"... [{len(body) - MAX_ENTRY} more characters]"

    entry = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "who": kind,
        "text": body,
    }
    try:
        with _lock:
            data = _read()
            if data.get("unreadable"):
                return  # never overwrite a file we could not parse
            data["transcript"].append(entry)
            _write(data)
    except OSError:
        pass  # logging must not break a command


def transcript_entries(limit: int = 40) -> list[dict]:
    """The most recent entries, oldest first."""
    entries = _read().get("transcript", [])
    return entries[-max(1, limit):]


def _topic_for(text: str) -> str:
    """A short handle for what a memory is about, when none was given.

    Only a fallback. A topic chosen by whoever wrote the memory will group
    better than one derived from its wording, which is why the tool asks for
    one - but a derived handle still beats no handle at all.
    """
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]
    return "-".join(words[:3]) or "general"


# --- what the model sees --------------------------------------------------


def load() -> str:
    """The memories as a block for the system prompt, or '' if there are none.

    Topics are shown, not hidden: they are how a memory gets corrected later,
    and she cannot reuse one she was never told about.
    """
    entries = _read()["memories"]
    if not entries:
        return ""
    block = "\n".join(f"- [{e.get('topic', 'general')}] {e.get('text', '')}" for e in entries)
    if len(block) > MAX_CHARS:
        block = block[:MAX_CHARS] + "\n- [older memories truncated]"
    return block


def looks_like_secret(text: str) -> str | None:
    """Return why this must not be stored, or None if it is fine."""
    from iris import redact

    if redact.scrub(text) != text:
        return "it contains something that looks like a password, key or personal detail"
    if re.search(r"(?i)\b(password|passcode|pin|api key|secret|token|cvv|card number)\b", text):
        return "it mentions a credential"
    return None


# --- changing what is remembered ------------------------------------------


def remember(fact: str, topic: str = "") -> str:
    """Save a memory, or replace the one already filed under this topic."""
    fact = " ".join(fact.split()).strip(" .")
    if not fact:
        return "Nothing to remember."
    if path() is None:
        return "Memory is switched off (IRIS_MEMORY is blank in .env)."

    reason = looks_like_secret(fact)
    if reason:
        return (
            f"I will not save that, because {reason}. The memory file is plain text on "
            "disk and is read into my context on every request, so it is the wrong place "
            "for anything sensitive."
        )

    with _lock:
        data = _read()
        if data.get("unreadable"):
            return (
                f"The memory file at {path()} could not be read, so I have not written to "
                "it rather than risk overwriting it. Worth a look."
            )

        slug = _topic_for(topic or fact)
        today = f"{date.today():%Y-%m-%d}"

        for entry in data["memories"]:
            if entry.get("topic") == slug:
                previous = entry.get("text", "")
                if previous.lower() == fact.lower():
                    return f"Already remembered: {fact}"
                entry["text"] = fact
                entry["updated"] = today
                _write(data)
                return f"Updated what I knew about {slug}: was {previous!r}, now {fact!r}."

        data["memories"].append(
            {"topic": slug, "text": fact, "created": today, "updated": today}
        )
        _write(data)
        return f"Remembered under {slug}: {fact}"


def forget(about: str) -> str:
    about = about.strip().lower()
    if not about:
        return "Say what to forget."

    with _lock:
        data = _read()
        if data.get("unreadable"):
            return f"The memory file at {path()} could not be read, so nothing was changed."

        keep = [
            e for e in data["memories"]
            if about not in e.get("topic", "").lower()
            and about not in e.get("text", "").lower()
        ]
        removed = len(data["memories"]) - len(keep)
        if not removed:
            return f"Nothing remembered about {about!r}."

        data["memories"] = keep
        _write(data)
        return f"Forgot {removed} thing{'s' if removed > 1 else ''} about {about!r}."


def listing() -> str:
    data = _read()
    if data.get("unreadable"):
        return f"The memory file at {path()} could not be read."
    entries = data["memories"]
    if not entries:
        return "I have not been asked to remember anything yet."
    lines = [
        f"  [{e.get('topic', 'general')}] {e.get('text', '')}  (updated {e.get('updated', '?')})"
        for e in entries
    ]
    return f"{len(entries)} thing(s) remembered:\n" + "\n".join(lines)


# --- moving on from the old format ----------------------------------------


def migrate_markdown(old: Path) -> int:
    """Bring bullets from an older memories.md across. Returns how many moved."""
    if not old.is_file():
        return 0
    facts = [
        line.strip()[2:].strip()
        for line in old.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ") and len(line.strip()) > 2
    ]
    moved = 0
    for fact in facts:
        # Old entries carried a trailing "(YYYY-MM-DD)"; drop it, the store
        # keeps its own dates now.
        fact = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*$", "", fact).strip()
        if fact and not fact.startswith("["):
            remember(fact)
            moved += 1
    return moved
