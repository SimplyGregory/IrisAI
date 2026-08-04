/* The wizard's four steps.
 *
 * Python owns the answers: every screen reads out of `choices` and writes back
 * into it, and Python validates before letting a step advance. That way the
 * rules live in one place - installer/setup.py - rather than being half in the
 * page and half in the installer, which is how the two drift apart.
 */

const $ = (id) => document.getElementById(id);
const LAST = 4;          // five steps, and then it is installed and gone
let at = 0;
let choices = {};
let options = {};

/* --- steps --------------------------------------------------------------- */

function show(step) {
  at = step;
  document.querySelectorAll(".step").forEach((section) => {
    section.hidden = Number(section.dataset.step) !== step;
  });
  document.querySelectorAll(".steps li").forEach((item) => {
    const n = Number(item.dataset.step);
    item.classList.toggle("at", n === step);
    item.classList.toggle("done", n < step);
  });
  $("back").hidden = step === 0;
  $("next").textContent = step === LAST ? "Install" : "Next";
  $("problem").textContent = "";
}

/* --- reading the screens ------------------------------------------------- */

function collect() {
  const backend = document.querySelector('input[name="backend"]:checked').value;
  return {
    backend,
    api_key: $("api_key").value.trim(),
    cli_path: $("cli_path").value.trim(),
    target: $("target").value.trim(),
    model: choices.model,
    effort: choices.effort,
    max_tokens: Number($("max_tokens").value),
    history: Number($("history").value),
    confirm: choices.confirm,
    redact_pii: $("redact_pii").classList.contains("on"),
    real_chrome_profile: $("real_chrome_profile").classList.contains("on"),
    cdp_port: Number($("cdp_port").value),
    vscode: $("vscode").classList.contains("on"),
    roku: $("roku").classList.contains("on"),
    roku_ip: chosenRoku,
  };
}

/* --- finding the Roku ---------------------------------------------------- */

let chosenRoku = "";

async function findRokus() {
  const host = $("roku_devices");
  host.innerHTML = '<p class="hint">Searching the network...</p>';
  const found = await pywebview.api.find_rokus();

  if (!found.length) {
    host.innerHTML =
      '<p class="hint">No Roku answered. Check it is on the same network, and that ' +
      '"Control by mobile apps" is enabled under Settings, System, Advanced system ' +
      'settings on the television.</p>';
    chosenRoku = "";
    return;
  }

  // Picked from a list rather than typed: nobody knows their television's IP,
  // and one wrong digit is a failure that looks like the feature is broken.
  radioList(
    host,
    found.map((r) => [
      r.ip,
      (r.name || "Roku") + (r.controllable ? "" : "  -  control is off"),
      `${r.ip}${r.model ? " - " + r.model : ""}`,
    ]),
    found[0].ip,
    (ip) => {
      chosenRoku = ip;
      showRokuProblem(found.find((r) => r.ip === ip));
    },
  );
  chosenRoku = found[0].ip;
  showRokuProblem(found[0]);
}

/* A Roku answers questions whether or not it accepts commands, so one that is
 * locked down looks identical here until something is asked of it. Saying so
 * during setup - with the menu path - beats finding out later when a request
 * quietly does nothing. It is not a blocker: the address is saved either way,
 * and switching the setting on afterwards needs no further setup. */
function showRokuProblem(device) {
  const note = $("roku_problem");
  if (!device || device.controllable) {
    note.hidden = true;
    return;
  }
  note.hidden = false;
  note.textContent = device.problem;
}

/* --- building the pickers ------------------------------------------------ */

function radioList(host, items, current, onPick) {
  host.innerHTML = items
    .map(([id, name, hint]) => `
      <label class="card" data-id="${id}">
        <input type="radio" name="${host.id}" value="${id}" ${id === current ? "checked" : ""}>
        <span class="card-body"><b>${name}</b><span>${hint}</span></span>
      </label>`)
    .join("");
  host.querySelectorAll("input").forEach((input) => {
    input.onchange = () => onPick(input.value);
  });
}

function effortDots() {
  const host = $("efforts");
  host.innerHTML = options.efforts
    .map((name) => `<button class="dot" data-effort="${name}" title="${name}"></button>`)
    .join("");
  host.querySelectorAll(".dot").forEach((dot) => {
    dot.onclick = () => { choices.effort = dot.dataset.effort; paintEffort(); };
  });
  paintEffort();
}

