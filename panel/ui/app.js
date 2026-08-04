/* The page half of the panel.
 *
 * Python calls window.panel.* to push things in; this file calls
 * pywebview.api.* to send things out. Nothing here knows about the agent - it
 * receives typed events and renders them.
 */

const $ = (id) => document.getElementById(id);
const thread = $("thread");
const input = $("input");

let busy = false;
let ready = false;

/* --- rendering ---------------------------------------------------------- */

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
}

/* Just enough markdown for what a reply actually contains. Claude writes
   plain prose most of the time, but paths and commands come back fenced. */
function render(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  return html;
}

function atBottom() {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
}

function add(node, { keepScroll = false } = {}) {
  const stick = keepScroll ? atBottom() : true;
  $("empty")?.remove();
  thread.appendChild(node);
  if (stick) thread.scrollTop = thread.scrollHeight;
  return node;
}

function element(className, html) {
  const node = document.createElement("div");
  node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function note(text, bad = false) {
  add(element("note" + (bad ? " bad" : ""), escapeHtml(text)));
}

/* The header's second line: the model and settings until the conversation has
   a name, then the name. A named one reads a shade brighter, because by then
   it is the useful half of the header rather than a footnote. */
function setSubtitle(text, named) {
  const subtitle = $("subtitle");
  subtitle.textContent = text;
  subtitle.title = text;           // the full text, when it has to be clipped
  subtitle.classList.toggle("named", named);
}

/* --- the reply arriving -------------------------------------------------- */

let streaming = null;   // the bubble currently being written into

/* Fragments are appended as plain text rather than re-rendered as markdown on
   every one: half-written text is full of unclosed fences and lone asterisks,
   and rendering that mid-flight makes the reply flicker between formattings.
   The "reply" event swaps in the properly rendered version at the end. */
function stream(fragment) {
  if (!streaming) {
    setThinking(false);          // the text itself is now the progress
    streaming = add(element("msg iris streaming"));
  }
  const stick = atBottom();
  streaming.textContent += fragment;
  if (stick) thread.scrollTop = thread.scrollHeight;
}

/* A tool belongs above the reply it produced.
 *
 * Appending is not enough to get that. If Iris says something before acting -
 * "I'll adjust that for you" - the reply bubble already exists by the time the
 * tool runs, and the finished reply lands in that same bubble when it settles.
 * Anything appended in between therefore ends up underneath the final reply.
 * Inserting above the open bubble puts it back in the order you read it in,
 * and costs nothing when there is no bubble yet, which is the common case. */
function addTool(name, args, failed = false) {
  const line = element(
    failed ? "tool failed" : "tool",
    failed
      ? `<b>&#10005;</b><span>${escapeHtml(args)}</span>`
      : `<b>${escapeHtml(name)}</b><span>${escapeHtml(args)}</span>`
  );
  const stick = atBottom();
  $("empty")?.remove();
  if (streaming) thread.insertBefore(line, streaming);
  else thread.appendChild(line);
  if (stick) thread.scrollTop = thread.scrollHeight;
}

/* --- the mascot ---------------------------------------------------------- */

/* Which tools look like looking, which look like typing, and which look like
   going somewhere. Grouped by what the *user* would say she is doing, not by
   what the code does: reading a file and reading a web page are the same act
   to watch, however different they are underneath. */
const MOOD_OF = {};
for (const [mood, tools] of Object.entries({
  looking: ["list_files", "search_files", "read_file", "browser_snapshot",
            "browser_read_text", "screenshot", "ui_inspect", "list_windows",
            "list_installed_apps", "app_interfaces", "fetch_url", "list_memories",
            "get_datetime", "browser_tabs", "reveal_redacted", "read_clipboard"],
  typing:  ["screen_type", "browser_type", "ui_set_text", "edit_file", "write_file",
            "run_shell", "remember", "forget", "copy_to_clipboard", "ask_user"],
  walking: ["launch_app", "window_control", "browser_open", "browser_click",
            "browser_back", "browser_scroll", "browser_switch_tab", "browser_close_tab",
            "screen_click", "screen_key", "ui_click", "wait_for_window", "wait",
            "browser_media", "window_settings", "speech_settings", "shut_down"],
})) tools.forEach((tool) => (MOOD_OF[tool] = mood));

let moodHold = null;
let petting = false;

/* A tool mood is held briefly rather than tied to the tool's real duration:
   most calls return in well under a second, and a mascot that flicked between
   poses that fast would read as a glitch rather than as activity.

   Petting outranks the lot. While it is happening the moods still arrive and
   are still tracked - they are just not shown, and whatever is current when
   you stop is what she goes back to. */
function setMood(mood, holdFor = 0) {
  clearTimeout(moodHold);
  if (!petting) $("mascot").dataset.mood = mood;
  if (holdFor) {
    moodHold = setTimeout(() => {
      if (!petting) $("mascot").dataset.mood = busy ? "thinking" : "idle";
    }, holdFor);
  }
}

/* --- petting -------------------------------------------------------------
 *
 * Hovering is not petting. Resting the pointer on her while reading, or
 * crossing the title bar on the way to the close button, should do nothing -
 * so what is detected is the back-and-forth specifically: the horizontal
 * direction has to reverse twice inside a short window. That is a stroke, and
 * it is hard to do by accident. */

const PET_WINDOW = 900;   // ms of movement history to judge on
const PET_FLIPS = 3;      // direction changes that count as a stroke
const PET_LEG = 5;        // px a run must cover before it counts as a direction
const PET_LINGER = 800;   // she stays happy this long after you stop

let petTrail = [];
let petStop = null;

$("mascot").addEventListener("mousemove", (event) => {
  const now = performance.now();
  petTrail.push({ x: event.clientX, t: now });
  petTrail = petTrail.filter((point) => now - point.t < PET_WINDOW);

  // Count reversals, but only ones with real travel behind them. Judging on
  // raw per-event direction made a single sweep register: the pointer wobbles
  // by a pixel between samples, and every wobble looked like a change of mind.
  let flips = 0;
  let heading = 0;
  let anchor = petTrail[0] ? petTrail[0].x : 0;
  for (const point of petTrail) {
    const travelled = point.x - anchor;
    if (Math.abs(travelled) < PET_LEG) continue;
    const step = Math.sign(travelled);
    if (heading && step !== heading) flips++;
    heading = step;
    anchor = point.x;
  }
  if (flips >= PET_FLIPS) startPetting();
});

$("mascot").addEventListener("mouseleave", () => { petTrail = []; });

function startPetting() {
  petting = true;
  $("mascot").dataset.mood = "petting";
  clearTimeout(petStop);
  petStop = setTimeout(() => {
    petting = false;
    petTrail = [];
    // Back to whatever she was actually doing while you were distracting her.
    $("mascot").dataset.mood = busy ? "thinking" : "idle";
  }, PET_LINGER);
}

/* --- thinking indicator ------------------------------------------------- */

let dots = null;

function setThinking(on) {
  busy = on;
  document.body.classList.toggle("thinking", on);
  if (on && !dots) {
    dots = add(element("dots", "<i></i><i></i><i></i>"));
  } else if (!on && dots) {
    dots.remove();
    dots = null;
  }
  updateSend();
}

/* --- questions ---------------------------------------------------------- */

function askConfirm({ id, question, detail }) {
  const card = element("ask");
  card.dataset.request = id;
  card.innerHTML = `
    <p>${escapeHtml(question)}</p>
    ${detail ? `<pre>${escapeHtml(detail)}</pre>` : ""}
    <div class="row">
      <button class="btn accent" data-value="yes">Yes</button>
      <button class="btn" data-value="always">Always</button>
      <button class="btn" data-value="no">No</button>
    </div>
    <div class="chosen"></div>`;

  card.querySelectorAll("button").forEach((button) => {
    button.onclick = () => {
      const value = button.dataset.value;
      card.querySelector(".chosen").textContent =
        { yes: "Allowed", always: "Allowed for this session", no: "Declined" }[value];
      card.classList.add("answered");
      pywebview.api.answer(id, value);
      input.focus();
    };
  });

  add(card);
  // Enter is the safe default here only because the button is focused, so a
  // stray keypress cannot approve something you never read.
  card.querySelector("button").focus();
}

function askQuestion({ id, question }) {
  const card = element("ask");
  card.dataset.request = id;
  card.innerHTML = `
    <p>${escapeHtml(question)}</p>
    <div class="row">
      <input class="btn" style="flex:1;text-align:left;cursor:text" autocomplete="off">
      <button class="btn accent" style="flex:0 0 72px">Reply</button>
    </div>
    <div class="chosen"></div>`;

  const box = card.querySelector("input");
  const send = () => {
    card.querySelector(".chosen").textContent = box.value || "(no answer)";
    card.classList.add("answered");
    pywebview.api.answer(id, box.value);
    input.focus();
  };
  card.querySelector("button").onclick = send;
  box.onkeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); send(); }
  };

  add(card);
  box.focus();
}

