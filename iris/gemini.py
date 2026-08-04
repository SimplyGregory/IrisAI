"""Asking Google, through Gemini's search grounding.

Iris could already fetch a page she knew the address of. What she could not do
was find one. The obvious answer - scrape a search engine - is what she had,
and it is poor: DuckDuckGo answers a scripted request with a bot challenge, and
when it does answer, the readable text is mostly its country list with the
results buried in markup.

Gemini's grounding does the searching and the reading, and hands back an answer
with the sources it used. One call instead of a search page, three fetches and
a guess at which result was the real one.

Optional, and off unless a key is configured. Nothing else in Iris depends on
it - fetch_url remains the route for a page whose address is already known,
which is most of them, and costs nobody an API key.

Unverified: written against Google's documentation. Their endpoint has moved
before and the response shape below is read defensively for that reason.
"""

import json
import re
import urllib.error
import urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 45.0


class SearchUnavailable(Exception):
    """No key, or Google would not answer."""


class NoSearchQuota(SearchUnavailable):
    """The key works, the model works, and grounding is not included.

    Told apart from real rate limiting by asking the same model a question
    without the search tool. Google reports both as 429, and the difference is
    the whole of whether waiting helps.
    """


class RateLimited(SearchUnavailable):
    """Google took the key and refused on quota.

    Worth separating, because it is the one failure that proves the key is
    good: the request got past authentication and was turned down for how much
    is being asked, not for who is asking. Setup should read that as a pass.
    """


def configured() -> bool:
    from iris import config

    return bool(config.GEMINI_KEY)


def _key() -> str:
    from iris import config

    if not config.GEMINI_KEY:
        raise SearchUnavailable(
            "No Google API key is set, so web search is not available. A free "
            "key comes from aistudio.google.com; put it in .env as "
            "IRIS_GEMINI_KEY, or run setup again."
        )
    return config.GEMINI_KEY


