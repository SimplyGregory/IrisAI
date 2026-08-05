"""Chrome control over the DevTools protocol.

Claude never guesses pixel coordinates in the browser. browser_snapshot tags
every visible interactive element with an index and returns a numbered list;
Claude then acts on an element by index. That is what makes "click the first
video" work reliably instead of hopefully.
"""

import socket
import subprocess
import time
import urllib.parse

from anthropic import beta_tool

from iris import config, untrusted
from iris.redact import scrubbed

_playwright = None
_browser = None
_page = None

# The Chrome process this session launched, when it launched its own. Kept so
# shutdown can close it - an orphaned Chrome left running after Iris quits is a
# window nobody asked to keep, sometimes with a logged-in session in it. Only
# ever set for the isolated profile: in real-profile mode Iris attaches to the
# user's Chrome, which it must never close.
_launched = None

# JS that tags visible interactive elements and returns a description of each.
_SNAPSHOT_JS = """
() => {
  document.querySelectorAll('[data-iris-idx]').forEach(e => e.removeAttribute('data-iris-idx'));
  const selector = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'video',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="option"]', '[onclick]', '[contenteditable="true"]',
    'ytd-thumbnail', 'ytd-rich-item-renderer'
  ].join(',');
  const out = [];
  const seenHref = new Map();
  for (const el of document.querySelectorAll(selector)) {
    if (out.length >= 120) break;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.bottom < -200 || r.top > window.innerHeight * 3) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    // Never read the value of a field that holds a credential. A password
    // manager will have autofilled these, and el.value would otherwise put the
    // plaintext secret into the model's context.
    const type = (el.getAttribute('type') || '').toLowerCase();
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    const sensitive =
      type === 'password' ||
      /password|cc-number|cc-csc|cc-exp|one-time-code/.test(auto) ||
      /password|passcode|pin|cvv|security code/i.test(
        (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('name') || '')
      );
    const value = sensitive ? '' : (el.value || '');
    let label = (
      el.getAttribute('aria-label') || el.innerText || value ||
      el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt') || ''
    ).trim().replace(/\\s+/g, ' ').slice(0, 110);
    if (sensitive) label = (label ? label + ' ' : '') + '[credential field - value hidden]';
    let href = '';
    if (el.tagName === 'A') {
      const raw = el.getAttribute('href') || '';
      if (raw) { try { href = new URL(raw, location.href).pathname + new URL(raw, location.href).search; } catch (e) { href = raw; } }
      href = href.slice(0, 60);
    }
    if (!label) label = href;
    if (!label) continue;
    // Sites commonly wrap one destination in two links (a thumbnail and a
    // title). Emit it once, keeping whichever label is more descriptive.
    if (href && seenHref.has(href)) {
      const prev = out[seenHref.get(href)];
      if (label.length > prev.label.length) prev.label = label;
      continue;
    }
    if (href) seenHref.set(href, out.length);
    el.setAttribute('data-iris-idx', String(out.length));
    const onScreen = r.top >= 0 && r.top < window.innerHeight;
    out.push({ i: out.length, tag: el.tagName.toLowerCase(), label, href, onScreen });
  }
  return out;
}
"""


