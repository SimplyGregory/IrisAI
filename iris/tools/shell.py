"""The general-purpose escape hatch: run a shell command.

One tool on both platforms. The argv that runs a command string differs -
PowerShell on Windows, zsh on a Mac - and that is the platform layer's
business, not this file's. The detach heuristics below are PowerShell
idioms and simply never match a zsh command, which is the right behaviour
rather than a gap: nothing on a Mac hands off the way Start-Process does.
"""

import re
import subprocess

from anthropic import beta_tool

from iris import memory
from iris.confirm import confirm
from iris import redact
from iris.redact import scrubbed

MAX_OUTPUT_CHARS = 15_000

# Commands that hand work to another process and return straight away. The
# command finishing tells you nothing about whether the app has started, so a
# guessed pause is the only thing left - which is exactly what wait_for_window
# exists to replace.
# `start` is a real alias for Start-Process, but a bare \bstart\b also matches
# Start-Sleep, Start-Job and Start-Service, so it has to exclude the hyphenated
# cmdlets it is not.
_DETACHES = re.compile(
    r"\bstart-process\b|\bsaps\b|\binvoke-item\b|\bexplorer(\.exe)?\b|\bstart\b(?!-)",
    re.I,
)
_WAITS_ALREADY = re.compile(r"(^|\s)-Wait\b", re.I)


@beta_tool
@scrubbed
@confirm("confirm")
def run_shell(command: str, purpose: str, timeout_seconds: int = 600) -> str:
    """Run a shell command on the user's machine and return its output.

    The shell is PowerShell on Windows and zsh on macOS, so write the command
    for whichever this machine is - check with get_system_info if unsure.

    This always waits for the command to finish, however long it takes, so never
    follow it with a pause to "let it complete" - by the time you read this, it
    has completed. A long install or copy is fine; leave timeout_seconds alone
    unless you have a specific reason, since it is a safety net for a hung
    command rather than a guess at how long the work should take. The user can
    always interrupt a long command by speaking.

    The exception is a command that hands off to another process, like
    Start-Process: that returns immediately and the app carries on starting
    afterwards. Use wait_for_window to wait for the result of those.

    This is the fallback for anything the other tools do not cover: querying
    system state, managing processes, networking, installed software. Prefer a
    dedicated tool when one fits, because those are faster and safer.

    Args:
        command: The command to run, in this machine's shell.
        purpose: Plain English for what this does and why, as an action the
            user would recognise - "list the drives on this machine", "find
            where Chrome stores its bookmarks". This is read aloud when asking
            permission, so no jargon, no shell syntax, no file paths.
        timeout_seconds: Safety net only. Kill the command if it is still
            running after this long. Capped at one hour.
    """
    # The shell can reach any file, so the store is defended here too rather
    # than trusting that no command ever names it.
    if memory.path() and str(memory.path()) in command:
        return (
            "That command touches my memory and transcript file, which I am not able to "
            "write to. The transcript is append-only by design. Nothing was run. Say so "
            "plainly rather than trying another way round it."
        )
    import threading
    import time

    from iris import interrupt

    try:
        from iris import platform

        process = subprocess.Popen(
            platform.shell_argv(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Output comes back through the panel, so the console Windows would
            # otherwise open is a blank window with nothing to see in it. On a
            # Mac there is no window to suppress and this contributes nothing.
            **platform.quiet_process(),
        )
    except OSError as exc:
        return f"Could not run the shell: {exc}"

    # Drain both pipes on threads as the command runs. communicate() only hands
    # anything back once the process ends, so a command killed on timeout used
    # to lose everything it had printed - the least helpful moment to have no
    # output. Collecting as we go means a timeout still reports how far it got,
    # and lets us tell a wedged command from a slow but working one.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    last_output = [time.monotonic()]

    def pump(stream, sink: list[str]) -> None:
        total = 0
        try:
            for line in iter(stream.readline, ""):
                last_output[0] = time.monotonic()
                if total <= MAX_OUTPUT_CHARS * 4:
                    sink.append(line)
                    total += len(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    pumps = [
        threading.Thread(target=pump, args=(process.stdout, stdout_lines), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, stderr_lines), daemon=True),
    ]
    for thread in pumps:
        thread.start()

    def collected() -> tuple[str, str]:
        return "".join(stdout_lines), "".join(stderr_lines)

    def partial_note(reason: str) -> str:
        out, err = collected()
        so_far = "\n".join(c.strip() for c in (out, f"[stderr]\n{err}" if err.strip() else "") if c.strip())
        body = f"\nOutput before it stopped:\n{so_far}" if so_far else ""
        return reason + body

    # Poll rather than block, so barge-in works during a long command. Unlike a
    # thread, a subprocess really can be stopped - but a command killed halfway
    # may have already changed something, so say so rather than implying it
    # never ran.
    limit = max(1, min(int(timeout_seconds), 3600))
    deadline = time.monotonic() + limit
    while process.poll() is None:
        if interrupt.pending():
            held = interrupt.check("run_shell")
            if held is not None:
                process.kill()
                return partial_note(
                    f"{held}\nThe command was already running and has been stopped "
                    "part-way through, so whatever it had done so far still stands."
                )
        if time.monotonic() > deadline:
            # "Recently active" only means anything if it produced output at all;
            # otherwise the timer still reads as fresh and every silent command
            # looks busy.
            spoke = bool(stdout_lines or stderr_lines)
            still_working = spoke and time.monotonic() - last_output[0] < 10
            process.kill()
            return partial_note(
                f"Command hit the {limit}s safety timeout and was stopped part-way "
                "through. "
                + (
                    "It was still producing output when stopped, so it was working, "
                    "just slow - re-run it with a larger timeout_seconds."
                    if still_working
                    else "It produced no output at all, so it is either genuinely slow "
                    "or stuck waiting for input - and -NonInteractive means a prompt "
                    "would never be answered. Re-run with a larger timeout_seconds if "
                    "you expect it to be slow, otherwise rework the command so it "
                    "cannot prompt."
                )
            )
        time.sleep(0.1)

    for thread in pumps:
        thread.join(timeout=5)

    stdout, stderr = collected()
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    chunks = []
    if result.stdout.strip():
        chunks.append(result.stdout.strip())
    if result.stderr.strip():
        chunks.append(f"[stderr]\n{result.stderr.strip()}")
    if result.returncode != 0:
        chunks.append(f"[exit code {result.returncode}]")

    output = "\n".join(chunks) or "[no output]"

    # A detached launch "succeeding" says only that the handoff worked. Without
    # this the empty result reads as "done", and the only way to find out
    # whether the app actually appeared is to guess a pause.
    if _DETACHES.search(command) and not _WAITS_ALREADY.search(command):
        output += (
            "\n[This command handed off to another process and returned straight away. "
            "It does NOT mean the app has finished starting. Do not guess a pause - "
            "call wait_for_window with the title you expect, which returns the moment "
            "it is actually there.]"
        )
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[output truncated]"

    # read_file hides every value in a .env, but a shell command reading that
    # same file would sail straight past it: pattern matching cannot tell that
    # SESSION_ID=a1b2c3 is a secret, only that the file it came from is one.
    # Apply the same whole-file rule whenever the command touches such a file.
    if redact.mentions_sensitive_file(command):
        output = redact.scrub_sensitive_file(output)
    return output


TOOLS = [run_shell]