/* --- what Python pushes in ---------------------------------------------- */

window.panel = {
  theme({ dark, accent, platform }) {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.setProperty("--accent", accent);
    // Drives the macOS block at the end of style.css. Set alongside the theme
    // rather than at startup so a redraw cannot lose it.
    if (platform) document.documentElement.dataset.platform = platform;
  },

  focusInput() {
    input.focus();
  },

  receive(event) {
    switch (event.type) {
      case "delta":
        stream(event.text);
        break;
      case "reply":
        // Settle whatever streamed in: swap the plain text for the rendered
        // markdown, or create the bubble outright if nothing streamed.
        if (streaming) {
          streaming.innerHTML = render(event.text);
          streaming.classList.remove("streaming");
          streaming = null;
        } else {
          add(element("msg iris", render(event.text)));
        }
        break;
      case "tool":
        addTool(event.name, event.args);
        setMood(MOOD_OF[event.name] || "walking", 2200);
        break;
      case "tool-failed":
        // Sits with the tool it belongs to, above the reply, so a call that
        // failed and was worked around is visible rather than silent.
        addTool(event.name, event.text, true);
        break;
      case "talking":
        setMood(event.value ? "speaking" : "idle");
        break;
      case "thinking":
        if (event.value) streaming = null;   // a new turn writes a new bubble
        setThinking(event.value);
        setMood(event.value ? "thinking" : "idle");
        break;
      case "confirm":
        askConfirm(event);
        break;
      case "question":
        askQuestion(event);
        break;
      case "note":
        note(event.text);
        break;
      case "error":
        // Keep whatever had already been written, but stop it looking live.
        if (streaming) { streaming.classList.remove("streaming"); streaming = null; }
        note(event.text, true);
        break;
      case "title":
        setSubtitle(event.text, true);
        break;
      case "wiped":
        // Emptied by the tool rather than by /reset, so there is no note to
        // add - she is about to say so herself in the reply that follows.
        thread.innerHTML = "";
        streaming = null;
        setSubtitle("", false);
        break;
      case "cleared":
        thread.innerHTML = "";
        streaming = null;
        setSubtitle("", false);
        note(event.text);
        break;
      case "speaking":
        document.body.classList.toggle("muted", !event.value);
        break;
      case "answered": {
        // Answered out loud rather than clicked: show the same thing a click
        // would have, so the card does not sit there still asking.
        const card = thread.querySelector(`.ask[data-request="${event.id}"]`);
        if (card && !card.classList.contains("answered")) {
          const said = { yes: "Allowed", always: "Allowed for this session",
                         no: "Declined" }[event.value] || event.value;
          card.querySelector(".chosen").textContent = said;
          card.classList.add("answered");
        }
        break;
      }
      case "voice_control":
        // The mic stays lit while she is listening for her name, so the state
        // is visible without opening the menu to check.
        $("mic").classList.toggle("on", event.value);
        if (config) config.voice_control = event.value;
        break;
      case "said":
        // Spoken rather than typed, but it is still your message.
        add(element("msg you", escapeHtml(event.text)));
        break;
      case "interrupted":
        // The message is already in the thread as your bubble; this explains
        // why the reply above it stops where it does.
        if (streaming) { streaming.classList.remove("streaming"); streaming = null; }
        note("Interrupted - picking that up instead");
        break;
      case "dictating": {
        const mic = $("mic");
        mic.classList.toggle("recording", event.stage === "recording");
        mic.classList.toggle("working", event.stage === "transcribing");
        mic.title = event.stage === "recording"
          ? "Stop recording" : "Dictate a message";
        input.placeholder = { recording: "Recording - click the mic to stop",
                              transcribing: "Working out what you said..." }[event.stage]
                            || "Message Iris";
        setMood(event.stage === "recording" ? "listening"
                : event.stage === "transcribing" ? "thinking" : "idle");
        break;
      }
      case "dictated":
        if (event.text) insert(event.text);
        break;
      case "retitle":
        setSubtitle("", false);
        break;
    }
  },
};

