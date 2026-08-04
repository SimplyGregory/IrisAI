"""Talking back to the user mid-task.

The per-tool confirmation gate in confirm.py is mechanical: it fires on tools
declared destructive, and knows nothing about what an action means. Clicking a
button is just a click as far as the harness can tell, so it cannot know that
this particular click says "Send" on it.

This tool closes that gap by letting Claude, which does understand intent, stop
and ask before doing something outward-facing or irreversible.
"""

from anthropic import beta_tool

from iris import redact
from iris.confirm import ask_question, confirm
from iris.redact import scrubbed


@beta_tool
def ask_user(question: str) -> str:
    """Ask the user something and wait for their answer.

    Call this BEFORE any action that other people will see, that spends money,
    or that cannot be undone: sending an email or message, posting publicly,
    making a purchase, or deleting anything. Summarise exactly what you are
    about to do and wait for approval.

    Also use it when a request is genuinely ambiguous and looking around has not
    resolved it, rather than guessing.

    Args:
        question: What to ask. Be specific and quote the details you are about
            to act on, e.g. "Send an email to hello@gmail.com with the subject
            'pizza' and the message 'food'?"
    """
    answer = ask_question(question)
    return f"The user replied: {answer!r}" if answer else "The user did not answer."


@beta_tool
@confirm("confirm")
def reveal_redacted(placeholder: str) -> str:
    """Reveal the real value behind a redaction placeholder.

    Personal details in tool output are replaced with placeholders like
    [email 1] so they do not leave the machine. Call this only when the user
    explicitly asks to see or hear one - "what is that email", "unredact it",
    "read it out". They are asked to approve before the value is revealed.

    If the user only wants the value *used* rather than spoken - copied to the
    clipboard, typed into a field - use copy_to_clipboard instead, which does
    not send the value anywhere.

    Args:
        placeholder: The tag exactly as it appeared, e.g. "[email 1]".
    """
    value = redact.lookup(placeholder)
    if value is None:
        known = ", ".join(redact.known_placeholders()) or "none"
        return f"No redacted value called {placeholder!r}. Known placeholders: {known}"

    # A revealed credential is a different order of harm from a revealed email,
    # so ask every time, even when confirmation is otherwise switched off.
    if placeholder.startswith("[secret"):
        answer = ask_question(
            f"Reveal the value of {placeholder} to the assistant? "
            "This sends the credential to the API. Say yes to allow."
        )
        if not answer.strip().lower().startswith(("y", "sure", "ok", "go")):
            return "The user declined to reveal this credential. Do not retry."

    redact.mark_revealed(placeholder)
    return f"{placeholder} is {value}"


@beta_tool
@scrubbed
def read_clipboard() -> str:
    """Read the current contents of the Windows clipboard.

    Only call this when the user wants to know *what* is on the clipboard, or
    when you need the content to make a decision. If they simply want it pasted
    somewhere, do not read it at all - focus the target window and send ctrl+v
    with screen_key. That moves the content without it passing through here.

    Credentials and personal details in the clipboard are replaced with
    placeholders, which you can still paste using copy_to_clipboard.
    """
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return "The clipboard does not currently hold text."
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return f"Could not read the clipboard: {exc}"

    if not text:
        return "The clipboard is empty."
    if len(text) > 8000:
        return text[:8000] + f"\n[clipboard truncated; {len(text):,} characters total]"
    return text


@beta_tool
def copy_to_clipboard(text: str) -> str:
    """Copy text to the Windows clipboard.

    Placeholders are resolved here on the machine, so "copy_to_clipboard
    ('[email 1]')" puts the real email on the clipboard without the value ever
    being sent to the API. Prefer this over reveal_redacted whenever the user
    wants to *use* a redacted value rather than hear it.

    Args:
        text: The text to copy. May contain placeholders like [email 1], which
            are substituted for their real values before copying.
    """
    resolved = text  # placeholders were already resolved at the tool boundary
    try:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, resolved)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        return f"Could not access the clipboard: {exc}"

    # Never echo the copied text back: it may be a resolved secret.
    return f"Copied {len(resolved)} characters to the clipboard."


TOOLS = [ask_user, reveal_redacted, read_clipboard, copy_to_clipboard]
