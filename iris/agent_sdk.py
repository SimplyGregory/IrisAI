"""Alternative backend: run Iris on a Claude subscription instead of API credits.

The Claude Agent SDK drives the Claude Code CLI, which authenticates with your
claude.ai login. Usage draws on your Pro/Max plan's limits rather than being
billed per token through the Console.

Nothing about the tools changes. Every tool in iris/tools is reused exactly as
it is - same redaction, same confirmation gate, same variable binding - by
adapting each one into an in-process MCP tool rather than rewriting it. The only
difference is who runs the agent loop.

Trade-off worth remembering: this shares one usage pool with Claude Code and
claude.ai, so a busy Iris session eats capacity you might want elsewhere. The
API backend has no such contention; you just pay for it.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from iris import config, memory, redact, transcript
from iris.agent import Iris as _ApiIris
from iris.tools import ALL_TOOLS

# Every tool call runs on ONE dedicated thread, for two reasons: the tools are
# synchronous and would otherwise block the event loop, and Playwright's sync
# API binds its objects to the thread that created them. A normal thread pool
# would scatter calls across workers and break the browser session.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iris-tool")

SERVER_NAME = "iris"

_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def find_cli() -> str | None:
    """Locate the Claude Code CLI, including the copy inside the VS Code extension."""
    from shutil import which

    found = which("claude")
    if found:
        return found

    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        Path.home() / ".claude" / "local" / "claude.exe",
    ]
    extensions = Path.home() / ".vscode" / "extensions"
    if extensions.is_dir():
        # e.g. anthropic.claude-code-2.1.220-win32-x64/resources/native-binary/claude.exe
        candidates += sorted(
            extensions.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
            reverse=True,  # highest version first
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def use_subscription_auth() -> list[str]:
    """Stop an API key in .env from overriding the claude.ai login.

    The CLI resolves credentials in a fixed order and ANTHROPIC_API_KEY wins
    over an OAuth login. Since .env keeps a key around for the api backend,
    it would otherwise be picked up here and the run would fail with "invalid
    API key" - even a placeholder counts. Remove it from this process only;
    the file is untouched.
    """
    removed = []
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.pop(name, None) is not None:
            removed.append(name)
    return removed


def _to_content_blocks(result) -> list[dict]:
    """Convert a Iris tool's return value into MCP content blocks.

    Iris tools return either a plain string or Messages-API content blocks
    (screenshot does the latter). The image shape differs between the two APIs,
    so translate rather than pass through.
    """
    if isinstance(result, str):
        return [{"type": "text", "text": result}]

    blocks = []
    for block in result or []:
        if not isinstance(block, dict):
            blocks.append({"type": "text", "text": str(block)})
        elif block.get("type") == "image":
            source = block.get("source", {})
            blocks.append(
                {
                    "type": "image",
                    "data": source.get("data", ""),
                    "mimeType": source.get("media_type", "image/png"),
                }
            )
        else:
            blocks.append(block)
    return blocks or [{"type": "text", "text": "(no output)"}]


def _adapt(iris_tool):
    """Wrap one existing Iris tool as an Agent SDK tool, reusing its schema."""
    spec = iris_tool.to_dict()

    @tool(spec["name"], spec["description"], spec["input_schema"])
    async def handler(args, _tool=iris_tool):
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(_EXECUTOR, _tool.call, args or {})
        except Exception as exc:  # surface failures as tool results, not crashes
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "is_error": True,
            }
        return {"content": _to_content_blocks(result)}

    return handler


def build_server():
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[_adapt(t) for t in ALL_TOOLS],
    )


@dataclass
class Usage:
    """Mirrors agent.Usage so the entry points can print either backend."""

    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0

    def add_result(self, message: ResultMessage) -> None:
        self.api_calls += getattr(message, "num_turns", 0) or 0
        self.cost += getattr(message, "total_cost_usd", 0.0) or 0.0
        usage = getattr(message, "usage", None) or {}
        if isinstance(usage, dict):
            self.input_tokens += usage.get("input_tokens", 0) or 0
            self.output_tokens += usage.get("output_tokens", 0) or 0
            self.cache_read_tokens += usage.get("cache_read_input_tokens", 0) or 0

    def summary(self) -> str:
        # total_cost_usd is what this turn *would* have cost at API rates. On a
        # subscription nothing is charged for it, so label it plainly rather
        # than letting it read like a bill.
        return (
            f"{self.api_calls} turn(s), in {self.input_tokens:,} "
            f"(+{self.cache_read_tokens:,} cached) / out {self.output_tokens:,}  "
            f"[free on your plan; ~${self.cost:.4f} at API rates]"
        )


@dataclass
class IrisSDK:
    """Same interface as agent.Iris, so the entry points need no changes."""

    session_usage: Usage = field(default_factory=Usage)
    last_usage: Usage = field(default_factory=Usage)
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=_ApiIris.system_text(),
            mcp_servers={SERVER_NAME: build_server()},
            # Only Iris's own tools. Without this the SDK also offers its
            # built-in Read/Write/Bash, which overlap with ours and muddy
            # tool selection.
            tools=[],
            allowed_tools=[f"mcp__{SERVER_NAME}__*"],
            # Iris has its own confirmation gate inside the tools, so the
            # SDK's separate permission prompt would ask twice.
            permission_mode="bypassPermissions",
            model=config.MODEL,
            cli_path=config.CLI_PATH or find_cli(),
            # Tool results travel as newline-delimited JSON over a pipe, and the
            # 1 MB default kills the whole session when one result is too big.
            # Screenshots are capped separately; this is headroom for anything
            # else large, such as a long file or a wide directory listing.
            max_buffer_size=16 * 1024 * 1024,
            # Emit the reply in fragments as it is written, so a caller that
            # wants to show it arriving can. Costs nothing to the callers that
            # do not: the extra messages are simply ignored.
            include_partial_messages=True,
            # How hard to think before acting, and whether to show its
            # reasoning at all. Both are fixed when the client is built, so
            # changing them mid-session means a new one - see set_model for
            # the thing that can be changed in place.
            effort=config.EFFORT if config.EFFORT in _EFFORTS else None,
            thinking=(
                {"type": "enabled", "budget_tokens": config.THINKING_BUDGET}
                if config.THINKING
                else {"type": "disabled"}
            ),
        )

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    async def _ask(self, text: str, on_tool, on_text=None, on_result=None) -> str:
        if self._client is None:
            use_subscription_auth()
            self._client = ClaudeSDKClient(options=self._options())
            await self._client.connect()

        transcript.separator(text)
        transcript.write("user", text)
        await self._client.query(text)

        reply_parts: list[str] = []
        # A tool result identifies itself by the id of the call it answers, not
        # by name, so the names are kept as the calls go past.
        called: dict[str, str] = {}

        async for message in self._client.receive_response():
            if isinstance(message, StreamEvent):
                # The reply as it is being written, one fragment at a time.
                # Display only: the finished text still comes from the
                # AssistantMessage below, so nothing here affects the return
                # value or the transcript.
                if on_text:
                    event = message.event or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            on_text(delta["text"])
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    kind = getattr(block, "type", None) or type(block).__name__
                    if "ToolUse" in type(block).__name__:
                        name = getattr(block, "name", "?").replace(f"mcp__{SERVER_NAME}__", "")
                        called[getattr(block, "id", "")] = name
                        if on_tool:
                            on_tool(name, getattr(block, "input", {}) or {})
                    elif kind == "text" or type(block).__name__ == "TextBlock":
                        text_value = getattr(block, "text", "")
                        if text_value.strip():
                            reply_parts.append(text_value.strip())
            elif on_result and isinstance(message, UserMessage):
                # What each tool actually returned. Claude reads these and
                # adapts, which is how a failed approach becomes a working one
                # - but until now nobody watching had any idea a call had
                # failed, so an approved action that errored twice looked
                # exactly like one that did nothing.
                for block in message.content if isinstance(message.content, list) else []:
                    if "ToolResult" not in type(block).__name__:
                        continue
                    content = getattr(block, "content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            part.get("text", "") for part in content if isinstance(part, dict)
                        )
                    on_result(
                        called.get(getattr(block, "tool_use_id", ""), "?"),
                        str(content or ""),
                        bool(getattr(block, "is_error", False)),
                    )

            elif isinstance(message, ResultMessage):
                self.last_usage.add_result(message)
                self.session_usage.add_result(message)
                if message.result and message.result.strip():
                    reply = message.result.strip()
                    reply, withheld = redact.guard_reply(reply)
                    if withheld:
                        reply += (
                                        f" I held back {len(withheld)} redacted detail"
                                        f"{'s' if len(withheld) > 1 else ''} there - ask me to reveal it if you want it."
                        )
                    transcript.write(config.ASSISTANT_NAME.lower(), reply)
                    return reply

        return " ".join(reply_parts).strip() or "Done."

    def send(self, user_text: str, on_tool=None, on_text=None, on_result=None) -> str:
        """Run one command.

        on_text receives the reply as it is written, and on_result receives what
        each tool handed back. Both optional: text and voice mode want the
        finished reply and nothing else, and ignore them.
        """
        self.last_usage = Usage()
        return self._ensure_loop().run_until_complete(
            self._ask(user_text, on_tool, on_text, on_result)
        )

    def interrupt(self) -> bool:
        """Stop the reply mid-sentence. Safe to call from another thread.

        The event loop is busy inside run_until_complete on the agent's own
        thread, so the coroutine is handed to it rather than awaited here.
        """
        if self._client is None or self._loop is None or not self._loop.is_running():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._loop)
            future.result(timeout=5)
            return True
        except Exception:
            return False

    def set_model(self, model: str) -> bool:
        """Switch model without losing the conversation.

        Effort and thinking have no equivalent - they are fixed when the
        client is built, so changing those means starting a new session.
        """
        config.MODEL = model
        if self._client is None or self._loop is None or not self._loop.is_running():
            return True  # nothing connected yet; the next client picks it up
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._client.set_model(model), self._loop
            )
            future.result(timeout=5)
            return True
        except Exception:
            return False

    def reset(self) -> None:
        if self._client is not None:
            try:
                self._ensure_loop().run_until_complete(self._client.disconnect())
            except Exception:
                pass
            self._client = None
        redact.reset()
