"""macOS notifications via osascript. Silent no-op on other platforms."""

import platform
import shlex
import subprocess


def notify(title: str, body: str, subtitle: str | None = None) -> None:
    if platform.system() != "Darwin":
        return
    # Strings embedded in AppleScript — escape backslashes and quotes so
    # arbitrary summary text can't break out of the string literal.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script_parts = [f'display notification "{esc(body)}"', f'with title "{esc(title)}"']
    if subtitle:
        script_parts.append(f'subtitle "{esc(subtitle)}"')
    script = " ".join(script_parts)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # osascript missing or hung — notifications are best-effort.
        pass
