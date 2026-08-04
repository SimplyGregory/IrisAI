"""Strip secrets out of tool output before it is sent to the API.

Two independent layers protect against leaking credentials:

1. Structural, at the source. browser_snapshot never reads the value of a
   password field in the first place - see _SNAPSHOT_JS in tools/browser.py.
   This is the reliable layer, because it does not depend on recognising what
   a secret looks like.

2. Pattern matching, here. Catches secrets that appear as ordinary text: a key
   in a config file, a token in a URL, an API key echoed by a shell command.
   This layer is best-effort. It will not catch a password that looks like an
   ordinary word, so do not treat it as a guarantee.

Redactions are replaced with a visible marker rather than deleted, so Claude
can tell that something was there and reason about it without seeing it.
"""

import functools
import re

# Vendor key formats. These are high confidence: the prefixes are distinctive
# enough that a match is essentially never a false positive.
_TOKEN_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"), "anthropic key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "api key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "github token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github token"),
    (re.compile(r"\bAIza[A-Za-z0-9\-_]{30,}"), "google key"),
    (re.compile(r"\bya29\.[A-Za-z0-9\-_]{20,}"), "google oauth token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws key id"),
    (re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "private key"),
]

# "password = hunter2", "db_password: x", "api_key: abc123".
# The leading [\w.-]{0,24}? allows a prefixed name like db_password or
# STRIPE_SECRET, while still requiring the keyword to sit immediately before
# the assignment - so "my_token_count = 5" is left alone.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b([\w.\-]{0,24}?
       (?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key
          |auth[_-]?token|client[_-]?secret|private[_-]?key|credential))
    \s*["']?\s*[:=]\s*["']?
    (?!\[(?:redacted|secret|email|phone|card)\b)   # already a placeholder
    ([^\s"'&,;<>]{4,})
    """
)

# "Authorization: Bearer <anything>" - catches opaque tokens that match no
# recognisable vendor format.
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]{12,}=*)")

# Secrets written as prose rather than as an assignment: "the password is
# hunter2", "pin was 4417". Clipboard contents and chat logs are full of these,
# and the = based pattern above never sees them.
_PROSE_SECRET = re.compile(
    r"""(?ix)
    \b(password|passphrase|passcode|pin|api\ key|secret|token)
    (\s*:\s+|\s+(?:is|was|=)\s+)        # "pin: 4417" and "pin is 4417" alike
    ([^\s,.;!?"']{4,})
    """
)
# Words that follow "the password is" without being one.
_NOT_A_SECRET = frozenset(
    """wrong right correct incorrect invalid required optional empty blank
    set unset stored saved changed updated missing expired case-sensitive
    same different known unknown hidden visible here there""".split()
)

# Sensitive values carried in a URL - OAuth callbacks are the common case.
_URL_PARAM = re.compile(
    r"""(?ix)
    ([?&\#](?:password|passwd|token|access_token|id_token|refresh_token
             |api_key|apikey|key|secret|client_secret|code|auth|sid|sessionid
             |session|jwt|bearer|dsh|ifkv|tl|sig|signature|nonce|state)=)
    ([^&\s\#]+)
    """
)

# Naming every sensitive parameter is a losing game - a real sign-in URL
# carries ifkv=, TL=, dsh= and others no list would predict. So also redact any
# query value that simply *looks* like a token: long, random-looking, mixed
# letters and digits. A readable search query does not match this; an opaque
# credential almost always does.
_URL_TOKENISH = re.compile(
    r"(?i)([?&#][A-Za-z0-9_\-.]{1,24}=)"
    r"((?=[A-Za-z0-9_\-]*[A-Za-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,})"
)

# Except that some public identifiers look exactly like tokens: a YouTube
# channel id is twenty-four opaque characters and appears in the address bar of
# every channel page. Redacting one costs a working URL and buys nothing, since
# the value is on the public page anyway - and worse, the assistant then cannot
# see it is holding an ordinary id rather than a credential.
_PUBLIC_URL_PARAMS = frozenset(
    "channel_id channelid playlist_id playlistid list video_id videoid".split()
)


# --- personal information -------------------------------------------------
#
# Credentials above are replaced with an anonymous marker: Claude never needs
# the value back. Personal details are different - the user may well want them,
# so each one is replaced with a *referenceable* placeholder like [email 1] and
# the real value is kept here on the machine. Claude can then act on the
# placeholder (copy it to the clipboard) without the value ever reaching the
# API, or ask for it explicitly with reveal_redacted.

def _luhn_ok(text: str) -> bool:
    """True if these digits satisfy the Luhn checksum, as every real card does.

    Without this, any long digit run reads as a card number. Discord registers
    protocol handlers named discord-<18 digit app id>, and redacting those turns
    a usable deep link into "discord-[card number 1]". Luhn never rejects a
    genuine card - the checksum is part of the format - so this only removes
    false positives, and it lets the pattern widen to 13 digits to cover Amex
    and short Visa numbers that were previously missed entirely.
    """
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# (pattern, kind, validator) - the validator, when present, gets the final say on
# whether a match is really PII.
_PII_PATTERNS: list[tuple[re.Pattern, str, object]] = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b"), "email", None),
    (
        re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])"),
        "phone",
        None,
    ),
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "card number", _luhn_ok),
]

_vault: dict[str, str] = {}  # placeholder -> real value
_reverse: dict[str, str] = {}  # real value -> placeholder
_revealed: set[str] = set()  # placeholders the user actually approved revealing


def _already_tagged(value: str) -> bool:
    """Is this the start of a placeholder some earlier rule already wrote?

    The named rules produce tags with a space in them - [secret access_token] -
    and the URL rules that run afterwards stop at whitespace, so without this
    they bite the front off one and leave "[secret 2] access_token]" behind.
    That is worse than either outcome on its own: the value is still hidden,
    but the tag is now corrupt, so it can never be resolved back and every tool
    given it fails with a nonsense argument.
    """
    return value.lstrip().startswith("[")


def _placeholder_for(kind: str, value: str) -> str:
    """Give a value a stable placeholder, so the same email keeps the same tag."""
    if value in _reverse:
        return _reverse[value]
    number = sum(1 for key in _vault if key.startswith(f"[{kind} ")) + 1
    placeholder = f"[{kind} {number}]"
    _vault[placeholder] = value
    _reverse[value] = placeholder
    return placeholder


def _named_placeholder(kind: str, name: str, value: str) -> str:
    """Placeholder that keeps the variable's own name, e.g. [secret DB_PASSWORD].

    Far more useful than an anonymous marker: Claude can tell which secret is
    which, and pass the right one to copy_to_clipboard, without ever holding
    the value.
    """
    if value in _reverse:
        return _reverse[value]
    placeholder = f"[{kind} {name}]"
    suffix = 2
    while placeholder in _vault:  # same name, different value, e.g. two .env files
        placeholder = f"[{kind} {name} {suffix}]"
        suffix += 1
    _vault[placeholder] = value
    _reverse[value] = placeholder
    return placeholder


# Files where every value should be treated as a secret, whatever it looks like.
_SENSITIVE_NAMES = (
    ".env", "credentials", "id_rsa", "id_ed25519", "id_ecdsa", ".netrc",
    ".npmrc", ".pgpass", "secrets", ".htpasswd", "keyfile", ".git-credentials",
    "kube/config", "docker/config", ".pypirc", "service-account",
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk")

# Inside a sensitive file every value is hidden by default. These are the
# exceptions: settings whose value is a mode or a flag, never a credential.
# Deliberately short - anything not obviously inert stays hidden, because the
# cost of leaking a token that looked boring is far higher than the cost of
# hiding a boolean.
_INERT_VALUES = frozenset(
    """true false yes no on off none null nil enabled disabled
    debug info warn warning error trace fatal verbose silent
    development production staging test testing local sandbox
    localhost 127.0.0.1 0.0.0.0 utf-8 utf8""".split()
)
_INERT_NUMERIC = re.compile(r"^\d+(?:\.\d+)*$")  # ports, timeouts, version strings


def _is_inert(value: str) -> bool:
    return value.lower() in _INERT_VALUES or bool(_INERT_NUMERIC.match(value))

# Any NAME=VALUE line, used only inside sensitive files.
_ANY_ASSIGNMENT = re.compile(r"(?m)^([ \t]*(?:export[ \t]+)?)([A-Za-z_][\w.\-]*)([ \t]*[:=][ \t]*)(.+?)[ \t]*$")


def is_sensitive_file(path) -> bool:
    """True for files whose entire contents should be treated as credentials."""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
        return True
    return any(marker in name for marker in _SENSITIVE_NAMES)


def mentions_sensitive_file(command: str) -> bool:
    """True if a shell command looks like it reads a credentials file.

    read_file hides every value in a .env; a shell command reading the same
    file would otherwise walk straight past that, since pattern matching alone
    cannot tell that SESSION_ID=a1b2c3 is a secret. Erring towards redaction
    here is cheap - the worst case is an over-hidden config value.
    """
    lowered = command.lower().replace("\\", "/")
    if any(lowered.endswith(s) or f"{s} " in lowered or f"{s}'" in lowered or f'{s}"' in lowered
           for s in _SENSITIVE_SUFFIXES):
        return True
    return any(marker in lowered for marker in _SENSITIVE_NAMES)


def _mask_loose_lines(text: str) -> str:
    """Hide anything left on a line of its own inside a credentials file.

    The assignment pass below only sees `NAME=value`. A secret pasted onto a
    line by itself has no name to match, so it walked straight out of a .env
    untouched while every named value around it was masked - the one shape the
    inverted rule was meant to catch and the one it missed.

    Anything with a separator in it has already been handled, and comments,
    ini section headers, existing placeholders and inert values are left as
    they are so the file still reads.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        token = line.strip()
        if (
            not token
            or token.startswith(("#", ";", "//", "["))  # comment, section, placeholder
            or "=" in token
            or ":" in token
            or len(token) < 8
            or " " in token  # prose, not a credential
            or _is_inert(token)
        ):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = indent + _placeholder_for("secret", token)
    return "\n".join(lines)


def scrub_sensitive_file(text: str) -> str:
    """Replace every value in a config file with a named placeholder.

    Pattern matching alone is not enough here: a line like SESSION_ID=a1b2c3
    matches no known key format and no credential-ish name, but in a .env file
    it is a secret all the same. So inside these files the rule is inverted -
    hide everything, and let the name carry the meaning.
    """

    def replace(match: re.Match) -> str:
        indent, name, sep, value = match.groups()
        if not value or value.startswith("#") or value.lstrip("\"'").startswith("["):
            return match.group(0)  # blank, a comment, or already a placeholder
        quote = value[0] if value[:1] in "\"'" else ""
        bare = value.strip("\"'")
        if not bare or _is_inert(bare):
            return match.group(0)  # a flag or a port, not a credential
        tag = _named_placeholder("secret", name, bare)
        return f"{indent}{name}{sep}{quote}{tag}{quote}"

    return _mask_loose_lines(_ANY_ASSIGNMENT.sub(replace, text))


def lookup(placeholder: str) -> str | None:
    """Return the real value behind a placeholder, if we still hold it."""
    return _vault.get(placeholder.strip())


def resolve(text: str) -> str:
    """Substitute any placeholders in text back to their real values.

    Used by tools that act on a value locally - putting it on the clipboard,
    typing it into a field - so the plaintext never has to travel via the API.
    """
    for placeholder, value in _vault.items():
        text = text.replace(placeholder, value)
    return text


def known_placeholders() -> list[str]:
    return list(_vault)


def mark_revealed(placeholder: str) -> None:
    """Record that the user approved revealing this one."""
    _revealed.add(placeholder.strip())


def guard_reply(text: str) -> tuple[str, list[str]]:
    """Catch a redacted value that reappears in a reply without being revealed.

    Hiding a value keeps it out of the model's context, but it does not stop a
    capable model *deducing* it from what is left - a contact address on
    example.com is guessable as something@example.com even when the string
    itself was replaced. That reconstruction skips reveal_redacted entirely, so
    the user is never asked and nothing is logged.

    This is the enforcement rather than the request: whatever the model worked
    out, a value it was never given back cannot leave in the reply.
    """
    caught = []
    for placeholder, value in _vault.items():
        if placeholder in _revealed or not value or len(value) < 6:
            continue
        if value in text:
            text = text.replace(value, placeholder)
            caught.append(placeholder)
    return text, caught


def reset() -> None:
    _vault.clear()
    _reverse.clear()
    _revealed.clear()


def scrub(text: str) -> str:
    """Replace credentials with markers and personal details with placeholders."""
    if not text:
        return text

    # Secrets get referenceable placeholders too, so a value can be used
    # (copied, typed) without ever being revealed. Where there is a variable
    # name to hand, keep it: [secret DB_PASSWORD] beats [secret 3].
    for pattern, label in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: _placeholder_for("secret", m.group(0)), text)

    text = _ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}={_named_placeholder('secret', m.group(1), m.group(2))}", text
    )
    text = _URL_PARAM.sub(
        lambda m: m.group(0) if _already_tagged(m.group(2))
        else f"{m.group(1)}{_placeholder_for('secret', m.group(2))}", text
    )
    def _tokenish(match: re.Match) -> str:
        name = match.group(1).strip("?&#=").lower()
        if name in _PUBLIC_URL_PARAMS or _already_tagged(match.group(2)):
            return match.group(0)
        return f"{match.group(1)}{_placeholder_for('secret', match.group(2))}"

    text = _URL_TOKENISH.sub(_tokenish, text)
    text = _BEARER.sub(
        lambda m: m.group(0) if _already_tagged(m.group(2))
        else f"{m.group(1)}{_placeholder_for('secret', m.group(2))}", text
    )

    def _prose(match: re.Match) -> str:
        label, joiner, value = match.groups()
        if value.lower() in _NOT_A_SECRET or value.startswith("["):
            return match.group(0)
        return f"{label}{joiner}{_named_placeholder('secret', label.strip(), value)}"

    text = _PROSE_SECRET.sub(_prose, text)

    from iris import config

    if config.REDACT_PII:
        for pattern, kind, validator in _PII_PATTERNS:

            def _replace(m, k=kind, check=validator):
                matched = m.group(0)
                if check is not None and not check(matched):
                    return matched  # looks like PII by shape, is not by content
                return _placeholder_for(k, matched)

            text = pattern.sub(_replace, text)
    return text


def scrub_blocks(value):
    """Scrub a tool return value, whether it is text or a list of content blocks."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        out = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                block = {**block, "text": scrub(block["text"])}
            out.append(block)
        return out
    return value


def bind_variables(tool, enabled: bool = True):
    """Make placeholders behave as variables in a tool's arguments.

    Claude receives [email 1] instead of a real address, and can pass it
    straight back into any tool. The substitution happens here, at the boundary,
    so a path like C:/Users/[email 1]/notes.txt resolves correctly and the real
    value is never in the model's context in either direction.

    The tool is told *that* a substitution happened, but not what it was.
    """
    if not enabled:
        return tool

    original = tool.call

    def call(tool_input, *args, **kwargs):
        # Between tool calls is the only safe place to pause, so barge-in is
        # checked here rather than mid-action.
        from iris import interrupt

        held = interrupt.check(tool.name)
        if held is not None:
            from iris import transcript

            transcript.write(f"tool:{tool.name}", held)
            return held

        substituted = []
        resolved_input = {}
        for key, value in (tool_input or {}).items():
            if isinstance(value, str):
                replaced = resolve(value)
                if replaced != value:
                    substituted.append(key)
                resolved_input[key] = replaced
            else:
                resolved_input[key] = value

        result = original(resolved_input, *args, **kwargs)
        if substituted and isinstance(result, str):
            result = f"[resolved placeholder in: {', '.join(substituted)}]\n{result}"

        # Every tool result reaches the model through here, so this is the one
        # place that sees exactly what is sent - already redacted.
        from iris import transcript

        transcript.write(
            f"tool:{tool.name}",
            result if isinstance(result, str) else "[non-text result: image or blocks]",
        )
        return result

    tool.call = call
    return tool


def scrubbed(fn):
    """Run a tool's output through the scrubber before Claude ever sees it.

    Apply *under* @beta_tool, alongside @confirm.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return scrub_blocks(fn(*args, **kwargs))

    return wrapper
