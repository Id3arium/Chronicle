"""Period label parsing + calendar math for Chronicle rollups.

Tier hierarchy (lowest → highest):
  half:    2026_04_H1     (days 1–15)   — reads conversation summaries
  half:    2026_04_H2     (days 16–end) — reads conversation summaries
  half:    2026_04_H1-H2  (full month)  — sparse-month merged form,
                                           same tier as a half, just wider
  half:    2026_04        (alias of H1-H2; produced by auto-merge logic)
  quarter: 2026_Q2                      — reads 6 halves directly
  year:    2026                         — reads 4 quarters

Canonical labels use numeric months (2026_04_H1). Abbreviated forms
(2026_Apr_H1) and dash-separated forms (2026-04-H1) are accepted as
input but canonicalized to the numeric underscore form.

H1 is always days 1–15, H2 is day 16 through the last day of the month.
H1-H2 is the full month. Ranges are inclusive.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ABBR_TO_MONTH = {m: i + 1 for i, m in enumerate(MONTH_ABBR)}


class PeriodParseError(ValueError):
    pass


# ────────────────── label → (start, end) ──────────────────

def parse_period(label: str) -> tuple[str, str, str]:
    """Return (tier, range_start_iso, range_end_iso) from a period label.

    tier ∈ {"half", "quarter", "year"}. Note: there is no "month" tier
    anymore — `2026_Apr` and `2026_Apr_H1-H2` both parse as half-tier
    entries spanning the whole month. They are aliases.

    Both abbreviated-month (2026_Apr) and numeric-month (2026_04) forms
    are accepted everywhere interchangeably.
    """
    # Normalize dash-separated half/month forms into canonical underscore form.
    # Accepts: 2026-03-h1, 2026-03-H1, 2026-03-h2, 2026-03-h1-h2, 2026-03
    # Also handles case: h1 → H1, q2 → Q2
    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])-(h[12](?:-h[12])?)", label, re.IGNORECASE)
    if m:
        suffix = m.group(3).upper()  # h1 → H1, h1-h2 → H1-H2
        return parse_period(f"{m.group(1)}_{m.group(2)}_{suffix}")
    # Bare dash-month: 2026-03 (without day or half suffix)
    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", label)
    if m:
        return parse_period(f"{m.group(1)}_{m.group(2)}")
    # Dash-separated quarter: 2026-q2 or 2026-Q2
    m = re.fullmatch(r"(\d{4})-([qQ][1-4])", label)
    if m:
        return parse_period(f"{m.group(1)}_{m.group(2).upper()}")

    # Single-day form: 2026-04-22 or 2026_04_22. Only used for
    # `summarize --period` to scope a run to one calendar day. Synthesize
    # never targets day tier — half is the lowest entry tier.
    m = re.fullmatch(r"(\d{4})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])", label)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Validate it's a real date (e.g. reject Feb 30).
        try:
            date(y, mo, d)
        except ValueError as e:
            raise PeriodParseError(f"Invalid date '{label}': {e}")
        iso = f"{y:04d}-{mo:02d}-{d:02d}"
        return ("day", iso, iso)
    if re.fullmatch(r"\d{4}", label):
        return _parse_year(label)
    m = re.fullmatch(r"(\d{4})_Q([1-4])", label)
    if m:
        return _parse_quarter(int(m.group(1)), int(m.group(2)))
    # Numeric month forms: 2026_04, 2026_04_H1, 2026_04_H2, 2026_04_H1-H2
    m = re.fullmatch(r"(\d{4})_(0[1-9]|1[0-2])(?:_(.+))?", label)
    if m:
        abbr = MONTH_ABBR[int(m.group(2)) - 1]
        suffix = m.group(3)  # None, "H1", "H2", or "H1-H2"
        rebuilt = f"{m.group(1)}_{abbr}" + (f"_{suffix}" if suffix else "")
        return parse_period(rebuilt)  # recurse with canonical abbr form
    # Abbreviated month forms
    m = re.fullmatch(r"(\d{4})_([A-Z][a-z]{2})_H1-H2", label)
    if m:
        return _parse_merged_half(int(m.group(1)), m.group(2))
    m = re.fullmatch(r"(\d{4})_([A-Z][a-z]{2})_H([12])", label)
    if m:
        return _parse_half(int(m.group(1)), m.group(2), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})_([A-Z][a-z]{2})", label)
    if m:
        # 2026_Apr is an alias for 2026_Apr_H1-H2.
        return _parse_merged_half(int(m.group(1)), m.group(2))
    raise PeriodParseError(
        f"Unrecognized period label '{label}'. Expected one of: "
        f"'2026', '2026_Q2', '2026_Apr' or '2026_04', "
        f"'2026_Apr_H1' or '2026_04_H1', '2026_Apr_H2', '2026_Apr_H1-H2', "
        f"'2026-04-22' (single day, summarize only)."
    )


def _parse_year(label: str) -> tuple[str, str, str]:
    y = int(label)
    return ("year", f"{y}-01-01", f"{y}-12-31")


def _parse_quarter(year: int, q: int) -> tuple[str, str, str]:
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    last_day = _last_day_of_month(year, end_month)
    return (
        "quarter",
        f"{year:04d}-{start_month:02d}-01",
        f"{year:04d}-{end_month:02d}-{last_day:02d}",
    )


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _parse_merged_half(year: int, abbr: str) -> tuple[str, str, str]:
    month = ABBR_TO_MONTH.get(abbr)
    if month is None:
        raise PeriodParseError(
            f"Unknown month abbreviation '{abbr}'. Use Jan, Feb, Mar, …, Dec."
        )
    last_day = _last_day_of_month(year, month)
    return (
        "half",
        f"{year:04d}-{month:02d}-01",
        f"{year:04d}-{month:02d}-{last_day:02d}",
    )


def _parse_half(year: int, abbr: str, h: int) -> tuple[str, str, str]:
    month = ABBR_TO_MONTH.get(abbr)
    if month is None:
        raise PeriodParseError(f"Unknown month abbreviation '{abbr}'.")
    if h == 1:
        return (
            "half",
            f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-15",
        )
    last_day = _last_day_of_month(year, month)
    return (
        "half",
        f"{year:04d}-{month:02d}-16",
        f"{year:04d}-{month:02d}-{last_day:02d}",
    )


# ────────────────── tier → children labels ──────────────────

def child_tier(tier: str) -> str | None:
    """The tier one step down. None if already at the lowest (half)."""
    return {"year": "quarter", "quarter": "half", "half": None, "day": None}[tier]


def canonical_merged_label(year: int, month: int) -> str:
    """The label we produce for an auto-merged sparse month: 2026_04_H1-H2."""
    return f"{year}_{month:02d}_H1-H2"


def canonical_label(label: str) -> str:
    """Convert any accepted label form to the canonical numeric form.

    2026-03-h1   → 2026_03_H1
    2026-03      → 2026_03
    2026-q2      → 2026_Q2
    2026_Apr_H1  → 2026_04_H1
    2026_04_H1   → 2026_04_H1  (already canonical, returned as-is)
    2026         → 2026          (already canonical)
    """
    tier, rs, re_ = parse_period(label)
    start = date.fromisoformat(rs)
    if tier == "day":
        return rs  # ISO date is its own canonical form
    if tier == "year":
        return str(start.year)
    if tier == "quarter":
        q = (start.month - 1) // 3 + 1
        return f"{start.year}_Q{q}"
    # half tier — determine H1, H2, or H1-H2 from the date range
    mm = f"{start.month:02d}"
    end = date.fromisoformat(re_)
    if start.day == 1 and end.day <= 15:
        return f"{start.year}_{mm}_H1"
    if start.day == 16:
        return f"{start.year}_{mm}_H2"
    # Full month (H1-H2 or bare month alias)
    return f"{start.year}_{mm}_H1-H2"


def children_for(label: str) -> list[str]:
    """Labels of the tier immediately below this one, covering its range.

    Quarterly returns 6 half labels (H1+H2 for each of 3 months). The caller
    is responsible for checking whether a merged H1-H2 entry exists and
    using it in place of both halves — see synthesize._gather_rollup_inputs.
    """
    tier, start_iso, _end_iso = parse_period(label)
    child = child_tier(tier)
    if child is None:
        return []
    start = date.fromisoformat(start_iso)
    if child == "quarter":
        year = int(label)
        return [f"{year}_Q{q}" for q in range(1, 5)]
    if child == "half":
        # Quarterly: 6 halves across 3 months. The synthesize layer prefers
        # a merged H1-H2 entry over individual halves when one exists.
        year = start.year
        q = (start.month - 1) // 3 + 1
        out = []
        for m in range((q - 1) * 3 + 1, (q - 1) * 3 + 4):
            out.append(f"{year}_{m:02d}_H1")
            out.append(f"{year}_{m:02d}_H2")
        return out
    return []
