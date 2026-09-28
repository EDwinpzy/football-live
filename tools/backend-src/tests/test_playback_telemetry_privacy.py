"""Privacy contract for the web playback telemetry module.

The resolver pipeline returns source references and the player holds real
playback URLs. Telemetry must report *what happened* without ever reporting
*where the bytes came from*. These tests fail if any sensitive shape (a URL,
an m3u8, a cookie, a token, an Authorization header, a signed query
parameter) can reach the emitted payload.
"""

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
APP = ROOT / "tools" / "backend-src" / "www" / "app.js"


def declared_events():
    source = APP.read_text(encoding="utf-8")
    block = re.search(r"TELEMETRY_EVENTS\s*=\s*Object\.freeze\(\{([\s\S]*?)\}\)", source)
    assert block, "TELEMETRY_EVENTS must be declared as a frozen object"
    return set(re.findall(r"([A-Za-z0-9_]+)\s*:", block.group(1)))


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


class TelemetryPrivacyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP.read_text(encoding="utf-8")

    def test_allowlist_is_explicit_and_closed(self):
        """Every emitted field must be nameable, and the name list is closed."""
        self.assertIn("TELEMETRY_ALLOWED_FIELDS", self.source)
        allowed = re.search(
            r"TELEMETRY_ALLOWED_FIELDS\s*=\s*new Set\(\[([\s\S]*?)\]\)", self.source)
        self.assertIsNotNone(allowed, "telemetry field allowlist must be an explicit set")
        names = set(re.findall(r'"([a-z0-9_]+)"', allowed.group(1)))
        metrics = re.search(r"METRIC_FIELDS\s*=\s*\[([\s\S]*?)\]", self.source)
        self.assertIsNotNone(metrics, "the funnel durations need an explicit list")
        names |= set(re.findall(r'"([a-z0-9_]+)"', metrics.group(1)))
        self.assertTrue(names, "allowlist must not be empty")
        for forbidden in ("url", "play_url", "m3u8", "playurl", "raw", "token",
                          "cookie", "authorization", "sign", "header"):
            self.assertNotIn(forbidden, names,
                             "allowlist must not contain a sensitive field name")

    def test_sanitizer_drops_anything_outside_the_allowlist(self):
        """The sanitizer is the single choke point: unknown keys are dropped."""
        block = function_body(self.source, "sanitizeTelemetry")
        self.assertIn("TELEMETRY_ALLOWED_FIELDS", block)
        self.assertIn("has(", block, "sanitizer must test allowlist membership")

    def test_sanitizer_redacts_url_shaped_values(self):
        """A value that looks like a URL is stripped even under an allowed key."""
        block = function_body(self.source, "looksLikeSensitiveValue")
        for pattern in (r"\.m3u8", "http", "authorization", "cookie",
                        "token", "sign", "auth_key", "md5"):
            self.assertIn(pattern, block,
                          "detector must recognise %s as sensitive" % pattern)

    def test_error_classifier_returns_categories_not_messages(self):
        """Errors are reported as a small closed category, never as raw text."""
        block = function_body(self.source, "classifyTelemetryError")
        self.assertIn("TELEMETRY_ERROR_CLASSES", block)
        self.assertNotRegex(block, r"return\s+\w+\.message")
        for category in re.findall(r'return "([a-z_]+)";', block):
            self.assertIn(category, self.source[self.source.index(
                "TELEMETRY_ERROR_CLASSES"):][:400],
                "%s must be a declared error class" % category)

    def test_emit_call_sites_never_pass_a_url(self):
        """No telemetry call site may hand a URL-ish expression to the emitter."""
        offenders = []
        for match in re.finditer(r"telemetryEmit\(\s*([^)]*)", self.source):
            args = match.group(1)
            if re.search(r"\b(url|raw|src|playUrl|m3u8)\b", args, re.I):
                offenders.append(args.strip()[:80])
        self.assertEqual(offenders, [],
                         "telemetry call sites must not pass URL-shaped values: %s"
                         % offenders)

    def test_emit_call_sites_only_use_declared_event_names(self):
        """Event names come from the closed list, so a typo cannot invent one."""
        keys = re.findall(r"telemetryEmit\(\s*TELEMETRY_EVENTS\.([A-Z0-9_]+)",
                          self.source)
        self.assertTrue(keys, "expected at least one telemetryEmit call site")
        for key in keys:
            self.assertIn(key, declared_events(),
                          "%s is not a declared telemetry event" % key)
        # The milestone helper passes a name through the same frozen table.
        self.assertIn("TELEMETRY_EVENTS[name]",
                      function_body(self.source, "metricMilestone"))

    def test_playback_context_is_never_serialised_wholesale(self):
        """Spreading live player state (which holds URLs) into telemetry is banned."""
        for match in re.finditer(r"telemetry\w*\(\s*\{[\s\S]{0,400}?\.\.\.", self.source):
            self.fail("telemetry must not receive a spread of playback state: %s"
                      % match.group(0)[:120])

    def test_queue_flush_still_goes_through_sanitizer(self):
        """The network flush path must sanitise, not trust, the queued events."""
        block = function_body(self.source, "flushTelemetry")
        self.assertIn("sanitizeTelemetry", block)


if __name__ == "__main__":
    unittest.main()
