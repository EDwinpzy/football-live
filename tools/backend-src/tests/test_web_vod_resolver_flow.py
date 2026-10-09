"""Web resolver flow contract.

The self-hosted VPS has no route to any video source, so `/vod/resolve` very
often answers `TEMPORARILY_UNAVAILABLE` with a null `preferred`. The web player
must therefore treat the resolver as an *accelerator*, never as a gate:

* a null/unsuccessful resolve must never remove the lines that
  `/vod/detail` already produced;
* the user-facing copy must never say the catalogue has no sources, because
  that misreports a network problem as an empty result;
* only a genuine total failure may show the recoverable message, and it must
  offer a retry.
"""

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
WWW = ROOT / "tools" / "backend-src" / "www"
APP = WWW / "app.js"
HTML = WWW / "index.html"


def function_body(source, name):
    """Return the source of a `function name(...) { ... }` block by brace match."""
    start = source.index("function " + name)
    params_at = source.index("(", start)
    params_depth = 0
    quote = None
    escaped = False
    close_at = None
    for i in range(params_at, len(source)):
        ch = source[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            params_depth += 1
        elif ch == ")":
            params_depth -= 1
            if params_depth == 0:
                close_at = i
                break
    if close_at is None:
        raise AssertionError("unbalanced parameters for " + name)
    open_at = source.index("{", close_at)
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


class WebVodResolverFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP.read_text(encoding="utf-8")
        cls.html = HTML.read_text(encoding="utf-8")

    def test_resolver_endpoint_is_called_with_canonical_mytv_id(self):
        """The request shape must match the documented backend contract."""
        self.assertIn('"/vod/resolve/"', self.source)
        block = function_body(self.source, "fetchResolverDecision")
        self.assertIn("episode", block)
        self.assertIn("encodeURIComponent", block)

    def test_resolver_rejects_ids_it_cannot_prove(self):
        """Douban ids are not mytv_ids; a non-canonical id must not be sent."""
        block = function_body(self.source, "canonicalMyTvId")
        self.assertIn("mytv:", block,
                      "only an explicit mytv: form may be accepted")
        self.assertIn("douban", block.lower() + "douban",
                      "the douban form must be named in the guard comment")
        self.assertNotIn("douban:", block.replace("douban:<id>", ""),
                         "the douban form must never be accepted as a mytv id")
        self.assertIn("return null", block,
                      "a non-canonical id must resolve to null, never a guess")

    def test_null_preferred_is_treated_as_a_fallback_signal(self):
        """preferred:null must map to a recoverable state, not to an error."""
        block = function_body(self.source, "readResolverDecision")
        self.assertIn("preferred", block)
        self.assertIn("TEMPORARILY_UNAVAILABLE", block)
        self.assertIn("backups", block)

    def test_every_declared_status_has_a_known_handling(self):
        """A new backend status must not silently fall into the happy path."""
        for status in ("MATCHED", "RESOLVING", "FAST_PATH_FAILED",
                       "FALLBACK_SEARCHING", "STALE_AVAILABLE",
                       "TEMPORARILY_UNAVAILABLE"):
            self.assertIn(status, self.source, "status %s must be handled" % status)

    def test_resolver_never_destroys_matched_lines(self):
        """The existing /vod/detail lines survive every resolver outcome."""
        block = function_body(self.source, "applyResolverDecision")
        self.assertNotIn("sources = []", block)
        self.assertNotIn("sources.length = 0", block)
        self.assertIn("keep", (block + block.lower()),
                      "fallback path must explicitly preserve existing lines")

    def test_resolver_result_only_promotes_an_ordered_source_candidate(self):
        """A preferred candidate may only reorder/select, never invent a URL."""
        block = (function_body(self.source, "applyResolverDecision")
                 + function_body(self.source, "findCandidateLine"))
        self.assertIn("source_id", block)
        self.assertIn("content_id", block)
        self.assertIn("episode_index", block)

    def test_playback_can_start_from_a_resolved_candidate(self):
        """Fast start: a resolved candidate is used without waiting for all lines."""
        self.assertIn("resolveFirstSource", self.source)
        block = function_body(self.source, "resolveFirstSource")
        self.assertIn("resolvePlayCached", block,
                      "fast start must reuse the existing play resolver")

    def test_default_play_does_not_pin_the_initial_line(self):
        block = function_body(self.source, "startPlayAt")
        self.assertIn("linePinned: false", block)
        picked = function_body(self.source, "selectVodLine")
        self.assertIn("linePinned: true", picked,
                      "manual source selection must pin the chosen line")

    def test_detail_open_starts_the_resolver_alongside_the_detail_request(self):
        """The resolver must be a parallel request, not an extra serial hop."""
        block = function_body(self.source, "openDetail")
        self.assertIn("startResolverRace", block)
        race = function_body(self.source, "startResolverRace")
        self.assertIn("Promise", race)
        self.assertIn("fetchResolverDecision", race)

    def test_user_never_sees_the_dead_end_no_sources_copy(self):
        """Plan 6.3: the dead-end copy is forbidden in the web player."""
        # Comments may discuss the forbidden copy; user-visible strings may not.
        for line in self.source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "/*", "//")):
                continue
            self.assertNotIn("暂无片源", line,
                             "user-visible copy must not say there are no sources: %s"
                             % stripped[:100])

    def test_recoverable_message_is_shown_instead_with_a_retry(self):
        """Total failure is presented as recoverable, not as an empty result."""
        self.assertIn("线路暂时不可用，正在后台恢复", self.source)
        self.assertIn('id="peRetry"', self.html)

    def test_in_progress_states_keep_the_find_sources_affordance(self):
        """While the chain is still working the primary button stays live."""
        self.assertIn("正在匹配最佳线路", self.source)
        block = function_body(self.source, "renderDetail")
        self.assertNotIn("正在查找片源", block,
                         "the old matching copy should be replaced by the new one")

    def test_unavailable_state_keeps_a_retry_affordance(self):
        """The unavailable branch must still offer recovery."""
        block = function_body(self.source, "renderDetail")
        self.assertIn("线路暂时不可用，正在后台恢复", block)
        self.assertIn("dPlayBtn", block, "a retry control must remain reachable")

    def test_second_stage_failure_uses_the_recoverable_copy(self):
        """The /vod/detail second stage must not fall back to the dead-end copy."""
        block = (function_body(self.source, "openDetail")
                 + function_body(self.source, "retryDetail"))
        self.assertIn("线路暂时不可用，正在后台恢复", block)
        self.assertIn("retryDetail", block, "the failure path must offer a retry")

    def test_resolver_timeout_is_bounded(self):
        """The accelerator must not be able to hold the detail page hostage."""
        declared = re.search(r"const RESOLVER_TIMEOUT_MS\s*=\s*(\d+)", self.source)
        self.assertIsNotNone(declared, "the resolver timeout must be a named constant")
        self.assertLessEqual(int(declared.group(1)), 8000,
                             "resolver must give up well before the detail call does")
        self.assertIn("RESOLVER_TIMEOUT_MS",
                      function_body(self.source, "fetchResolverDecision"),
                      "the call must actually use the bounded constant")

    def test_resolver_never_gates_the_episode_list_paint(self):
        """Plan §5: the resolver is an accelerator, so it must not be awaited
        on the path that paints /vod/detail's sources.

        Regression: `if (!resolverState.preferred) await resolverRace;` sat
        between the second-stage detail response and `applyDetail(d)`. On the
        self-hosted VPS `/vod/resolve` is unreachable, so that await burned the
        full RESOLVER_TIMEOUT_MS (6s) *after* the lines had already arrived -
        exactly the case the comment claimed it was protecting.
        """
        block = function_body(self.source, "openDetail")
        self.assertNotRegex(
            block, r"await\s+resolverRace",
            "openDetail must not await the resolver race; arriving lines must "
            "paint immediately and the resolver may only preheat in the "
            "background",
        )
        self.assertNotIn(
            "if (!resolverState.preferred) await",
            block,
            "the detail paint must not be gated on a resolver outcome",
        )

    def test_arriving_resolver_decision_still_preheats_the_preferred_line(self):
        """Dropping the await must not drop the accelerator's only payoff."""
        self.assertIn("startResolverRace", function_body(self.source, "openDetail"),
                      "the race must still be started, just not awaited")
        self.assertNotRegex(
            function_body(self.source, "openDetail"), r"await\s+startResolverRace",
            "a late hit may only preheat in the background",
        )
        race = function_body(self.source, "startResolverRace")
        self.assertIn("prefetchPlay", race,
                      "a late resolver hit must still preheat its line")

    def test_fast_start_never_overrides_an_explicit_line_pick(self):
        """A manual line choice (dLineIdx) outranks the resolver's suggestion.

        `ctx.playRef` is what the user picked via the detail page or the
        "更多" panel's line switcher. Honouring `preferred` unconditionally
        would silently play a different line than the one highlighted.
        """
        block = function_body(self.source, "resolveFirstSource")
        self.assertIn("playRef", block,
                      "the selected playRef must stay on the fast path")
        self.assertRegex(
            block, r"(userPicked|explicitLine|linePinned|selectedByUser)",
            "fast start must be able to tell a user-picked line apart from a "
            "defaulted one, and only override the latter",
        )

    def test_retry_does_not_repaint_a_poisoned_unavailable_cache(self):
        """retryDetail() re-enters openDetail(), which paints the SWR cache
        first. A cached blob carrying source_state=unavailable would bounce the
        button straight back to the recoverable dead end, so the flag must be
        cleared before the cache is applied.
        """
        block = function_body(self.source, "openDetail")
        self.assertRegex(
            block, r"delete\s+[A-Za-z_$.\[\]]*source_state",
            "the cached detail must have source_state stripped before it is "
            "re-applied, otherwise a retry re-renders the unavailable copy",
        )

    def test_a_superseded_resolver_response_cannot_touch_the_current_film(self):
        """Open film A, then B: A's late resolve must not rewrite B's state.

        startResolverRace() reads the module-global `detail` and writes the
        module-global `resolverState`. Without a generation check, A's response
        arriving after the user moved to B preheats a line of B's sources
        against A's candidate and leaves `preferred` pointing at A.
        """
        race = function_body(self.source, "startResolverRace")
        self.assertIn("resolverSeq", race,
                      "the race must claim a generation and check it on arrival")
        self.assertRegex(
            race, r"seq\s*===\s*resolverSeq",
            "a stale response must be discarded on arrival",
        )
        self.assertRegex(
            self.source, r"let\s+resolverSeq\s*=",
            "the generation counter must be declared alongside resolverState",
        )

    def test_direct_first_relay_fallback_strategy_is_preserved(self):
        """The existing https-direct-then-relay policy must not regress."""
        self.assertIn("function vodDirectOk", self.source)
        self.assertIn("function vodAttach", self.source)
        block = function_body(self.source, "vodAttach")
        self.assertIn("relay(", block)

    def test_telemetry_stays_silent_when_no_endpoint_is_configured(self):
        """The self-hosted backend exposes no /api/telemetry route, so an
        unconfigured build must never put a request on the wire.
        """
        block = function_body(self.source, "telemetryEmit")
        self.assertIn("telemetryEnabled", block,
                      "emitting must be gated on a configured endpoint")
        endpoint = re.search(
            r"const TELEMETRY_ENDPOINT\s*=\s*\(\(\)\s*=>\s*\{([\s\S]*?)\n\}\)\(\);",
            self.source)
        self.assertIsNotNone(endpoint, "the endpoint must be derived once")
        self.assertIn("return \"\"", endpoint.group(1),
                      "no configuration must resolve to an empty endpoint")
        self.assertNotIn("/api/telemetry", self.source,
                         "the client must not hardcode a route the backend "
                         "does not serve")

    def test_telemetry_flush_survives_a_rejecting_endpoint(self):
        """A configured-but-missing endpoint must degrade silently."""
        block = function_body(self.source, "flushTelemetry")
        self.assertIn("catch", block,
                      "telemetry failures must never surface to the player")


if __name__ == "__main__":
    unittest.main()