def _normalize_url(url: str) -> str:
    """Turn something a person said into something Chrome can load.

    A spoken site name often arrives with no scheme and no TLD ("bisecthosting"),
    which would otherwise be sent as the literal hostname and fail DNS.
    """
    target = url.strip()
    if target.startswith(("http://", "https://", "file://", "data:", "about:", "chrome:")):
        return target
    host = target.split("/")[0].split("?")[0]
    if host and "." not in host and ":" not in host:
        target = f"{host}.com" + target[len(host) :]
    return "https://" + target


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _launch_chrome() -> str | None:
    """Start Chrome with remote debugging. Returns an error string, or None on success."""
    exe = config.chrome_path()
    if exe is None:
        return "Chrome is not installed in any of the usual locations."

    args = [
        str(exe),
        f"--remote-debugging-port={config.CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if not config.USE_REAL_CHROME_PROFILE:
        config.CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args.append(f"--user-data-dir={config.CHROME_PROFILE_DIR}")

    global _launched

    # CREATE_NEW_CONSOLE is a Windows-only flag and does not even exist as a
    # constant on a Mac, where referencing it raises AttributeError - so it is
    # Windows-only here too. Chrome is a GUI app and shows its own window on
    # both; the flag only ever kept its console off Iris's.
    from iris import platform

    flags = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if platform.is_windows() else {}
    try:
        proc = subprocess.Popen(args, **flags)
        # Remembered only for the isolated profile - the one Iris owns and may
        # close on the way out. Never the user's own Chrome.
        if not config.USE_REAL_CHROME_PROFILE:
            _launched = proc
    except OSError as exc:
        return f"Could not start Chrome: {exc}"

    for _ in range(40):  # up to ~12 seconds
        if _port_open(config.CDP_PORT):
            return None
        time.sleep(0.3)

    if config.USE_REAL_CHROME_PROFILE:
        return (
            "Chrome started but its debugging port never opened. This usually means "
            "Chrome was already running without remote debugging. Ask the user to close "
            "all Chrome windows and try again."
        )
    return "Chrome started but its debugging port never opened."


def _ensure_page():
    """Connect (launching Chrome if needed) and return the active page.

    Returns the page, or a string describing what went wrong.
    """
    global _playwright, _browser, _page
    from playwright.sync_api import sync_playwright

    if _browser is not None:
        try:
            if not _browser.is_connected():
                _browser = None
        except Exception:
            _browser = None

    if _browser is None:
        if not _port_open(config.CDP_PORT):
            error = _launch_chrome()
            if error:
                return error
        if _playwright is None:
            _playwright = sync_playwright().start()
        try:
            _browser = _playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{config.CDP_PORT}"
            )
        except Exception as exc:
            return f"Could not attach to Chrome: {exc}"
        _page = None

    contexts = _browser.contexts
    if not contexts:
        return "Chrome is running but has no browser context open."
    context = contexts[0]

    if _page is None or _page.is_closed() or _page not in context.pages:
        _page = context.pages[-1] if context.pages else context.new_page()
    return _page


def _settle(page) -> None:
    """Wait for the page to stop moving.

    domcontentloaded is not enough for JS-rendered sites: on YouTube the video
    grid does not exist yet at that point. Wait for network quiet as well, and
    fall back to a short fixed pause when a page never goes fully idle (ads,
    long-polling, video players).
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        page.wait_for_timeout(1200)


def _adopt_newest_tab(page):
    """If an action opened a new tab, switch to it."""
    global _page
    try:
        pages = page.context.pages
        if pages and pages[-1] is not page:
            _page = pages[-1]
            _settle(_page)
            return _page
    except Exception:
        pass
    return page


@beta_tool
@scrubbed
def browser_open(url: str, new_tab: bool = False) -> str:
    """Open a URL in Chrome, launching the browser if it is not already running.

    By default this navigates the tab you are currently working in. Pass
    new_tab=True to leave that tab alone and open a fresh one alongside it.

    Args:
        url: The address to open. A bare domain like "youtube.com" is fine.
        new_tab: Open in a new tab instead of reusing the current one.
    """
    global _page
    page = _ensure_page()
    if isinstance(page, str):
        return page

    if new_tab:
        try:
            page = page.context.new_page()
            _page = page
        except Exception as exc:
            return f"Could not open a new tab: {exc}"

    target = _normalize_url(url)

    try:
        page.goto(target, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        # People say site names the way they say them out loud ("go to
        # bisecthosting"), which may not be a resolvable host. Rather than
        # returning a raw DNS error, search for it and say so.
        if "ERR_NAME_NOT_RESOLVED" in str(exc):
            query = url.strip()
            search = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
            # The failed navigation is still settling onto Chrome's error page;
            # a goto fired immediately gets cancelled by it. Let it land first,
            # and retry once if we still collide.
            for attempt in range(2):
                page.wait_for_timeout(500)
                try:
                    page.goto(search, wait_until="domcontentloaded", timeout=30000)
                    break
                except Exception as search_exc:
                    if attempt:
                        return f"{target} did not resolve, and searching failed too: {search_exc}"
            _settle(page)
            return (
                f"{target} is not a real address, so I searched for {query!r} instead.\n"
                f"Now on: {page.url}\n"
                "Call browser_snapshot and click the correct result."
            )
        return f"Could not load {target}: {exc}"

    try:
        page.bring_to_front()
    except Exception:
        pass
    return f"Opened {page.url}\nPage title: {page.title()}\nCall browser_snapshot to see what is on the page."


@beta_tool
@scrubbed
def browser_snapshot() -> str:
    """List the interactive elements on the current page, each with an index.

    Always call this before browser_click or browser_type, and call it again
    after the page changes. Indices are only valid until the next snapshot.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    # Content on JS-heavy sites streams in after load. Poll until the element
    # count stops growing rather than snapshotting a half-built page.
    # A single "count stopped changing" check fires too early on sites where the
    # chrome renders instantly and the content streams in behind it: the nav bar
    # alone looks stable. Require the count to hold steady across two consecutive
    # polls *and* a minimum number of polls before trusting it.
    elements = []
    previous = -1
    stable = 0
    for attempt in range(12):
        try:
            elements = page.evaluate(_SNAPSHOT_JS)
        except Exception as exc:
            return f"Could not read the page: {exc}"
        stable = stable + 1 if len(elements) == previous and elements else 0
        if stable >= 2 and attempt >= 3:
            break
        previous = len(elements)
        page.wait_for_timeout(500)

    if not elements:
        return f"{page.url}\nNo interactive elements found. The page may still be loading."

    lines = [f"{page.url}", f"Title: {page.title()}", ""]
    for el in elements:
        # The href tail is what lets you tell a real content link (/watch?v=...)
        # from a nav item, without needing site-specific knowledge.
        target = f"  -> {el['href']}" if el.get("href") else ""
        marker = "  (below the fold)" if not el["onScreen"] else ""
        lines.append(f"[{el['i']}] {el['tag']}: {el['label']}{target}{marker}")
    return "\n".join(lines)


