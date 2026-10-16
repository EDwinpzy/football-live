/* Runtime regression harness for VOD source selection and request races.
 * Run: node tests/web_player_sources_harness.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = fs.readFileSync(path.join(here, "..", "www", "app.js"), "utf8");
const helperStart = app.indexOf("function vodIdentity(");
const helperEnd = app.indexOf("async function vodBrief(", helperStart);
const playerStart = app.indexOf("const MAX_RECOVERY_ATTEMPTS =");
const playerEnd = app.indexOf("/* 手势层", playerStart);
const cacheStart = app.indexOf("const playResolve =");
const cacheEnd = app.indexOf("\nfunction bustResolvedPlay", cacheStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, "VOD route helpers are present");
assert.ok(playerStart >= 0 && playerEnd > playerStart, "player implementation is present");
assert.ok(cacheStart >= 0 && cacheEnd > cacheStart, "play resolution cache is present");

const relayStart = app.indexOf("function relay(u, ref)");
const relayEnd = app.indexOf("\n}", relayStart) + 2;
assert.ok(relayStart >= 0 && relayEnd > relayStart, "relay helper is present");
const relayContext = vm.createContext({ BASE: "/web" });
vm.runInContext(app.slice(relayStart, relayEnd), relayContext);
assert.equal(
  vm.runInContext('relay("https://cdn.test/live.m3u8?token=x", "https://play.88player.top/m3u8.html?url=https%3A%2F%2Fcdn.test%2Flive.m3u8")', relayContext),
  "/web/api/relay?u=https%3A%2F%2Fcdn.test%2Flive.m3u8%3Ftoken%3Dx&ref=https%3A%2F%2Fplay.88player.top%2Fm3u8.html%3Furl%3Dhttps%253A%252F%252Fcdn.test%252Flive.m3u8",
  "relay forwards the 88player Referer without losing the signed stream URL",
);
assert.equal(vm.runInContext('relay("https://cdn.test/live.m3u8", "none")', relayContext),
  "/web/api/relay?u=https%3A%2F%2Fcdn.test%2Flive.m3u8&ref=none",
  "the existing no-Referer behavior remains available");
assert.doesNotMatch(app, /LIVE_SRCS|LIVE_SRC_LABELS/,
  "football source selection is not restricted to a static source allowlist");
assert.match(app, /srcIds: \[\]/, "live playback retains each backend source ID");
assert.match(app, /sourceRefs: \[\]/, "live playback keeps each source's relay Referer");

console.log("== live player lists only backend-probed HLS routes and renews by source ID ==");
{
  const calls = [];
  const attached = [];
  const fatal = [];
  const handlers = [];
  const nodes = new Map();
  const liveCtx = { kind: "live", matchId: "match/42" };
  const routeStart = app.indexOf("async function resolveLiveAndPlay(");
  const routeEnd = app.indexOf("\nfunction attachPlayer(", routeStart);
  assert.ok(routeStart >= 0 && routeEnd > routeStart, "live source resolver is present");
  const liveSandbox = {
    BASE: "/web",
    pv: { ctx: liveCtx, playerSession: 7, sources: [], srcIds: [], srcNames: [], sourceRefs: [],
      srcIdx: 0, picker: true, recoveryGen: 0 },
    fetchJSON: async (url, options) => {
      calls.push({ url: String(url), options });
      if (calls.length === 1) return { sources: [
        { url: "https://cdn.test/a2.m3u8?sig=first", player: "m3u8", source: "88z_0",
          name: "线路A2-高清", referer: "https://www.88zhibo.tv/" },
        { url: "https://cdn.test/not-hls.mp4", player: "mp4", source: "88z_1", name: "错误类型" },
        { url: "", player: "m3u8", source: "88z_2", name: "空地址" },
        { url: "https://cdn.test/premier.m3u8?sig=next", player: "m3u8", source: "88z_17",
          name: "Premier-EN", referer: "https://embed.test/match/42" },
      ] };
      return { url: "https://cdn.test/premier-fresh.m3u8?sig=renewed",
        referer: "https://embed.test/match/42?renewed=1" };
    },
    relay: (url, referer) => `/api/relay?u=${encodeURIComponent(url)}&ref=${encodeURIComponent(referer)}`,
    attachPlayer: (url, resume) => attached.push({ url, resume }),
    renderSrcPicker: null,
    playerFatal: error => fatal.push(String(error)),
    srcLabel: value => String(value || "信号源"),
    esc: value => String(value),
    syncPlayerBackBtn() {},
    $: selector => {
      if (!nodes.has(selector)) nodes.set(selector, { innerHTML: "", hidden: true });
      return nodes.get(selector);
    },
    $$: selector => selector === "#srcPicker .spchip"
      ? liveSandbox.pv.srcNames.map((_, index) => ({ dataset: { i: String(index) },
        addEventListener: (name, callback) => { if (name === "click") handlers[index] = callback; } })) : [],
  };
  liveSandbox.window = liveSandbox;
  const liveContext = vm.createContext(liveSandbox);
  vm.runInContext(app.slice(routeStart, routeEnd) + `
    globalThis.__openLive = () => resolveLiveAndPlay(pv.ctx, pv.playerSession);
    globalThis.__freshLive = index => resolveFreshLiveSource(index);
  `, liveContext);
  await liveContext.__openLive();
  assert.equal(calls[0].url, "/web/api/stream/match%2F42?src=all&fresh=1",
    "opening a match asks the backend to discover and probe every route");
  assert.deepEqual(Array.from(liveSandbox.pv.srcIds), ["88z_0", "88z_17"],
    "only probed m3u8 routes with a URL and stable source ID are kept");
  assert.deepEqual(Array.from(liveSandbox.pv.srcNames), ["线路A2-高清", "Premier-EN"],
    "dynamic backend route names are retained for the picker");
  assert.equal(nodes.get("#srcPicker").innerHTML.includes("Premier-EN"), true,
    "the picker displays dynamic route names, including hyphenated names");
  assert.equal(nodes.get("#srcPicker").innerHTML.includes("错误类型"), false,
    "a non-HLS route is not displayed");
  assert.equal(attached[0].url.includes(encodeURIComponent("https://www.88zhibo.tv/")), true,
    "the initial playable route is relayed with its Referer");

  await handlers[1]();
  assert.equal(calls[1].url, "/web/api/stream/match%2F42?src=88z_17&fresh=1",
    "manual switching renews the selected route by its backend source ID");
  assert.equal(liveSandbox.pv.sourceRefs[1], "https://embed.test/match/42?renewed=1",
    "route renewal keeps the returned Referer for subsequent relays");
  assert.equal(attached.at(-1).url.includes(encodeURIComponent("https://embed.test/match/42?renewed=1")), true,
    "switch playback uses the renewed route Referer");
  assert.deepEqual(fatal, [], "usable routes do not raise a playback error");
}

class FakeNode {
  constructor() {
    this.hidden = true;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.rectWidth = 0;
    this.offsetTop = 0;
    this.chips = [];
    this.style = { setProperty() {} };
    this.dataset = {};
    this.listeners = new Map();
    this.attributes = new Map();
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
      toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
    this.complete = false;
    this.duration = 0;
    this.currentTime = 0;
    this.paused = true;
    this.ended = false;
    this.readyState = 0;
    this.playbackRate = 1;
    this.volume = 1;
    this.src = "";
    this.parentElement = { style: { setProperty() {} } };
    this.addEventListener = (name, callback) => {
      if (!this.listeners.has(name)) this.listeners.set(name, []);
      this.listeners.get(name).push(callback);
    };
  }
  removeEventListener() {}
  dispatch(name, event = {}) { for (const fn of this.listeners.get(name) || []) fn(event); }
  querySelector() { return new FakeNode(); }
  querySelectorAll(selector) { return selector === ".lchip" ? this.chips : []; }
  getBoundingClientRect() { return { width: this.rectWidth, top: this.rectTop }; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); if (name === "src") this.src = ""; }
  getAttribute(name) { return this.attributes.get(name) || null; }
  setPointerCapture() {}
  load() { this.loadCount = (this.loadCount || 0) + 1; }
  pause() { this.paused = true; this.pauseCount = (this.pauseCount || 0) + 1; }
  play() { this.paused = false; return Promise.resolve(); }
  focus() {}
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function lines() {
  return [
    { id: "line-a", source_id: "a", name: "源 A", episodes: [
      { pid: "a", vid: 0, ep: "第1集" }, { pid: "a", vid: 1, ep: "第2集" },
    ] },
    { id: "line-b", source_id: "b", name: "源 B", episodes: [
      { pid: "b", vid: 0, ep: "第1集" }, { pid: "b", vid: 1, ep: "第2集" },
    ] },
  ];
}

function providerLines() {
  const [lineA, lineB] = lines();
  return [
    { ...lineA, name: "好好看 · 源 A" },
    { ...lineB, name: "非凡资源 · 源 B" },
    { ...lineA, id: "line-c", source_id: "c", name: "单独源", episodes: lineA.episodes.map(ep => ({ ...ep, pid: "c" })) },
    { ...lineA, id: "line-d", source_id: "d", name: "好好看 · 源 D", episodes: lineA.episodes.map(ep => ({ ...ep, pid: "d" })) },
    { ...lineB, id: "line-e", source_id: "e", name: "非凡资源 · 源 E", episodes: lineB.episodes.map(ep => ({ ...ep, pid: "e" })) },
  ];
}

function makeRuntime(detailFactory = async () => ({
  title: "测试片", poster_url: "", sources: lines(),
})) {
  const nodes = new Map();
  const windowListeners = new Map();
  const fetches = [];
  const attached = [];
  const requests = [];
  const persisted = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new FakeNode());
    return nodes.get(selector);
  };
  const sandbox = {
    console, Promise, Date, Math, JSON, Number, String, Object, Array, Set, Map,
    Error, RegExp, URLSearchParams, AbortController,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    location: { pathname: "/web/", search: "", origin: "https://example.test" },
    navigator: {},
    document: {
      body: { style: {} }, fullscreenElement: null,
      querySelector: $, querySelectorAll: () => [], addEventListener() {},
      exitFullscreen: () => Promise.resolve(),
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    state: { portrait: false },
    BASE: "/web",
    addEventListener: (name, callback) => windowListeners.set(name, callback),
    store: {
      get: (key, fallback) => persisted.has(key) ? persisted.get(key) : fallback,
      set: (key, value) => persisted.set(key, value),
      remove: key => persisted.delete(key),
    },
    $,
    $$: () => [],
    fmtTime: value => String(Math.floor(value || 0)),
    img: value => value || "",
    esc: value => String(value),
    gatePlay: () => true,
    hint() {},
    metricMark() {},
    telemetryEmit() {},
    TELEMETRY_EVENTS: { PLAYER_PREPARE: "player_prepare", FATAL: "fatal" },
    classifyTelemetryError: () => "unknown",
    metricBuffer() {},
    metricMilestone() {},
    metricDelta: () => 0,
    metrics: { marks: {} },
    pushHistory() {},
    renderDetailEps() {},
    applyAspect() {},
    saveProgress() {},
    getProgress: () => null,
    resumable: () => false,
    setPlayIcon() {},
    detail: { vid: "douban:1", title: "测试片", meta: "电视剧", desc: "简介", sources: lines() },
    dLineIdx: 0, dEpIdx: 0, dEpPage: 0,
    resolverState: { preferred: null },
    __detailFactory: detailFactory,
    __playFactory: async () => ({ sources: [{ url: "https://media.test/play.m3u8" }] }),
    __persisted: persisted,
    __fetches: fetches,
    __attached: attached,
    __requests: requests,
    __resolveImpl: null,
    fetchJSON: async (url, options = {}) => {
      fetches.push({ url: String(url), signal: options.signal || null, timeout: options.timeout });
      if (/\/vod\/detail\/|\/hhkan\/detail\//.test(String(url))) return sandbox.__detailFactory(url, options);
      if (/\/vod\/play\//.test(String(url))) return sandbox.__playFactory(url, options);
      throw new Error("unexpected fetch " + url);
    },
  };
  sandbox.window = sandbox;
  sandbox.resolveFirstSource = (ctx, options) => {
    const item = deferred();
    requests.push({ pid: ctx.playRef.pid, force: !!options.force, resolve: item.resolve, reject: item.reject,
      signal: sandbox.__pv && sandbox.__pv.playController && sandbox.__pv.playController.signal });
    return item.promise;
  };
  sandbox.vodAttach = (url, resume) => attached.push({ url, resume });
  const prelude = `
    let detail = globalThis.detail;
    let dLineIdx = 0, dEpIdx = 0, dEpPage = 0;
    let resolverState = globalThis.resolverState;
    function renderDetailEps() {}
  `;
  const routeHelpers = app.slice(helperStart, helperEnd);
  const playerCode = app.slice(playerStart, playerEnd);
  const context = vm.createContext(sandbox);
  vm.runInContext(prelude + routeHelpers + playerCode + `
    globalThis.__pv = pv;
    globalThis.__setDetail = value => { detail = value; };
    globalThis.__getDetailSelection = () => ({ line: dLineIdx, episode: dEpIdx });
    globalThis.__getPlayer = () => ({
      ctx: pv.ctx, sources: pv.vodSources, selected: pv.vodLineIdx,
      status: pv.vodSourceStatus, message: pv.vodSourceMessage, panelOpen: pv.panelOpen,
    });
    globalThis.__setPlayerSourcePanel = open => setPlayerSourcePanel(open);
    globalThis.__providerFor = (line, index, ctx) => playerSourceProvider(line, index, ctx);
  `, context);
  return { context, nodes, fetches, attached, requests, $, windowListeners, __persisted: persisted };
}

const flush = async () => {
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
  await Promise.resolve();
};

console.log("== provider grouping uses source metadata and HHKan reference for raw HN routes ==");
{
  const { context: c } = makeRuntime();
  assert.equal(c.__providerFor({ name: "HN线路", episodes: [{ pid: 9 }] }, 0,
    { vid: "hhkan:12345" }), "好好看", "raw native HHKan lines inherit their provider from HHKan detail reference and pid");
  assert.equal(c.__providerFor({ name: "HN线路", episodes: [{ pid: 9 }] }, 0,
    { vid: "douban:12345" }), "其他信号源", "an unprefixed route is not guessed to be HHKan for another provider");
  assert.equal(c.__providerFor({ name: "HN线路", source_name: "好好看", episodes: [{ pid: 9 }] }, 0,
    { vid: "douban:12345" }), "好好看", "explicit source metadata takes precedence over label/context inference");
}

console.log("== landscape VOD source groups, fixed list, retry and latest request wins ==");
{
  const groupedSources = providerLines();
  const runtime = makeRuntime(async () => ({ title: "测试片", poster_url: "", sources: groupedSources }));
  const { context: c, $, requests, attached, fetches } = runtime;
  c.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", cover: "", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 1 }, sources: groupedSources, resume: 0 });
  await flush();

  assert.equal($("#mpToggle").hidden, false, "landscape VOD exposes a source-panel entry while loading");
  assert.equal($("#morePanel").hidden, true, "panel remains closed until selected");
  assert.equal($("#mpLinesSec").hidden, false, "the one-or-more-source section is available");
  assert.match($("#mpLines").innerHTML, /源 A/);
  assert.match($("#mpLineProviders").innerHTML, /好好看/);
  assert.match($("#mpLineProviders").innerHTML, /非凡资源/);
  assert.match($("#mpLineProviders").innerHTML, /其他信号源/);
  const visibleSourceIndices = () => Array.from($("#mpLines").innerHTML.matchAll(/data-i="(\d+)"/g), match => +match[1]);
  assert.deepEqual(visibleSourceIndices(), [0, 3], "the active source's provider is selected initially");
  assert.equal($("#mpLines").innerHTML.includes('aria-current="true"'), true,
    "the currently playing route remains marked inside its provider group");
  assert.equal($("#morePanel").hidden, true, "the panel remains closed until selected");
  assert.ok(fetches.some(row => row.url.endsWith("/vod/detail/1?refresh=1")));
  assert.equal(fetches.find(row => row.url.endsWith("/vod/detail/1?refresh=1")).timeout, 300000,
    "source-list refresh allows up to five minutes");

  $("#mpToggle").dispatch("click");
  assert.equal($("#morePanel").hidden, false, "the grouped source list stays in the original panel");
  $("#mpLines").scrollTop = 54;
  const requestsBeforeProviderChange = requests.length;
  $("#mpLineProviders").dispatch("click", {
    target: { closest: () => ({ dataset: { provider: "非凡资源" } }) },
  });
  assert.deepEqual(visibleSourceIndices(), [1, 4], "provider filtering retains original global source indices");
  assert.equal($("#mpLines").scrollTop, 0, "changing provider starts at the top of its list");
  assert.equal(requests.length, requestsBeforeProviderChange, "browsing providers does not start playback");
  assert.match($("#mpLineProviders").innerHTML, /class="mp-provider on"[^]*aria-pressed="true"[^]*data-provider="非凡资源"/,
    "the selected provider is visibly and accessibly marked");

  const oldSignal = c.__pv.playController.signal;
  c.__pv.video.duration = 600;
  c.__pv.video.currentTime = 125;
  assert.equal(c.selectVodLine(1), true, "a second source is selectable");
  assert.equal(oldSignal.aborted, true, "switching aborts the old playback request");
  assert.equal(c.__getPlayer().ctx.playRef.pid, "b");
  assert.equal(c.__getPlayer().ctx.epIdx, 1, "switch keeps the corresponding episode");
  assert.equal(c.__getPlayer().ctx.resume, 125, "switch keeps useful playback progress");
  await flush();
  assert.equal(requests.at(-1).force, true, "manual selection explicitly forces a fresh resolve");
  assert.ok(fetches.some(row => row.url.endsWith("/vod/detail/1?refresh=1")),
    "manual Douban selection refreshes its detail before resolving playback");
  assert.ok($("#video").loadCount >= 1, "switch unloads the old media pipeline before resolving");

  const currentSignal = c.__pv.playController.signal;
  assert.equal(c.selectVodLine(1), true, "selecting the active source retries it");
  assert.equal(currentSignal.aborted, true, "same-source retry also stops its old request");
  await flush();
  assert.equal(requests.at(-1).force, true, "same-source retry bypasses the client cache");

  const latest = requests.at(-1);
  const older = requests[1];
  latest.resolve({ sources: [{ url: "https://media.test/latest.m3u8" }] });
  await flush();
  older.resolve({ sources: [{ url: "https://media.test/stale.m3u8" }] });
  await flush();
  assert.deepEqual(attached.map(item => item.url), ["https://media.test/latest.m3u8"],
    "late results from a superseded source never replace the latest selection");
  assert.equal(c.__getPlayer().status, "ready", "successful resolution clears the transient retry state");

  c.playerFatal("线路恢复失败");
  assert.equal($("#pError").hidden, false, "error state remains visible");
  $("#mpClose").dispatch("click");
  assert.equal($("#morePanel").hidden, true);
  assert.equal($("#mpToggle").hidden, false, "source panel can be reopened from the error state");
  $("#mpToggle").dispatch("click");
  assert.equal($("#morePanel").hidden, false);
}

console.log("== manually chosen VOD source persists by title and episode across reopens ==");
{
  const runtime = makeRuntime(async () => ({ title: "测试片", sources: lines() }));
  const { context: c, __persisted: persisted } = runtime;
  c.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 0 }, sources: lines() });
  await flush();
  assert.equal(c.selectVodLine(1), true, "manual source selection records a title preference");
  assert.equal(persisted.get("otvw:vod:source:douban:1").sourceKey, "id:line-b");

  c.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 1 }, sources: lines() });
  assert.equal(c.__getPlayer().selected, 1, "reopening the title defaults to the saved source");
  assert.equal(c.__getPlayer().ctx.playRef.pid, "b", "the saved source supplies the requested episode");
  assert.equal(c.__getPlayer().ctx.playRef.vid, 1);
  assert.equal(c.__getPlayer().ctx.linePinned, true, "the saved manual choice is pinned against resolver changes");
}

console.log("== Douban initial playback stays fast while manual refresh remaps the selected episode ==");
{
  const openingDetail = deferred();
  const manualDetail = deferred();
  let detailCalls = 0;
  const refreshed = [
    { id: "line-a-v2", source_id: "a", name: "源 A", episodes: [
      { pid: "a-v2", vid: 0, name: "第1集" }, { pid: "a-v2", vid: 1, name: "第2集" },
    ] },
    { id: "line-b-v2", source_id: "b", name: "源 B", episodes: [
      { pid: "b-v2", vid: 0, name: "第1集" }, { pid: "b-v2", vid: 1, name: "第2集" },
    ] },
  ];
  const runtime = makeRuntime(() => detailCalls++ === 0 ? openingDetail.promise : manualDetail.promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 1, sourceKey: "id:line-b",
    playRef: { pid: "b", vid: 1 }, linePinned: true, sources: lines() });
  assert.equal(runtime.requests.length, 1,
    "ordinary player opening starts its initial resolve without waiting for refreshed details");
  openingDetail.resolve({ title: "测试片", sources: lines() });
  await flush();

  runtime.context.selectVodLine(1);
  const stalePlay = runtime.requests[0];
  assert.equal(stalePlay.signal.aborted, true, "manual retry stops the previous play request");
  const manualRequest = runtime.context.__pv.detailRequest.promise;
  assert.equal(runtime.requests.length, 1,
    "manual Douban playback waits for the detail refresh before resolving the source");
  manualDetail.resolve({ title: "测试片", sources: refreshed });
  await manualRequest;
  await flush();
  assert.equal(runtime.requests.length, 2);
  assert.equal(runtime.requests[1].pid, "b-v2",
    "manual playback resolves the refreshed playRef for the same stable source and episode");
  assert.equal(runtime.requests[1].force, true);
  assert.equal(runtime.context.__getPlayer().ctx.epIdx, 1);
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第2集");

  runtime.requests[1].resolve({ sources: [{ url: "https://media.test/fresh-line.m3u8" }] });
  await flush();
  runtime.requests[0].resolve({ sources: [{ url: "https://media.test/stale-line.m3u8" }] });
  await flush();
  assert.deepEqual(runtime.attached.map(item => item.url), ["https://media.test/fresh-line.m3u8"],
    "the older source resolution cannot attach after the fresh selection wins");
}

console.log("== superseded Douban manual detail refresh cannot resolve or remap stale choices ==");
{
  const replies = [deferred(), deferred(), deferred()];
  let detailCalls = 0;
  const changed = [
    { id: "line-a-v2", source_id: "a", name: "源 A", episodes: [
      { pid: "a-v2", vid: 0, ep: "第1集" }, { pid: "a-v2", vid: 1, ep: "第2集" },
    ] },
    { id: "line-b-v2", source_id: "b", name: "源 B", episodes: [
      { pid: "b-v2", vid: 0, ep: "第1集" }, { pid: "b-v2", vid: 1, ep: "第2集" },
    ] },
  ];
  const runtime = makeRuntime(() => replies[detailCalls++].promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 1 }, linePinned: true, sources: lines() });
  await flush();
  replies[0].resolve({ title: "测试片", sources: lines() });
  await flush();

  runtime.context.selectVodLine(1);
  const supersededRefresh = runtime.context.__pv.detailRequest.promise;
  runtime.context.selectVodLine(0);
  const winningRefresh = runtime.context.__pv.detailRequest.promise;
  assert.equal(runtime.fetches[1].signal.aborted, true,
    "a newer manual selection aborts the pending detail refresh from the previous selection");
  replies[2].resolve({ title: "测试片", sources: [changed[0]] });
  await winningRefresh;
  await flush();
  assert.equal(runtime.requests.length, 2, "only the winning manual refresh starts a fresh play request");
  assert.equal(runtime.requests[1].pid, "a-v2");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "a-v2");

  replies[1].resolve({ title: "测试片", sources: [changed[1]] });
  await supersededRefresh;
  await flush();
  assert.equal(runtime.requests.length, 2, "a late superseded response never starts its own resolve");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "a-v2",
    "a late response cannot overwrite the latest selected source");
  assert.equal(runtime.context.__getPlayer().selected, 0);
}

console.log("== Douban episode-name identity wins over a reused line ID and episode index ==");
{
  const oldLine = { id: "same-line", source_id: "source-a", name: "源 A", episodes: [
    { pid: "a", vid: 0, ep: "第1集" }, { pid: "a", vid: 1, ep: "第2集" },
  ] };
  const reorderedLine = { id: "same-line", source_id: "source-a", name: "源 A", episodes: [
    { pid: "a", vid: 0, ep: "第2集" }, { pid: "a", vid: 1, ep: "第1集" },
  ] };
  let detailCalls = 0;
  const runtime = makeRuntime(async () => ({ title: "测试片",
    sources: detailCalls++ === 0 ? [oldLine] : [reorderedLine] }));
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, lineIndex: 0, sourceKey: "id:same-line",
    playRef: { pid: "a", vid: 0 }, linePinned: true, sources: [oldLine] });
  await flush();
  runtime.requests[0].resolve({ sources: [{ url: "https://media.test/old-episode.m3u8" }] });
  await flush();

  assert.equal(runtime.context.selectVodLine(0), true, "same-source retry starts a fresh detail lookup");
  await flush();
  assert.equal(runtime.requests.length, 2);
  assert.equal(runtime.requests[1].pid, "a");
  assert.equal(runtime.requests[1].force, true);
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第1集");
  assert.equal(runtime.context.__getPlayer().ctx.epIdx, 1,
    "the selected episode follows its unique name after the line keeps its ID but reorders episodes");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.vid, 1,
    "the reused numeric episode index is replaced with the refreshed index for the same named episode");
}

console.log("== a single real source remains visible and can be retried ==");
{
  const oneLine = lines()[0];
  const runtime = makeRuntime(async () => ({ title: "测试片", sources: [oneLine] }));
  runtime.context.__setDetail({ vid: "douban:1", title: "测试片", meta: "", desc: "", sources: [oneLine] });
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, playRef: { pid: "a", vid: 0 }, sources: [oneLine] });
  await flush();
  assert.equal(runtime.$("#mpLinesSec").hidden, false);
  assert.equal((runtime.$("#mpLines").innerHTML.match(/class="lchip/g) || []).length, 1);
  assert.match(runtime.$("#mpLines").innerHTML, /源 A/);
  assert.match(runtime.$("#mpLines").innerHTML, /再次点击重试当前信号源/,
    "the active single-source control explains its same-source retry action");
  assert.match(runtime.$("#mpSourceState").textContent, /再次点击当前线路重试/);
  assert.equal(runtime.context.selectVodLine(0), true, "active one-source chip provides a retry action");
  await flush();
  assert.equal(runtime.requests.at(-1).force, true);
  assert.match(runtime.$("#mpSourceState").textContent, /正在解析 源 A/);
}

console.log("== source loading failure and empty result are visible ==");
{
  const failed = makeRuntime(async () => { throw new Error("offline"); });
  failed.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "正片", epCount: 1, playRef: { pid: "a", vid: 0 }, sources: lines() });
  await flush();
  assert.equal(failed.$("#mpSourceState").dataset.state, "error");
  assert.match(failed.$("#mpSourceState").textContent, /更新失败/);

  const empty = makeRuntime(async () => ({ title: "测试片", sources: [] }));
  empty.context.__setDetail({ vid: "douban:1", title: "测试片", meta: "", desc: "", sources: [] });
  empty.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "正片", epCount: 1, playRef: { pid: "a", vid: 0 } });
  await flush();
  assert.equal(empty.$("#mpSourceState").dataset.state, "empty");
  assert.match(empty.$("#mpSourceState").textContent, /暂未匹配到其他信号源/);

  const hhkan = makeRuntime(async () => ({ title: "测试片", sources: lines() }));
  hhkan.context.__setDetail({ vid: "hhkan:7", title: "测试片", meta: "电视剧", desc: "", sources: lines() });
  hhkan.context.openPlayer({ kind: "vod", vid: "hhkan:7", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, playRef: { pid: "a", vid: 0 }, sources: lines() });
  await flush();
  assert.ok(hhkan.fetches.some(row => row.url.endsWith("/hhkan/detail/7?refresh=1&enrich=1")),
    "HHKan refresh bypasses detail caches and waits for current episode sources");
}

console.log("== VOD resolution failure does not automatically select another line ==");
{
  const runtime = makeRuntime();
  const { context: c } = runtime;
  c.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 0 }, linePinned: true, sources: lines() });
  await flush();
  assert.equal(runtime.requests.length, 1, "the first request targets the selected line only");
  runtime.requests[0].reject(new Error("selected line failed"));
  await flush();
  assert.equal(runtime.requests.length, 1, "failure does not issue a playback request for another line");
  assert.equal(c.__getPlayer().selected, 0, "the selected source remains visible after failure");
  assert.equal(runtime.$("#pError").hidden, false, "the user gets an explicit failure state");

  c.__pv.sources = ["https://media.test/current-1.m3u8", "https://media.test/current-2.m3u8"];
  c.__pv.recoveryAttempts = 2;
  const selectedKey = c.__getPlayer().ctx.sourceKey;
  await c.recoverPlayback("stalled");
  assert.equal(c.__getPlayer().ctx.sourceKey, selectedKey, "stalled recovery keeps the same VOD line");
  assert.equal(c.__getPlayer().selected, 0);
  assert.equal(runtime.attached.at(-1).url, "https://media.test/current-2.m3u8",
    "recovery can still rotate among play URLs within the selected line");
}

console.log("== reopening the episode panel forces a fresh list without interrupting playback ==");
{
  const oldRequest = deferred();
  const latestRequest = deferred();
  let detailCalls = 0;
  const episodes = count => Array.from({ length: count }, (_, index) => ({
    pid: "a", vid: index, ep: `第${index + 1}集`,
  }));
  const oldLine = { id: "line-a", source_id: "a", name: "源 A", episodes: episodes(20) };
  const latestLine = { ...oldLine, episodes: episodes(21) };
  const runtime = makeRuntime(() => detailCalls++ === 0 ? oldRequest.promise : latestRequest.promise);
  const { context: c, fetches, requests } = runtime;
  c.openPlayer({ kind: "vod", vid: "hhkan:7", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 20, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 1 }, sources: [oldLine] });
  await flush();
  assert.equal(fetches[0].url, "/web/hhkan/detail/7?refresh=1&enrich=1");
  const playerSignal = c.__pv.playController.signal;
  const startedPlayRequests = requests.length;

  c.__setPlayerSourcePanel(true);
  await flush();
  assert.equal(fetches.length, 2, "opening the panel issues a new detail request");
  assert.equal(fetches[0].signal.aborted, true, "the superseded initial detail request is cancelled");
  assert.equal(fetches[1].signal.aborted, false);
  assert.equal(playerSignal.aborted, false, "a list refresh leaves the active play pipeline alive");
  assert.equal(requests.length, startedPlayRequests, "opening the panel does not restart playback");

  latestRequest.resolve({ title: "测试片", sources: [latestLine] });
  await c.__pv.detailRequest.promise;
  assert.equal(c.__getPlayer().sources[0].episodes.length, 21);
  assert.equal(c.__getPlayer().selected, 0);
  assert.equal(c.__getPlayer().ctx.epName, "第2集");
  assert.equal(c.__getPlayer().ctx.playRef.vid, 1);

  oldRequest.resolve({ title: "测试片", sources: [oldLine] });
  await flush();
  assert.equal(c.__getPlayer().sources[0].episodes.length, 21,
    "a late superseded response cannot replace the refreshed episode list");
  assert.equal(c.__getPlayer().ctx.playRef.vid, 1,
    "the current episode remains selected while the new episode becomes visible");
}

console.log("== refreshed lines preserve the selected source and episode ==");
{
  const refreshed = [
    { id: "line-b-new", source_id: "b", name: "源 B", episodes: [
      { pid: "b-new", vid: 0, ep: "第1集" }, { pid: "b-new", vid: 1, ep: "第2集" },
    ] },
    lines()[0],
  ];
  const refreshResponse = deferred();
  const runtime = makeRuntime(() => refreshResponse.promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 1, sourceKey: "id:line-b",
    playRef: { pid: "b", vid: 1 }, linePinned: true, sources: lines() });
  await flush();
  runtime.requests[0].resolve({ sources: [{ url: "https://media.test/old-current.m3u8" }] });
  await flush();
  refreshResponse.resolve({ title: "测试片", sources: refreshed });
  await flush();
  assert.equal(runtime.context.__getPlayer().selected, 0, "reordered fresh detail keeps the same source selected");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "b-new",
    "after the active request settles, the current episode follows its name when the fresh play id changes");
  assert.equal(runtime.context.__getPlayer().ctx.epIdx, 1);
  assert.equal(runtime.context.__getDetailSelection().line, 0);
  assert.equal(runtime.context.__getDetailSelection().episode, 1,
    "the underlying detail selection follows the remapped episode");
  assert.equal(runtime.context.__getPlayer().sources.length, 2, "fresh lines replace the stale source list");
}

console.log("== detail refresh cannot rewrite a pending play target ==");
{
  const refreshed = [
    { id: "line-b-new", source_id: "b", name: "源 B", episodes: [
      { pid: "b-new", vid: 0, ep: "第1集" }, { pid: "b-new", vid: 1, ep: "第2集" },
    ] },
    lines()[0],
  ];
  const refreshResponse = deferred();
  const runtime = makeRuntime(() => refreshResponse.promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 1, sourceKey: "id:line-b",
    playRef: { pid: "b", vid: 1 }, linePinned: true, sources: lines() });
  await flush();
  refreshResponse.resolve({ title: "测试片", sources: refreshed });
  await flush();
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "b",
    "a changed playRef cannot invalidate the already pending resolution");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.vid, 1);
  assert.equal(runtime.context.__getPlayer().ctx.lineIndex, 2,
    "the old selected line remains available while its request is pending");
  assert.equal(runtime.context.__getDetailSelection().line, 2);
  assert.equal(runtime.context.__getDetailSelection().episode, 1);
  assert.equal(runtime.context.__getPlayer().status, "ready");
  runtime.requests[0].resolve({ sources: [{ url: "https://media.test/current.m3u8" }] });
  await flush();
  assert.deepEqual(runtime.attached.map(item => item.url), ["https://media.test/current.m3u8"]);
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "b");
  assert.equal(runtime.context.__getPlayer().status, "ready");
}

console.log("== repeated refreshes with duplicate line IDs preserve identity and episode order ==");
{
  const replies = [deferred(), deferred(), deferred()];
  let detailCalls = 0;
  const duplicateIdLines = [
    { id: "old-duplicate", source_id: "source-a", name: "源 A", episodes: [
      { pid: "shared", vid: 0, ep: "第1集" }, { pid: "shared", vid: 1, ep: "第2集" },
    ] },
    { id: "old-duplicate", source_id: "source-b", name: "源 B", episodes: [
      { pid: "shared", vid: 0, ep: "第1集" }, { pid: "shared", vid: 1, ep: "第2集" },
    ] },
  ];
  const runtime = makeRuntime(() => replies[detailCalls++].promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 1,
    epName: "第2集", epCount: 2, lineIndex: 1, sourceKey: "id:old-duplicate",
    playRef: { pid: "shared", vid: 1 }, linePinned: true, sources: duplicateIdLines });
  await flush();
  const initialRequest = runtime.context.__pv.detailRequest.promise;
  const firstRefresh = runtime.context.refreshPlayerEpisodes({ force: true });
  const latestRefresh = runtime.context.refreshPlayerEpisodes({ force: true });
  assert.equal(runtime.fetches.length, 3, "repeated refreshes for the same film issue independent requests");
  assert.equal(runtime.fetches[0].signal.aborted, true);
  assert.equal(runtime.fetches[1].signal.aborted, true);
  assert.equal(runtime.fetches[2].signal.aborted, false);

  const latestLines = [
    { id: "duplicate-refresh-id", source_id: "source-a", name: "源 A", episodes: [
      { pid: "shared", vid: 1, ep: "第2集" }, { pid: "shared", vid: 0, ep: "第1集" },
    ] },
    { id: "duplicate-refresh-id", source_id: "source-b", name: "源 B", episodes: [
      { pid: "shared", vid: 1, ep: "第2集" }, { pid: "shared", vid: 0, ep: "第1集" },
    ] },
  ];
  replies[2].resolve({ title: "测试片", sources: latestLines });
  await latestRefresh;
  assert.equal(runtime.context.__getPlayer().selected, 1,
    "duplicate line IDs are disambiguated by stable source identity");
  assert.equal(runtime.context.__getPlayer().ctx.epIdx, 0,
    "the current episode follows its playRef when ordering changes");
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第2集");
  assert.equal(runtime.context.__getDetailSelection().line, 1);
  assert.equal(runtime.context.__getDetailSelection().episode, 0);

  replies[0].resolve({ title: "测试片", sources: duplicateIdLines });
  replies[1].resolve({ title: "测试片", sources: duplicateIdLines });
  await Promise.all([initialRequest, firstRefresh]);
  assert.equal(runtime.context.__getPlayer().selected, 1,
    "late results from superseded refreshes cannot replace the latest list");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.vid, 1);
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第2集");
}

console.log("== removed selected episode is retained with an explicit error ==");
{
  const initialDetail = deferred();
  const removedEpisodeDetail = deferred();
  let detailCalls = 0;
  const oldLine = { id: "line-a", source_id: "a", name: "源 A", episodes: [
    { pid: "a", vid: 0, ep: "第1集" }, { pid: "a", vid: 1, ep: "第2集" },
  ] };
  const runtime = makeRuntime(() => detailCalls++ === 0
    ? initialDetail.promise : removedEpisodeDetail.promise);
  runtime.context.openPlayer({ kind: "vod", vid: "douban:1", title: "测试片", epIdx: 0,
    epName: "第1集", epCount: 2, lineIndex: 0, sourceKey: "id:line-a",
    playRef: { pid: "a", vid: 0 }, linePinned: true, sources: [oldLine] });
  await flush();
  initialDetail.resolve({ title: "测试片", sources: [oldLine] });
  await flush();
  runtime.requests[0].resolve({ sources: [{ url: "https://media.test/old-selection.m3u8" }] });
  await flush();

  const refresh = runtime.context.refreshPlayerEpisodes({ force: true });
  removedEpisodeDetail.resolve({ title: "测试片", sources: [{
    id: "line-a", source_id: "a", name: "源 A",
    episodes: [{ pid: "a", vid: 1, name: "第2集" }],
  }] });
  await refresh;
  assert.equal(runtime.context.__getPlayer().ctx.playRef.pid, "a");
  assert.equal(runtime.context.__getPlayer().ctx.playRef.vid, 0,
    "a missing selected episode must keep the original playRef, not take the new episode at its index");
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第1集");
  assert.equal(runtime.context.__getPlayer().ctx.epIdx, 0);
  assert.equal(runtime.context.__getPlayer().selected, 1,
    "the original line is retained explicitly instead of silently selecting another episode");
  assert.equal(runtime.context.__getPlayer().status, "error");
  assert.match(runtime.context.__getPlayer().message, /当前集“第1集”已不在刷新后的线路中/);
  assert.equal(runtime.context.__getDetailSelection().line, 1);
  assert.equal(runtime.context.__getDetailSelection().episode, 0);
  const resolveCount = runtime.requests.length;
  assert.equal(runtime.context.selectVodLine(1), true, "retry can still target the preserved original source");
  await flush();
  assert.equal(runtime.requests.length, resolveCount,
    "manual retry does not resolve a stale playRef after refresh proves the selected episode is gone");
  assert.equal(runtime.context.__getPlayer().ctx.epName, "第1集");
  assert.match(runtime.$("#peMsg").textContent, /当前集“第1集”已不在刷新后的线路中/);
}

console.log("== forced play resolution uses fresh endpoint and latest cache write wins ==");
{
  const calls = [];
  const context = vm.createContext({
    BASE: "/web",
    fetchJSON: (url, options) => {
      const item = deferred();
      calls.push({ url: String(url), signal: options.signal, timeout: options.timeout, ...item });
      return item.promise;
    },
  });
  vm.runInContext(app.slice(helperStart, helperEnd) + app.slice(cacheStart, cacheEnd)
    + "\nglobalThis.__resolve=resolvePlayCached;", context);
  const first = context.__resolve("hhkan:7", 20, 3);
  const forced = context.__resolve("hhkan:7", 20, 3, { force: true });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].timeout, 300000, "play address resolution allows up to five minutes");
  assert.equal(calls[1].timeout, 300000, "forced play address resolution also allows up to five minutes");
  assert.equal(calls[0].url, "/web/hhkan/play/7/20/3");
  assert.equal(calls[1].url, "/web/hhkan/play/7/20/3?fresh=1");
  calls[1].resolve({ sources: [{ url: "latest" }] });
  await forced;
  calls[0].resolve({ sources: [{ url: "old" }] });
  await first;
  const cached = await context.__resolve("hhkan:7", 20, 3);
  assert.equal(cached.sources[0].url, "latest", "late cached result cannot overwrite forced result");
  const douban = context.__resolve("douban:1292052", "source/one", 2, { force: true });
  assert.equal(calls[2].url, "/web/vod/play/1292052/source%2Fone/2?fresh=1");
  calls[2].resolve({ sources: [{ url: "douban-fresh" }] });
  await douban;
}

console.log("All player-source harness checks passed.");
