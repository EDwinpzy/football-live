# Existing VPS VOD Performance Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent code tasks. Check each task before the shared deployment and emulator gates.

**Goal:** Make the existing VPS serve usable Web VOD lines while keeping TV and mobile device-side playback fast and reliable.

**Architecture:** The VPS owns Web source discovery, metadata, indexing, and resolver ranking. Web and App share one published source registry; Android devices keep their embedded backend as the primary path to source APIs and media. When a Web source fails, matching continues across the other enabled sources. A candidate source is published only after the VPS can query its list and detail and a device can fetch its playback manifest and first segment.

**Tech Stack:** Python backend, static JSON VOD index, Kotlin/Compose/Media3 Android clients, Docker Compose on the existing VPS.

**Spec:** `C:\Users\63054\Desktop\MyTV-新云服务器全链路性能优化方案-彻底去CloudBase版-2026-09-24.md`; latest user clarification in this chat: use one source registry and let Web try other reachable sources when a source fails, while retaining device-side App playback.

## Global Constraints

- Keep CloudBase disabled in every production path.
- Preserve device-side source resolution and local playback fallback.
- Do not select a source solely because its home page responds; verify its VOD API and actual media path.
- Preserve the existing live source configuration until a candidate passes end-to-end checks.
- Keep secrets, signed media URLs, and long-lived tokens out of Git and test records.
- Do not deploy a new APK or source registry before the new build passes TV and mobile emulator playback checks.

## Review Focus

- VPS can query a candidate API but cannot reach its media CDN: Web should prefer browser direct playback and retain a recoverable error when neither path works.
- Registry IDs are UUIDs while historical index IDs are short names: resolver must not silently discard all indexed candidates.
- Historical index entries lack `match_score`: rebuild verified data without treating `score` (line quality) as match confidence.
- Cloud resolver times out: Android local source resolution must not wait on that failure.
- Failed first line or buffering: only one recovery job should act; backup switching and engine fallback must remain bounded.

---

### Task 1: Web source and resolver

**Files:** `tools/backend-src/vod_resolver.py`, `tools/backend-src/vod_sources.default.json`, `tools/build_vod_line_index.py`, their focused Python tests.

**Interfaces:** Source registry remains `SourceRegistry`; resolver output retains `preferred`, `backups`, and status fields. Index output retains `entries` and adds verified fields required by the resolver.

- [ ] Add a failing test with a short-name index source and UUID-backed live registry. Confirm the candidate is lost before fixing the mapping.
- [ ] Make source identity mapping explicit and preserve the UUID in resolver results. Re-run the focused test until it passes.
- [ ] Add a failing test for stale but valid indexed lines, then return them with a stale status rather than emptying the result.
- [ ] Verify index generation emits a genuine `match_score`; force a rebuild of historical entries that lack it.
- [ ] Add a candidate only after the VPS returns valid list and detail JSON, subject matching passes, and an emulator can fetch its manifest and segment.
- [ ] Run `python -m unittest -v tests.test_vod_resolver_contract tests.test_vod_resolver_fast_path tests.test_vod_line_index_builder tests.test_vod_sources` from `tools/backend-src`.

### Task 2: TV playback

**Files:** `android/app/src/main/java/com/qiubo/optimaltv/data/`, `android/app/src/main/java/com/qiubo/optimaltv/ui/player/`, focused JVM tests.

**Interfaces:** `VodRepository` still returns playable local lines; `PlayerViewModel` still exposes the current UI state and switch actions.

- [ ] Reproduce the existing five failing telemetry tests and fix logging seams and `DECODING_FAILED` classification.
- [ ] Add a failing test proving cloud resolver failure does not delay device-local lines; make local resolution the immediate path.
- [ ] Add a failing recovery test for a previous first-frame job acting after a line switch; cancel it on every new session.
- [ ] Verify network errors switch line and decode errors switch engine before a fatal state.
- [ ] Run `./gradlew.bat :app:testDebugUnitTest :app:assembleDebug --console=plain` from `android`.

### Task 3: Mobile playback

**Files:** `internal/mobile/app/src/main/java/com/qiubo/optimaltv/data/`, `internal/mobile/app/src/main/java/com/qiubo/optimaltv/ui/player/`, focused JVM tests.

**Interfaces:** The resolver can rank candidates but may not replace or block device-local lines; `preferred` stays ahead of backups.

- [ ] Reproduce the failing telemetry test and remove its dependency on unmocked Android logging.
- [ ] Add a failing test for preferred/backups order, then preserve the resolver's order.
- [ ] Add a failing test for local lines while cloud resolution stalls, then keep the local path responsive.
- [ ] Verify cooldown survives automatic line switching and recovery terminates rather than looping forever.
- [ ] Run `./gradlew.bat :app:testDebugUnitTest :app:assembleDebug --console=plain` from `internal/mobile`.

### Task 4: Web UI fallback

**Files:** `tools/backend-src/www/app.js`, `tools/backend-src/tests/test_web_douban_contract.py`, `tools/backend-src/tests/test_web_vod_resolver_flow.py`, `tools/backend-src/tests/web_resolver_harness.mjs`.

**Interfaces:** Existing `/vod/detail` lines remain usable while `/vod/resolve` is pending or unavailable.

- [ ] Add a failing browser harness case where resolver stalls but detail lines arrive promptly.
- [ ] Keep detail rendering and the Play action responsive while resolver ranking continues asynchronously.
- [ ] Verify recoverable states preserve retry; unconfigured telemetry must not send requests.
- [ ] Run `python -m unittest -v tests.test_web_douban_contract tests.test_web_vod_resolver_flow` from `tools/backend-src`.

### Task 5: Integration and emulator gate

**Files:** `docs/04-records/版本史.md`, `docs/04-records/测试记录.md`; screenshots under ignored `artifacts/`.

- [ ] Inspect the complete diff, remove accidental artifacts, and run backend and both Android test suites.
- [ ] Install new TV and mobile debug builds on separate emulators, check version, navigate to a film, and verify actual first frame with screenshots and log evidence.
- [ ] Verify first-line failure changes line once, no overlapping watchdog jobs act, and local matching still works when the VPS resolver is unavailable.
- [ ] Rebuild the Web index from the current source registry; validate index schema, candidate IDs, and match confidence before upload.
- [ ] Apply the source registry and Web deployment serially, then check Web detail, resolver, playback, and source response times from the public endpoint.
- [ ] Record exact test counts, emulator evidence, and any unmet target in the test record and version history.
- [ ] At conversation close, check other active chats for this repository and follow the repository's release rule. Publish and push only after the required gates pass.
