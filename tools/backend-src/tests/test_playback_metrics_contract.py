"""Timing contract for the web playback funnel.

The optimisation plan's whole point is measuring the click-to-frame path, so
the five funnel durations must be computed from monotonic marks, not from
wall-clock deltas that a tab wake-up can distort.
"""

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
APP = ROOT / "tools" / "backend-src" / "www" / "app.js"


def function_body(source, name):
    """Return the source of a `function name(...) { ... }` block by brace match."""
    start = source.index("function " + name)
    open_at = source.index("{", start)
    depth = 0
    for i in range(open_at, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced braces for " + name)


class PlaybackMetricsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP.read_text(encoding="utf-8")

    def test_all_five_funnel_durations_are_emitted(self):
        """detail_ms / resolve_ms / prepare_ms / first_frame_ms / total."""
        for field in ("detail_ms", "resolve_ms", "prepare_ms", "first_frame_ms",
                      "total_click_to_frame_ms"):
            self.assertIn('"%s"' % field, self.source,
                          "funnel duration %s must be reported" % field)
        self.assertIn("METRIC_FIELDS", self.source)
        block = re.search(r"METRIC_FIELDS\s*=\s*(\[[^\]]*\])", self.source)
        self.assertIsNotNone(block)
        self.assertIn("total_click_to_frame_ms", block.group(1))

    def test_duration_math_is_monotonic_and_guarded(self):
        """Durations must never go negative when marks land out of order."""
        block = function_body(self.source, "metricDelta")
        self.assertIn("Math.max(0", block,
                      "duration helper must clamp to 0 so marks reorder safely")
        self.assertIn("Math.round", block, "durations must be integers")

    def test_marks_use_performance_now(self):
        """Marks must come from the monotonic clock, not Date.now()."""
        self.assertIn("metricNow", self.source)
        block = function_body(self.source, "metricNow")
        self.assertIn("performance", block)

    def test_play_click_marks_the_start_of_the_funnel(self):
        """A play click opens the funnel; first frame closes it."""
        self.assertIn("metricMark", self.source)
        self.assertIn('"play_click"', self.source)
        self.assertIn('"first_frame"', self.source)
        self.assertIn('"player_prepare"', self.source)

    def test_resolved_and_detail_durations_are_reported(self):
        """detail_ms and resolve_ms are closed by the detail/resolver results."""
        self.assertIn("function metricCloseDetail", self.source)
        self.assertIn("detail_ms", function_body(self.source, "metricCloseDetail"))
        self.assertIn("function metricCloseResolve", self.source)
        self.assertIn("resolve_ms", function_body(self.source, "metricCloseResolve"))

    def test_session_engagement_milestones_are_emitted_once(self):
        """play_5min / play_30min must fire once per playback session."""
        for milestone in ("play_5min", "play_30min"):
            self.assertIn('"%s"' % milestone, self.source)
        self.assertIn("metricMilestone", self.source)
        block = function_body(self.source, "metricMilestone")
        self.assertIn("indexOf", block, "milestone must be deduplicated per session")

    def test_buffering_is_reported_as_a_start_end_pair(self):
        """buffer_start / buffer_end must be emitted as matched events."""
        self.assertIn('"buffer_start"', self.source)
        self.assertIn('"buffer_end"', self.source)
        block = function_body(self.source, "metricBuffer")
        self.assertIn("stall", block)

    def test_fatal_and_fallback_events_are_reachable(self):
        """engine_fallback and fatal must be wired to real code paths."""
        self.assertIn('"engine_fallback"', self.source)
        self.assertIn('"fatal"', self.source)
        self.assertIn("telemetryEmit(TELEMETRY_EVENTS.FATAL", self.source)

    def test_telemetry_is_best_effort_and_never_breaks_playback(self):
        """Telemetry must be wrapped so a transport failure cannot break play."""
        block = function_body(self.source, "telemetryEmit")
        self.assertIn("try", block)
        self.assertIn("catch", block)

    def test_telemetry_is_disabled_without_a_configured_endpoint(self):
        """No endpoint means no beacons at all, so nothing leaks by default."""
        self.assertIn("OTV_TELEMETRY_URL", self.source)
        block = self.source[self.source.index("const TELEMETRY_ENDPOINT"):
                             self.source.index("const TELEMETRY_EVENT_VALUES")]
        self.assertIn("OTV_TELEMETRY_URL", block)
        # The endpoint must resolve to empty unless explicitly configured.
        self.assertRegex(block, r'\} catch \(e\) \{ return ""; \}\n\}\)\(\);',
                         "an unconfigured host must yield an empty endpoint")
        self.assertIn('u.origin === location.origin', block,
                      "an absolute endpoint must be same-origin only")
        self.assertIn("function telemetryEnabled()",
                      self.source, "emission must be gated on an explicit endpoint")


if __name__ == "__main__":
    unittest.main()
