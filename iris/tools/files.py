"""Filesystem tools."""

import os
from datetime import datetime
from pathlib import Path

from anthropic import beta_tool

from iris import redact
from iris import memory
from iris.confirm import confirm
from iris.redact import scrubbed

MAX_READ_CHARS = 40_000


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


@beta_tool
@scrubbed
def list_files(directory: str, sort_by: str = "modified", limit: int = 20) -> str:
    """List the contents of a directory - folders and files - newest first.

    Use this to answer questions like "what did I download recently", to find a
    file the user referred to by name only, or to find which folder something
    lives in. Folders are listed first and marked as such.

    Args:
        directory: Absolute path or shorthand like ~/Downloads or %USERPROFILE%\\Desktop.
        sort_by: One of "modified", "created", "size", "name".
        limit: Maximum number of entries to return.
    """
    root = _expand(directory)
    if not root.is_dir():
        return f"Not a directory: {root}"

    keys = {
        "modified": lambda p: p.stat().st_mtime,
        "created": lambda p: p.stat().st_ctime,
        "size": lambda p: p.stat().st_size,
        "name": lambda p: p.name.lower(),
    }
    if sort_by not in keys:
        return f"sort_by must be one of {list(keys)}, got {sort_by!r}"

    try:
        everything = list(root.iterdir())
    except PermissionError:
        return f"Permission denied reading {root}"

    def _sort_key(p):
        try:
            return keys[sort_by](p)
        except OSError:  # a broken link or a file that vanished mid-listing
            return 0

    reverse = sort_by != "name"
    folders = sorted((p for p in everything if p.is_dir()), key=_sort_key, reverse=reverse)
    files = sorted((p for p in everything if p.is_file()), key=_sort_key, reverse=reverse)

    # Folders first, and never crowded out by the limit. Sorting everything by
    # modification time together meant a folder someone wanted to navigate into
    # sat below thirty recent downloads and was invisible.
    entries = (folders + files)[: max(1, limit)]
    if not entries:
        return f"{root} is empty."

    lines = [f"{len(entries)} item(s) in {root}:"]
    for p in entries:
        try:
            stat = p.stat()
            when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            lines.append(f"  {p.name}")
            continue
        size = "folder" if p.is_dir() else f"{stat.st_size:,} bytes"
        name = f"{p.name}\\" if p.is_dir() else p.name
        lines.append(f"  {name}  |  {size}  |  modified {when}")

    remaining = len(everything) - len(entries)
    if remaining > 0:
        lines.append(f"[{remaining} more not shown - raise limit to see them]")
    return "\n".join(lines)


@beta_tool
@scrubbed
def search_files(pattern: str, root: str = "~", max_results: int = 25) -> str:
    """Find files anywhere under a folder by glob pattern.

    Use this when the user names a file but not its location, e.g. "hello.txt".

    Args:
        pattern: Glob pattern such as "hello.txt", "*.pdf", "report*.docx".
        root: Folder to search under. Defaults to the user's home directory.
        max_results: Stop after this many matches.
    """
    base = _expand(root)
    if not base.is_dir():
        return f"Not a directory: {base}"

    skip = {"AppData", "node_modules", ".git", "$Recycle.Bin", "Windows", "__pycache__"}
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for match in Path(dirpath).glob(pattern):
            if match.is_file():
                found.append(match)
                if len(found) >= max_results:
                    break
        if len(found) >= max_results:
            break

    if not found:
        return f"No files matching {pattern!r} under {base}"
    return f"{len(found)} match(es):\n" + "\n".join(f"  {p}" for p in found)


@beta_tool
@scrubbed
def read_file(path: str) -> str:
    """Read a text file and return its contents.

    Args:
        path: Path to the file.
    """
    target = _expand(path)
    if not target.is_file():
        return f"No such file: {target}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {target}: {exc}"
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n\n[truncated at {MAX_READ_CHARS} characters]"

    # In a .env, a private key, or a credentials file, every value is a secret
    # regardless of whether it matches a known format. Invert the rule for
    # those: hide all of them, and let the variable names carry the meaning.
    if redact.is_sensitive_file(target):
        return (
            f"[{target.name} holds credentials; values are hidden behind placeholders. "
            "Use copy_to_clipboard to use one, or reveal_redacted to show it.]\n"
            + redact.scrub_sensitive_file(text)
        )
    return text or "[file is empty]"


@beta_tool
@confirm("confirm")
def edit_file(path: str, old_text: str, new_text: str, purpose: str = "") -> str:
    """Replace an exact piece of text inside a file.

    Read the file first so old_text matches byte for byte. If old_text appears
    more than once this fails rather than guessing which one was meant.

    Args:
        path: Path to the file to edit.
        old_text: The exact existing text to replace.
        new_text: The text to put in its place.
        purpose: Plain English for the change, read aloud when asking
            permission, e.g. "change the greeting in hello.txt".
    """
    if memory.is_protected(path):
        return (
            "That file is my memory and my transcript. The transcript is append-only - "
            "I can read it but not change it, on purpose, because a record that could be "
            "rewritten on request would not be worth keeping. Memories change through "
            "remember and forget instead. Nothing was written. Tell the user plainly "
            "that I am not able to edit it."
        )
    target = _expand(path)
    if not target.is_file():
        return f"No such file: {target}"

    original = target.read_text(encoding="utf-8", errors="replace")
    count = original.count(old_text)
    if count == 0:
        return f"{old_text!r} does not appear in {target.name}. Read the file and retry with exact text."
    if count > 1:
        return f"{old_text!r} appears {count} times in {target.name}. Include more surrounding text to make it unique."

    target.write_text(original.replace(old_text, new_text), encoding="utf-8")
    return f"Replaced 1 occurrence in {target}."


@beta_tool
@confirm("confirm")
def write_file(path: str, content: str, purpose: str = "") -> str:
    """Create a file, or overwrite one that already exists.

    To change part of an existing file prefer edit_file, which will not clobber
    the rest of it.

    Args:
        path: Path to write to.
        content: Full contents of the file.
        purpose: Plain English for what is being written and why, read aloud
            when asking permission, e.g. "save your notes to a new file".
    """
    if memory.is_protected(path):
        return (
            "That file is my memory and my transcript. The transcript is append-only - "
            "I can read it but not change it, on purpose, because a record that could be "
            "rewritten on request would not be worth keeping. Memories change through "
            "remember and forget instead. Nothing was written. Tell the user plainly "
            "that I am not able to edit it."
        )
    target = _expand(path)
    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Created'} {target} ({len(content):,} characters)."


TOOLS = [list_files, search_files, read_file, edit_file, write_file]