/* --- sending ------------------------------------------------------------ */

function updateSend() {
  $("send").classList.toggle("ready", input.value.trim().length > 0 && !busy);
}

function submit() {
  const text = input.value.trim();
  if (!text || !ready) return;
  closeMenu();
  // Sent while she is still working, this becomes an interruption rather than
  // a queued message - Python decides which, and says so in the thread.
  if (!text.startsWith("/")) add(element("msg you", escapeHtml(text)));
  input.value = "";
  resize();
  updateSend();
  pywebview.api.send(text);
}

function resize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 116) + "px";
}

input.addEventListener("input", () => { resize(); updateSend(); });
input.addEventListener("focus", () => $("field").classList.add("focused"));
input.addEventListener("blur", () => $("field").classList.remove("focused"));

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

/* --- the menus ----------------------------------------------------------- */

let config = null;        // last known settings, from pywebview.api.settings()
let openMenu = null;      // which button opened the one on screen

const menu = $("menu");
const TICK = '<svg class="tick" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
             '<path d="M3 8.5l3.5 3.5L13 5" fill="none" stroke="currentColor" ' +
             'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function closeMenu() {
  menu.hidden = true;
  openMenu = null;
}

function showMenu(which, html, side = "left") {
  if (openMenu === which) return closeMenu();
  menu.innerHTML = html;
  menu.classList.toggle("from-left", side === "left");
  menu.classList.toggle("from-right", side === "right");
  menu.hidden = false;
  menu.scrollTop = 0;
  openMenu = which;
}