@beta_tool
@scrubbed
def browser_click(index: int) -> str:
    """Click an element by the index shown in the most recent browser_snapshot.

    Args:
        index: The bracketed number from browser_snapshot.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    selector = f'[data-iris-idx="{index}"]'
    try:
        element = page.query_selector(selector)
        if element is None:
            return f"No element [{index}] on this page. Call browser_snapshot again for fresh indices."
        label = (element.get_attribute("aria-label") or element.inner_text() or "")[:80].strip()
        element.scroll_into_view_if_needed(timeout=4000)
        element.click(timeout=8000)
    except Exception as exc:
        return f"Could not click [{index}]: {exc}"

    _settle(page)
    page = _adopt_newest_tab(page)
    return f"Clicked [{index}] {label!r}.\nNow on: {page.url}\nTitle: {page.title()}"


@beta_tool
@scrubbed
def browser_type(index: int, text: str, press_enter: bool = False) -> str:
    """Type text into an input field identified by its snapshot index.

    Args:
        index: The bracketed number of the input from browser_snapshot.
        text: The text to type.
        press_enter: Press Enter afterwards, e.g. to submit a search.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    selector = f'[data-iris-idx="{index}"]'
    try:
        element = page.query_selector(selector)
        if element is None:
            return f"No element [{index}] on this page. Call browser_snapshot again."
        element.scroll_into_view_if_needed(timeout=4000)
        element.click(timeout=6000)
        element.fill("")
        element.type(text, delay=15)
        if press_enter:
            element.press("Enter")
    except Exception as exc:
        return f"Could not type into [{index}]: {exc}"

    _settle(page)
    page = _adopt_newest_tab(page)
    return f"Typed into [{index}].\nNow on: {page.url}"


