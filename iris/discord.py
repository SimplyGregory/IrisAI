"""Sending a Discord message by driving the real web client.

Not the API. The token is never touched and no request is forged: this reads
Discord's own page and dispatches real clicks and keystrokes into it, so every
request that goes out is made by Discord's own code, with its own gateway
connection and headers. It is the client, doing what the client does.

The privacy line matters as much as the mechanism. Claude decides two things
only - which named target, and what to say - and never sees anything else.
Everything sensitive happens here in Python: the login check, reading names off
the page, turning a name into an id, navigating, typing, sending. The channel
that gets opened has its history on screen like any chat view, but none of that
is ever returned to the caller. The guarantee lives in what these functions
return, which is names and status and nothing more.

Unverified. This drives a live, logged-in Discord session and cannot be tested
without one, and Discord's markup shifts - so treat the selectors below as a
starting point to tune against the real page, not settled fact.
"""

import re
import time

APP_URL = "https://discord.com/channels/@me"
LOGIN_URL = "https://discord.com/login"

# Where Discord's own window sits. Off-screen means invisible but still a real,
# rendered, undetectable Chrome - unlike headless, which advertises itself. On
# a normal desktop nothing lives at -32000; a window there is simply not seen.
_OFFSCREEN = {"left": -32000, "top": -32000, "width": 1280, "height": 900}
_ONSCREEN = {"left": 80, "top": 80, "width": 1120, "height": 840}

# Discord's own page, kept apart from browser._page so that hiding Discord's
# window never hides - or is polluted by - Iris's ordinary browsing, which
# lives in its own window in the same Chrome.
_dpage = None


def _hidden() -> bool:
    from iris import config

    return config.DISCORD_HIDDEN


class DiscordUnavailable(Exception):
    """Chrome is not up, or Discord would not load."""


class NotLoggedIn(Exception):
    """No Discord session in Iris's browser profile yet."""


# --- the page ---------------------------------------------------------------

def _reset() -> None:
    """Drop the cached Chrome connection so the next call launches a fresh one.

    Needed because the login window is a real window the user can close, and
    when they do the whole browser connection dies under us - the next Playwright
    call then throws TargetClosedError against a browser that is gone. Clearing
    the cache makes _ensure_page relaunch cleanly, and the profile persists, so
    the relaunched Chrome is still logged in.
    """
    global _dpage, _dhwnd
    from iris.tools import browser

    browser._playwright = browser._browser = browser._page = browser._launched = None
    _dpage = None
    _dhwnd = None  # a relaunched Chrome has a new window; the old handle is dead


_dhwnd = None  # handle to Discord's window, so it can be shown again after hiding


def _position(page, visible: bool) -> None:
    """Move Discord's window on- or off-screen. Cosmetic and best-effort.

    Wrapped so a failure here can never stop a send: if the window will not
    move, it just stays where it is - visible, which is the old behaviour - and
    the message still goes. Invisibility is a nicety; delivering is the job.

    Off-screen alone leaves a taskbar button, so after parking it there the
    window is also hidden outright (Windows), which clears the taskbar and
    Alt-Tab too. For login it is shown and brought back on-screen.
    """
    global _dhwnd
    from iris import platform

    if visible and _dhwnd is not None:
        platform.show_window(_dhwnd)  # unhide before moving it into view

    try:
        session = page.context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        session.send("Browser.setWindowBounds", {
            "windowId": window["windowId"],
            "bounds": _ONSCREEN if visible else _OFFSCREEN,
        })
    except Exception:
        pass

    if not visible:
        time.sleep(0.3)  # let it actually reach -32000 before we look for it there
        handle = platform.hide_offscreen_window()
        if handle is not None:
            _dhwnd = handle


def _page():
    """Discord's page, in its own window, off-screen when hiding is on.

    Its own window on purpose: Iris's ordinary browsing has one too, in the same
    Chrome, and pushing Discord out of sight must not take that with it. If a
    separate window cannot be made, it falls back to a plain tab - hidden or not,
    Discord still has somewhere to work.

    Tries twice, because closing the window kills the connection and the first
    call then throws; the second relaunches against the persistent profile.
    """
    global _dpage
    from iris.tools import browser

    for attempt in range(2):
        main = browser._ensure_page()
        if isinstance(main, str):
            raise DiscordUnavailable(main)

        try:
            if _dpage is not None and not _dpage.is_closed():
                # Reposition on reuse too, so the window that came on-screen for
                # login goes back out of sight once past it. Skipped while still
                # on the login page - that is the one time it should stay visible.
                if _hidden() and "/login" not in (_dpage.url or ""):
                    _position(_dpage, visible=False)
                return _dpage

            context = main.context
            before = set(context.pages)

            # A window of its own via CDP. newWindow is what keeps it apart from
            # the browsing window; a plain new_page would land beside those tabs.
            try:
                context.browser.new_browser_cdp_session().send(
                    "Target.createTarget", {"url": APP_URL, "newWindow": True}
                )
                time.sleep(1.0)
            except Exception:
                pass

            fresh = [p for p in context.pages if p not in before]
            _dpage = fresh[-1] if fresh else context.new_page()
            if "discord.com" not in (_dpage.url or ""):
                _dpage.goto(APP_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)  # the SPA needs a moment to draw

            _position(_dpage, visible=not _hidden())
            return _dpage
        except Exception as exc:  # noqa: BLE001 - usually TargetClosedError
            _dpage = None
            if attempt == 0:
                _reset()
                continue
            raise DiscordUnavailable(
                "The Discord window was closed. Ask me again and I will reopen it."
            ) from exc


