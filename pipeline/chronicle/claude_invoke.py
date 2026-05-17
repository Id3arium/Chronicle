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


def _is_rate_limit_error(result: subprocess.CompletedProcess) -> bool:
    """Check if a failed claude invocation looks like a rate limit."""
    combined = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(s in combined for s in ("rate", "429", "overloaded", "too many"))


def run_claude(
    instruction_path: Path,
    input_text: str,
    *,
    max_budget_usd: float | None = None,
    timeout_seconds: int = 600,
    model: str | None = None,
    max_retries: int = 3,
    retry_wait: int = 30,
) -> str:
    """Run `claude -p` with the instruction file prepended to input_text.

    Returns Claude's stdout (the model's response). Raises on non-zero exit or
    missing binary. No file writes happen here — caller writes the output.

    On rate-limit errors, retries up to max_retries times with retry_wait
    seconds between attempts.
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
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit",
        # Override the user's global defaultMode. If they have plan-mode set
        # globally (common), headless `-p` invocations leak preamble like
        # "Plan exit was denied..." into our entry/summary output. Pipeline
        # calls have all write-tools disabled anyway, so plan mode adds no
        # safety — only contamination.
        "--permission-mode",
        "default",
    ]
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if model:
        cmd.extend(["--model", model])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    import time
    last_error: ClaudeInvocationError | None = None

    for attempt in range(max_retries):
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

        if result.returncode == 0:
            return result.stdout

        # Build the error for reporting / retry decision.
        err_tail = (result.stderr or "").strip().splitlines()[-20:]
        out_tail = (result.stdout or "").strip().splitlines()[-20:]
        parts = [f"claude exited with code {result.returncode}."]
        if err_tail:
            parts.append("Last stderr:\n" + "\n".join(err_tail))
        if out_tail:
            parts.append("Last stdout:\n" + "\n".join(out_tail))
        last_error = ClaudeInvocationError(
            "\n".join(parts),
            stderr=result.stderr or "",
        )

        if _is_rate_limit_error(result) and attempt < max_retries - 1:
            print(
                f"    rate limited — waiting {retry_wait}s "
                f"(attempt {attempt + 1}/{max_retries})",
                flush=True,
            )
            time.sleep(retry_wait)
            continue

        # Non-rate-limit error, or final attempt — don't retry.
        break

    raise last_error
