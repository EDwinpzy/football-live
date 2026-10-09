/*
 * Async harness for the real heroSwap implementation in www/app.js.
 * Run: node tests/web_hero_swap_harness.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, "..", "www", "app.js"), "utf8");
const fallbackStart = source.indexOf("const imageFallbackStates = new WeakMap();");
const fallbackEnd = source.indexOf("\nfunction doubanImg", fallbackStart);
const listenerStart = source.indexOf('document.addEventListener("error", ev => {', fallbackEnd);
const listenerEnd = source.indexOf("\n\n/* 是否本地内嵌", listenerStart);
const start = source.indexOf("const heroSwapStates = new WeakMap();");
const end = source.indexOf("\n/* hero 文案列轻位移淡入", start);
assert.ok(fallbackStart >= 0 && fallbackEnd > fallbackStart, "image fallback implementation is present");
assert.ok(listenerStart >= 0 && listenerEnd > listenerStart, "document image fallback listener is present");
assert.ok(start >= 0 && end > start, "hero swap implementation is present");
const fallbackCode = source.slice(fallbackStart, fallbackEnd);
const listenerCode = source.slice(listenerStart, listenerEnd);
const heroCode = source.slice(start, end);

class FakeImage {
  constructor() {
    this.tagName = "IMG";
    this.dataset = {};
    this.style = {};
    this.id = "";
    this.alt = "";
    this.complete = false;
    this.naturalWidth = 0;
    this.parentElement = null;
    this.isConnected = false;
    this.onload = null;
    this.onerror = null;
    this.animations = [];
    this.removedSrc = false;
    this._src = "";
    this._classes = new Set();
    this.classList = {
      add: name => this._classes.add(name),
      remove: name => this._classes.delete(name),
      contains: name => this._classes.has(name),
    };
    let resolveDecode;
    let rejectDecode;
    this.decodePromise = new Promise((resolve, reject) => {
      resolveDecode = resolve;
      rejectDecode = reject;
    });
    this.resolveDecode = resolveDecode;
    this.rejectDecode = rejectDecode;
  }
  set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(" "); }
  set src(value) { this._src = String(value); this.complete = false; this.naturalWidth = 0; this.removedSrc = false; }
  get src() { return this._src; }
  getAttribute(name) { return name === "src" && this._src ? this._src : null; }
  removeAttribute(name) {
    if (name === "src") { this._src = ""; this.complete = false; this.naturalWidth = 0; this.removedSrc = true; }
  }
  decode() { return this.decodePromise; }
  after(node) {
    const siblings = this.parentElement.children;
    const index = siblings.indexOf(this);
    siblings.splice(index + 1, 0, node);
    node.parentElement = this.parentElement;
    node.isConnected = true;
  }
  remove() {
    if (!this.parentElement) return;
    const siblings = this.parentElement.children;
    const index = siblings.indexOf(this);
    if (index >= 0) siblings.splice(index, 1);
    this.parentElement = null;
    this.isConnected = false;
  }
  animate(keyframes, options) {
    const animation = { keyframes, options, cancelled: false, cancel() { this.cancelled = true; } };
    this.animations.push(animation);
    return animation;
  }
  fireLoad() { this.complete = true; this.naturalWidth = 1920; this.onload?.(); }
  fireError() {
    this.complete = true; this.naturalWidth = 0;
    this.lastErrorEvent = { target: this, type: "error" };
    this.onerror?.(this.lastErrorEvent);
  }
}

class FakeBand {
  constructor(current) {
    this.children = [current];
    current.parentElement = this;
    current.isConnected = true;
  }
  querySelector(selector) {
    assert.equal(selector, "img.hero-img");
    return this.children.find(image => image.classList.contains("hero-img")) || null;
  }
}

function makeHarness() {
  const created = [];
  const listeners = new Map();
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    Promise,
    DOUBAN_IMG_HOSTS: ["img9.doubanio.com", "img3.doubanio.com", "img2.doubanio.com", "img1.doubanio.com"],
    DOUBAN_IMG_RE: /^(https?:\/\/)img\d+\.doubanio\.com/i,
    document: {
      createElement(tag) { assert.equal(tag, "img"); const image = new FakeImage(); created.push(image); return image; },
      addEventListener(type, callback, capture) { if (type === "error" && capture) listeners.set(type, callback); },
    },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(fallbackCode + "\n" + listenerCode + "\n" + heroCode +
    "\n;globalThis.__heroSwap = heroSwap; globalThis.__clearHeroSwap = clearHeroSwap; globalThis.__retryImageFallback = retryImageFallback;", context);
  return {
    heroSwap: context.__heroSwap,
    clearHeroSwap: context.__clearHeroSwap,
    retryImageFallback: context.__retryImageFallback,
    documentError: listeners.get("error"),
    created,
    finishTimers(delay) {
      const due = [...timers.entries()].filter(([, timer]) => timer.delay === delay);
      for (const [id, timer] of due) { timers.delete(id); timer.callback(); }
    },
  };
}

async function flushPromises() {
  for (let i = 0; i < 6; i++) await Promise.resolve();
}

function makeCurrent(url = "/old.jpg", id = "vodHeroImg") {
  const image = new FakeImage();
  image.className = "hero-img";
  image.id = id;
  image.dataset.url = url;
  image.style.opacity = "1";
  return image;
}

async function testKeepsCurrentUntilLoadAndDecode() {
  const h = makeHarness();
  const current = makeCurrent();
  const band = new FakeBand(current);
  h.heroSwap(band, "/ready.jpg");
  const candidate = h.created[0];
  assert.deepEqual(band.children, [current], "unloaded image stays detached and current remains visible");
  assert.equal(current.id, "vodHeroImg", "the stable id stays on the current image while loading");

  candidate.fireLoad();
  await flushPromises();
  assert.equal(candidate.animations.length, 0, "load alone does not start the cross-fade");
  assert.deepEqual(band.children, [current], "current stays visible while decode is pending");

  candidate.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, candidate], "decoded image enters the DOM for the cross-fade");
  assert.equal(candidate.animations.length, 1, "the cross-fade starts after decode");
  assert.equal(candidate.id, "vodHeroImg", "the hero id transfers when the decoded image is committed");
  h.finishTimers(520);
  assert.deepEqual(band.children, [candidate], "the old image is removed after the fade");
}

async function testOnlyLatestRapidSelectionCanCommit() {
  const h = makeHarness();
  const current = makeCurrent();
  const band = new FakeBand(current);
  h.heroSwap(band, "/slow-a.jpg");
  const stale = h.created[0];
  stale.fireLoad();
  await flushPromises();
  h.heroSwap(band, "/latest-b.jpg");
  const latest = h.created[1];
  assert.equal(stale.removedSrc, true, "superseded pending request is released");
  assert.deepEqual(band.children, [current], "no pending target is attached invisibly");

  stale.resolveDecode();
  latest.fireLoad();
  await flushPromises();
  assert.deepEqual(band.children, [current], "a stale decode cannot be committed");
  latest.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, latest], "only the latest decoded target is attached");
  assert.equal(latest.dataset.url, "/latest-b.jpg");
}

async function testDetachedCandidateUsesExistingFallbackBeforeDecode() {
  const h = makeHarness();
  const current = makeCurrent();
  const band = new FakeBand(current);
  const original = "https://vres.example/poster.jpg";
  const proxied = "/hhkan/proxy?u=" + encodeURIComponent(original);
  h.heroSwap(band, proxied);
  const candidate = h.created[0];

  candidate.fireError();
  assert.equal(candidate.src, original, "detached hero retries the same proxy-to-origin fallback as page images");
  assert.deepEqual(band.children, [current], "fallback remains detached while the current hero stays visible");
  candidate.fireLoad();
  await flushPromises();
  candidate.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, candidate], "fallback image commits only after it loads and decodes");
  h.finishTimers(520);
  assert.deepEqual(band.children, [candidate]);
}

async function testDocumentAndElementHandlersShareOneFallbackPerError() {
  const h = makeHarness();
  const image = new FakeImage();
  image.dataset.fbk = "";
  const original = "https://img9.doubanio.com/poster.jpg";
  image.src = "/hhkan/proxy?u=" + encodeURIComponent(original);
  let targetFailures = 0;
  image.onerror = event => { if (!h.retryImageFallback(image, event)) targetFailures++; };

  const event = { target: image, type: "error" };
  h.documentError(event);
  image.onerror(event);
  assert.equal(image.src, original, "document and target handlers share the same single proxy fallback");
  assert.equal(targetFailures, 0, "a fallback already issued during capture is not mistaken for final failure");
  assert.equal(h.retryImageFallback(image, event), true, "the same event remains handled without issuing another retry");
  assert.equal(image.src, original, "duplicate handling does not skip to another mirror");
}

async function testImageFallbackUsesTheExistingFiniteHostChain() {
  const h = makeHarness();
  const current = makeCurrent();
  const band = new FakeBand(current);
  const proxy = "/hhkan/proxy?u=" + encodeURIComponent("https://img9.doubanio.com/poster.jpg");
  h.heroSwap(band, proxy);
  const candidate = h.created[0];

  const tried = [];
  for (let i = 0; i < 5; i++) {
    tried.push(candidate.src);
    const failed = candidate.src;
    candidate.fireError();
    if (i === 0) {
      assert.equal(h.retryImageFallback(candidate, candidate.lastErrorEvent), true, "same error event remains handled");
      assert.notEqual(candidate.src, failed, "handling the same event does not undo the fallback");
    }
  }
  await flushPromises();
  assert.deepEqual(tried, [
    proxy,
    "https://img9.doubanio.com/poster.jpg",
    "https://img3.doubanio.com/poster.jpg",
    "https://img2.doubanio.com/poster.jpg",
    "https://img1.doubanio.com/poster.jpg",
  ], "proxy fallback and mirror rotation stop at the same finite host list");
  assert.equal(candidate.src, "", "exhausted fallback chain fails cleanly");
  assert.deepEqual(band.children, [current], "exhausting fallbacks leaves the current hero untouched");
}

async function testEmptyHeroResetCancelsWorkAndSameUrlCanReload() {
  const h = makeHarness();
  const current = makeCurrent("/same.jpg");
  current.src = "/same.jpg";
  current.complete = true;
  current.naturalWidth = 1920;
  const band = new FakeBand(current);

  h.heroSwap(band, "/same.jpg");
  assert.equal(h.created.length, 0, "a valid current image is not needlessly reloaded");

  h.heroSwap(band, "/fade.jpg");
  const fading = h.created[0];
  fading.fireLoad(); fading.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, fading]);

  h.heroSwap(band, "/waiting.jpg");
  const pending = h.created[1];
  h.clearHeroSwap(band);
  assert.deepEqual(band.children, [current], "empty reset cancels an active transition");
  assert.equal(pending.removedSrc, true, "empty reset cancels the detached request");
  assert.equal(current.getAttribute("src"), null, "empty reset clears the current source");
  assert.equal(current.dataset.url, undefined, "empty reset clears the cached URL");

  pending.fireLoad(); pending.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current], "a canceled request cannot commit after reset");

  h.heroSwap(band, "/fade.jpg");
  const reloaded = h.created[2];
  assert.equal(reloaded.src, "/fade.jpg", "the same URL can be requested again after an empty state");
  reloaded.fireLoad(); reloaded.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, reloaded]);
}

async function testReadyLatestWaitsForActiveFadeAndFailureKeepsCurrent() {
  const h = makeHarness();
  const current = makeCurrent("/old.jpg", "liveHeroImg");
  const band = new FakeBand(current);
  h.heroSwap(band, "/first.jpg");
  const first = h.created[0];
  first.fireLoad(); first.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, first]);

  h.heroSwap(band, "/latest.jpg");
  const latest = h.created[1];
  latest.fireLoad(); latest.resolveDecode();
  await flushPromises();
  assert.deepEqual(band.children, [current, first], "ready target waits for the active fade instead of stacking images");

  h.finishTimers(520);
  assert.deepEqual(band.children, [first, latest], "latest target starts after the existing fade completes");
  assert.equal(first.id, "liveHeroImg", "the id remains attached to the currently visible hero");
  h.finishTimers(520);
  assert.deepEqual(band.children, [latest]);
  assert.equal(latest.id, "liveHeroImg");

  h.heroSwap(band, "/broken.jpg");
  const broken = h.created[2];
  broken.fireError();
  await flushPromises();
  assert.deepEqual(band.children, [latest], "failed image leaves the last good hero visible");
  assert.equal(latest.id, "liveHeroImg", "failed image cannot steal the stable hero id");
}

await testKeepsCurrentUntilLoadAndDecode();
await testOnlyLatestRapidSelectionCanCommit();
await testDetachedCandidateUsesExistingFallbackBeforeDecode();
await testDocumentAndElementHandlersShareOneFallbackPerError();
await testImageFallbackUsesTheExistingFiniteHostChain();
await testEmptyHeroResetCancelsWorkAndSameUrlCanReload();
await testReadyLatestWaitsForActiveFadeAndFailureKeepsCurrent();
console.log("hero swap async harness: 7 scenarios passed");
