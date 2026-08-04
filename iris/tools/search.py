"""Looking something up on the web.

One tool, registered only when a Google API key is configured. Everything else
about finding things - fetching a page whose address is already known - stays
in info.py and needs no key.
"""

from anthropic import beta_tool

from iris import gemini, untrusted


@beta_tool
def web_search(question: str) -> str:
    """Search the web and get an answer with its sources.

    Use this when you need to FIND something rather than read a page you can
    already name: current events, "what is the id for X", who someone is, what
    a product costs, anything where you would otherwise be guessing at a URL.

    Prefer fetch_url when the address is already known or obvious - a specific
    page, an API, wttr.in for weather. That costs nothing and answers faster.
    This is for when you do not know where to look.

    The question is sent to Google, so write it as a question rather than as
    keywords, and do not put anything private in it. Redaction placeholders are
    NOT resolved for this tool: [email 1] is sent as those literal characters,
    which is deliberate - the value stays on this machine.

    Args:
        question: What you want to know, phrased as a question.
    """
    asked = question.strip()
    if not asked:
        return "web_search needs a question."

    try:
        found = gemini.search(asked)
    except gemini.SearchUnavailable as exc:
        return str(exc)

    text = found["answer"]
    if found["sources"]:
        text += "\n\nSources:\n" + "\n".join(
            f"  {s['title'] or s['url']}\n    {s['url']}" for s in found["sources"]
        )

    # Google read the web and summarised it, so what comes back is still a
    # stranger's writing at one remove. Same treatment as a fetched page.
    return untrusted.wrap(text, source="Google Search via Gemini")


TOOLS = [web_search]
