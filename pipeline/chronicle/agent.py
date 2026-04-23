"""launchd agent for auto-ingesting files dropped into data/exports/.

WatchPaths fires on kernel-level FS events — zero idle CPU. The agent just
runs `chronicle ingest` once whenever a file in data/exports/ changes, then
exits. Opt-in: users who want manual control never install it.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from .paths import exports_dir

AGENT_LABEL = "com.chronicle.autoingest"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "Chronicle"


def _chronicle_binary() -> str:
    binary = shutil.which("chronicle")
    if not binary:
        raise SystemExit(
            "`chronicle` binary not on $PATH. Install the pipeline with "
            "`pip install -e ./pipeline` (from the repo root) and re-run."
        )
    return binary


def install() -> None:
    binary = _chronicle_binary()
    log_dir().mkdir(parents=True, exist_ok=True)
    exports_dir().mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [binary, "ingest"],
        "WatchPaths": [str(exports_dir())],
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir() / "autoingest.out.log"),
        "StandardErrorPath": str(log_dir() / "autoingest.err.log"),
    }

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    # Unload first in case it was previously loaded, then load. Errors are
    # non-fatal — `launchctl unload` complains harmlessly if nothing is loaded.
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"launchctl load failed: {result.stderr.strip()}\n"
            f"Plist is at {path}. You can try `launchctl load {path}` manually."
        )

    print(f"✓ Installed launchd agent {AGENT_LABEL}")
    print(f"  Watching: {exports_dir()}")
    print(f"  Plist:    {path}")
    print(f"  Logs:     {log_dir()}")


def uninstall() -> None:
    path = plist_path()
    if not path.exists():
        print("No launchd agent installed. Nothing to do.")
        return
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    print(f"✓ Uninstalled launchd agent {AGENT_LABEL}")
