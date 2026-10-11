/* Web/Windows live playback without a license and football-card inset contract.
 * Run: node tests/web_live_freeplay_alignment_harness.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, "..", "www");
const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
const style = fs.readFileSync(path.join(web, "style.css"), "utf8");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");

assert.doesNotMatch(app, /gatePlay|pwSubmit|#paywall/, "no playback path may invoke a license gate");
assert.doesNotMatch(html, /id="paywall"|id="pwBtn"/, "the automatic playback paywall is removed");

class NodeStub {
  constructor() {
    this.hidden = true;
    this.innerHTML = "";
    this.textContent = "";
    this.value = "";
    this.dataset = {};
    this.style = { setProperty() {} };
    this.listeners = new Map();
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
}

const playerStart = app.indexOf("function openPlayer(ctx)");
const playerEnd = app.indexOf("\nfunction playerVodSourceKey", playerStart);
assert.ok(playerStart >= 0 && playerEnd > playerStart, "openPlayer is present");
const nodes = new Map();
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, new NodeStub());
  return nodes.get(selector);
};
const resolved = [];
const pv = {
  ctx: null, playerSession: 0, playController: null, detailController: null, detailRequest: null,
  sources: [], srcIds: [], srcNames: [], sourceRefs: [], srcIdx: 0, recoveryGen: 0,
  recoveryAttempts: 0, recovering: false, loadGen: 0, vodSources: [], vodLineIdx: 0,
  vodSourceStatus: "idle", vodSourceMessage: "", panelOpen: false, loadPending: false,
};
const playerContext = vm.createContext({
  AbortController, pv, state: { portrait: false }, document: { body: { style: {} } }, $, 
  store: { get: (_key, fallback) => fallback, set() {} },
  initialPlayerVodSources: () => [], playerVodLineIndex: () => 0,
  syncPlayerSourcePanel() {}, setPlayIcon() {}, applyAspect() {},
  renderMorePanel() {}, refreshPlayerEpisodes() {}, showControls() {},
  resolveLiveAndPlay: ctx => resolved.push(ctx.kind), resolveVodAndPlay: ctx => resolved.push(ctx.kind),
  stopVodMediaPipeline() {}, playerFatal: message => { throw new Error(message); },
});
vm.runInContext(app.slice(playerStart, playerEnd), playerContext);
playerContext.openPlayer({ kind: "live", matchId: "match-1", title: "测试直播", urls: [] });
assert.equal($("#pPlayer").hidden, false, "an unlicensed user can open the live player");
assert.deepEqual(resolved, ["live"], "live playback resolution starts without a license gate");
playerContext.openPlayer({ kind: "vod", vid: "vod-1", title: "测试点播", urls: [] });
assert.deepEqual(resolved, ["live", "vod"], "VOD playback also starts without a license gate");

const visibilityStart = app.indexOf("function isFinishedLiveMatch");
const visibilityEnd = app.indexOf("\nfunction renderLive()", visibilityStart);
assert.ok(visibilityStart >= 0 && visibilityEnd > visibilityStart, "finished-match filter is present");
const visibilityContext = vm.createContext({
  live: { league: "all", data: { matches: [
    { match_id: "finished", status: "finished" },
    { match_id: "legacy-ended", status: " ended " },
    { match_id: "legacy-close", status: "CLOSE" },
    { match_id: "live", status: "live" },
    { match_id: "upcoming", status: "upcoming" },
  ] } },
  matchLeagueFilter: () => true,
});
vm.runInContext(app.slice(visibilityStart, visibilityEnd), visibilityContext);
assert.deepEqual(Array.from(visibilityContext.liveMatches(), m => m.match_id), ["live", "upcoming"],
  "finished, ended, and close matches are hidden while live and upcoming matches remain");

const clockStart = app.indexOf("function matchStartMillis(m)");
const clockEnd = app.indexOf("/* App heroList", clockStart);
assert.ok(clockStart >= 0 && clockEnd > clockStart, "football clock helpers are present");
const clockContext = vm.createContext({
  isFinishedLiveMatch: m => ["finished", "ended", "close"].includes(String(m.status || "").trim().toLowerCase()),
});
vm.runInContext(app.slice(clockStart, clockEnd), clockContext);
const kickoff = Date.parse("2026-12-31T23:50:00+08:00");
const previousTimeZone = process.env.TZ;
try {
  process.env.TZ = "America/Los_Angeles";
  assert.equal(clockContext.matchStartMillis({ date_full: "2026-12-31", date: "12-31", time: "23:50" }), kickoff,
    "a full Beijing kickoff date is independent of the device time zone");
} finally {
  if (previousTimeZone === undefined) delete process.env.TZ;
  else process.env.TZ = previousTimeZone;
}
const acrossNewYear = { date_full: "2026-12-31", date: "12-31", time: "23:50", status: "upcoming", minute: "" };
const duringRegularMatch = clockContext.projectLiveMatchClock(acrossNewYear, Date.parse("2027-01-01T00:10:00+08:00"));
assert.equal(duringRegularMatch.status, "live", "an upcoming match starts after the year boundary");
assert.equal(duringRegularMatch.minute, "20", "the minute projection uses the exact cross-day elapsed time");
const regularAtFinishGrace = clockContext.projectLiveMatchClock(acrossNewYear, kickoff + 150 * 60000);
assert.equal(regularAtFinishGrace.status, "upcoming", "the browser never creates a finished state");
const sourceLivePastGrace = { ...acrossNewYear, status: "live" };
assert.equal(clockContext.projectLiveMatchClock(sourceLivePastGrace, kickoff + 180 * 60000).status, "live",
  "a client clock never ends a source-live match");
const sourceLiveBeforeKickoff = { ...acrossNewYear, status: "live" };
assert.equal(clockContext.projectLiveMatchClock(sourceLiveBeforeKickoff, kickoff - 60000).status, "upcoming",
  "a false source-live label before kickoff is corrected");
const postponed = { ...acrossNewYear, status: "postponed" };
assert.equal(clockContext.projectLiveMatchClock(postponed, kickoff + 240 * 60000).status, "postponed",
  "a postponed match is not projected to live or finished");
assert.equal(clockContext.liveMatchDisplayStatus(postponed), "延期");
assert.match(app, /BASE \+ "\/api\/matches\?fresh=1"/, "web refreshes through the backend fresh-match endpoint");

const heroStart = app.indexOf("function renderLiveHero()");
const heroEnd = app.indexOf("\nfunction teamNameFont", heroStart);
assert.ok(heroStart >= 0 && heroEnd > heroStart, "live hero renderer is present");
const heroNodes = new Map();
const hero$ = selector => {
  if (!heroNodes.has(selector)) heroNodes.set(selector, new NodeStub());
  return heroNodes.get(selector);
};
hero$("#liveHero").hidden = false;
hero$("#liveScoreRow").innerHTML = "old match";
hero$("#liveStatus").innerHTML = "old status";
hero$("#liveDots").innerHTML = "old dots";
hero$("#liveCta").onclick = () => {};
const heroContext = vm.createContext({ live: { heroList: [], heroId: "finished" }, $: hero$ });
vm.runInContext(app.slice(heroStart, heroEnd), heroContext);
heroContext.renderLiveHero();
assert.equal(hero$("#liveHero").hidden, true, "empty visible-match list hides the stale hero");
assert.equal(hero$("#liveScoreRow").innerHTML, "", "stale finished score is cleared");
assert.equal(hero$("#liveCta").onclick, null, "stale finished play action is cleared");

const tvStart = app.indexOf("function playTvChannel(gi, ci, silent)");
const tvEnd = app.indexOf("\nfunction stopTvPlayback", tvStart);
assert.ok(tvStart >= 0 && tvEnd > tvStart, "TV channel playback function is present");
const tvNodes = new Map();
const tv$ = selector => {
  if (!tvNodes.has(selector)) tvNodes.set(selector, new NodeStub());
  return tvNodes.get(selector);
};
const tv = { groups: [{ channels: [{ name: "测试频道", urls: ["https://cdn.test/live.m3u8"] }] }],
  sidebarOpen: false, urls: [], lineIdx: 0 };
const tvContext = vm.createContext({
  tv, state: { portrait: false }, store: { set() {} }, $: tv$, relay: url => url,
  tvAttach() {}, hint() {}, probeAll() {}, renderTvChannels() {}, fz() {},
});
vm.runInContext(app.slice(tvStart, tvEnd), tvContext);
tvContext.playTvChannel(0, 0, false);
assert.equal(tv.playing, true, "an unlicensed user can open a TV channel");
assert.equal(tv$.call(null, "#tvPlay").hidden, false, "TV playback UI is shown without a license gate");

const desktopInset = /\.row-sec-title\.live-sec\{[^}]*padding:40px 86px 0\}/.test(style)
  && /\.today-pages\{[^}]*padding:24px 86px 40px[^}]*overflow-x:auto[^}]*scroll-padding-inline-start:86px/.test(style)
  && /\.live-grid\{[^}]*padding:24px 86px 0/.test(style);
assert.equal(desktopInset, true, "desktop date title and both card layouts share a non-edge 86px inset");
assert.match(style, /\.today-page\{[^}]*scroll-snap-align:start/,
  "desktop pages keep their horizontal snap navigation");
assert.match(style, /body\.portrait \.row-sec-title\.live-sec\{padding:40px 40px 0\}/);
assert.match(style, /body\.portrait \.live-grid\{padding:24px 40px 0\}/);
assert.match(style, /\.today-grid\{[^}]*padding:24px 40px 40px/);
assert.doesNotMatch(app, /today-pages" style="padding:/,
  "today-card inset is controlled by the shared layout styles, not a divergent inline value");

console.log("== web and Windows playback is open without a card key; football cards align to section titles ==");
