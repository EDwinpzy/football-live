/* Runtime harness: drive the real resolver/telemetry functions in app.js with a
 * stubbed DOM+fetch, and assert the two behaviours the plan cares about:
 *   1. a TEMPORARILY_UNAVAILABLE resolve never removes matched lines;
 *   2. telemetry never carries a URL, m3u8, token or cookie.
 * Run: node tests/web_resolver_harness.mjs
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = fs.readFileSync(path.join(here, "..", "www", "app.js"), "utf8");

let failures = 0;
const ok = (name, cond, extra = "") => {
  if (cond) console.log("  ok  " + name);
  else { failures++; console.log("  FAIL " + name + (extra ? " :: " + extra : "")); }
};

function makeSandbox(fetchImpl) {
  const el = () => ({
    textContent: "", innerHTML: "", hidden: true, value: "", style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    querySelector: () => el(), querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    dataset: {}, children: [], focus() {}, play: () => Promise.resolve(),
  });
  const nodes = new Map();
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    Promise, Date, Math, JSON, Number, String, Object, Array, Set, Map,
    Error, RegExp, isNaN, parseInt, parseFloat, URL, URLSearchParams, Blob,
    Intl, encodeURIComponent, decodeURIComponent, AbortController,
    location: {
      pathname: "/web/", search: "?telemetry=/api/telemetry",
      origin: "https://x.test", href: "https://x.test/web/",
    },
    navigator: { sendBeacon: () => true, mediaSession: null },
    performance: { now: () => Date.now() },
    document: {
      hidden: false, title: "",
      body: { classList: { toggle() {}, contains: () => false, add() {}, remove() {} }, style: {} },
      getElementById: () => el(), querySelector: () => el(), querySelectorAll: () => [],
      addEventListener() {}, createElement: () => el(),
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: fetchImpl,
    addEventListener() {}, removeEventListener() {},
    pv: { ctx: null, vodSources: [], vodLineIdx: 0 },
    playerVodSourceKey: (line, index) => line && line.id != null ? "id:" + line.id : "line:" + index,
    playerVodLineIndex: (lines, playRef, sourceKey, preferredIndex) => {
      const exact = sourceKey ? lines.findIndex((line, index) =>
        (line && line.id != null ? "id:" + line.id : "line:" + index) === sourceKey) : -1;
      if (exact >= 0) return exact;
      const byRef = lines.findIndex(line => (line.episodes || []).some(ep =>
        ep && String(ep.pid) === String(playRef && playRef.pid)
          && String(ep.vid) === String(playRef && playRef.vid)));
      return byRef >= 0 ? byRef : (Number.isInteger(preferredIndex) ? preferredIndex : 0);
    },
    Hls: undefined, window: undefined,
  };
  sandbox.window = sandbox;
  /* Same-origin API root. Required by any code path that builds a request URL
   * (fetchResolverDecision does `BASE + path`) -- without it the expression
   * throws before the stubbed fetch is ever reached. */
  sandbox.BASE = "";
  sandbox.$ = (sel) => {
    if (!nodes.has(sel)) nodes.set(sel, el());
    return nodes.get(sel);
  };
  sandbox.$$ = () => [];
  return vm.createContext(sandbox);
}

function sliceRegion(src) {
  const start = src.indexOf("const TELEMETRY_EVENTS");
  const end = src.indexOf("function metricBuffer");
  const endBlock = src.indexOf("\n}", end) + 2;
  const resolverStart = src.indexOf("let resolverState");
  const retryEnd = src.indexOf("\n}", src.indexOf("async function resolveFirstSource")) + 2;
  /* startResolverRace reads dResumeEp, which is declared with the detail
   * state outside the resolver block. Declare it here so the extracted code
   * behaves the way it does in the page. */
  const stubs = "\nlet dResumeEp = -1;\n";
  return src.slice(start, endBlock) + "\n" + stubs
    + src.slice(resolverStart, src.indexOf("/* 快速起播"))
    + src.slice(src.indexOf("/* 快速起播"), retryEnd);
}

const region = sliceRegion(app);

