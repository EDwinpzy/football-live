/* Runtime regression harness for VOD seek gestures, stalls, and resume behavior.
 * Run: node tests/web_player_seek_harness.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = fs.readFileSync(path.join(here, "..", "www", "app.js"), "utf8");
const stateStart = app.indexOf("const MAX_RECOVERY_ATTEMPTS =");
const stateEnd = app.indexOf("\nfunction openPlayer(", stateStart);
const facadeStart = app.indexOf("function vodDirectOk(");
const facadeEnd = app.indexOf("\n/* 影视页顶部 hero", facadeStart);
const attachStart = app.indexOf("function attachPlayer(");
const recoverStart = app.indexOf("async function recoverPlayback(", attachStart);
const recoverEnd = app.indexOf("\nfunction nextSource(", recoverStart);
const scrubStart = app.indexOf("/* 进度条：", recoverEnd);
const scrubEnd = app.indexOf("/* 画幅（", scrubStart);
const gestureStart = app.indexOf("/* 手势层", scrubEnd);
const gestureEnd = app.indexOf('\n$("#ctlBack")', gestureStart);
assert.ok(stateStart >= 0 && stateEnd > stateStart, "player state and seek helpers are present");
assert.ok(facadeStart >= 0 && facadeEnd > facadeStart, "VOD attach facade is present");
assert.ok(attachStart >= 0 && recoverStart > attachStart && recoverEnd > recoverStart,
  "player attach and recovery implementations are present");
assert.ok(scrubStart >= 0 && scrubEnd > scrubStart && gestureEnd > gestureStart,
  "progress and gesture handlers are present");

class FakeNode {
  constructor(name) {
    this.name = name;
    this.hidden = true;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.paused = true;
    this.ended = false;
    this.seeking = false;
    this.readyState = 4;
    this.playbackRate = 1;
    this.volume = 1;
    this.muted = false;
    this.src = "";
    this.style = { filter: "", setProperty() {} };
    this.parentElement = { style: { setProperty() {} } };
    this.listeners = new Map();
    this.classes = new Set();
    this.classList = {
      add: value => this.classes.add(value),
      remove: value => this.classes.delete(value),
      toggle: (value, force) => force ? this.classes.add(value) : this.classes.delete(value),
      contains: value => this.classes.has(value),
    };
    this.rect = { left: 0, top: 0, width: 100 };
    this.timeWrites = [];
    this._currentTime = 0;
  }
  get currentTime() { return this._currentTime; }
  set currentTime(value) { this._currentTime = value; this.timeWrites.push(value); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  dispatch(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }
  querySelector(selector) {
    if (selector === ".ctl-rail" && this.name === "#cTrack") return this.rail;
    return new FakeNode(`${this.name} ${selector}`);
  }
  getBoundingClientRect() { return this.rect; }
  setPointerCapture() {}
  removeAttribute(name) { if (name === "src") this.src = ""; }
  getAttribute() { return null; }
  setAttribute() {}
  load() {}
  pause() { this.paused = true; }
  play() { this.paused = false; return Promise.resolve(); }
  focus() {}
}

function makeRuntime() {
  let now = 1_800_000_000_000;
  let nextTimer = 0;
  const timers = new Map();
  const schedule = (callback, delay, interval = 0) => {
    const id = ++nextTimer;
    const step = Math.max(1, Number(delay) || 0);
    timers.set(id, { id, callback, due: now + step, interval: interval ? step : 0 });
    return id;
  };
  const clear = id => timers.delete(id);
  const advance = duration => {
    const end = now + duration;
    let iterations = 100000;
    while (iterations-- > 0) {
      const next = Array.from(timers.values())
        .filter(timer => timer.due <= end)
        .sort((a, b) => a.due - b.due || a.id - b.id)[0];
      if (!next) break;
      now = next.due;
      if (next.interval) next.due += next.interval;
      else timers.delete(next.id);
      next.callback();
    }
    assert.ok(iterations > 0, "fake timer queue must settle");
    now = end;
  };
  class FakeDate extends Date { static now() { return now; } }
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new FakeNode(selector));
    return nodes.get(selector);
  };
  const video = $("#video");
  video.duration = 100;
  video.currentTime = 50;
  video.timeWrites.length = 0;
  const track = $("#cTrack");
  track.rail = new FakeNode(".ctl-rail");
  track.rail.rect = { left: 0, top: 0, width: 100 };
  const recoveryCalls = [];
  const sandbox = {
    console, Date: FakeDate, Math, JSON, Number, String, Object, Array, Set, Map, Promise, Error,
    window: null, navigator: {}, innerWidth: 300, innerHeight: 500,
    $,
    setTimeout: (callback, delay) => schedule(callback, delay),
    clearTimeout: clear,
    setInterval: (callback, delay) => schedule(callback, delay, delay),
    clearInterval: clear,
    requestAnimationFrame: callback => schedule(() => callback(now), 16),
    cancelAnimationFrame: clear,
    fmtTime: seconds => String(Math.floor(Number(seconds) || 0)),
    setPlayerProgress() {}, setPlayerPositionText() {},
    hint() {}, telemetryEmit() {}, metricBuffer() {}, metricDelta: () => 0,
    metricMilestone() {}, saveCurProgress() {}, setPlayIcon() {}, updatePb() {}, onEnded() {}, applyAspect() {},
    metrics: { marks: { click: null, prepare: null } },
    TELEMETRY_EVENTS: { ENGINE_FALLBACK: "engine", AUTO_SWITCH: "recovery", FIRST_FRAME: "first_frame" },
    classifyTelemetryError: () => "test", playerFatal: message => { throw new Error(String(message)); },
    relay: raw => `/relay/${raw}`,
    renderSrcPicker() {}, syncPlayerBackBtn() {}, armHide() {}, showControls() {}, hideControls() {},
    pv: undefined,
    __recoveryCalls: recoveryCalls,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(
    app.slice(stateStart, stateEnd)
      + app.slice(facadeStart, facadeEnd)
      + app.slice(attachStart, recoverStart)
      + app.slice(recoverStart, recoverEnd)
      + app.slice(scrubStart, scrubEnd)
      + app.slice(gestureStart, gestureEnd)
      + `
        pv.ctx = { kind: "vod", vid: "test", playRef: { pid: "p", vid: 1 } };
        pv.video = globalThis.__video;
        globalThis.__pv = pv;
        globalThis.__attach = (...args) => attachPlayer(...args);
        globalThis.__recover = (...args) => recoverPlayback(...args);
        globalThis.__seek = (...args) => seekPlayerTo(...args);
        globalThis.__replaceRecover = () => {
          recoverPlayback = (reason, generation) => globalThis.__recoveryCalls.push([reason, generation]);
        };
      `,
    context,
  );
  context.__video = video;
  vm.runInContext("pv.video = globalThis.__video", context);
  return { context, nodes, video, advance, now: () => now, recoveryCalls };
}

function tap(runtime, x) {
  const layer = runtime.nodes.get("#video");
  const point = { clientX: x, clientY: 200, pointerType: "mouse", pointerId: 1 };
  layer.dispatch("pointerdown", point);
  layer.dispatch("pointerup", point);
}

console.log("== all seek entry points clamp to duration minus one and reject unknown duration ==");
{
  const runtime = makeRuntime();
  const { context: c, video, nodes } = runtime;
  const track = nodes.get("#cTrack");
  track.dispatch("pointerdown", { clientX: 100, pointerId: 1 });
  track.dispatch("pointerup", { clientX: 100, pointerId: 1 });
  assert.equal(video.currentTime, 99, "the progress rail right endpoint seeks to duration - 1");

  video.currentTime = 40;
  video.timeWrites.length = 0;
  const layer = nodes.get("#video");
  layer.dispatch("pointerdown", { clientX: 100, clientY: 200, pointerType: "mouse" });
  layer.dispatch("pointermove", { clientX: 500, clientY: 200, pointerType: "mouse" });
  layer.dispatch("pointerup", { clientX: 500, clientY: 200, pointerType: "mouse" });
  assert.equal(video.currentTime, 99, "horizontal swipe seeks clamp to duration - 1");

  video.currentTime = 95;
  tap(runtime, 290);
  runtime.advance(100);
  tap(runtime, 290);
  assert.equal(video.currentTime, 99, "right-side double tap clamps its fast-forward target");
  video.currentTime = 50;
  tap(runtime, 10);
  runtime.advance(100);
  tap(runtime, 10);
  assert.equal(video.currentTime, 40, "left-side double tap retains its fixed ten-second rewind");
  tap(runtime, 290);
  runtime.advance(100);
  tap(runtime, 290);
  assert.equal(video.currentTime, 50, "right-side double tap retains its fixed ten-second fast-forward");

  for (const duration of [0, Infinity, NaN]) {
    video.duration = duration;
    video.currentTime = 42;
    video.timeWrites.length = 0;
    track.dispatch("pointerdown", { clientX: 100, pointerId: 2 });
    track.dispatch("pointerup", { clientX: 100, pointerId: 2 });
    tap(runtime, 290);
    runtime.advance(100);
    tap(runtime, 290);
    assert.deepEqual(Array.from(video.timeWrites), [], `duration ${duration} does not assign currentTime`);
  }
  video.duration = 100;
  assert.equal(c.__seek(video, 150), true);
  assert.equal(video.currentTime, 99);
  assert.equal(c.__seek(video, Number.NaN), false, "non-finite seek targets are rejected");
}

console.log("== long press steps left and right while the center keeps temporary 3x speed ==");
{
  const left = makeRuntime();
  left.video.currentTime = 50;
  left.video.timeWrites.length = 0;
  left.nodes.get("#video").dispatch("pointerdown", { clientX: 10, clientY: 200, pointerType: "touch" });
  left.advance(450);
  assert.equal(left.video.currentTime, 40, "left long press immediately seeks back ten seconds");
  left.advance(500);
  assert.equal(left.video.currentTime, 40, "a queued seek avoids issuing requests faster than the throttle");
  assert.match(left.nodes.get("#gestInd").innerHTML, /20/, "the second exponential step previews a thirty-second rewind");
  left.advance(500);
  assert.equal(left.video.currentTime, 20, "the queued target is dispatched after the request interval");
  assert.equal(left.video.timeWrites.length, 2, "held rewinds are limited to one request per 750ms");
  left.nodes.get("#video").dispatch("pointerup", { clientX: 10, clientY: 200, pointerType: "touch" });
  assert.equal(left.video.currentTime, 1, "accelerated rewind clamps above zero to avoid resetting playback");
  left.advance(2000);
  assert.equal(left.video.currentTime, 1, "release stops further long-press seeking");

  const right = makeRuntime();
  right.video.currentTime = 50;
  right.nodes.get("#video").dispatch("pointerdown", { clientX: 290, clientY: 200, pointerType: "touch" });
  right.advance(950);
  right.nodes.get("#video").dispatch("pointerup", { clientX: 290, clientY: 200, pointerType: "touch" });
  assert.equal(right.video.currentTime, 80, "right long press progressively seeks forward");

  const center = makeRuntime();
  center.nodes.get("#video").dispatch("pointerdown", { clientX: 150, clientY: 200, pointerType: "touch" });
  center.advance(450);
  assert.equal(center.video.playbackRate, 3, "center long press retains temporary 3x speed");
  center.nodes.get("#video").dispatch("pointerup", { clientX: 150, clientY: 200, pointerType: "touch" });
  assert.equal(center.video.playbackRate, 1, "center release restores the selected playback speed");
}

console.log("== seeking and its 15-second grace suppress false stall recovery ==");
{
  const runtime = makeRuntime();
  const { context: c, video, recoveryCalls } = runtime;
  c.__replaceRecover();
  c.__pv.ctx = { kind: "vod", vid: "test", playRef: { pid: "p", vid: 1 } };
  video.paused = false;
  c.__attach("https://media.test/current.m3u8", 0);

  video.seeking = true;
  runtime.advance(10000);
  assert.equal(recoveryCalls.length, 0, "native seeking does not count as a stall");
  video.seeking = false;
  c.__pv.seeking = true;
  runtime.advance(10000);
  assert.equal(recoveryCalls.length, 0, "progress-bar seeking does not count as a stall");
  c.__pv.seeking = false;

  assert.equal(c.__seek(video, 70), true);
  runtime.advance(15000);
  assert.equal(recoveryCalls.length, 0, "the post-seek grace period is 15 seconds");
  runtime.advance(7000);
  assert.equal(recoveryCalls.length, 0, "the eight-second stall counter restarts after grace expires");
  runtime.advance(1000);
  assert.equal(recoveryCalls.length, 1, "a real stall still recovers after the grace period");
  assert.equal(recoveryCalls[0][0], "播放 8 秒无进展");
}

console.log("== automatic recovery restores near-end progress while ordinary resume keeps its cutoff ==");
{
  const recovery = makeRuntime();
  recovery.video.duration = 100;
  recovery.video.currentTime = 98;
  recovery.video.paused = false;
  recovery.context.__pv.ctx = { kind: "vod", vid: "test", playRef: { pid: "p", vid: 1 } };
  recovery.context.__pv.sources = ["https://media.test/recovered.m3u8"];
  recovery.context.__pv.srcIdx = 0;
  recovery.context.__pv.recoveryAttempts = 2;
  await recovery.context.__recover("stalled", 0);
  assert.equal(recovery.video.currentTime, 98, "metadata has not been restored until it is available");
  recovery.video.onerror();
  assert.equal(recovery.video.src, "/relay/https://media.test/recovered.m3u8",
    "a failed direct recovery route falls back to the relay");
  recovery.video.onloadedmetadata();
  assert.equal(recovery.video.currentTime, 96,
    "near-end recovery position survives direct-to-relay fallback");

  const ordinary = makeRuntime();
  ordinary.video.duration = 100;
  ordinary.video.currentTime = 7;
  ordinary.context.__pv.ctx = { kind: "vod" };
  ordinary.context.__attach("https://media.test/ordinary.m3u8", 96);
  ordinary.video.onloadedmetadata();
  assert.equal(ordinary.video.currentTime, 7, "ordinary resume still skips positions within 30 seconds of the end");

  ordinary.context.__attach("https://media.test/ordinary.m3u8", 60);
  ordinary.video.currentTime = 7;
  ordinary.video.onloadedmetadata();
  assert.equal(ordinary.video.currentTime, 60, "ordinary resume continues working outside the end cutoff");
}

console.log("All player-seek harness checks passed.");
