"""The setup window.

A framed, resizable window rather than the panel's frameless flyout: this one
is a document you work through, and it wants a title bar to move and close like
any other dialog.

The window is only the front of it. Every rule about what is valid, and
everything that gets written to disk, lives in installer/setup.py - so setup
can be run and tested without a screen, and the page cannot disagree with the
installer about what a valid answer is.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from installer import setup
from iris import paths

TITLE = "Set up Iris"


class Api:
    def __init__(self):
        # Underscored deliberately. pywebview walks the public attributes of a
        # js_api object to decide what to expose, and a Window held in a public
        # one sends it into itself until the recursion limit - which shows up
        # as the wizard simply never responding.
        self._window = None
        self.installed_to: Path | None = None

    # --- what the page asks for on load -----------------------------------

    def begin(self) -> dict:
        from installer import vsix
        from iris.agent_sdk import find_cli

        try:
            detected = find_cli()
        except Exception:
            detected = None

        return {
            "defaults": setup.DEFAULTS,
            "target": str(paths.default_install_dir()),
            "detected_cli": detected or "",
            "has_vscode": vsix.is_available(),
            "theme": _theme(),
            "options": {
                "models": setup.MODELS,
                "efforts": setup.EFFORTS,
                "safety": setup.SAFETY,
            },
        }

    # --- moving on --------------------------------------------------------

    def check(self, step: int, answers: dict) -> str:
        """Why this step cannot be left, or "" if it can.

        Each step only answers for its own fields, so a blank API key on the
        last screen does not block the first one - and the whole lot is
        validated again before anything is written.
        """
        answers = {**setup.DEFAULTS, **answers}

        if step == 0:
            if answers["backend"] == "api" and not answers["api_key"].strip():
                return "An API key is needed for the Anthropic API backend."
            if answers["backend"] == "sdk" and answers["cli_path"].strip():
                if not Path(answers["cli_path"]).expanduser().is_file():
                    return "No file there. Leave it blank to detect it automatically."
            if not answers["target"].strip():
                return "Choose somewhere to install."
            return _writable(Path(answers["target"]).expanduser())

        if step == 1:
            if not 1000 <= int(answers["max_tokens"]) <= 64000:
                return "Max tokens per reply should be between 1,000 and 64,000."
            if not 0 <= int(answers["history"]) <= 50:
                return "History should be between 0 and 50 commands."

        if step == 3:
            port = int(answers["cdp_port"])
            if not 1024 <= port <= 65535:
                return "The DevTools port has to be between 1024 and 65535."

        return ""

    def find_rokus(self) -> list[dict]:
        """Every Roku on the network, for the page to offer as a choice.

        Called from the page rather than at startup: it is a multicast sweep
        with a timeout, and delaying the first screen of setup for a feature
        most people will not switch on is the wrong trade.
        """
        from iris import roku

        try:
            found = roku.discover(timeout=4.0)
        except Exception:
            return []

        listed = []
        for device in found:
            model = ""
            try:
                model = roku.device_info(device["ip"]).get("model-name", "")
            except Exception:
                pass  # it answered discovery; a model name is a nicety
            listed.append({"ip": device["ip"], "name": device["name"], "model": model})
        return listed

    def install(self, answers: dict) -> dict:
        problem = setup.validate(answers)
        if problem:
            return {"problem": problem}

        target = Path(answers["target"]).expanduser()
        try:
            report = setup.install(answers, target)
        except OSError as exc:
            return {"problem": f"Could not write to {target}: {exc}"}

        self.installed_to = target
        return report

    # --- pickers ----------------------------------------------------------

    def pick_file(self) -> str:
        import webview

        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Programs (*.exe;*.cmd;*.bat)", "All files (*.*)"),
        )
        return picked[0] if picked else ""

    def pick_folder(self) -> str:
        import webview

        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return picked[0] if picked else ""

    # --- done -------------------------------------------------------------

    def finish(self) -> None:
        self._window.destroy()


def _writable(target: Path) -> str:
    """Check now rather than after four screens of questions."""
    probe = target if target.exists() else target.parent
    try:
        probe.mkdir(parents=True, exist_ok=True)
        test = probe / ".iris-write-test"
        test.write_text("", encoding="utf-8")
        test.unlink()
    except OSError:
        return (
            f"Cannot write to {target}. Program Files needs administrator rights, "
            "and Iris could not save her memory there afterwards either - pick a "
            "folder under your user account."
        )
    return ""


def _theme() -> dict:
    """Match the Windows accent and light/dark, same as the panel does."""
    sys.path.insert(0, str(paths.resource("panel")))
    try:
        import chrome

        return chrome.theme()
    except Exception:
        return {"dark": True, "accent": "#4CC2FF"}


def run() -> Path | None:
    """Show the wizard. Returns where it installed, or None if it was closed."""
    import webview

    sys.path.insert(0, str(paths.resource("panel")))
    try:
        import chrome

        chrome.set_dpi_aware()
    except Exception:
        pass

    ui = paths.resource("installer", "ui", "wizard.html")
    # The logo lives with the panel; the page expects it alongside.
    logo = ui.parent / "claude-logo.png"
    if not logo.is_file():
        source = paths.resource("panel", "ui", "claude-logo.png")
        if source.is_file():
            try:
                shutil.copy2(source, logo)
            except OSError:
                pass

    api = Api()
    window = webview.create_window(
        TITLE, str(ui), js_api=api, width=940, height=660,
        min_size=(820, 600), background_color="#202020",
    )
    api._window = window
    webview.start(gui="edgechromium")
    return api.installed_to


def launch_installed(folder: Path) -> None:
    """Start the copy that was just installed, and let this one go.

    With --show, so setup finishing is visible. Ending on a hidden window and
    a hotkey nobody has learned yet is indistinguishable from setup failing.
    """
    exe = folder / "IrisAI.exe"
    try:
        if exe.is_file():
            subprocess.Popen([str(exe), "--show"], cwd=str(folder), close_fds=True)
        elif not paths.is_frozen():
            subprocess.Popen(
                [sys.executable, str(paths.resource("panel", "app.py")), "--show"],
                cwd=str(paths.resource()), close_fds=True,
            )
    except OSError:
        pass