console.log("== resolver fallback behaviour ==");
{
  const ctx = makeSandbox(async () => ({ ok: true, json: async () => ({}) }));
  vm.runInContext(region + "\n;globalThis.__x={applyResolverDecision,readResolverDecision,canonicalMyTvId,RESOLVER_STATUS,findCandidateLine};", ctx);
  const x = ctx.__x;

  const lines = [
    { id: "s1", name: "红牛", episodes: [{ pid: "c1", vid: 0 }, { pid: "c2", vid: 1 }] },
    { id: "s2", name: "好好看", episodes: [{ pid: "c3", vid: 0 }] },
  ];

  const d1 = x.readResolverDecision({
    mytv_id: 10000001, episode: 1, preferred: null, backups: [],
    cache: { kind: "static_index", age_ms: 123 }, status: "TEMPORARILY_UNAVAILABLE",
  });
  ok("null preferred maps to TEMPORARILY_UNAVAILABLE", d1.status === "TEMPORARILY_UNAVAILABLE");
  ok("unavailable resolve returns no forced line", x.applyResolverDecision(d1, { sources: lines }) === null);

  const d2 = x.readResolverDecision({
    mytv_id: 1, episode: 1, status: "MATCHED",
    preferred: { source_id: "s2", source_name: "好好看", content_id: "c3", episode_index: 1, match_score: 90, quality: {} },
    backups: [],
  });
  const r2 = x.applyResolverDecision(d2, { sources: lines });
  ok("MATCHED selects the right line", r2 && r2.line.id === "s2", String(r2 && r2.line.id));
  ok("MATCHED selects the right episode", r2 && r2.ep.pid === "c3");

  const d3 = x.readResolverDecision({
    status: "MATCHED",
    preferred: { source_id: "nope", source_name: "不存在的源", content_id: "x", episode_index: 1 },
    backups: [],
  });
  ok("unknown source yields no line", x.applyResolverDecision(d3, { sources: lines }) === null);

  const d4 = x.readResolverDecision({
    status: "MATCHED",
    preferred: { source_id: "hongniu", source_name: "红牛", content_id: "", episode_index: 2 },
    backups: [],
  });
  /* No positional hint: the candidate's own episode_index must win. */
  const r4 = x.applyResolverDecision(d4, { sources: lines }, undefined);
  ok("slug id falls back to source name", r4 && r4.line.id === "s1", String(r4 && r4.line.id));
  ok("episode index honoured (ep2)", r4 && r4.ep.pid === "c2");

  ok("douban id rejected", x.canonicalMyTvId("douban:1292052") === null);
  ok("numeric id accepted", x.canonicalMyTvId(10000001) === 10000001, String(x.canonicalMyTvId(10000001)));
  ok("mytv: form accepted", x.canonicalMyTvId("mytv:42") === 42);
  ok("zero rejected", x.canonicalMyTvId(0) === null);

  /* content_id must win over the positional episode when they disagree. */
  const d5 = x.readResolverDecision({
    status: "MATCHED",
    preferred: { source_id: "s1", source_name: "红牛", content_id: "c2", episode_index: 1 },
    backups: [],
  });
  const r5 = x.applyResolverDecision(d5, { sources: lines }, 1);
  ok("content_id overrides positional episode", r5 && r5.ep.pid === "c2", String(r5 && r5.ep.pid));
}

console.log("== every start respects the selected line ==");
{
  const calls = [];
  const ctx = makeSandbox(async () => ({ ok: true, json: async () => ({}) }));
  ctx.resolvePlayCached = (vid, pid, evid) => { calls.push(pid); return Promise.resolve({ sources: [] }); };
  ctx.detail = {
    vid: "douban:1",
    sources: [
      { id: "s1", name: "红牛", episodes: [{ pid: "c1", vid: 0 }, { pid: "c2", vid: 1 }] },
      { id: "s2", name: "好好看", episodes: [{ pid: "c3", vid: 0 }] },
    ],
  };
  /* resolverState is a top-level `let`, i.e. a lexical binding inside the
   * context -- assigning ctx.resolverState from out here would create an
   * unrelated sandbox property. Set it through a closure defined in the VM. */
  vm.runInContext(
    region + "\n;globalThis.__x={resolveFirstSource};"
    + "\n;globalThis.__setState=(s)=>{resolverState=s;};", ctx);
  const x = ctx.__x;

  const preferred = { source_id: "s2", source_name: "好好看", content_id: "c3", episode_index: 1 };
  ctx.__setState({ status: "MATCHED", mytvId: 1, preferred, backups: [] });

  /* Even with a preferred resolver candidate, the selected default line stays active. */
  const defaulted = {
    vid: "douban:1", epIdx: 0, lineIndex: 0, sourceKey: "id:s1",
    playRef: { pid: "c1", vid: 0 },
  };
  await x.resolveFirstSource(defaulted);
  ok("defaulted start stays on the selected line", calls[0] === "c1", calls.join(","));
  ok("resolver preference does not rewrite the selected episode",
    defaulted.playRef.pid === "c1" && defaulted.lineIndex === 0);

  /* A manually chosen second line is honoured as well. */
  calls.length = 0;
  await x.resolveFirstSource({
    vid: "douban:1", epIdx: 0, lineIndex: 1, sourceKey: "id:s2",
    playRef: { pid: "c3", vid: 0 },
  });
  ok("manual source choice stays selected", calls[0] === "c3", calls.join(","));
}