function paintEffort() {
  const index = options.efforts.indexOf(choices.effort);
  document.querySelectorAll("#efforts .dot").forEach((dot, i) => {
    dot.classList.toggle("on", i <= index);
    dot.classList.toggle("at", i === index);
  });
  $("effort-name").textContent = choices.effort;
}

/* --- switches ------------------------------------------------------------ */

document.querySelectorAll(".switch").forEach((sw) => {
  sw.onclick = () => {
    const on = sw.classList.toggle("on");
    sw.setAttribute("aria-checked", String(on));
    if (sw.id === "roku") {
      $("roku-fields").hidden = !on;
      // Only when switched on: an SSDP sweep on every wizard load would delay
      // the first screen for something most people will not turn on.
      if (on && !chosenRoku) findRokus();
    }
  };
});

/* --- backend reveal ------------------------------------------------------ */

document.querySelectorAll('input[name="backend"]').forEach((radio) => {
  radio.onchange = () => {
    const sdk = radio.value === "sdk";
    $("sdk-fields").hidden = !sdk;
    $("api-fields").hidden = sdk;
  };
});

/* --- file pickers -------------------------------------------------------- */

document.querySelectorAll("[data-browse]").forEach((button) => {
  button.onclick = async () => {
    const picked = await pywebview.api.pick_file();
    if (picked) $(button.dataset.browse).value = picked;
  };
});

document.querySelectorAll("[data-browse-folder]").forEach((button) => {
  button.onclick = async () => {
    const picked = await pywebview.api.pick_folder();
    if (picked) $(button.dataset.browseFolder).value = picked;
  };
});

$("roku_rescan").onclick = () => findRokus();

/* --- moving between steps ------------------------------------------------ */

$("back").onclick = () => show(Math.max(0, at - 1));

$("next").onclick = async () => {
  const answers = collect();
  const problem = await pywebview.api.check(at, answers);
  if (problem) {
    $("problem").textContent = problem;
    return;
  }

  if (at < LAST) {
    show(at + 1);
    return;
  }

  $("next").disabled = true;
  $("next").textContent = "Installing...";
  const report = await pywebview.api.install(answers);
  $("next").disabled = false;

  if (report.problem) {
    $("problem").textContent = report.problem;
    $("next").textContent = "Install";
    return;
  }

  // No "all done" screen. It would exist only to be dismissed: the panel
  // appearing is the confirmation, and a page telling you setup finished is
  // one more click between you and using the thing.
  pywebview.api.finish();
};

/* --- startup ------------------------------------------------------------- */

window.addEventListener("pywebviewready", async () => {
  const info = await pywebview.api.begin();
  options = info.options;
  choices = { ...info.defaults };

  document.documentElement.dataset.theme = info.theme.dark ? "dark" : "light";
  document.documentElement.style.setProperty("--accent", info.theme.accent);

  $("target").value = info.target;
  $("cli_path").value = choices.cli_path;
  $("max_tokens").value = choices.max_tokens;
  $("history").value = choices.history;
  $("cdp_port").value = choices.cdp_port;
  $("redact_pii").classList.toggle("on", choices.redact_pii);
  $("real_chrome_profile").classList.toggle("on", choices.real_chrome_profile);

  if (info.detected_cli) {
    $("cli-hint").textContent = `Blank is right almost always - found ${info.detected_cli}`;
  }

  // Offering to connect to an editor that is not installed would be a switch
  // that cannot work, so it turns itself off and says why. Default on when
  // VS Code is there: someone who has it is someone who works in it.
  if (info.has_vscode) {
    $("vscode").classList.add("on");
    $("vscode").setAttribute("aria-checked", "true");
  } else {
    $("vscode").disabled = true;
    $("vscode_hint").textContent =
      "VS Code was not found on this machine. Install it first, then run setup " +
      "again if you want Iris to work inside it.";
  }

  radioList($("models"), options.models, choices.model, (id) => (choices.model = id));
  radioList($("safety"), options.safety, choices.confirm, (id) => (choices.confirm = id));
  effortDots();
  show(0);
});
