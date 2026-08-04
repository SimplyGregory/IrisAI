"""Build IrisAI.exe.

    python build.py            build
    python build.py --clean    throw away the previous build first

The source is not touched. Everything lands in dist/IrisAI, and that folder is
what the wizard copies into place when the exe is first run.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "IrisAI"


def folder_size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1_048_576:,.0f} MB"


def main() -> int:
    if "--clean" in sys.argv:
        for stale in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(stale, ignore_errors=True)
        print("  cleaned build/ and dist/")

    # A running copy holds its DLLs open, and PyInstaller cannot replace dist/
    # while it does - the build dies partway through with "Access is denied" on
    # whichever file it reached first. Stop it rather than fail.
    stopped = subprocess.run(
        ["taskkill", "/F", "/IM", "IrisAI.exe"], capture_output=True, text=True
    )
    if stopped.returncode == 0:
        print("  stopped a running IrisAI.exe so its files can be replaced")
        time.sleep(2)

    started = time.time()
    print("  building - this takes a few minutes")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "IrisAI.spec"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("\n  build failed")
        return result.returncode

    # PyInstaller leaves an identical-looking exe in build/ that has no runtime
    # beside it, so double-clicking it fails with "Failed to load Python DLL".
    # Two files with the same name and icon, one of which is a trap - delete it.
    # The rest of build/ is the cache that makes rebuilds quick, so it stays.
    stray = ROOT / "build" / "IrisAI" / "IrisAI.exe"
    if stray.is_file():
        stray.unlink()
        print("  removed the intermediate exe from build/ (only dist/ is runnable)")

    exe = DIST / "IrisAI.exe"
    print(f"\n  built in {time.time() - started:.0f}s")
    print(f"  {exe}")
    print(f"  {folder_size(DIST)} in {DIST}")
    print("\n  Run it to get the setup wizard. The source folder is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
