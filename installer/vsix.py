"""Packing and installing the VS Code extension.

A .vsix is a zip with a manifest beside the extension folder, so it is packed
here with zipfile rather than by vsce. That keeps npm off the critical path:
the extension is plain JavaScript with no dependencies, so there is nothing to
compile, and a machine that has VS Code but no Node toolchain can still
install it.
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from iris import paths

EXTENSION_ID = "iris.iris-bridge"
SOURCE = ("vscode_ext",)  # relative to the program, bundled or from source

# Where VS Code keeps its CLI when it is not on PATH. The user install comes
# first: it is what you get from the normal download, and a machine with both
# is running the user one.
CLI_CANDIDATES = [
    r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd",
    r"%PROGRAMFILES%\Microsoft VS Code\bin\code.cmd",
    r"%PROGRAMFILES(X86)%\Microsoft VS Code\bin\code.cmd",
]

_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}" />
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Categories>Other</Categories>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
</Types>
"""


def find_cli() -> str | None:
    """The VS Code command line, or None if VS Code is not installed."""
    import os

    found = shutil.which("code") or shutil.which("code.cmd")
    if found:
        return found
    for candidate in CLI_CANDIDATES:
        expanded = Path(os.path.expandvars(candidate))
        if "%" not in str(expanded) and expanded.is_file():
            return str(expanded)
    return None


def is_available() -> bool:
    return find_cli() is not None


def _run(cli: str, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cli, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        # code.cmd is a batch file, so this would flash a console window every
        # time - and when frozen there is no console to inherit at all.
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def source_dir() -> Path:
    return paths.resource(*SOURCE)


def pack(into: Path) -> Path:
    """Build the .vsix. Returns the file written."""
    source = source_dir()
    manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))

    into.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(into, "w", zipfile.ZIP_DEFLATED) as vsix:
        vsix.writestr("[Content_Types].xml", _CONTENT_TYPES)
        vsix.writestr(
            "extension.vsixmanifest",
            _MANIFEST.format(
                name=manifest["name"],
                version=manifest["version"],
                publisher=manifest["publisher"],
                display=manifest["displayName"],
                description=manifest["description"],
            ),
        )
        for item in sorted(source.iterdir()):
            if item.is_file() and item.suffix in (".js", ".json"):
                vsix.write(item, f"extension/{item.name}")
    return into


def installed_version(cli: str | None = None) -> str | None:
    """The version VS Code currently has, or None if it has none."""
    cli = cli or find_cli()
    if not cli:
        return None
    try:
        listed = _run(cli, "--list-extensions", "--show-versions", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in listed.stdout.splitlines():
        if line.lower().startswith(EXTENSION_ID + "@"):
            return line.split("@", 1)[1].strip()
    return None


def our_version() -> str:
    return json.loads((source_dir() / "package.json").read_text(encoding="utf-8"))["version"]


def install(into: Path) -> dict:
    """Pack the extension and hand it to VS Code. Never raises."""
    cli = find_cli()
    if not cli:
        return {"ok": False, "problem": "VS Code was not found on this machine."}

    try:
        vsix = pack(into / "iris-bridge.vsix")
    except (OSError, KeyError, ValueError) as exc:
        return {"ok": False, "problem": f"Could not build the extension: {exc}"}

    try:
        # --force so re-running setup upgrades in place instead of refusing
        # because the same version is already there.
        done = _run(cli, "--install-extension", str(vsix), "--force")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "problem": f"Could not run the VS Code CLI: {exc}"}

    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return {"ok": False, "problem": detail[-1] if detail else "VS Code refused the extension."}

    return {
        "ok": True,
        "version": our_version(),
        "vsix": str(vsix),
        "note": "Reload any VS Code window that is already open.",
    }


def uninstall() -> bool:
    cli = find_cli()
    if not cli:
        return False
    try:
        return _run(cli, "--uninstall-extension", EXTENSION_ID).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
