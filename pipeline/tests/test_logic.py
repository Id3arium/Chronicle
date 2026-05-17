"""Pure-logic tests for the parts that fail silently a tier later.

Scope is deliberate: the freshness/cascade rules in state.py and the
sparse-month merge logic in synthesize.py. These take plain dicts, have
no I/O, and a wrong answer here surfaces as a wrong *quarter or year*
entry long after the mistake — exactly the kind of bug that warrants a
regression net. The Claude-calling paths are integration-only and not
covered here.

Run: `uv run python -m unittest discover -s pipeline/tests` (or
`python -m unittest` from inside pipeline/).
"""

from __future__ import annotations

import unittest

from chronicle import state as state_mod
from chronicle.metrics import (
    entry_body,
    parse_frontmatter,
    render_with_frontmatter,
    split_frontmatter,
)
from chronicle.calendar import (
    PeriodParseError,
    canonical_label,
    children_for,
    parse_period,
)
from chronicle.synthesize import (
    _half_label_kind,
    _resolve_children_with_merges,
    _resolve_half_label,
)


def _conv(created, updated, summarized=None, deleted=None):
    c = {"created_at": created, "updated_at": updated}
    if summarized is not None:
        c["summarized_at"] = summarized
    if deleted is not None:
        c["deleted_at"] = deleted
    return c


# ────────────────── state.summary_stale ──────────────────

class SummaryStale(unittest.TestCase):
    def test_no_summary_is_stale(self):
        self.assertTrue(state_mod.summary_stale(
            _conv("2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z")))

    def test_summarized_after_update_is_fresh(self):
        self.assertFalse(state_mod.summary_stale(_conv(
            "2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z",
            summarized="2026-04-03T00:00:00Z")))

    def test_updated_after_summary_is_stale(self):
        self.assertTrue(state_mod.summary_stale(_conv(
            "2026-04-01T00:00:00Z", "2026-04-05T00:00:00Z",
            summarized="2026-04-03T00:00:00Z")))

    def test_equal_timestamps_is_fresh(self):
        # updated_at == summarized_at: not strictly greater, so fresh.
        self.assertFalse(state_mod.summary_stale(_conv(
            "2026-04-01T00:00:00Z", "2026-04-03T00:00:00Z",
            summarized="2026-04-03T00:00:00Z")))

    def test_deleted_is_never_stale(self):
        # Deleted conversations must not pull into a summarize run even if
        # they look stale on timestamps.
        self.assertFalse(state_mod.summary_stale(_conv(
            "2026-04-01T00:00:00Z", "2026-04-09T00:00:00Z",
            summarized="2026-04-03T00:00:00Z",
            deleted="2026-04-10T00:00:00Z")))


# ────────────────── state.entry_stale + cascade ──────────────────