@beta_tool
@scrubbed
def browser_scroll(direction: str = "down", amount: int = 1) -> str:
    """Scroll the current page.

    Args:
        direction: "down", "up", "top", or "bottom".
        amount: Number of screen-heights to scroll, for "down" and "up".
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    try:
        if direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction in ("down", "up"):
            sign = 1 if direction == "down" else -1
            page.evaluate(f"window.scrollBy(0, {sign * max(1, amount)} * window.innerHeight * 0.9)")
        else:
            return 'direction must be "down", "up", "top", or "bottom".'
    except Exception as exc:
        return f"Could not scroll: {exc}"

    page.wait_for_timeout(400)
    return f"Scrolled {direction}. Call browser_snapshot for the updated element list."


@beta_tool
@scrubbed
def browser_read_text() -> str:
    """Read the visible text of the current page.

    browser_snapshot lists only things you can click or type into, so it cannot
    answer "what does this page say". Use this to actually read a page's
    content. For a page you only need to read and never interact with,
    fetch_url is cheaper still.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    _settle(page)
    try:
        text = page.inner_text("body")
    except Exception as exc:
        return f"Could not read the page text: {exc}"

    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) > 12_000:
        text = text[:12_000] + "\n[truncated at 12000 characters]"
    if not text:
        return f"{page.url}\n[no visible text]"
    return f"{page.url}\nTitle: {page.title()}\n\n" + untrusted.wrap(text, source=page.url)


_MEDIA_JS = """
(args) => {
  const all = [...document.querySelectorAll('video, audio')];
  if (!all.length) return {found: false};
  // Do NOT require a loaded source: before playback starts YouTube's player
  // has readyState 0, no currentSrc and a NaN duration, and skipping it would
  // mean "there is no video on this page" on a video page. Prefer whatever is
  // already playing, then the longest - ads and preview loops are short.
  const score = e => (e.paused ? 0 : 100000) + (isFinite(e.duration) ? e.duration : 0);
  const m = all.sort((a, b) => score(b) - score(a))[0];

  switch (args.action) {
    case 'play':    m.play(); break;
    case 'pause':   m.pause(); break;
    case 'toggle':  m.paused ? m.play() : m.pause(); break;
    case 'mute':    m.muted = true; break;
    case 'unmute':  m.muted = false; break;
    case 'volume':  m.volume = Math.max(0, Math.min(1, args.value)); m.muted = false; break;
    case 'seek':    m.currentTime = Math.max(0, args.value); break;
    case 'forward': m.currentTime = m.currentTime + (args.value || 10); break;
    case 'back':    m.currentTime = Math.max(0, m.currentTime - (args.value || 10)); break;
    case 'restart': m.currentTime = 0; m.play(); break;
  }
  return {found: true, paused: m.paused, muted: m.muted, ended: m.ended,
          ready: m.readyState > 0,
          volume: Math.round(m.volume * 100), position: Math.round(m.currentTime || 0),
          duration: Math.round(isFinite(m.duration) ? m.duration : 0),
          count: all.length, title: document.title};
}
"""


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


