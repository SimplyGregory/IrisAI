"""The tool surface Claude sees.

Keep this list tight. Every tool's schema is re-sent on every API call in the
loop, so each one you add is a fixed token cost on all future requests. If this
grows past ~25 tools, switch to the tool-search tool and defer_loading instead
of shipping every schema every time.
"""

from iris import confirm as _confirm
from iris import editor as _editor
from iris import roku as _roku
from iris.redact import bind_variables
from iris.tools import (
    apps, browser, files, info, interaction, mic, myself, recall, roku,
    screen, shell, uia, vscode, windows,
)

# reveal_redacted takes a placeholder as its *subject*, so resolving its
# argument would hand it the answer and leave it looking the value up by itself.
_NO_VARIABLE_BINDING = {"reveal_redacted"}


def _gated(tool):
    """Put every tool behind the confirmation gate.

    @confirm marks the handful of genuinely destructive tools, and for a long
    time it was also the only thing that asked - which meant "ask before every
    single tool call" actually meant "ask before the ten I remembered to
    decorate". launch_app, the browser tools, screenshot and read_file all went
    through untouched no matter what the mode said.

    Gating here instead makes the mode mean what it says, and makes the
    decorator's job the narrower one it should always have had: saying which
    tools are dangerous enough to ask about even in "risk".

    Tools that carry their own level are left alone, so nothing is asked twice.
    """
    if getattr(tool.func, "__iris_level__", None) is not None:
        return tool

    original = tool.call

    def call(tool_input, *args, **kwargs):
        # Placeholders are still unresolved here, by design: the confirmation
        # prompt should show [email 1] rather than the address behind it.
        declined = _confirm.gate(tool.name, dict(tool_input or {}))
        if declined is not None:
            return declined
        return original(tool_input, *args, **kwargs)

    tool.call = call
    return tool


# Order matters: bind_variables wraps outermost so the barge-in check runs
# first, then the gate, then the tool. Being asked to approve something you
# have already interrupted would be the wrong way round.
ALL_TOOLS = [
    bind_variables(_gated(tool), enabled=tool.name not in _NO_VARIABLE_BINDING)
    for tool in [
        *files.TOOLS,
        *info.TOOLS,
        *mic.TOOLS,
        *shell.TOOLS,
        *apps.TOOLS,
        *windows.TOOLS,
        *browser.TOOLS,
        *screen.TOOLS,
        *uia.TOOLS,
        *interaction.TOOLS,
        *recall.TOOLS,
        *myself.TOOLS,
        # Only when the editor connection was turned on in setup. Two schemas
        # on every request is a real cost to carry for someone who does not use
        # VS Code, and the tools cannot work without the extension anyway.
        *(vscode.TOOLS if _editor.enabled() else []),
        # Same bargain as the editor above: two schemas on every request is
        # not worth carrying for someone who owns no Roku.
        *(roku.TOOLS if _roku.enabled() else []),
    ]
]

__all__ = [
    "ALL_TOOLS",
    "apps",
    "browser",
    "files",
    "info",
    "interaction",
    "mic",
    "myself",
    "recall",
    "screen",
    "shell",
    "roku",
    "uia",
    "vscode",
    "windows",
]
