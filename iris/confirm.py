"""Harness-level confirmation gate.

Confirmation happens here, between Claude asking for a tool and the tool
actually running. That matters for cost: making the *model* ask permission
would burn a full extra API round trip per confirmation. This is free.

A declined action returns an ordinary tool result string, not an exception,
so Claude reads "user declined", adapts, and carries on.
"""

import functools
import inspect
from pathlib import Path
from typing import Callable, Literal

from iris import config

# silent   never asks, whatever the mode. For things that are Iris's own
#          housekeeping rather than actions taken on the user's machine.
# announce asked about in "all" only. The default for anything undeclared.
# confirm  asked about in "all" and "risk". Destructive or irreversible.
Level = Literal["silent", "announce", "confirm"]
Answer = Literal["yes", "no", "always"]

# Tools the user said "always" to. Cleared on restart.
_session_approved: set[str] = set()


def _console_asker(question: str, detail: str = "") -> Answer:
    print(f"\n  [?] {question}")
    if detail:
        for line in detail.splitlines():
            print(f"      {line}")
    reply = input("      [y]es / [n]o / [a]lways: ").strip().lower()
    if reply.startswith("a"):
        return "always"
    if reply.startswith("y") or reply == "":
        return "yes"
    return "no"


def _console_question(question: str) -> str:
    print(f"\n  [?] {question}")
    return input("      > ").strip()


_asker: Callable[[str], Answer] = _console_asker
_question_asker: Callable[[str], str] = _console_question


def set_asker(fn: Callable[[str], Answer]) -> None:
    """Swap in a different confirmation channel (e.g. speak-and-listen)."""
    global _asker
    _asker = fn


def set_question_asker(fn: Callable[[str], str]) -> None:
    """Swap in a different channel for open-ended questions."""
    global _question_asker
    _question_asker = fn


def ask_question(question: str) -> str:
    return _question_asker(question)


def reset_session_approvals() -> None:
    _session_approved.clear()


def _describe(fn_name: str, args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
    return f"{fn_name}({', '.join(parts)})"


def _phrase(fn_name: str, args: dict) -> tuple[str, str]:
    """Build (what to say out loud, what to print).

    Reading a raw PowerShell command aloud is useless - "dollar p equals dollar
    env local app data" tells you nothing about whether to approve it. So the
    gated tools take a `purpose` argument that Claude fills in with plain
    English, and that is what gets spoken. The literal command is still printed
    in full underneath, so the terminal shows exactly what will run.
    """
    from iris import config

    purpose = str(args.get("purpose") or "").strip().rstrip(".")
    detail_args = {k: v for k, v in args.items() if k != "purpose"}

    if purpose:
        spoken = f"I'd like to {purpose}. Is that okay?"
    else:
        spoken = f"I'd like to run {fn_name}. Is that okay?"

    lines = [f"{config.ASSISTANT_NAME} wants to run: {fn_name}"]
    for key, value in detail_args.items():
        text = str(value)
        if len(text) > 400:
            text = text[:400] + " ...[truncated]"
        lines.append(f"  {key} = {text}")
    return spoken, "\n".join(lines)


DECLINED = (
    "The user declined this action. Do not retry it. "
    "Either take a different approach or tell the user you stopped."
)

# File tools pointed at Iris's own memory file are memory operations, whatever
# their name says. Only these three, and only on an exact path match: a shell
# command that happens to mention the path is still an arbitrary shell command,
# so run_shell is deliberately not on this list.
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})


def _is_own_memory(fn_name: str, args: dict) -> bool:
    """True when a file tool is acting on Iris's own memory file."""
    if fn_name not in _FILE_TOOLS:
        return False

    from iris import memory

    target = memory.path()
    raw = args.get("path")
    if target is None or not isinstance(raw, str) or not raw.strip():
        return False
    try:
        return Path(raw).expanduser().resolve() == target.resolve()
    except (OSError, ValueError):
        return False

# What a tool is treated as when it does not declare a level. "announce" means
# it is asked about in "all" and passed through in "risk", which is the whole
# point of the two modes being different.
DEFAULT_LEVEL: Level = "announce"


def gate(fn_name: str, args: dict, level: Level = DEFAULT_LEVEL) -> str | None:
    """Ask the user, if this mode calls for it.

    Returns None to let the call proceed, or the message to hand back to the
    model instead of running it. Shared by the decorator below and by the
    blanket gate in iris/tools/__init__.py, so both ask the same question the
    same way and honour the same "always" answers.
    """
    # "silent" means never ask, in any mode - not "ask less often". There is a
    # real difference between acting on your machine and Iris keeping her own
    # notes: remembering that you are called Bob is not something you should
    # have to approve, and a prompt for each one turns a background habit into
    # a chore you would switch off. The same goes for touching the one file
    # those notes live in.
    if level == "silent" or _is_own_memory(fn_name, args):
        return None

    mode = config.CONFIRM_MODE
    needs_ask = mode == "all" or (mode == "risk" and level == "confirm")
    if not needs_ask or fn_name in _session_approved:
        return None

    spoken, detail = _phrase(fn_name, args)
    answer = _asker(spoken, detail)
    if answer == "always":
        _session_approved.add(fn_name)
        return None
    if answer == "yes":
        return None
    return DECLINED


def confirm(level: Level):
    """Declare a tool's risk level, and gate it at that level.

    Apply *under* @beta_tool so the schema is still generated from the real
    signature::

        @beta_tool
        @confirm("confirm")
        def run_shell(command: str) -> str: ...

    Only tools that are destructive need this. Everything else is gated
    centrally at DEFAULT_LEVEL, so forgetting to decorate a tool no longer
    means it escapes the gate entirely.
    """

    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            declined = gate(fn.__name__, dict(bound.arguments), level)
            if declined is not None:
                return declined
            return fn(*args, **kwargs)

        wrapper.__iris_level__ = level  # type: ignore[attr-defined]
        return wrapper

    return decorator