function option(selected, name, hint) {
  return `<span class="body">${escapeHtml(name)}` +
         (hint ? `<span class="hint">${escapeHtml(hint)}</span>` : "") +
         `</span>${TICK}`;
}

/* Applying a setting returns the new settings, so the menus never drift out of
   step with what Python actually did - it is the one deciding, not us. */
async function apply(promise) {
  config = await promise;
  paintMode();
  return config;
}

function paintMode() {
  const current = config.modes.find((m) => m.id === config.mode);
  $("mode-name").textContent = current ? current.name : config.mode;
}

function modeMenu() {
  showMenu("mode",
    `<div class="menu-label">How much Iris asks before acting</div>` +
    config.modes.map((m) =>
      `<button class="menu-item${m.id === config.mode ? " selected" : ""}" data-mode="${m.id}">
         ${option(m.id === config.mode, m.name, m.hint)}</button>`).join(""),
    "right");   // the mode chip sits on the right of the toolbar

  menu.querySelectorAll("[data-mode]").forEach((button) => {
    button.onclick = async () => {
      await apply(pywebview.api.set_mode(button.dataset.mode));
      closeMenu();
      input.focus();
    };
  });
}

function tuneMenu() {
  showMenu("tune",
    `<div class="menu-label">Model</div>` +
    config.models.map((m) =>
      `<button class="menu-item${m.id === config.model ? " selected" : ""}" data-model="${m.id}">
         ${option(m.id === config.model, m.name, m.hint)}</button>`).join("") +
    `<div class="menu-sep"></div>` +
    `<div class="menu-label">Effort <span id="effort-name"></span></div>` +
    `<div class="steps">` +
      config.efforts.map((e, i) =>
        `<button class="step" data-effort="${e}" title="${e}"></button>`).join("") +
      `<span class="caption" id="effort-caption"></span>` +
    `</div>` +
    `<div class="menu-sep"></div>` +
    `<button class="menu-item" id="thinking-row">
       <span class="body">Thinking<span class="hint">Reason before acting. Changing this
       starts a new conversation.</span></span>
       <span class="switch${config.thinking ? " on" : ""}" id="thinking-switch"></span>
     </button>` +
    `<button class="menu-item" id="voice-row">
       <span class="body">Voice control<span class="hint">Listen for
       &ldquo;${escapeHtml(config.wake_phrase || "iris")}&rdquo;, then act on whatever you
       say next.</span></span>
       <span class="switch${config.voice_control ? " on" : ""}" id="voice-switch"></span>
     </button>`);

  paintEffort();

  menu.querySelectorAll("[data-model]").forEach((button) => {
    button.onclick = async () => {
      await apply(pywebview.api.set_model(button.dataset.model));
      tuneMenu();
    };
  });
  menu.querySelectorAll("[data-effort]").forEach((button) => {
    button.onclick = async () => {
      await apply(pywebview.api.set_effort(button.dataset.effort));
      tuneMenu();
    };
  });
  $("thinking-row").onclick = async () => {
    await apply(pywebview.api.set_thinking(!config.thinking));
    tuneMenu();
  };
  $("voice-row").onclick = async () => {
    await apply(pywebview.api.set_voice_control(!config.voice_control));
    tuneMenu();
  };
}