def tidy_tabs(page) -> None:
    """Close every tab but the working one.

    After signing in, the login tab has done its job, and stray blank tabs pile
    up across a session. Only the page actually being used is kept - the send
    needs one tab, not the clutter of all of them.
    """
    try:
        for other in list(page.context.pages):
            if other is not page and not other.is_closed():
                other.close()
    except Exception:
        pass


def logged_in(page) -> bool:
    """Whether this profile has a live Discord session.

    The login page and the app are different URLs, and the app only ever draws
    the server rail when authenticated - so both are checked, since a slow load
    can sit on the app URL with nothing rendered yet.
    """
    if "/login" in (page.url or ""):
        return False
    try:
        return bool(page.query_selector('[data-list-item-id^="guildsnav"], [class*="privateChannels"]'))
    except Exception:
        return False


def status() -> bool:
    """Whether this profile has a live Discord session right now.

    Checked live rather than remembered: the session lives in the browser's
    cookies, which is the only thing that actually knows, and a saved flag would
    drift out of sync the moment a session expired.
    """
    page = _page()
    signed_in = logged_in(page)
    if signed_in:
        # In and confirmed - so the login tab and any strays can go now, which
        # is what "close it once I've logged in" means in practice.
        tidy_tabs(page)
    return signed_in


def open_login() -> bool:
    """Bring up the visible login page if needed. Returns whether already in.

    Does not block the turn - it opens the page, starts a watcher, and returns.
    The watcher notices the moment the login page redirects to the logged-in URL
    and closes the login tab itself, so the user does not have to say "done" for
    the clutter to clear.
    """
    page = _page()
    if logged_in(page):
        return True
    # Nobody can type a password into a window off the edge of the screen, so
    # for login it comes into view - then the watcher puts it back out of sight
    # once the redirect says signing in worked.
    if _hidden():
        _position(page, visible=True)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    _start_login_watcher()
    return False


def _start_login_watcher(timeout: float = 300.0) -> None:
    """Watch for the login redirect and close the login tab when it happens.

    Over Chrome's DevTools HTTP endpoint, not Playwright: /json lists the tabs
    and /json/close ends one, and being plain HTTP it can run from this daemon
    thread without touching the Playwright connection, which is not safe to use
    off its own thread. Signing in redirects discord.com/login to
    discord.com/channels/@me; the instant a tab shows that, the login and any
    blank tabs are closed and the app tab is kept.
    """
    import json
    import threading
    import urllib.request

    from iris import config

    base = f"http://127.0.0.1:{config.CDP_PORT}"

    def watch():
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                targets = json.loads(urllib.request.urlopen(base + "/json", timeout=3).read())
            except Exception:
                continue
            pages = [t for t in targets if t.get("type") == "page"]
            app = [t for t in pages if "discord.com/channels" in (t.get("url") or "")]
            if not app:
                continue  # still on the login page, or still signing in
            keep = app[0]["id"]
            for target in pages:
                url = target.get("url") or ""
                stray = "discord.com/login" in url or url in ("about:blank", "chrome://newtab/", "")
                if target["id"] != keep and stray:
                    try:
                        urllib.request.urlopen(base + "/json/close/" + target["id"], timeout=3).read()
                    except Exception:
                        pass
            # The window is left as it is - still the logged-in one _dpage points
            # to. It is put back out of sight by the next _page call on the main
            # thread, which is the only place the Playwright connection may be
            # touched. Moving a window needs that connection; closing a tab, done
            # above, does not.
            return

    threading.Thread(target=watch, name="iris-discord-login", daemon=True).start()


# --- reading names off the page --------------------------------------------
#
# Everything here returns {name -> id}. The id is Python's business; only names
# are ever passed back out to where Claude can see them.

# Discord's class names are generated and change constantly, so nothing keys on
# them. These attributes are stable because Discord's own code relies on them:
# the guild id rides in data-list-item-id, links carry the ids in their href.
_GUILD_JS = r"""
() => {
  const out = {};
  for (const el of document.querySelectorAll('[data-list-item-id^="guildsnav___"]')) {
    const id = el.getAttribute('data-list-item-id').split('___')[1];
    if (!/^\d+$/.test(id || '')) continue;
    const named = el.closest('[data-dnd-name]');
    const name = named ? named.getAttribute('data-dnd-name') : '';
    if (name) out[name] = id;
  }
  return out;
}
"""