class EntryStaleCascade(unittest.TestCase):
    def _state(self):
        return {
            "conversations": {
                "a": _conv("2026-04-02T00:00:00Z", "2026-04-02T00:00:00Z",
                           summarized="2026-04-03T00:00:00Z"),
            },
            "entries": {},
        }

    def test_missing_entry_is_stale(self):
        st = self._state()
        self.assertTrue(state_mod.entry_stale(
            st, "2026_Apr_H1", "2026-04-01", "2026-04-15"))

    def test_fresh_entry_not_stale(self):
        st = self._state()
        st["entries"]["2026_Apr_H1"] = {"synthesized_at": "2026-04-04T00:00:00Z"}
        self.assertFalse(state_mod.entry_stale(
            st, "2026_Apr_H1", "2026-04-01", "2026-04-15"))

    def test_conversation_updated_after_entry_is_stale(self):
        st = self._state()
        st["entries"]["2026_Apr_H1"] = {"synthesized_at": "2026-04-04T00:00:00Z"}
        st["conversations"]["a"]["updated_at"] = "2026-04-05T00:00:00Z"
        self.assertTrue(state_mod.entry_stale(
            st, "2026_Apr_H1", "2026-04-01", "2026-04-15"))

    def test_child_resynthesized_after_parent_cascades_stale(self):
        # The load-bearing cascade clause: a quarter is stale if a child
        # half was re-synthesized after the quarter was built. This is the
        # exact Q2-after-late-edit scenario.
        st = {
            "conversations": {},
            "entries": {
                "2026_Q2": {
                    "synthesized_at": "2026-07-01T00:00:00Z",
                    "children": ["2026_Apr_H1"],
                },
                "2026_Apr_H1": {"synthesized_at": "2026-07-05T00:00:00Z"},
            },
        }
        self.assertTrue(state_mod.entry_stale(
            st, "2026_Q2", "2026-04-01", "2026-06-30"))

    def test_child_older_than_parent_not_stale(self):
        st = {
            "conversations": {},
            "entries": {
                "2026_Q2": {
                    "synthesized_at": "2026-07-10T00:00:00Z",
                    "children": ["2026_Apr_H1"],
                },
                "2026_Apr_H1": {"synthesized_at": "2026-07-05T00:00:00Z"},
            },
        }
        self.assertFalse(state_mod.entry_stale(
            st, "2026_Q2", "2026-04-01", "2026-06-30"))

    def test_deleted_conversation_excluded_from_period(self):
        st = {
            "conversations": {
                "a": _conv("2026-04-02T00:00:00Z", "2026-09-09T00:00:00Z",
                           summarized="2026-04-03T00:00:00Z",
                           deleted="2026-09-10T00:00:00Z"),
            },
            "entries": {
                "2026_Apr_H1": {"synthesized_at": "2026-04-20T00:00:00Z"},
            },
        }
        # The deleted conv has a very late updated_at; if it weren't
        # excluded the entry would read stale.
        self.assertFalse(state_mod.entry_stale(
            st, "2026_Apr_H1", "2026-04-01", "2026-04-15"))


# ────────────────── calendar.parse_period ──────────────────

class ParsePeriod(unittest.TestCase):
    def test_half_h1_days(self):
        self.assertEqual(parse_period("2026_Apr_H1"),
                         ("half", "2026-04-01", "2026-04-15"))

    def test_half_h2_through_month_end(self):
        self.assertEqual(parse_period("2026_Apr_H2"),
                         ("half", "2026-04-16", "2026-04-30"))

    def test_h2_february_non_leap(self):
        self.assertEqual(parse_period("2025_Feb_H2"),
                         ("half", "2025-02-16", "2025-02-28"))

    def test_h2_february_leap(self):
        self.assertEqual(parse_period("2024_Feb_H2"),
                         ("half", "2024-02-16", "2024-02-29"))

    def test_merged_half_full_month(self):
        self.assertEqual(parse_period("2026_Apr_H1-H2"),
                         ("half", "2026-04-01", "2026-04-30"))

    def test_bare_month_is_merged_half_alias(self):
        self.assertEqual(parse_period("2026_Apr"),
                         parse_period("2026_Apr_H1-H2"))

    def test_numeric_month_equals_abbr(self):
        self.assertEqual(parse_period("2026_04_H1"),
                         parse_period("2026_Apr_H1"))

    def test_quarter(self):
        self.assertEqual(parse_period("2026_Q2"),
                         ("quarter", "2026-04-01", "2026-06-30"))

    def test_q4_ends_dec_31(self):
        self.assertEqual(parse_period("2026_Q4"),
                         ("quarter", "2026-10-01", "2026-12-31"))

    def test_year(self):
        self.assertEqual(parse_period("2026"),
                         ("year", "2026-01-01", "2026-12-31"))

    def test_single_day(self):
        self.assertEqual(parse_period("2026-04-22"),
                         ("day", "2026-04-22", "2026-04-22"))

    def test_dash_forms_normalize(self):
        self.assertEqual(parse_period("2026-04-h1"),
                         parse_period("2026_Apr_H1"))
        self.assertEqual(parse_period("2026-q2"), parse_period("2026_Q2"))

    def test_invalid_date_rejected(self):
        with self.assertRaises(PeriodParseError):
            parse_period("2026-02-30")

    def test_garbage_rejected(self):
        with self.assertRaises(PeriodParseError):
            parse_period("not-a-period")


# ────────────────── calendar.canonical_label round-trip ──────────────────