def search(question: str) -> dict:
    """Ask Google a question. Returns {"answer": str, "sources": [{title, url}]}."""
    from iris import config

    payload = json.dumps({
        "model": config.GEMINI_MODEL,
        "input": question,
        # The whole reason for being here. Without this it is just another
        # language model answering from memory, which Iris already has.
        "tools": [{"type": "google_search"}],
    }).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        method="POST",
        headers={"x-goog-api-key": _key(), "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        message, reason = _error_detail(exc)
        # A rejected key comes back 400, not 401 or 403 - it is reported as a
        # bad argument rather than as a refusal. Checked against the live API,
        # because assuming the usual codes gave "HTTP 400" and no reason.
        if reason == "API_KEY_INVALID" or exc.code in (401, 403):
            raise SearchUnavailable(
                "Google rejected the API key. Check IRIS_GEMINI_KEY in .env - a "
                f"free one comes from aistudio.google.com. ({message})"
            ) from exc
        if exc.code == 429:
            # Google says "quota exceeded" both for going too fast and for
            # having no allowance at all, and the difference decides whether
            # waiting is worth anything. Asking the same model to answer
            # WITHOUT search separates them: if that works, the key and the
            # model are fine and it is grounding specifically that is not
            # included - which no amount of waiting will change.
            if _answers_without_search():
                raise NoSearchQuota(
                    "This Google key has no quota for Search grounding, which is "
                    "what web search needs. The key and the model are fine - the "
                    "same model answers normally without search - so this is not "
                    "a pace you can wait out.\n"
                    "Grounding is a billed feature: enabling billing on the "
                    "project at aistudio.google.com turns it on, and searches "
                    "are charged individually.\n"
                    "Until then fetch_url still reads any page whose address is "
                    "known, which covers most lookups."
                ) from exc
            raise RateLimited(
                "Google is rate limiting this key, so this search did not run. "
                "The key itself is fine - a quota refusal happens after it has "
                "been accepted. Wait a moment and try again, or use fetch_url "
                "if the address is already known."
                + (f" ({message})" if message else "")
            ) from exc
        if exc.code == 404:
            # The model is retired, or was never on this key's tier. Google
            # retires models for new keys while leaving them for existing
            # ones, so no single default is right for everybody - ask.
            usable = available_models()
            suggestion = ""
            if usable:
                suggestion = (
                    "\nThis key can use: " + ", ".join(usable[:6])
                    + f"\nSet IRIS_GEMINI_MODEL in .env to one of those - "
                    f"{usable[0]} is the newest."
                )
            raise SearchUnavailable(
                f"The model {config.GEMINI_MODEL} is not available to this key. "
                f"{message}{suggestion}"
            ) from exc
        raise SearchUnavailable(
            f"Google returned HTTP {exc.code}." + (f" {message}" if message else "")
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SearchUnavailable(f"Could not reach Google: {type(exc).__name__}") from exc

    return _read(body)


def _answers_without_search() -> bool:
    """Can this key use the model at all, with search switched off?

    The cheapest possible question, on the older generateContent endpoint,
    which is the one that answers without grounding. A yes here means the 429
    was about the search feature and not about the key, the model or the pace.
    """
    from iris import config

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent"
    )
    payload = json.dumps({"contents": [{"parts": [{"text": "ok"}]}]}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"x-goog-api-key": config.GEMINI_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except Exception:  # noqa: BLE001 - a no here just means we cannot tell
        return False


def available_models(key: str = "") -> list[str]:
    """Which models this key can actually use, newest-looking first.

    Worth asking rather than guessing. Google retires models for new keys
    without retiring them for existing ones, so a name that is correct in the
    documentation and correct on one account is a 404 on another - which is
    exactly how the default here came to be wrong twice.
    """
    from iris import config

    request = urllib.request.Request(
        MODELS_ENDPOINT,
        headers={"x-goog-api-key": key or config.GEMINI_KEY},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a failure here costs a suggestion, not the answer
        return []

    names = []
    for model in body.get("models", []) if isinstance(body, dict) else []:
        name = str(model.get("name", "")).removeprefix("models/")
        # Only the ones that can answer a prompt at all; embedding and vision
        # models are in this list too and would be useless suggestions.
        methods = model.get("supportedGenerationMethods") or []
        if name and (not methods or "generateContent" in methods):
            names.append(name)

    # Newest first by the number in the name, so the suggestion is a good one.
    def rank(name: str) -> tuple:
        digits = re.findall(r"\d+(?:\.\d+)?", name)
        return (-float(digits[0]) if digits else 0, name)

    return sorted(names, key=rank)


def _error_detail(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """The message and machine-readable reason out of a Google error body.

    Which arrives wrapped in a list - [{"error": {...}}] - not as the bare
    object the shape suggests. Reading it as an object silently produced no
    detail at all, so a rejected key reported itself as a naked "HTTP 400".
    """
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - the status alone is still something
        return "", ""

    if isinstance(body, list):
        body = body[0] if body else {}
    error = body.get("error", {}) if isinstance(body, dict) else {}

    reason = ""
    for detail in error.get("details", []) or []:
        if isinstance(detail, dict) and detail.get("reason"):
            reason = detail["reason"]
            break
    return str(error.get("message", "")), reason


def _read(body: dict) -> dict:
    """Pull the answer and its sources out, whatever shape they arrived in.

    Written to survive the response moving: every field is looked for rather
    than indexed into, and a missing one costs the citations rather than the
    answer. Google has reorganised this API before.
    """
    answer_parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                answer_parts.append(text)
            # Citations travel as url_citation annotations beside the text.
            url = node.get("url") or node.get("uri")
            if isinstance(url, str) and url.startswith("http") and url not in seen:
                seen.add(url)
                sources.append({"title": str(node.get("title") or "").strip(), "url": url})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)

    answer = "\n".join(dict.fromkeys(answer_parts)).strip()
    if not answer:
        raise SearchUnavailable(
            "Google answered but with nothing readable in it. The response "
            "shape may have changed."
        )
    return {"answer": answer, "sources": sources[:8]}