_DM_JS = r"""
() => {
  const out = {};
  for (const a of document.querySelectorAll('a[href^="/channels/@me/"]')) {
    const id = a.getAttribute('href').split('/').pop();
    if (!/^\d+$/.test(id || '')) continue;
    // The DM's name is the person, in the row's own text. Kept short so a
    // status line or a preview does not end up mistaken for the name.
    const label = (a.getAttribute('aria-label') || a.textContent || '').trim();
    if (label) out[label.slice(0, 60)] = id;
  }
  return out;
}
"""

_CHANNEL_JS = r"""
(guildId) => {
  const out = {};
  const prefix = '/channels/' + guildId + '/';
  for (const a of document.querySelectorAll('a[href^="' + prefix + '"]')) {
    const id = a.getAttribute('href').slice(prefix.length).split('/')[0];
    if (!/^\d+$/.test(id || '')) continue;
    const label = (a.getAttribute('aria-label') || a.textContent || '').trim();
    if (label) out[label.slice(0, 60)] = id;
  }
  return out;
}
"""


def guilds(page) -> dict:
    return page.evaluate(_GUILD_JS) or {}


def dms(page) -> dict:
    if "@me" not in (page.url or ""):
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1.5)
    return page.evaluate(_DM_JS) or {}


def channels(page, guild_id: str) -> dict:
    page.goto(f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded", timeout=30000)
    time.sleep(1.5)
    return page.evaluate(_CHANNEL_JS, guild_id) or {}


# --- turning a name into somewhere to send ----------------------------------

def _match(name: str, options: dict) -> tuple[str, str] | list[str]:
    """One (name, id) if it is unambiguous, else the candidate names to choose."""
    wanted = name.strip().lower()
    exact = [(n, i) for n, i in options.items() if n.lower() == wanted]
    if len(exact) == 1:
        return exact[0]

    partial = [(n, i) for n, i in options.items() if wanted in n.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        return [n for n, _ in partial]  # ambiguous: names only, for Claude to refine
    return []  # nothing matched


def resolve(page, to: str, channel: str = "") -> dict:
    """Work out where 'to' means, as far as it can without guessing.

    Returns one of:
      {"url": ...}                    - a definite place to send
      {"choices": [names]}            - ambiguous; needs a more specific name
      {"channels": [names]}           - a guild matched but no channel was named
      {"error": "..."}                - nothing matched
    Never returns an id. The caller passes names back to Claude, not these.
    """
    # A direct message first: "send Alex ..." usually means the person.
    dm = _match(to, dms(page))
    if isinstance(dm, tuple):
        return {"url": f"https://discord.com/channels/@me/{dm[1]}"}
    dm_choices = dm if isinstance(dm, list) else []

    # Then a server, which needs a channel to land in.
    guild = _match(to, guilds(page))
    if isinstance(guild, tuple):
        found = channels(page, guild[1])
        if not channel:
            return {"channels": list(found)[:40], "guild": guild[0]}
        chan = _match(channel, found)
        if isinstance(chan, tuple):
            return {"url": f"https://discord.com/channels/{guild[1]}/{chan[1]}"}
        if isinstance(chan, list) and chan:
            return {"choices": chan}
        return {"error": f"No channel like {channel!r} in {guild[0]}."}
    guild_choices = guild if isinstance(guild, list) else []

    both = dm_choices + guild_choices
    if both:
        return {"choices": both}
    return {"error": f"Nothing on Discord matches {to!r} - no friend or server by that name."}


# --- sending ----------------------------------------------------------------

def send(page, url: str, message: str) -> None:
    """Open the target and type the message into the real composer, then Enter."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(1.5)

    # A session can expire between logging in and sending. Landing on the login
    # page, or losing the app shell, means exactly that - and it is a different
    # thing from "the message box is missing", so it gets its own error and its
    # own advice: sign in again, do not just retry.
    if not logged_in(page) or "/login" in (page.url or ""):
        raise NotLoggedIn(
            "You have been signed out of Discord, so nothing was sent. Sign in "
            "again - I can open the window - and ask me once more."
        )

    # Discord's box is a contenteditable slate, not a plain input. Focused by
    # role, then typed into with real key events so its own handlers fire the
    # same way they do for a person - which is what sends the message properly
    # rather than leaving text sitting unsent.
    box = page.query_selector('[role="textbox"]')
    if box is None:
        raise DiscordUnavailable(
            "The message box did not appear. The page may still be loading, or "
            "this is somewhere messages cannot be sent."
        )
    box.click()
    time.sleep(0.3)
    page.keyboard.insert_text(message)
    time.sleep(0.3)
    page.keyboard.press("Enter")
    time.sleep(0.5)


def enabled() -> bool:
    from iris import config

    return config.DISCORD
