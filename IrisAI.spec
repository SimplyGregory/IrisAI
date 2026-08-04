# PyInstaller build for Iris.  Build with:  python build.py
#
# One windowed exe. The source folder is untouched by this - the build goes to
# dist/ and the exe installs itself into the IrisAI folder from there.
#
# The awkward parts, all for the same underlying reason - these packages are
# imported lazily or dynamically, so the dependency scanner cannot see them:
#
#   pywebview   picks its backend at runtime by name, and the WinForms one
#               drags in pythonnet's runtime assemblies.
#   playwright  ships a Node driver as data, not as Python.
#   faster-whisper / ctranslate2 / onnxruntime carry native DLLs.
#   piper       loads its voice models from ~/.iris at runtime, so only the
#               package itself is bundled; the models stay where they are.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [
    ("panel/ui", "panel/ui"),
    ("installer/ui", "installer/ui"),
    # The VS Code extension travels as source and is zipped into a .vsix at
    # install time. It is plain JavaScript with no dependencies precisely so
    # that packing it needs nothing but Python's zipfile.
    ("vscode_ext", "vscode_ext"),
    ("claude-logo.ico", "."),
]
binaries = []
hiddenimports = [
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "win32com.client",
    "pythoncom",
    "pywintypes",
]

# Nothing here imports anything.
#
# collect_all is deliberately not used. It calls collect_submodules, which
# imports every submodule of a package in a subprocess to enumerate it - and an
# import is arbitrary code. playwright starts its Node driver and waits on it,
# pystray starts a tray backend, sounddevice initialises PortAudio. Any one of
# those hangs the build with every process sitting at 0% CPU and the log frozen
# mid-line, which looks exactly like "slow" until you check.
#
# collect_data_files and collect_dynamic_libs only read the installed files, so
# they are safe. What the scanner then cannot see - anything imported lazily
# inside a function, which is most of the optional machinery here - is listed
# by hand below.
for package in (
    "webview", "clr_loader", "anthropic", "claude_agent_sdk",
    "faster_whisper", "ctranslate2", "onnxruntime", "playwright",
    "sounddevice", "soundfile", "pystray", "piper", "_sounddevice_data",
    "tokenizers", "huggingface_hub",
    # av is faster-whisper's audio decoder, imported at the top of
    # faster_whisper/audio.py. Excluding it as "unused heavy" broke every
    # microphone feature in the build with ModuleNotFoundError.
    "av",
):
    try:
        datas += collect_data_files(package)
        binaries += collect_dynamic_libs(package)
    except Exception:
        pass  # not installed; every one of these is optional

# Imported inside functions, so the dependency scan never sees them.
hiddenimports += [
    "anthropic", "claude_agent_sdk",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub", "av",
    "playwright", "playwright.sync_api",
    "sounddevice", "soundfile", "numpy",
    "pystray", "pystray._win32", "PIL.Image", "PIL.ImageDraw",
    "piper", "onnxruntime",
    "pygetwindow", "pyautogui", "pywinauto", "mss", "dotenv",
]

# panel/ is not a package - its modules are imported as top-level names after
# the folder is put on sys.path, which is why they have to be named explicitly
# and the folder added to pathex. Enumerating "panel" like the others would
# produce "panel.app", which is not what anything imports.
PANEL_MODULES = [m.stem for m in Path("panel").glob("*.py")]
hiddenimports += PANEL_MODULES

# Our own modules, found by walking the folder rather than importing it.
# iris.tools imports every tool module, and those reach for playwright and the
# audio stack - exactly the imports that hang a build when a hook runs them.
for package in ("iris", "installer"):
    for module in Path(package).rglob("*.py"):
        parts = module.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            hiddenimports.append(".".join(parts))

a = Analysis(
    ["IrisAI.py"],
    pathex=[".", "panel"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nothing here is imported by Iris. They are installed on the build
        # machine as other packages' optional extras, and PyInstaller pulls in
        # whatever it can see - so torch alone would add gigabytes, and their
        # hooks are what hung the build twice: each one imports the real
        # package in a subprocess to enumerate it.
        "torch", "torchaudio", "torchvision",
        "scipy", "sklearn", "numba", "llvmlite",
        "transformers", "datasets", "safetensors",
        # pywebview ships a backend for every toolkit; we use WinForms only.
        "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "gtk",
        # Test frameworks and notebook machinery.
        "tkinter", "pytest", "IPython", "notebook", "matplotlib", "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IrisAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no console window; it is a desktop app
    icon="claude-logo.ico",
)

# One folder rather than one file. A --onefile build unpacks ~400MB to a temp
# directory on every launch, which turns a hotkey into a five second wait; this
# way the exe starts immediately and the folder is what gets installed.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="IrisAI",
)