class CanonicalLabel(unittest.TestCase):
    def test_idempotent_on_canonical_forms(self):
        for lbl in ("2026_Apr_H1", "2026_Apr_H2", "2026_Apr_H1-H2",
                    "2026_Q2", "2026"):
            self.assertEqual(canonical_label(lbl), lbl, lbl)

    def test_numeric_month_canonicalizes_to_abbr(self):
        self.assertEqual(canonical_label("2026_04_H1"), "2026_Apr_H1")

    def test_dash_forms_canonicalize(self):
        self.assertEqual(canonical_label("2026-03-h1"), "2026_Mar_H1")
        self.assertEqual(canonical_label("2026-q2"), "2026_Q2")


# ────────────────── calendar.children_for ──────────────────

class ChildrenFor(unittest.TestCase):
    def test_year_children_are_four_quarters(self):
        self.assertEqual(children_for("2026"),
                         ["2026_Q1", "2026_Q2", "2026_Q3", "2026_Q4"])

    def test_quarter_children_are_six_halves(self):
        self.assertEqual(
            children_for("2026_Q2"),
            ["2026_Apr_H1", "2026_Apr_H2",
             "2026_May_H1", "2026_May_H2",
             "2026_Jun_H1", "2026_Jun_H2"])

    def test_half_has_no_children(self):
        self.assertEqual(children_for("2026_Apr_H1"), [])


# ────────────────── synthesize sparse-month merge logic ──────────────────

class HalfLabelKind(unittest.TestCase):
    def test_h1_h2_classification(self):
        self.assertEqual(_half_label_kind("2026_Apr_H1"), "half_h1")
        self.assertEqual(_half_label_kind("2026_Apr_H2"), "half_h2")
        self.assertEqual(_half_label_kind("2026_04_H1"), "half_h1")

    def test_merged_and_bare_month_are_merged(self):
        self.assertEqual(_half_label_kind("2026_Apr_H1-H2"), "merged")
        self.assertEqual(_half_label_kind("2026_Apr"), "merged")
        self.assertEqual(_half_label_kind("2026_04"), "merged")

    def test_non_half_labels_are_none(self):
        self.assertIsNone(_half_label_kind("2026_Q2"))
        self.assertIsNone(_half_label_kind("2026"))


class ResolveHalfLabel(unittest.TestCase):
    def _state_with_n_convs(self, n, month="2026-04"):
        convs = {}
        for i in range(n):
            day = f"{(i % 28) + 1:02d}"
            convs[f"u{i}"] = _conv(
                f"{month}-{day}T00:00:00Z", f"{month}-{day}T00:00:00Z")
        return {"conversations": convs, "entries": {}}

    def test_sparse_h1_redirects_to_merged(self):
        st = self._state_with_n_convs(4)
        label, msg = _resolve_half_label(st, "2026_Apr_H1")
        self.assertEqual(label, "2026_Apr_H1-H2")
        self.assertIsNotNone(msg)

    def test_dense_h1_passes_through(self):
        st = self._state_with_n_convs(15)
        label, msg = _resolve_half_label(st, "2026_Apr_H1")
        self.assertEqual(label, "2026_Apr_H1")
        self.assertIsNone(msg)

    def test_dense_merged_form_refused(self):
        st = self._state_with_n_convs(15)
        with self.assertRaises(SystemExit):
            _resolve_half_label(st, "2026_Apr_H1-H2")

    def test_sparse_bare_month_normalizes_to_canonical_merged(self):
        st = self._state_with_n_convs(4)
        label, msg = _resolve_half_label(st, "2026_Apr")
        self.assertEqual(label, "2026_Apr_H1-H2")
        self.assertIsNotNone(msg)

    def test_threshold_boundary_is_sparse_below_only(self):
        # SPARSE_MONTH_THRESHOLD == 10: exactly 10 is dense (>=), 9 sparse.
        st9 = self._state_with_n_convs(9)
        self.assertEqual(
            _resolve_half_label(st9, "2026_Apr_H1")[0], "2026_Apr_H1-H2")
        st10 = self._state_with_n_convs(10)
        self.assertEqual(
            _resolve_half_label(st10, "2026_Apr_H1")[0], "2026_Apr_H1")


