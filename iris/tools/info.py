"""Cheap ways to look something up, so the browser is not the default answer.

Driving Chrome to read a fact costs several tool calls and a page load. Most
lookups are a plain HTTP GET, and some - the date, the time - are already on
this machine. Without these, the only routes were the browser (slow) or
run_shell (confirmation-gated, for a read-only fetch).
"""

import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from anthropic import beta_tool

from iris import untrusted
from iris.redact import scrubbed

MAX_CHARS = 12_000

# A real browser's headers, not a script's. This is not cosmetic: with the old
# "Iris/1.0" agent and no Accept headers, DuckDuckGo answered 202 and a "pick
# the squares with ducks" challenge, and the same request with these came back
# 200 and readable. Bot defences key on the whole shape of a request - a
# missing Accept-Language is as telling as an odd agent - so the realistic set
# is sent rather than a token gesture at one.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](og:title|og:description|description)["\']'
    r'[^>]+content=["\']([^"\']*)["\']', re.I,
)
_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_YT_DATA = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});</script>", re.S)


def _decompress(response, raw: bytes) -> bytes:
    """Undo whatever Content-Encoding was applied.

    urllib does not do this for us, and asking for gzip is part of looking like
    a browser - so having asked, we have to be able to read the answer.
    """
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    try:
        if encoding == "gzip":
            import gzip

            return gzip.decompress(raw)
        if encoding == "deflate":
            import zlib

            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass  # served mislabelled; the raw bytes are the better guess
    return raw


def _walk(node, wanted: tuple):
    """Every dict at any depth carrying all of `wanted` as keys."""
    if isinstance(node, dict):
        if all(key in node for key in wanted):
            yield node
        for value in node.values():
            yield from _walk(value, wanted)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, wanted)


def _video_listing(html: str) -> str:
    """The videos on a YouTube listing page, read out of its embedded data.

    Site-specific, and deliberately so: YouTube renders its grid with
    JavaScript, so the visible HTML says only the channel name, and the whole
    list would otherwise cost a browser tab to read. The data is already in the
    page - this reads it.

    The structure is walked rather than pattern-matched, because YouTube
    reshapes it periodically. It has already moved from videoRenderer to view
    models once; walking for "a thing with an id and some metadata" survives
    that, where a regex tuned to today's field names would not. If the shape
    moves far enough, this finds nothing and the caller carries on without it.
    """
    found = _YT_DATA.search(html)
    if not found:
        return ""
    try:
        import json

        data = json.loads(found.group(1))
    except Exception:
        return ""

    videos = []
    for item in _walk(data, ("contentId", "metadata")):
        code = item.get("contentId")
        if not isinstance(code, str) or len(code) != 11:
            continue
        for holder in _walk(item["metadata"], ("content",)):
            title = holder.get("content")
            if isinstance(title, str) and len(title) > 3:
                videos.append(f"  {title}  (youtu.be/{code})")
                break
        if len(videos) >= 30:
            break

    if not videos:
        return ""
    return "Videos listed on this page, newest first:\n" + "\n".join(videos)


def _structured(html: str) -> str:
    """Title, description and any schema.org data, taken before scripts are cut.

    A page that renders itself with JavaScript has almost no visible text, but
    it nearly always carries this - which is the difference between "the page
    said nothing, open a browser" and an actual answer.
    """
    import html as html_module
    import json

    lines = []
    title = _TITLE.search(html)
    if title:
        lines.append("Title: " + html_module.unescape(title.group(1).strip()))

    seen = set()
    for name, content in _META.findall(html):
        value = html_module.unescape(content).strip()
        if value and value not in seen:
            seen.add(value)
            lines.append(f"{name}: {value}")

    for block in _LD_JSON.findall(html)[:3]:
        try:
            parsed = json.loads(block.strip())
        except ValueError:
            continue
        text = json.dumps(parsed, separators=(",", ":"))
        lines.append("Structured data: " + text[:1200])

    return "\n".join(lines)


def _html_to_text(html: str) -> str:
    import html as html_module

    text = _SCRIPT_STYLE.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text, flags=re.I)
    text = _TAG.sub("", text)
    text = html_module.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()


@beta_tool
@scrubbed
def fetch_url(url: str) -> str:
    """Fetch a URL over HTTP and return its text.

    This is the cheapest way to look a fact up. Use it for APIs, JSON, plain
    text services and simple pages - for example https://wttr.in/Richmond
    returns a weather report as text, and most public APIs answer in one call.

    Try this before the browser even for pages that render with JavaScript. It
    reads what the page declares about itself - its title, description and any
    schema.org data - and on a YouTube channel or playlist it lists the videos
    with their links. That is often the whole answer, and it costs one call
    rather than opening a tab.

    Use the browser tools when you need to click, type, log in, or when this
    has actually come back without what you needed. Do not open a tab on the
    assumption that a page will be empty; look first.

    Args:
        url: The address to fetch. https:// is assumed if no scheme is given.
    """
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target

    from iris import interrupt

    def _get():
        request = urllib.request.Request(target, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(3_000_000)
            return response.headers.get("Content-Type", ""), _decompress(response, raw)

    # A slow server should not make Iris unstoppable for twenty seconds.
    # The worker's exception is re-raised by run_interruptible itself, so the
    # guard has to wrap that call rather than the unpacking below it.
    try:
        held, fetched = interrupt.run_interruptible(_get, "fetch_url", timeout=25)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} from {target}: {exc.reason}"
    except Exception as exc:
        return f"Could not fetch {target}: {type(exc).__name__}: {exc}"
    if held is not None:
        return held
    content_type, raw = fetched

    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    text = raw.decode(charset, errors="replace")

    if "html" in content_type.lower() or text.lstrip()[:15].lower().startswith("<!doctype html"):
        # Read what the page declares about itself before the markup is thrown
        # away. Scripts are stripped to get readable text, and everything a
        # JavaScript-rendered page knows lives in exactly those scripts - so
        # taking it first is the difference between an answer and "the page was
        # empty, open a browser".
        head = "\n\n".join(part for part in (_structured(text), _video_listing(text)) if part)
        body = _html_to_text(text)
        text = f"{head}\n\n{body}".strip() if head else body

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n[truncated at {MAX_CHARS} characters]"
    if not text:
        return "[empty response]"
    # Everything past this point came from a stranger's server.
    return untrusted.wrap(text, source=target)


@beta_tool
def get_datetime() -> str:
    """Get the current local date, time and time zone on this machine.

    Use this rather than searching the web or shelling out; the answer is
    already here and costs nothing.
    """
    now = datetime.now().astimezone()
    return (
        f"{now:%A %d %B %Y, %H:%M:%S} "
        f"({now.tzname()}, UTC{now:%z})\n"
        f"UTC: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}"
    )


TOOLS = [fetch_url, get_datetime]
