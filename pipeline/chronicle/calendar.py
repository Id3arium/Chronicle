"""Period label parsing + calendar math for Chronicle rollups.

Label formats:
  week:    2026_Apr_19-25   (Mon–Sun, range is the two dates in the month)
  month:   2026_Apr
  quarter: 2026_Q2
  year:    2026

All ranges are inclusive on both ends, YYYY-MM-DD strings (UTC day-boundary
comparisons happen elsewhere).
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

    tier ∈ {"week", "month", "quarter", "year"}.
    Dates are "YYYY-MM-DD" strings.
    """
    if re.fullmatch(r"\d{4}", label):
        return _parse_year(label)
    m = re.fullmatch(r"(\d{4})_Q([1-4])", label)
    if m:
        return _parse_quarter(int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{4})_([A-Z][a-z]{2})_(\d{1,2})-(\d{1,2})", label)
    if m:
        return _parse_week(
            int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        )
    m = re.fullmatch(r"(\d{4})_([A-Z][a-z]{2})", label)
    if m:
        return _parse_month(int(m.group(1)), m.group(2))
    raise PeriodParseError(
        f"Unrecognized period label '{label}'. Expected one of: "
        f"'2026', '2026_Q2', '2026_Apr', '2026_Apr_19-25'."
    )


def _parse_year(label: str) -> tuple[str, str, str]:
    y = int(label)
    return ("year", f"{y}-01-01", f"{y}-12-31")


def _parse_quarter(year: int, q: int) -> tuple[str, str, str]:
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    # Last day of end_month.
    if end_month == 12:
        last_day = 31
    else:
        last_day = (date(year, end_month + 1, 1) - timedelta(days=1)).day
    return (
        "quarter",
        f"{year:04d}-{start_month:02d}-01",
        f"{year:04d}-{end_month:02d}-{last_day:02d}",
    )


def _parse_month(year: int, abbr: str) -> tuple[str, str, str]:
    month = ABBR_TO_MONTH.get(abbr)
    if month is None:
        raise PeriodParseError(
            f"Unknown month abbreviation '{abbr}'. Use Jan, Feb, Mar, …, Dec."
        )
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return (
        "month",
        f"{year:04d}-{month:02d}-01",
        f"{year:04d}-{month:02d}-{last_day:02d}",
    )


def _parse_week(year: int, abbr: str, d_start: int, d_end: int) -> tuple[str, str, str]:
    month = ABBR_TO_MONTH.get(abbr)
    if month is None:
        raise PeriodParseError(f"Unknown month abbreviation '{abbr}'.")
    if d_end < d_start:
        raise PeriodParseError(
            f"Week label has end day {d_end} before start day {d_start}."
        )
    return (
        "week",
        f"{year:04d}-{month:02d}-{d_start:02d}",
        f"{year:04d}-{month:02d}-{d_end:02d}",
    )


# ────────────────── tier → children labels ──────────────────

def child_tier(tier: str) -> str | None:
    """The tier one step down. None if already at the lowest (week)."""
    return {"year": "quarter", "quarter": "month", "month": "week", "week": None}[tier]


def children_for(label: str) -> list[str]:
    """Labels of the tier immediately below this one, covering its range."""
    tier, start_iso, end_iso = parse_period(label)
    child = child_tier(tier)
    if child is None:
        return []
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    if child == "quarter":
        year = int(label)
        return [f"{year}_Q{q}" for q in range(1, 5)]
    if child == "month":
        # covers the label's range.
        year = start.year
        q = (start.month - 1) // 3 + 1
        months = range((q - 1) * 3 + 1, (q - 1) * 3 + 4)
        return [f"{year}_{MONTH_ABBR[m - 1]}" for m in months]
    if child == "week":
        return weeks_in_range(start, end)
    return []


def weeks_in_range(start: date, end: date) -> list[str]:
    """Mon–Sun ISO weeks that intersect [start, end], clipped to month
    boundaries so a week label never crosses months (labels carry a single
    month abbreviation). That means a week split by a month boundary
    becomes two labels (one per month), which is intentional — easier to
    scan 2026_Apr_27-30 + 2026_May_1-3 than invent cross-month syntax.
    """
    labels: list[str] = []
    current = start
    while current <= end:
        # Find Monday on/before current.
        monday = current - timedelta(days=current.weekday())
        sunday = monday + timedelta(days=6)
        # Clip to the calendar month current belongs to.
        month_start = current.replace(day=1)
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1, day=1)
        else:
            next_month = current.replace(month=current.month + 1, day=1)
        month_end = next_month - timedelta(days=1)
        win_start = max(monday, month_start, start)
        win_end = min(sunday, month_end, end)
        abbr = MONTH_ABBR[win_start.month - 1]
        labels.append(
            f"{win_start.year}_{abbr}_{win_start.day}-{win_end.day}"
        )
        current = win_end + timedelta(days=1)
    return labels