@beta_tool
@scrubbed
def browser_media(action: str = "status", value: float = 0) -> str:
    """Inspect or control audio and video playing on the current page.

    Use this for anything about playback rather than hunting for a play button
    in browser_snapshot. The snapshot lists clickable elements and says nothing
    about whether something is actually playing, so judging playback from it is
    guesswork. This reads the media element itself, which is authoritative, and
    controls it directly rather than by clicking.

    Args:
        action: One of "status", "play", "pause", "toggle", "mute", "unmute",
            "volume" (with value 0 to 1), "seek" (value in seconds),
            "forward"/"back" (value in seconds, default 10), "restart".
        value: The number used by volume, seek, forward and back.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    action = (action or "status").strip().lower()
    allowed = {"status", "play", "pause", "toggle", "mute", "unmute", "volume",
               "seek", "forward", "back", "restart"}
    if action not in allowed:
        return f"action must be one of {sorted(allowed)}, got {action!r}"

    try:
        state = page.evaluate(_MEDIA_JS, {"action": action, "value": float(value)})
    except Exception as exc:
        return f"Could not reach the media on this page: {exc}"

    if not state or not state.get("found"):
        return "There is no audio or video on this page."

    if action not in ("status",):
        page.wait_for_timeout(250)
        try:
            state = page.evaluate(_MEDIA_JS, {"action": "status", "value": 0})
        except Exception:
            pass

    if not state.get("ready") and state.get("paused"):
        return (
            f"{state.get('title', '')}\n"
            "There is a player on this page but nothing has been loaded into it yet - "
            "playback has not started. Use action \"play\" to begin, or click the video."
        )

    playing = "paused" if state.get("paused") else "playing"
    position = _clock(state.get("position", 0))
    duration = state.get("duration", 0)
    where = f"{position} of {_clock(duration)}" if duration else position
    volume = "muted" if state.get("muted") else f"volume {state.get('volume', 0)}%"
    extra = f" ({state['count']} media elements on the page)" if state.get("count", 1) > 1 else ""
    return f"{state.get('title', '')}\n{playing} at {where}, {volume}{extra}"


@beta_tool
@scrubbed
def browser_tabs() -> str:
    """List every open Chrome tab, marking the one browser actions currently apply to.

    Use this when the user refers to a particular tab ("the google tab", "the one
    you opened earlier"), or to check you are acting on the tab you think you are.
    """
    page = _ensure_page()
    if isinstance(page, str):
        return page

    try:
        pages = page.context.pages
    except Exception as exc:
        return f"Could not list tabs: {exc}"

    lines = []
    for index, candidate in enumerate(pages):
        marker = "  <- active" if candidate is page else ""
        try:
            title = candidate.title()
        except Exception:
            title = "(unavailable)"
        lines.append(f"[{index}] {title[:60]} - {candidate.url[:80]}{marker}")
    return f"{len(pages)} tab(s) open:\n" + "\n".join(lines)


@beta_tool
@scrubbed
def browser_switch_tab(index: int) -> str:
    """Make a different tab the one that browser actions apply to.

    Args:
        index: The tab number from browser_tabs.
    """
    global _page
    page = _ensure_page()
    if isinstance(page, str):
        return page

    try:
        pages = page.context.pages
        if not 0 <= index < len(pages):
            return f"No tab [{index}]. There are {len(pages)} tab(s); call browser_tabs."
        _page = pages[index]
        _page.bring_to_front()
    except Exception as exc:
        return f"Could not switch to tab [{index}]: {exc}"

    return f"Switched to tab [{index}]: {_page.title()}\nURL: {_page.url}"


@beta_tool
@scrubbed
def browser_close_tab(index: int) -> str:
    """Close a Chrome tab.

    Args:
        index: The tab number from browser_tabs.
    """
    global _page
    page = _ensure_page()
    if isinstance(page, str):
        return page

    try:
        pages = page.context.pages
        if not 0 <= index < len(pages):
            return f"No tab [{index}]. There are {len(pages)} tab(s); call browser_tabs."
        victim = pages[index]
        was_active = victim is page
        title = victim.title()
        victim.close()
    except Exception as exc:
        return f"Could not close tab [{index}]: {exc}"

    if was_active:
        _page = None  # _ensure_page picks a surviving tab next time
    return f"Closed tab [{index}] ({title[:50]})."


@beta_tool
@scrubbed
def browser_back() -> str:
    """Go back to the previous page in Chrome's history."""
    page = _ensure_page()
    if isinstance(page, str):
        return page
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        return f"Could not go back: {exc}"
    return f"Now on: {page.url}\nTitle: {page.title()}"


def shutdown() -> None:
    """Detach from Chrome, and close the copy Iris launched herself.

    The isolated-profile Chrome is Iris's own window; left running after she
    quits it sits orphaned on screen, sometimes still signed into something. So
    it is closed here. The user's own Chrome, in real-profile mode, is only
    detached from and never closed - _launched is set only for the isolated one.
    """
    global _playwright, _browser, _page, _launched

    # Ask the isolated Chrome to close itself cleanly, before disconnecting.
    if _launched is not None and _browser is not None:
        try:
            _browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass

    try:
        if _browser is not None:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass

    # Backstop: if Browser.close did not take, end the tree we started. Chrome
    # is several processes, so terminating the launcher alone can leave workers.
    if _launched is not None:
        try:
            if _launched.poll() is None:
                from iris import platform

                platform.kill_process_tree(_launched.pid)
        except Exception:
            pass

    _playwright = _browser = _page = _launched = None


TOOLS = [
    browser_open,
    browser_snapshot,
    browser_click,
    browser_type,
    browser_scroll,
    browser_back,
    browser_read_text,
    browser_media,
    browser_tabs,
    browser_switch_tab,
    browser_close_tab,
]