console.log("== a superseded resolve cannot touch the current film ==");
{
  /* Two films, two in-flight resolves. Film A's response lands after the user
   * already opened film B. It must not rewrite resolverState for B, and must
   * not preheat a line using B's sources. */
  const ctx = makeSandbox(async () => ({ ok: true, json: async () => ({}) }));
  const prefetched = [];
  ctx.prefetchPlay = (vid, pid) => { prefetched.push(vid + ":" + pid); };
  /* `detail` and `resolverState` are top-level `let` bindings inside the
   * context, i.e. lexical bindings rather than sandbox properties. They must be
   * written through a closure defined inside the VM, otherwise the code keeps
   * reading its own initial value. */
  /* A is slow, B is fast: A's response therefore arrives while B is live. */
  let releaseA = null;
  ctx.fetchJSON = (url) => {
    /* The path is `/vod/resolve/<id>?episode=1`, so match the id with a
     * boundary rather than assuming a trailing slash. */
    if (/resolve\/2(\?|$)/.test(String(url))) {
      return Promise.resolve({
        status: "MATCHED", preferred: { source_id: "s2", content_id: "c3", episode_index: 1 },
      });
    }
    return new Promise(res => { releaseA = () => res({
      status: "MATCHED", preferred: { source_id: "s1", content_id: "c1", episode_index: 1 },
    }); });
  };
  vm.runInContext(
    region
    + "\n;globalThis.__x={startResolverRace};"
    + "\n;globalThis.__state=()=>resolverState;"
    + "\n;globalThis.__setDetail=(d)=>{detail=d;};", ctx);

  /* Open A, then the user moves on to B before A's resolve comes back.
   * The `mytv:<id>` form is what actually reaches the network: applyDetail
   * always sets `detail.vid = vid`, so the arrival guard compares like with
   * like. A `douban:<id>` key short-circuits to UNSUPPORTED before any fetch. */
  ctx.__setDetail({ vid: "mytv:1", sources: [{ id: "s1", name: "红牛", episodes: [{ pid: "c1", vid: 0 }] }] });
  const aDone = vm.runInContext('__x.startResolverRace("mytv:1", 1);', ctx);
  ctx.__setDetail({
    vid: "mytv:2",
    sources: [
      { id: "s1", name: "红牛", episodes: [{ pid: "c1", vid: 0 }] },
      { id: "s2", name: "好好看", episodes: [{ pid: "c3", vid: 0 }] },
    ],
  });
  const bDone = vm.runInContext('__x.startResolverRace("mytv:2", 1);', ctx);
  await bDone;
  const afterB = JSON.stringify(ctx.__state());
  ok("film B's own result landed",
    ctx.__state().mytvId === 2 && ctx.__state().status === "MATCHED", afterB);

  /* Now the superseded A finally answers, while B is the visible film. */
  releaseA();
  await aDone;
  const afterA = JSON.stringify(ctx.__state());
  const state = ctx.__state();
  ok("film B keeps its state after film A lands late",
    state.mytvId === 2 && state.status === "MATCHED", afterA);
  ok("film B keeps its candidate after film A lands late",
    state.preferred && state.preferred.source_id === "s2", afterA);
  ok("A did not preheat against B's sources",
    prefetched.every(p => p.indexOf("mytv:2:") === 0), prefetched.join(","));
}

