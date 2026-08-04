# PyInstaller spec for the macOS build. Run on a Mac; PyInstaller cannot
# cross-compile, so this does nothing useful on Windows.
#
# Kept separate from IrisAI.spec rather than branched inside it: the two differ
# in what they collect (no pywin32, no pywinauto here), what they exclude, and
# in producing a BUNDLE rather than a folder of files. One file trying to be
# both would be harder to read than two that are each honest about one target.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [
    ("panel/ui", "panel/ui"),
    ("installer/ui", "installer/ui"),
    ("vscode_ext", "vscode_ext"),
]

binaries = []
hiddenimports = [
    "webview.platforms.cocoa",  # pywebview's Mac backend, found only at runtime
    "objc",
    "Foundation",
    "AppKit",
    "WebKit",
    "Quartz",
    "ApplicationServices",
]

# The packages whose data files matter: the speech models and the audio stack.
for package in ("faster_whisper", "piper", "sounddevice", "soundfile", "av"):
    try:
        datas += collect_data_files(package)
        binaries += collect_dynamic_libs(package)
    except Exception:
        pass  # not installed yet; the build will say so plainly

# Our own modules, found by walking the folder rather than importing it -
# iris.tools imports every tool module, and those reach for playwright and the
# audio stack, which is exactly what hangs a build when a hook runs them.
PANEL_MODULES = [m.stem for m in Path("panel").glob("*.py")]
hiddenimports += PANEL_MODULES
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
    excludes=[
        # Nothing Windows-only can come along, and several of these do not even
        # exist on a Mac - naming them keeps the failure at build time with a
        # clear message rather than at launch with a traceback.
        "win32com", "win32api", "win32gui", "win32file", "pythoncom",
        "pywinauto", "pygetwindow", "comtypes", "winreg",
        "torch", "scipy", "sklearn", "PyQt5", "PySide2", "tkinter",
        "transformers", "datasets", "safetensors",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="IrisAI",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # whatever this Mac is; universal2 needs universal wheels
    codesign_identity=None,
    entitlements_file=None,
)

collected = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="IrisAI",
)

app = BUNDLE(
    collected,
    name="IrisAI.app",
    icon="claude-logo.icns" if Path("claude-logo.icns").is_file() else None,
    bundle_identifier="com.iris.assistant",
    info_plist={
        "CFBundleName": "Iris",
        "CFBundleDisplayName": "Iris",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        # Iris lives in the menu bar and a flyout panel, not the Dock, and
        # without this the Dock icon appears the moment she speaks.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        # macOS refuses the permission outright unless the reason is declared,
        # and shows these sentences in the dialog the user has to agree to.
        "NSMicrophoneUsageDescription":
            "Iris listens for the wake word and takes spoken commands.",
        "NSSpeechRecognitionUsageDescription":
            "Iris turns what you say into text so it can act on it.",
        "NSAppleEventsUsageDescription":
            "Iris controls other applications on your behalf, such as focusing a "
            "window or opening a file.",
        "NSSystemAdministrationUsageDescription":
            "Iris runs commands you ask it to run.",
    },
)
