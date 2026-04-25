"""Single wrapper around `claude -p`. Security-hardened:

- `--disallowedTools Bash,Write,Edit,NotebookEdit` means Claude can only
  produce text. Critical because conversation content is attacker-controllable
  (past me, copy-pasted material from anywhere); prompt-injection can't trigger
  filesystem writes if Claude has no file tools.
- `--max-budget-usd` caps runaway cost per invocation.
- The Python wrapper is the only thing that writes files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class ClaudeNotFoundError(RuntimeError):
    pass


class ClaudeInvocationError(RuntimeError):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr


def run_claude(
    instruction_path: Path,
    input_text: str,
    *,
    max_budget_usd: float = 0.50,
    timeout_seconds: int = 600,
) -> str:
    """Run `claude -p` with the instruction file prepended to input_text.

    Returns Claude's stdout (the model's response). Raises on non-zero exit or
    missing binary. No file writes happen here — caller writes the output.
    """
    binary = shutil.which("claude")
    if not binary:
        raise ClaudeNotFoundError(
            "`claude` binary not found on $PATH. Install Claude Code "
            "(https://claude.com/claude-code) and ensure `claude --version` works, "
            "then try again."
        )

    prompt = instruction_path.read_text(encoding="utf-8") + "\n\n---\n\n" + input_text

    cmd = [
        binary,
        "-p",
        prompt,
        "--max-budget-usd",
        str(max_budget_usd),
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit",
    ]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeInvocationError(
            f"claude timed out after {timeout_seconds}s. Re-run with a smaller "
            f"input, or increase timeout via --timeout."
        ) from e

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-20:]
        raise ClaudeInvocationError(
            f"claude exited with code {result.returncode}. Last stderr:\n"
            + "\n".join(tail),
            stderr=result.stderr or "",
        )

    return result.stdout