class ResolveChildrenWithMerges(unittest.TestCase):
    def test_both_halves_collapse_to_merged_when_present(self):
        st = {"entries": {"2026_Apr_H1-H2": {}}}
        needed = ["2026_Apr_H1", "2026_Apr_H2", "2026_May_H1"]
        self.assertEqual(
            _resolve_children_with_merges(st, needed),
            ["2026_Apr_H1-H2", "2026_May_H1"])

    def test_no_merged_entry_leaves_halves_intact(self):
        st = {"entries": {}}
        needed = ["2026_Apr_H1", "2026_Apr_H2"]
        self.assertEqual(
            _resolve_children_with_merges(st, needed),
            ["2026_Apr_H1", "2026_Apr_H2"])

    def test_merged_takes_position_of_first_half(self):
        st = {"entries": {"2026_May_H1-H2": {}}}
        needed = ["2026_Apr_H1", "2026_May_H1", "2026_May_H2", "2026_Jun_H1"]
        self.assertEqual(
            _resolve_children_with_merges(st, needed),
            ["2026_Apr_H1", "2026_May_H1-H2", "2026_Jun_H1"])


# ────────────────── metrics frontmatter split/render ──────────────────

class FrontmatterSplit(unittest.TestCase):
    def test_body_thematic_break_not_mistaken_for_fence(self):
        # The exact bug the rewrite fixes: a `---` separator in the prose
        # body must NOT be read as the closing frontmatter fence.
        doc = (
            "---\n"
            "title: Example\n"
            "significance: high\n"
            "---\n\n"
            "First section.\n\n"
            "---\n\n"
            "Second section after a thematic break.\n"
        )
        fm, body = split_frontmatter(doc)
        self.assertEqual(fm, {"title": "Example", "significance": "high"})
        self.assertIn("First section.", body)
        self.assertIn("Second section after a thematic break.", body)
        # The body's own --- survived intact (not consumed as a fence).
        self.assertIn("\n---\n", "\n" + body + "\n")

    def test_no_frontmatter_returns_text_unchanged(self):
        doc = "# Just a heading\n\nNo frontmatter here.\n"
        fm, body = split_frontmatter(doc)
        self.assertEqual(fm, {})
        self.assertEqual(body, doc)

    def test_unterminated_frontmatter_returns_empty(self):
        doc = "---\ntitle: x\nno closing fence ever\n"
        fm, body = split_frontmatter(doc)
        self.assertEqual(fm, {})
        self.assertEqual(body, doc)

    def test_round_trip_preserves_key_order_and_body(self):
        fields = {"period": "2026_Q2", "tier": "quarter", "is_partial": "true"}
        body = "## The Record\n\nstuff\n\n---\n\n## Sources\n- a\n"
        rendered = render_with_frontmatter(fields, body)
        fm2, body2 = split_frontmatter(rendered)
        self.assertEqual(list(fm2.keys()), list(fields.keys()))
        self.assertEqual(fm2, fields)
        self.assertEqual(body2, body)

    def test_inject_into_existing_frontmatter_appends_keys(self):
        # Mirrors _inject_metrics: parse, add keys, reserialize.
        doc = "---\ntitle: T\n---\n\nBody with --- inside.\n"
        fm, body = split_frontmatter(doc)
        fm["summary_words"] = 1234
        out = render_with_frontmatter(fm, body)
        fm2, body2 = split_frontmatter(out)
        self.assertEqual(fm2["title"], "T")
        self.assertEqual(fm2["summary_words"], "1234")
        self.assertEqual(body2, body)

    def test_entry_body_strips_frontmatter_else_identity(self):
        self.assertEqual(entry_body("no fm\n\n---\n\nx"), "no fm\n\n---\n\nx")
        self.assertEqual(
            entry_body("---\na: b\n---\n\nhello\n"), "hello\n")

    def test_parse_frontmatter_wrapper_matches_split(self):
        doc = "---\nk: v\n---\n\nbody\n"
        self.assertEqual(parse_frontmatter(doc), split_frontmatter(doc)[0])


if __name__ == "__main__":
    unittest.main()
