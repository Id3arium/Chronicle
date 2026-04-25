"""Chronicle CLI entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ingest_cmd(args: argparse.Namespace) -> None:
    from .ingest import ingest_all
    explicit = Path(args.path).expanduser().resolve() if args.path else None
    totals = ingest_all(explicit)
    counts = totals["counts"]
    print(f"Processed {len(totals['files'])} export(s): {', '.join(totals['files']) or '(none)'}")
    print(
        f"  Added:     {len(totals['added'])}\n"
        f"  Updated:   {len(totals['updated'])}\n"
        f"  Unchanged: {len(totals['unchanged'])}\n"
        f"  Deleted:   {len(totals['deleted'])} (tombstoned)"
    )
    print(
        f"Pending: {counts['new']} new · {counts['updated']} updated · "
        f"{counts['awaiting']} awaiting · {counts['stale_entries']} stale entries"
    )


def _status_cmd(args: argparse.Namespace) -> None:
    from .status import print_status
    print_status()


def _summarize_cmd(args: argparse.Namespace) -> None:
    from . import summarize
    summarize.run(args)


def _synthesize_cmd(args: argparse.Namespace) -> None:
    from . import synthesize
    synthesize.run(args)


def _install_agent_cmd(args: argparse.Namespace) -> None:
    from .agent import install
    install()


def _uninstall_agent_cmd(args: argparse.Namespace) -> None:
    from .agent import uninstall
    uninstall()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chronicle", description="Chronicle pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Split export JSON into per-conversation files.")
    p_ingest.add_argument("path", nargs="?", help="Specific export file. Default: every unprocessed file in data/exports/.")
    p_ingest.set_defaults(func=_ingest_cmd)

    p_status = sub.add_parser("status", help="Print pipeline state.")
    p_status.set_defaults(func=_status_cmd)

    p_sum = sub.add_parser("summarize", help="Run Claude over conversations to generate summaries.")
    sum_target = p_sum.add_mutually_exclusive_group()
    sum_target.add_argument("--uuid", help="Summarize a single conversation by UUID.")
    sum_target.add_argument("--all-stale", action="store_true", help="Default. Summarize every stale conversation.")
    sum_target.add_argument("--period", help="Summarize every stale conversation in a given month (YYYY-MM).")
    p_sum.add_argument("--budget", type=float, default=0.50, help="Max USD per claude invocation (default 0.50).")
    p_sum.add_argument("--workers", type=int, default=1, help="Parallel claude invocations (default 1; try 4 for bulk runs). Watch for API rate limits.")
    p_sum.add_argument("--model", default="sonnet", help="Claude model alias passed to `claude --model` (default: sonnet — fast/cheap, good for extraction). Override with 'opus' if a run reads weak.")
    p_sum.set_defaults(func=_summarize_cmd)

    p_syn = sub.add_parser(
        "synthesize",
        help="Build a period entry. Tier inferred from the label "
        "(2026_Apr_19-25=week, 2026_Apr=month, 2026_Q2=quarter, 2026=year).",
    )
    p_syn.add_argument(
        "--period",
        required=True,
        help="Period label. Examples: 2026_Apr_19-25, 2026_Apr, 2026_Q2, 2026.",
    )
    p_syn.add_argument("--budget", type=float, default=2.00, help="Max USD per claude invocation (default 2.00).")
    p_syn.add_argument("--model", default="opus", help="Claude model alias passed to `claude --model` (default: opus — synthesis is the interpretive tier, worth the cost).")
    p_syn.set_defaults(func=_synthesize_cmd)

    p_cap = sub.add_parser(
        "capacity",
        help="Preview what a synthesize call would pack (char count, tokens, missing children).",
    )
    p_cap.add_argument("period", help="Period label (same format as synthesize --period).")
    p_cap.set_defaults(func=lambda a: __import__("chronicle.capacity", fromlist=["run"]).run(a))

    p_install = sub.add_parser("install-agent", help="Install launchd agent that auto-ingests new exports.")
    p_install.set_defaults(func=_install_agent_cmd)
    p_uninstall = sub.add_parser("uninstall-agent", help="Remove the launchd agent.")
    p_uninstall.set_defaults(func=_uninstall_agent_cmd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