console.log("== telemetry privacy ==");
await (async () => {
  const sent = [];
  const ctx = makeSandbox(async () => ({ ok: true, json: async () => ({}) }));
  /* sendBeacon is the preferred transport; intercept it so the batch is visible. */
  ctx.navigator.sendBeacon = (url, blob) => {
    if (typeof blob === "string") { sent.push(blob); return true; }
    if (blob && typeof blob.text === "function") {
      blob.text().then(t => sent.push(t)).catch(() => {});
      return true;
    }
    sent.push(String(blob));
    return true;
  };
  vm.runInContext(region + "\n;globalThis.__x={telemetryEmit,TELEMETRY_EVENTS,sanitizeTelemetry,looksLikeSensitiveValue,classifyTelemetryError,metricMilestone,metricDelta,metricNow,TELEMETRY_ENDPOINT};", ctx);
  const x = ctx.__x;

  x.telemetryEmit(x.TELEMETRY_EVENTS.PLAY_CLICK, {
    source_id: "hongniu", prepare_ms: 12.7,
    url: "https://cdn.example.com/hls/x.m3u8?sign=abc&token=deadbeef",
    play_url: "https://cdn.example.com/hls/x.m3u8",
    Cookie: "session=zzz", authorization: "Bearer eyJhbGciOi", m3u8: "x.m3u8",
    error_class: x.classifyTelemetryError(new Error("HTTP 503 vod unavailable")),
  });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const body = sent.join("\n");
  ok("no m3u8 leaked", !/m3u8/i.test(body), body);
  ok("no https url leaked", !/https?:\/\//i.test(body), body);
  ok("no token leaked", !/deadbeef/i.test(body));
  ok("no cookie leaked", !/session=zzz/i.test(body));
  ok("no authorization leaked", !/bearer/i.test(body));
  ok("source_id survived", /hongniu/.test(body), body);
  ok("duration survived as int", /"prepare_ms":13/.test(body), body);
  ok("error classified, not raw", /"error_class":"http"/.test(body), body);
  ok("raw error text absent", !/vod unavailable/.test(body), body);

  ok("detector catches m3u8", x.looksLikeSensitiveValue("https://a/b.m3u8"));
  ok("detector catches token query", x.looksLikeSensitiveValue("/a?sign=zz&uid=1"));
  ok("detector allows a plain source id", !x.looksLikeSensitiveValue("hongniu"));

  const clean = x.sanitizeTelemetry({ event: "play_click", nope: "x", line_count: 3 });
  ok("unknown field dropped", !("nope" in clean), JSON.stringify(clean));
  ok("endpoint resolved from query", x.TELEMETRY_ENDPOINT === "/api/telemetry", x.TELEMETRY_ENDPOINT);
  ok("negative delta clamped", x.metricDelta(x.metricNow() + 5000) === 0);

  const before = sent.length;
  const y = vm.runInContext("({m: metricMilestone})", ctx);
  y.m("PLAY_5MIN"); y.m("PLAY_5MIN"); y.m("PLAY_30MIN");
  /* Blob bodies are captured asynchronously; give the microtasks a turn. */
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const all = sent.join("\n");
  const play5 = (all.match(/play_5min/g) || []).length;
  const play30 = (all.match(/play_30min/g) || []).length;
  ok("milestone deduplicated", play5 === 1 && play30 === 1, "5min=" + play5 + " 30min=" + play30);
})();

console.log("== telemetry stays off when nothing is configured ==");
{
  /* The self-hosted backend serves no /api/telemetry route. A default build
   * must therefore keep every event queued in memory and put nothing on the
   * wire -- no sendBeacon, no fetch. */
  const beacons = [];
  const fetches = [];
  const ctx = makeSandbox((url, init) => {
    fetches.push(String(url));
    return Promise.resolve({ ok: true, json: async () => ({}) });
  });
  ctx.location.search = "";              /* no ?telemetry= ... */
  ctx.window.OTV_TELEMETRY_URL = "";      /* ... and no host injection */
  ctx.navigator.sendBeacon = (url) => { beacons.push(String(url)); return true; };
  vm.runInContext(region + "\n;globalThis.__x={telemetryEmit,TELEMETRY_EVENTS,telemetryEnabled,flushTelemetry,TELEMETRY_ENDPOINT};", ctx);
  const x = ctx.__x;

  ok("endpoint is empty by default", x.TELEMETRY_ENDPOINT === "", x.TELEMETRY_ENDPOINT);
  ok("telemetry reports disabled", x.telemetryEnabled() === false);

  x.telemetryEmit(x.TELEMETRY_EVENTS.PLAY_CLICK, { kind: "click", source_id: "hongniu" });
  x.telemetryEmit(x.TELEMETRY_EVENTS.FIRST_FRAME, { kind: "playback", first_frame_ms: 120 });
  x.flushTelemetry();
  /* Let any stray microtask run before asserting nothing escaped. */
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  ok("no beacon sent when unconfigured", beacons.length === 0, beacons.join(","));
  ok("no fetch sent when unconfigured", fetches.length === 0, fetches.join(","));
}

console.log("== a misconfigured endpoint degrades silently ==");
{
  /* An operator can still point ?telemetry= at a path the server does not
   * serve. That must not surface as a player error. */
  const beacons = [];
  const ctx = makeSandbox(async () => { throw new Error("network down"); });
  ctx.location.search = "?telemetry=/api/telemetry";
  ctx.navigator.sendBeacon = (url) => { beacons.push(String(url)); return false; };
  vm.runInContext(region + "\n;globalThis.__x={telemetryEmit,TELEMETRY_EVENTS,flushTelemetry};", ctx);
  const x = ctx.__x;

  let threw = null;
  try {
    x.telemetryEmit(x.TELEMETRY_EVENTS.PLAY_CLICK, { kind: "click" });
    x.flushTelemetry();
  } catch (e) { threw = e; }
  await new Promise(r => setImmediate(r));
  ok("a rejecting endpoint does not throw into the player", threw === null, String(threw));
}

console.log(failures ? "\nFAILURES: " + failures : "\nAll harness checks passed.");
process.exit(failures ? 1 : 0);
