#!/bin/bash
# Build Iris for macOS: a .app bundle, then a .dmg to hand to someone.
#
# Run this ON A MAC. PyInstaller cannot cross-compile and hdiutil does not
# exist elsewhere, so there is no way to produce either of these from Windows.
#
#   chmod +x build_macos.sh && ./build_macos.sh
#
# Everything it prints is meant to be readable when it goes wrong, because the
# macOS half of this project has never been run. Expect the first attempt to
# fail; the failures are the point of the first attempt.

set -o pipefail
cd "$(dirname "$0")" || exit 1

LOG="build-macos.log"
: > "$LOG"

say() { printf '\n  %s\n' "$*" | tee -a "$LOG"; }
die() { printf '\n  ERROR: %s\n' "$*" | tee -a "$LOG"; exit 1; }

# --- the things that have to be true before starting ------------------------

[ "$(uname)" = "Darwin" ] || die "this has to run on a Mac ($(uname) is not Darwin)"

command -v python3 >/dev/null || die "python3 is not installed"
python3 - <<'PY' || die "Python 3.11 or newer is needed"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

say "python: $(python3 --version)"
say "on:     $(sw_vers -productName) $(sw_vers -productVersion) ($(uname -m))"

# --- dependencies -----------------------------------------------------------
# pyobjc is the Mac-only half: the permission checks and pywebview's Cocoa
# backend both need it, and neither is in requirements.txt because installing
# it on Windows fails.

say "installing dependencies (this is slow the first time)"
python3 -m pip install --quiet --upgrade pip >>"$LOG" 2>&1
# requirements.txt carries environment markers, so this installs the pyobjc
# frameworks here and skips pywin32 - the same one file works on both machines.
python3 -m pip install --quiet -r requirements.txt >>"$LOG" 2>&1 \
    || die "requirements.txt failed to install - see $LOG"

# --- a quick sanity check before the slow part ------------------------------
# Twelve minutes of packaging only to fail on an import is a bad trade when the
# same failure shows up in two seconds here.

say "checking the code imports before packaging it"
python3 - <<'PY' 2>&1 | tee -a "$LOG"
import sys
sys.path.insert(0, "panel")
problems = []
for module in ("iris.platform", "iris.config", "iris.agent", "iris.tools",
               "installer.setup", "chrome", "hotkey", "bridge", "app"):
    try:
        __import__(module)
    except Exception as exc:
        problems.append(f"  {module}: {type(exc).__name__}: {exc}")
if problems:
    print("imports that failed:")
    print("\n".join(problems))
    sys.exit(1)
from iris import platform
print(f"  platform detected as: {platform.name()}")
print(f"  shell: {platform.shell_argv('echo hi')}")
print(f"  install dir: {platform.default_install_dir()}")
missing = platform.permissions_missing()
print(f"  permissions still to grant: {missing or 'none'}")
PY
[ "${PIPESTATUS[0]}" -eq 0 ] || die "the code does not import cleanly yet - fix that first"

# --- build ------------------------------------------------------------------

say "removing previous build output"
rm -rf build dist

say "packaging (several minutes)"
python3 -m PyInstaller --noconfirm --clean IrisAI-macos.spec >>"$LOG" 2>&1 \
    || die "PyInstaller failed - the last 40 lines of $LOG will say why"

[ -d "dist/IrisAI.app" ] || die "no dist/IrisAI.app was produced - see $LOG"

# An unsigned bundle is quarantined and launches to "damaged and can't be
# opened". This ad-hoc signature does not make it trusted - only a paid Apple
# developer account and notarisation do that - but it does keep macOS from
# refusing it outright on the machine that built it.
say "signing ad-hoc (right-click > Open is still needed on another Mac)"
codesign --force --deep --sign - "dist/IrisAI.app" >>"$LOG" 2>&1 \
    || say "  codesign failed; the app may need right-click > Open"

# --- the disk image ---------------------------------------------------------

say "building the disk image"
STAGING="dist/dmg"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "dist/IrisAI.app" "$STAGING/"
# The symlink is what makes the window say "drag this there", which is the
# install convention every Mac user already knows.
ln -s /Applications "$STAGING/Applications"

rm -f "dist/IrisAI.dmg"
hdiutil create -volname "Iris" -srcfolder "$STAGING" -ov -format UDZO \
    "dist/IrisAI.dmg" >>"$LOG" 2>&1 || die "hdiutil failed - see $LOG"
rm -rf "$STAGING"

SIZE=$(du -h "dist/IrisAI.dmg" | cut -f1)
say "built: dist/IrisAI.dmg ($SIZE)"
say "open it, drag Iris to Applications, then right-click Iris > Open the first time."
say ""
say "On first run macOS will ask for Microphone, and Iris will need Accessibility"
say "and Screen Recording granted by hand in System Settings > Privacy & Security."
