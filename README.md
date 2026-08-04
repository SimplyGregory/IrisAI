# Iris

An assistant with direct control of the machine, reached through a flyout panel,
a terminal, or the wake word. Runs on Windows and macOS from one codebase.

## Getting started

```bash
git clone https://github.com/SimplyGregory/IrisAI
cd IrisAI
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 selftest.py --no-build
```

Needs Python 3.11 or newer.

## Testing

```bash
python3 selftest.py --no-build
```

Twenty checks - dependencies, shell, windows, screen, voice, network, the
editor bridge, the panel - each reported on its own line as `ok`, `skipped` or
`FAILED` with the exact error. One failure never stops the rest.

`skipped` is not a failure. It means the check does not apply here: no VS Code
installed, no `.env` yet, or a permission Windows grants automatically.

Four things the script cannot judge for you, because they need eyes or ears:

```bash
python3 main.py                    # text mode: the agent, no window code involved
python3 IrisAI.py --hotkey-test    # press the hotkey and watch for it
python3 selftest.py --panel        # opens the panel for six seconds
python3 selftest.py --speak        # says a line out loud
```

Start with `main.py`. It exercises the whole agent while touching none of the
window code, so if it works, anything still broken is the panel.

## Building

```bash
python3 selftest.py
```

The same checks, then it builds for whichever system it is on - a folder and an
`.exe` on Windows, an `.app` and a `.dmg` on macOS. It refuses to build if
anything failed, since a green build over a red check only hides the problem.

## First run

There is no `.env` in this repository, deliberately - it holds an API key, and a
copied one carries the previous machine's microphone calibration. Make your own:

```bash
python3 IrisAI.py --setup
```

## macOS permissions

Screenshots need Screen Recording, and clicking or typing needs Accessibility.
Until they are granted, those actions **silently do nothing** - no error, no
effect. `selftest.py` names whichever is missing.

An application that has never asked does not appear in that list at all, so run
something that needs the permission first (`--hotkey-test` will do), let macOS
prompt, then grant it in System Settings > Privacy & Security and run again.

The build is unsigned, so the first launch is right-click > Open rather than a
double-click.

## Layout

```
iris/            the agent loop, tools, memory, redaction
iris/platform/   the handful of things Windows and macOS cannot share
panel/           the flyout window and the page inside it
installer/       the setup wizard and what it writes
vscode_ext/      a bridge extension, so Iris can see inside the editor
selftest.py      checks this machine, then builds for it
```