function paintEffort() {
  const at = config.efforts.indexOf(config.effort);
  menu.querySelectorAll(".step").forEach((dot, i) => {
    dot.classList.toggle("on", i <= at);
    dot.classList.toggle("current", i === at);
  });
  const caption = $("effort-caption");
  if (caption) caption.textContent = config.effort;
}

/* Both of these drop an @mention into the message rather than doing anything
   themselves. Python expands them on the way out - @file: becomes the file's
   contents, @browser: becomes an instruction to go and look - so what you see
   in the box stays short and still says what it means. */
function attachMenu() {
  showMenu("attach",
    `<button class="menu-item" id="pick-file">
       <span class="body">Add a file<span class="hint">Reads it in as context</span></span>
     </button>` +
    `<button class="menu-item" id="browse-web">
       <span class="body">Browse the web<span class="hint">Sends her to the browser</span></span>
     </button>`);

  $("pick-file").onclick = async () => {
    closeMenu();
    const path = await pywebview.api.pick_file();
    if (path) insert(`@file:"${path}" `);
  };
  $("browse-web").onclick = () => {
    closeMenu();
    insert("@browser: ");
  };
}

/* Put text into the box at the caret rather than replacing what is there, so a
   file can be added to a sentence already half written. */
function insert(text) {
  const at = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, at);
  const after = input.value.slice(input.selectionEnd ?? at);
  const spacer = before && !before.endsWith(" ") ? " " : "";
  input.value = before + spacer + text + after;
  const caret = (before + spacer + text).length;
  input.setSelectionRange(caret, caret);
  input.focus();
  resize();
  updateSend();
}

$("mode").onclick = () => (config ? modeMenu() : null);
$("tune").onclick = () => (config ? tuneMenu() : null);
$("attach").onclick = attachMenu;
$("mic").onclick = () => pywebview.api.dictate();

// Anywhere else dismisses it, the way a Windows flyout does.
document.addEventListener("mousedown", (event) => {
  if (openMenu && !menu.contains(event.target) && !event.target.closest(".toolbar")) {
    closeMenu();
  }
});

$("send").onclick = submit;
$("close").onclick = () => pywebview.api.hide_panel();
$("mute").onclick = () => pywebview.api.send(
  document.body.classList.contains("muted") ? "/speak" : "/mute"
);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") pywebview.api.hide_panel();
});

/* --- startup ------------------------------------------------------------ */

window.addEventListener("pywebviewready", async () => {
  const info = await pywebview.api.ready();
  ready = true;
  // The platform rides along with the theme rather than being applied
  // separately, so the two can never disagree about how the page should look.
  window.panel.theme({ ...info.theme, platform: info.platform });
  $("title").textContent = info.name;
  // Blank until the conversation earns a name. The model and backend are
  // settings you chose, not news, and they were only ever filling the gap.
  setSubtitle("", false);
  $("hotkey").textContent = info.hotkey || "";
  document.body.classList.toggle("muted", !info.speaking);
  config = await pywebview.api.settings();
  paintMode();
  input.focus();
});
