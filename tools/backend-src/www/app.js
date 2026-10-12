/* My TV 网页版 — APK 前端 1:1 复刻（v1.23）
 * 设计坐标系与 App 一致：横屏 1920 / 竖屏 750，body.zoom = 视口宽/基准（sx() 同思路）。
 * 数据层：与 App 共用同一后端 API（proxy.py）：
 *   足球  /api/matches → /api/stream/{id}?src=all&fresh=1 → /api/relay
 *   影视目录 /hhkan/home|channel|latest|detail|search|filters|show|channel-sections（好好看唯一卡片源）；
 *   播放    /hhkan/play（详情中可附加其他信号源线路）
 *   电视  /api/iptv（服务端聚合频道表）→ /api/relay
 *   队标  /api/team-icon-img?name=   豆瓣 /api/douban?q=
 * 逻辑逐条对照 Kotlin 源码（LiveScreen/VodScreen/AllScreen/DetailScreen/SearchScreen/
 * FavoritesScreen/TvScreen/PlayerScreen + VodRepository/LiveRepository/IptvRepository/PlayerViewModel）。
 */
"use strict";

const VOD_CATEGORIES = [["movie", "电影"], ["tv", "电视剧"], ["anime", "国漫"], ["variety", "综艺"], ["short", "短剧"]];
const VOD_SORTS = [["hot", "热门"], ["new", "最新上映"], ["rating", "豆瓣高分"]];

/* 好好看的频道号（hhkan_snapshot.json 的 channels 键，与上面五个分类一一对应：
   1 电影 / 2 电视剧 / 3 动漫 / 4 综艺 / 6 短剧）。
   好好看是「唯一卡片源」——它不是第六个分类，而是这五个分类的片源后盾：
   豆瓣某栏空/降级时就用它补位，所以 chips 仍然只有五类。 */
const HHKAN_CHANNELS = { movie: 1, tv: 2, anime: 3, variety: 4, short: 6 };

function normalizeDoubanItem(it, category) {
  return { ...it, id: String(it.id || ""), title: it.title || "", cover: it.poster_url || it.cover || "",
    hero: heroImg(it.hero_url || it.poster_url || it.cover || ""),
    score: +it.rating || +it.score || 0, categoryId: "douban:" + category,
    area: (it.regions || []).join(" / "), tags: it.genres || it.tags || [], remark: it.remark || "" };
}

/* 好好看条目：线上 /hhkan/* 实测字段为 {id:number, title, cover, score, remark}，
   没有 hero/regions/genres。id 必须带 hhkan: 前缀，否则详情页会按豆瓣 id 去查
   /vod/detail/ 拿不到东西（2026-09-30 接入时踩过）。
   categoryId 传当前分类（douban:movie 等），让它和同栏豆瓣条目行为一致。 */
function normalizeHhkanItem(it, category) {
  const cover = it.cover || it.poster_url || "";
  const id = String(it.id == null ? "" : it.id);
  return { ...it, id: /^\d+$/.test(id) ? "hhkan:" + id : id, title: it.title || "",
    cover, hero: heroImg(cover), score: +it.score || 0,
    categoryId: category ? "douban:" + category : "", area: it.area || "", tags: it.tags || [],
    remark: it.remark || "" };
}

/* ---------------- 工具 ---------------- */
const $ = (s, p) => (p || document).querySelector(s);
const $$ = (s, p) => Array.from((p || document).querySelectorAll(s));

const BASE = (location.pathname.replace(/\/[^/]*$/, "") || "").replace(/\/+$/, "");

/* 豆瓣图床 img1..img9 同源同路径，但对第三方 Referer 的放行策略不一致：img9 放行，
   img1/img2/img3 会回 HTML 被浏览器 ORB 拦掉（net::ERR_BLOCKED_BY_ORB），表现为
   海报整片不出图。统一首选 img9，加载失败再按序轮换其他图床主机，哪个可达用哪个。 */
const DOUBAN_IMG_HOSTS = ["img9.doubanio.com", "img3.doubanio.com", "img2.doubanio.com", "img1.doubanio.com"];
const DOUBAN_IMG_RE = /^(https?:\/\/)img\d+\.doubanio\.com/i;
/* Douban CDN 图片走 /hhkan/proxy：服务端补豆瓣 Referer、轮换镜像并缓存海报。 */
const DOUBAN_ANY_RE = /^(https?:\/\/)([a-z0-9-]+\.)*douban(io)?\.com/i;
const DOUBAN_RELAY_PREFIX = "/hhkan/proxy?u=";
const imageFallbackStates = new WeakMap();
const handledImageFallbackEvents = new WeakSet();
const MAX_IMAGE_FALLBACK_RETRIES = DOUBAN_IMG_HOSTS.length + 1;
function retryImageFallback(image, event = null) {
  if (!image) return false;
  /* A capture listener and the target handler can see the same Event. Treat the
     already-issued retry as handled so the target does not fail the candidate. */
  if (event && handledImageFallbackEvents.has(event)) return true;
  const failedSrc = image.getAttribute("src") || "";
  if (!failedSrc) return false;
  let state = imageFallbackStates.get(image);
  if (!state) state = { retries: 0, lastFailedSrc: "" };
  /* Capture and element handlers must never retry the same failed source twice. */
  if (state.lastFailedSrc === failedSrc || state.retries >= MAX_IMAGE_FALLBACK_RETRIES) return false;

  let nextSrc = "";
  if (image.dataset && image.dataset.fbk !== "1") {
    const proxyMatch = failedSrc.match(/\/hhkan\/proxy\?u=([^&]+)/);
    if (proxyMatch) {
      image.dataset.fbk = "1";
      try { nextSrc = decodeURIComponent(proxyMatch[1]); } catch (e) { /* keep the original source */ }
    }
  }
  if (!nextSrc && DOUBAN_IMG_RE.test(failedSrc)) {
    const host = (failedSrc.match(/^https?:\/\/([^/]+)/i) || [])[1] || "";
    const index = DOUBAN_IMG_HOSTS.indexOf(host);
    const nextHost = DOUBAN_IMG_HOSTS[index < 0 ? 0 : index + 1];
    if (nextHost) nextSrc = failedSrc.replace(DOUBAN_IMG_RE, "$1" + nextHost);
  }
  if (!nextSrc || nextSrc === failedSrc) return false;

  try { image.src = nextSrc; } catch (e) { return false; }
  state.lastFailedSrc = failedSrc;
  state.retries++;
  imageFallbackStates.set(image, state);
  if (event) handledImageFallbackEvents.add(event);
  return true;
}
function doubanImg(u) {
  /* imgN 与 asset 图床都要求豆瓣 Referer；/hhkan/proxy 同时提供服务端镜像轮换和海报缓存。 */
  if (DOUBAN_ANY_RE.test(u))
    return DOUBAN_RELAY_PREFIX + encodeURIComponent(u) + "&poster_cache=2";
  return u;
}
document.addEventListener("error", ev => {
  const el = ev.target;
  if (!el || el.tagName !== "IMG") return;
  retryImageFallback(el, ev);
}, true);

/* 是否本地内嵌后端（App 里 Chaquopy 跑的 127.0.0.1:8090）。
   本地出口就是用户家宽，好好看 CDN 不拒；此时图片必须走本机中继，才能吃到
   服务端的 vres 镜像轮换（IMG_DOMAINS 三域）+ 磁盘缓存 + Referer 兜底。
   云端（腾讯云出口）反过来——出口 IP 被 hhkan CDN 拒，只能让访客浏览器直连。 */
const IS_LOCAL_BACKEND = (() => {
  try {
    const h = location.hostname;
    return location.protocol === "file:" || h === "localhost" || h === "127.0.0.1" || h === "[::1]";
  } catch (e) { return false; }
})();
function img(u) {
  u = doubanImg((u || "").trim());
  if (!u || u.startsWith("data:")) return u || "";
  /* 豆瓣图床的中继路径：已经是本服务地址，不能再套一层代理 */
  if (u.startsWith(DOUBAN_RELAY_PREFIX)) return BASE + u;
  /* 本地内嵌后端：一律走中继（镜像轮换 + 磁盘缓存 + Referer 兜底）。
     2026-09-19 修：vres.* 封面原先被直接放行给浏览器直连，CDN 偶发拒连/超时后
     前端只对豆瓣图床做失败轮换，vres 死卡 → 「最近热门」间歇整片空卡。
     云端（非本地）保持直连：腾讯云出口被 hhkan CDN 拒，直连反而正常出图。 */
  if (IS_LOCAL_BACKEND) return BASE + "/hhkan/proxy?u=" + encodeURIComponent(u);
  if (/^https:\/\//i.test(u)) return u;
  return BASE + "/hhkan/proxy?u=" + encodeURIComponent(u);
}
function relay(u, ref) {
  return BASE + "/api/relay?u=" + encodeURIComponent(u) + (ref ? "&ref=" + encodeURIComponent(ref) : "");
}

/* 影视播放的取流策略（2026-09-24）：
   自建服务器出口连不上部分影视 CDN（实测 hnts.ymuuy.com:65 / c.baisiweiting.com:18443
   从服务器 curl 全部超时），中继只能返回 499，网页版点播就卡在「无法播放 HTTP 400」。
   但用户浏览器所在的网络能直连这些 CDN（实测同一地址 200 + Access-Control-Allow-Origin: *）。
   所以 https 直链优先浏览器直连，直连失败再立刻回中继（同一线路只多试一次）；
   http 直链保持中继——https 页面拉 http 分片会被浏览器按混合内容拦掉。 */
function vodDirectOk(u) { return /^https:\/\//i.test(String(u || "")); }
function vodAttach(raw, resume) {
  if (!raw) return;
  const direct = vodDirectOk(raw);
  attachPlayer(direct ? raw : relay(raw, "none"), resume, { raw, direct });
}

/* 影视页顶部 hero 是全宽铺图：豆瓣列表接口只给 ~240 宽的 s_ratio_poster，拉到 1920
   糊成一片。同一张图有 /view/photo/large/public/（1142×1600）这个最大尺寸，这里统一升级。
   后端 /vod/home 已下发 hero_url 时优先用它，取不到再就地换算。 */
const DOUBAN_PHOTO_RE = /^(https?:\/\/[^/]*doubanio\.com)\/view\/photo\/[^/]+\/(?:public\/)?(p\d+\.[A-Za-z]+)$/;
function heroImg(u) {
  const s = String(u || "").trim();
  const m = s.match(DOUBAN_PHOTO_RE);
  return m ? m[1] + "/view/photo/large/public/" + m[2] : s;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
/* App formatTime：H:MM:SS / M:SS */
function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}
/* App OtvHint：底部轻提示 2500ms */
let hintTimer = 0;
function hint(msg, ms) {
  const el = $("#hint");
  el.textContent = msg; el.hidden = false;
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => el.hidden = true, ms || 2500);
}
async function fetchJSON(url, opts) {
  opts = opts || {};
  const ctrl = new AbortController();
  /* 页面切换可主动取消，超时保护仍由本请求统一持有。 */
  const external = opts.signal;
  const cancel = () => ctrl.abort(external && external.reason);
  if (external) {
    if (external.aborted) cancel();
    else external.addEventListener("abort", cancel, { once: true });
  }
  const kill = setTimeout(() => ctrl.abort(), opts.timeout || 25000);
  try {
    const r = await fetch(url, { signal: ctrl.signal, cache: opts.cache || "default" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } finally {
    clearTimeout(kill);
    if (external) external.removeEventListener("abort", cancel);
  }
}
/* Android os() 同款消毒：JSON null → ""（需求 足球#1「null'」根因） */
function os(v) { return v == null || v === "null" ? "" : String(v); }
function pct(n) { return Math.max(0, Math.min(100, Math.round(n))); }

/* ---------------- 本地存储（进度/历史/收藏，App 断点续播同语义） ---------------- */
/* ================================================================
 * 播放埋点（2026-09-28 优化方案 §7）
 *
 * 目标是把「点播放 → 首帧」这条链路量化出来（detail_ms / resolve_ms /
 * prepare_ms / first_frame_ms / total_click_to_frame_ms）。
 *
 * 隐私红线（源码级锁死，契约测试 test_playback_telemetry_privacy.py 守住）：
 *   埋点**只**允许 source_id、耗时、错误分类、布尔标记；
 *   播放地址、m3u8、Cookie、token、Authorization、签名参数一律不得上报。
 * 实现上用两道闸：① 字段白名单 TELEMETRY_ALLOWED_FIELDS；
 * ② 值级检测 looksLikeSensitiveValue——即使字段名在白名单里，
 * 值长得像地址/凭据也直接丢弃。发送前再过一遍 sanitizeTelemetry。
 *
 * 默认不发：未配置 ?telemetry= 时 TELEMETRY_ENDPOINT 为空，
 * 队列只进不出，什么都不会离开浏览器。
 * ================================================================ */
const TELEMETRY_EVENTS = Object.freeze({
  APP_START: "app_start",
  HOME_FIRST_CONTENT: "home_first_content",
  DETAIL_OPEN: "detail_open",
  RESOLVE_START: "resolve_start",
  RESOLVE_FIRST_SOURCE: "resolve_first_source",
  PLAY_CLICK: "play_click",
  PLAYER_PREPARE: "player_prepare",
  FIRST_FRAME: "first_frame",
  BUFFER_START: "buffer_start",
  BUFFER_END: "buffer_end",
  AUTO_SWITCH: "auto_switch",
  ENGINE_FALLBACK: "engine_fallback",
  PLAY_5MIN: "play_5min",
  PLAY_30MIN: "play_30min",
  FATAL: "fatal",
});
const METRIC_FIELDS = ["detail_ms", "resolve_ms", "prepare_ms", "first_frame_ms",
  "total_click_to_frame_ms"];
/* 唯一允许出网的字段。任何新增字段必须先进这张表，否则发不出去。 */
const TELEMETRY_ALLOWED_FIELDS = new Set([
  "event", "kind", "source_id", "engine", "portrait", "status", "resolve_status",
  "reason", "error_class", "attempt", "episode_count", "line_count", "has_resume",
  "app_version", "ts",
].concat(METRIC_FIELDS));
const TELEMETRY_ERROR_CLASSES = new Set(["timeout", "network", "http", "abort",
  "no_source", "decode", "unknown"]);
/* 埋点默认关闭：必须显式配置（?telemetry=/ 同源路径，或宿主注入
   OTV_TELEMETRY_URL）才会发任何数据。绝不把数据送到第三方域。 */
const TELEMETRY_ENDPOINT = (() => {
  try {
    const q = (new URLSearchParams(location.search).get("telemetry") || "").trim();
    if (q) return q;
    const injected = (window.OTV_TELEMETRY_URL || "").trim();
    /* 只接受同源相对路径 / 同源绝对地址，杜绝把埋点发到第三方。 */
    if (injected && /^\//.test(injected)) return injected;
    if (injected) {
      const u = new URL(injected, location.href);
      if (u.origin === location.origin) return u.pathname + u.search;
    }
    return "";
  } catch (e) { return ""; }
})();
const TELEMETRY_EVENT_VALUES = new Set(Object.values(TELEMETRY_EVENTS));
const TELEMETRY_QUEUE_MAX = 60;
let telemetryQueue = [];

function looksLikeSensitiveValue(v) {
  if (typeof v !== "string" || !v) return false;
  const s = v.toLowerCase();
  /* 播放地址本体：.m3u8 / http(s):// */
  if (/\.m3u8/.test(s)) return true;
  if (/https?:\/\//.test(s)) return true;
  /* 凭据类 key=value（大小写不敏感） */
  if (/(authorization|cookie|token|secret|password|passwd|auth_key|authkey|sign|md5|uid|\bkey\b)/.test(s)
      && /[:=]/.test(s)) return true;
  /* 常见的签名/鉴权 query 片段 */
  if (/[?&](sign|token|auth|key|md5|uid|t)=/.test(s)) return true;
  return false;
}
function classifyTelemetryError(err) {
  const code = String((err && (err.name || err.code)) || "").toLowerCase();
  const msg = String((err && err.message) || "").toLowerCase();
  /* 分类是封闭集合：命不中就落到 unknown，绝不把原始报错文案当分类上报。 */
  const rules = [
    ["abort", /aborterror|abort/],
    ["timeout", /timeout|timed out|超时/],
    ["no_source", /no_source|未解析到播放地址|片源/],
    ["http", /http\s*[45]\d\d|\bhttp\b/],
    ["network", /network|failed to fetch|网络|load failed/],
    ["decode", /decode|manifest|demux/],
  ];
  for (const [name, re] of rules) {
    if (TELEMETRY_ERROR_CLASSES.has(name) && (re.test(code) || re.test(msg))) return name;
  }
  return TELEMETRY_ERROR_CLASSES.has("unknown") ? "unknown" : "unknown";
}
function sanitizeTelemetry(payload) {
  const out = {};
  if (!payload || typeof payload !== "object") return out;
  Object.keys(payload).forEach(k => {
    if (!TELEMETRY_ALLOWED_FIELDS.has(k)) return;
    const v = payload[k];
    if (v == null) return;
    if (typeof v === "string") {
      if (!v || v.length > 64 || looksLikeSensitiveValue(v)) return;
      out[k] = v;
      return;
    }
    if (typeof v === "number") { out[k] = Math.round(v); return; }
    if (typeof v === "boolean") { out[k] = v; }
  });
  return out;
}
function telemetryEnabled() { return !!TELEMETRY_ENDPOINT; }
function flushTelemetry() {
  if (!telemetryEnabled() || !telemetryQueue.length) return;
  const batch = telemetryQueue.splice(0, TELEMETRY_QUEUE_MAX);
  try {
    const body = JSON.stringify(batch.map(sanitizeTelemetry).filter(e => e.event));
    if (body === "[]") return;
    /* sendBeacon 走 keepalive 通道，页面关闭也能把数据送出去。 */
    if (navigator.sendBeacon) {
      navigator.sendBeacon(TELEMETRY_ENDPOINT,
        new Blob([body], { type: "application/json" }));
      return;
    }
    fetch(TELEMETRY_ENDPOINT, {
      method: "POST", body, keepalive: true,
      headers: { "Content-Type": "application/json" },
    }).catch(() => {});
  } catch (e) { /* 埋点永远不能影响播放 */ }
}
function telemetryEmit(event, props) {
  try {
    if (!TELEMETRY_EVENT_VALUES.has(event)) return;
    const payload = sanitizeTelemetry(Object.assign({}, props || {}, {
      event: event, ts: Date.now(),
    }));
    if (!payload.event) return;
    telemetryQueue.push(payload);
    if (telemetryQueue.length > TELEMETRY_QUEUE_MAX) telemetryQueue.shift();
    if (telemetryEnabled()) flushTelemetry();
  } catch (e) { /* 埋点永远不能影响播放 */ }
}
addEventListener("visibilitychange", () => { if (document.hidden) flushTelemetry(); });
addEventListener("pagehide", flushTelemetry);

/* ---- 漏斗计时：全部走 performance.now()，不受系统时钟/标签页休眠影响 ---- */
function metricNow() {
  return (window.performance && typeof performance.now === "function")
    ? performance.now() : Date.now();
}
function metricDelta(from) {
  if (from == null) return null;
  /* clamp 到 0：后台标签页恢复后 mark 顺序可能倒置，不能出负数 */
  return Math.max(0, Math.round(metricNow() - from));
}
/* 每条会话的 mark 集合 + 已发里程碑，避免同一集重复计数 */
const metrics = { marks: {}, milestones: [], bufferStart: null };
function metricMark(name, at) { metrics.marks[name] = at == null ? metricNow() : at; }
function metricCloseDetail() {
  const d = metricDelta(metrics.marks.detail);
  if (d != null) {
    telemetryEmit(TELEMETRY_EVENTS.DETAIL_OPEN, { detail_ms: d, kind: "detail" });
  }
  return d;
}
function metricCloseResolve(status) {
  const d = metricDelta(metrics.marks.resolve);
  if (d != null) {
    telemetryEmit(TELEMETRY_EVENTS.RESOLVE_FIRST_SOURCE, {
      resolve_ms: d, resolve_status: status || "",
    });
  }
  return d;
}
function metricResetSession() {
  metrics.marks = {};
  metrics.milestones = [];
  metrics.bufferStart = null;
}
function metricMilestone(name, seconds) {
  if (metrics.milestones.indexOf(name) >= 0) return;
  metrics.milestones.push(name);
  telemetryEmit(TELEMETRY_EVENTS[name], { kind: "engagement" });
}
function metricBuffer(stall) {
  if (stall) {
    if (metrics.bufferStart != null) return;
    metrics.bufferStart = metricNow();
    telemetryEmit(TELEMETRY_EVENTS.BUFFER_START, { kind: "buffer" });
    return;
  }
  const d = metricDelta(metrics.bufferStart);
  metrics.bufferStart = null;
  telemetryEmit(TELEMETRY_EVENTS.BUFFER_END, { kind: "buffer", prepare_ms: d || 0 });
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
};
function getProgress(id) { return store.get("otvw:prog:" + id, null); }
/* App PlayerViewModel：pos>1s 才算有效进度（t<1s 不算） */
function saveProgress(id, pos, dur) {
  if (!id || !dur || pos < 1) return;
  store.set("otvw:prog:" + id, { pos, dur, ts: Date.now() });
}
/* App 语义：pos>1s 且 pos < dur-30s 才可续播（旧版 0.97 比例废除，对齐 durationMs-30_000） */
function resumable(p) { return !!(p && p.pos > 1 && (!p.dur || p.pos < p.dur - 30)); }
function pushHistory(it) {
  const list = store.get("otvw:hist", []).filter(h => h.id !== it.id);
  list.unshift(it); store.set("otvw:hist", list.slice(0, 50));
}
/* 只看影视历史（与 App HistoryEntity 同构：直播不入历史）。
   filter 兼作存量清理：旧版本把足球写成了 "live:<matchId>"，已落盘的记录不再回显。 */
function getHistory() { return store.get("otvw:hist", []).filter(h => !String(h.id || "").startsWith("live:")); }
function getFavs() { return store.get("otvw:favs", []); }
function toggleFav(it) {
  const list = getFavs();
  const i = list.findIndex(x => x.id === it.id);
  if (i >= 0) list.splice(i, 1); else list.unshift({ id: it.id, title: it.title, cover: it.cover || "", remark: it.remark || "" });
  store.set("otvw:favs", list);
  return i < 0;
}

/* ---------------- 缩放自适应（sx() 同思路） ---------------- */
const state = { tab: "live", base: 1920, zoom: 1, portrait: false };
function fit() {
  const vw = innerWidth, vh = innerHeight;
  state.portrait = vh > vw;
  state.base = state.portrait ? 750 : 1920;
  state.zoom = vw / state.base;
  document.body.style.zoom = state.zoom;
  document.body.classList.toggle("portrait", state.portrait);
  /* 物理尺寸控件（App 用 dp 不用 sx 的部件：圆形按钮/FAB/返回钮/轨道）按 zoom 反算 */
  document.documentElement.style.setProperty("--z", state.zoom);
  document.documentElement.style.setProperty("--vh", (vh / state.zoom) + "px");
  $("#tvFab") && fz($("#tvFab"));
}
addEventListener("resize", fit);
function fz(el) { el.style.setProperty("--fz", state.zoom); }
/* rememberRowColumns 同款列数公式（注册的网格在 resize/转向时重算列数） */
const GRIDS = [];
function registerGrid(sel, cardW, gap, sidePad) {
  const i = GRIDS.findIndex(g => g[0] === sel);
  if (i >= 0) GRIDS[i] = [sel, cardW, gap, sidePad]; else GRIDS.push([sel, cardW, gap, sidePad]);
  applyGridCols(sel, cardW, gap, sidePad);
}
function applyGridCols(sel, cardW, gap, sidePad) {
  const cols = Math.max(2, Math.floor((state.base - 2 * sidePad + gap) / (cardW + gap)));
  const el = $(sel);
  if (el) el.style.gridTemplateColumns = `repeat(${cols},1fr)`;
}
addEventListener("resize", () => setTimeout(() => GRIDS.forEach(g => applyGridCols(...g)), 60));

/* ---------------- hero 海报带切换动画（需求⑦：左右切换影片/比赛时交叉淡入） ---------------- */
const heroSwapStates = new WeakMap();
function prepareHeroImage(image, url) {
  let settled = false, decodeStarted = false, resolveLoad, rejectLoad;
  const promise = new Promise((resolve, reject) => { resolveLoad = resolve; rejectLoad = reject; });
  const cleanup = () => { image.onload = null; image.onerror = null; };
  const fail = error => {
    if (settled) return;
    settled = true; cleanup(); rejectLoad(error || new Error("hero image failed to load"));
  };
  const decode = () => {
    if (settled || decodeStarted) return;
    decodeStarted = true;
    if (!image.naturalWidth) { fail(new Error("hero image has no decoded pixels")); return; }
    let decoded;
    try { decoded = typeof image.decode === "function" ? image.decode() : Promise.resolve(); }
    catch (error) { fail(error); return; }
    Promise.resolve(decoded).then(() => {
      if (settled) return;
      if (!image.naturalWidth) { fail(new Error("hero image decode produced no pixels")); return; }
      settled = true; cleanup(); resolveLoad(image);
    }, fail);
  };
  image.onload = decode;
  image.onerror = event => {
    if (!retryImageFallback(image, event)) fail(new Error("hero image failed to load"));
  };
  try {
    image.src = url;
    if (image.complete && image.naturalWidth > 0) decode();
  } catch (error) { fail(error); }
  return {
    promise,
    cancel() {
      if (settled) return;
      settled = true; cleanup(); image.removeAttribute("src");
      rejectLoad(new Error("hero image request superseded"));
    },
  };
}
function cancelHeroPending(state) {
  if (!state.pending) return;
  const pending = state.pending;
  state.pending = null;
  pending.load.cancel();
  pending.image.removeAttribute("src");
}
function cancelHeroTransition(state) {
  const transition = state.transition;
  if (!transition) return;
  clearTimeout(transition.timer);
  transition.animation?.cancel();
  transition.next.classList.remove("hero-swap-in");
  transition.next.remove();
  transition.current.style.opacity = "1";
  state.transition = null;
}
function completeHeroTransition(band, state, transition) {
  if (state.transition !== transition) return;
  transition.current.remove();
  transition.animation?.cancel();
  transition.next.classList.remove("hero-swap-in");
  transition.next.style.opacity = "1";
  state.current = transition.next;
  state.transition = null;
  const pending = state.pending;
  if (pending && pending.ready) {
    state.pending = null;
    commitHeroImage(band, state, pending.image);
  }
}
function commitHeroImage(band, state, image) {
  const current = state.current;
  /* Keep the stable id on the attached image while loading. Transfer it only when
     the decoded candidate is ready to enter the cross-fade. */
  if (current.id) image.id = current.id;
  image.className = "hero-img";
  image.style.opacity = "1";
  current.after(image);
  const animation = typeof image.animate === "function"
    ? image.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 450, easing: "ease" })
    : null;
  if (!animation) image.classList.add("hero-swap-in");
  const transition = { current, next: image, animation, timer: 0 };
  transition.timer = setTimeout(() => {
    completeHeroTransition(band, state, transition);
  }, 520);
  state.transition = transition;
}
function heroSwap(band, url) {
  if (!band || !url) return;
  let state = heroSwapStates.get(band);
  if (!state) {
    const current = band.querySelector("img.hero-img");
    if (!current) return;
    state = { current, pending: null, transition: null, sequence: 0 };
    heroSwapStates.set(band, state);
  }

  if (state.pending && state.pending.url === url) return;
  if (state.transition && state.transition.next.dataset.url === url) {
    state.sequence++;
    cancelHeroPending(state);
    return;
  }
  if (state.current.dataset.url === url && state.current.getAttribute("src") &&
      state.current.complete && state.current.naturalWidth > 0) {
    state.sequence++;
    cancelHeroPending(state);
    cancelHeroTransition(state);
    return;
  }

  const sequence = ++state.sequence;
  cancelHeroPending(state);

  const image = document.createElement("img");
  image.className = "hero-img"; image.alt = ""; image.decoding = "async"; image.fetchPriority = "high";
  image.dataset.url = url;
  const pending = { image, url, sequence, load: null };
  state.pending = pending;
  pending.load = prepareHeroImage(image, url);
  pending.load.promise.then(() => {
    if (state.pending !== pending || state.sequence !== sequence) return;
    pending.ready = true;
    if (state.transition) return;
    state.pending = null;
    commitHeroImage(band, state, image);
  }, () => {
    if (state.pending !== pending || state.sequence !== sequence) return;
    state.pending = null;
    image.removeAttribute("src");
  });
}
function clearHeroSwap(band) {
  if (!band) return;
  const state = heroSwapStates.get(band);
  if (state) {
    state.sequence++;
    cancelHeroPending(state);
    cancelHeroTransition(state);
  }
  const current = state ? state.current : band.querySelector("img.hero-img");
  if (current) {
    current.removeAttribute("src");
    delete current.dataset.url;
    current.style.opacity = "1";
  }
  heroSwapStates.delete(band);
}
/* hero 文案列轻位移淡入（与海报同步换内容时） */
const refadeAnimations = new WeakMap();
function refade(el) {
  if (!el) return;
  refadeAnimations.get(el)?.cancel();
  if (typeof el.animate === "function") {
    const animation = el.animate(
      [{ opacity: .25, transform: "translateY(6px)" }, { opacity: 1, transform: "translateY(0)" }],
      { duration: 450, easing: "ease" }
    );
    refadeAnimations.set(el, animation);
    animation.onfinish = () => {
      if (refadeAnimations.get(el) === animation) refadeAnimations.delete(el);
    };
    return;
  }
  el.classList.remove("fade-swap");
  setTimeout(() => el.classList.add("fade-swap"), 0);
}

/* ---------------- 手势返回（竖屏适配④：左缘右滑 = 返回） ---------------- */
function uiBack() {
  if (!$("#pPlayer").hidden) { closePlayer(); return; }
  if (!$("#tvPlay").hidden) {
    if (!state.portrait && !$("#tvSidebar").hidden) tvCloseSidebar();
    else stopTvPlayback();
    return;
  }
  if (!$("#pDetail").hidden || !$("#pAll").hidden) { closePush(); return; }
  if (!$("#morePanel").hidden) { $("#morePanel").hidden = true; return; }
  if (state.tab === "search") { switchTab(store.get("otvw:lastTab", "live")); return; }
  scrollTo({ top: 0, behavior: "smooth" });
}
(function () {
  /* 设计像素阈值：与 App 的左缘返回意图一致，但不依赖 body.zoom，避免横竖屏切换时
     触控命中区随页面缩放失真。播放器横滑快进/亮度手势继续由 #video 自己处理。 */
  const EDGE_BACK_ZONE = 44;
  const EDGE_BACK_DISTANCE = 64;
  const EDGE_BACK_VERTICAL_TOLERANCE = 90;
  let pointer = null;
  const blocked = target => target && target.closest("#video, #cTrack, .ctl-track, input, textarea");
  const beginPointer = e => {
    /* 只接管主指针的触控/手写笔；鼠标拖动不应触发页面返回。 */
    if (!e.isPrimary || e.pointerType === "mouse") return;
    if (e.clientX > EDGE_BACK_ZONE || blocked(e.target)) return;
    pointer = { id: e.pointerId, startX: e.clientX, startY: e.clientY };
  };
  const endPointer = e => {
    if (!pointer || e.pointerId !== pointer.id) return;
    const { startX, startY } = pointer;
    pointer = null;
    const endX = e.clientX, endY = e.clientY;
    if (endX - startX > EDGE_BACK_DISTANCE && Math.abs(endY - startY) < EDGE_BACK_VERTICAL_TOLERANCE) uiBack();
  };
  const cancelPointer = () => { pointer = null; };
  if ("PointerEvent" in window) {
    addEventListener("pointerdown", beginPointer, { passive: true });
    addEventListener("pointerup", endPointer, { passive: true });
    addEventListener("pointercancel", cancelPointer, { passive: true });
  } else {
    /* 老 WebView 兜底：没有 Pointer Events 时沿用 Touch Events，且多指直接放弃。 */
    addEventListener("touchstart", e => {
      if (e.touches.length !== 1 || blocked(e.target)) return;
      const t = e.touches[0];
      if (t.clientX > EDGE_BACK_ZONE) return;
      pointer = { id: "touch", startX: t.clientX, startY: t.clientY };
    }, { passive: true });
    addEventListener("touchend", e => {
      if (!pointer || pointer.id !== "touch" || e.changedTouches.length !== 1) return;
      const t = e.changedTouches[0];
      endPointer({ pointerId: "touch", clientX: t.clientX, clientY: t.clientY });
    }, { passive: true });
    addEventListener("touchcancel", cancelPointer, { passive: true });
  }
})();

function hideBoot() { /* v1.25：启动过渡屏已按需求移除，保留空函数兼容旧调用点 */ }

/* ---------------- 会员系统（授权协议 v2，与 App LicenseManager 一致） ----------------
 * PostgreSQL 统一计算 activatedAt/expireAt，Web 只验签并保存服务端结果；
 * 最近一次成功复验后允许 24 小时网络故障，明确的封禁/无效/解绑立即失效。 */
/* 激活端点走同源 /api/activate（proxy.py 服务端转发到云端 activate）：
 * 直连云端会被网关 CORS 头合并问题拒收（函数 * + 网关回显 Origin → 非法头），
 * 同源转发在网页版/App 内嵌 webview/本地调试三种形态下都成立 */
const LICENSE_ENDPOINT = BASE + "/api/activate";
const LICENSE_REVERIFY_MS = 6 * 60 * 60 * 1000;
const LICENSE_OFFLINE_GRACE_MS = 24 * 60 * 60 * 1000;
const PLAN_DAYS = { monthly: 30, quarterly: 90, yearly: 365, lifetime: 0, weekly: 7 };
const LICENSE_PUBKEY_B64 = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJd4v53jwXWpinnG1qgVU3D9gM0VvvEFB83Bm/sAEwTcFnISEOJu8DyvyiEW7q9fR9Si7M0afjbMt6k3IOw1hw==";
function b64bytes(b64) { return Uint8Array.from(atob(b64), c => c.charCodeAt(0)); }
/* Java SHA256withECDSA 输出 DER(SEQ(r,s))；WebCrypto 要 IEEE-P1363 r||s 各 32 字节 */
function derToP1363(der) {
  const v = new Uint8Array(der);
  let i = 2; if (v[1] & 0x80) i += v[1] & 0x7f;
  const rd = () => { const l = v[i + 1]; const s = v.subarray(i + 2, i + 2 + l); i += 2 + l; return s[0] === 0 ? s.slice(1) : s; };
  const r = rd(), s = rd();
  const out = new Uint8Array(64);
  out.set(r, 32 - r.length); out.set(s, 64 - s.length);
  return out;
}
async function verifyTicketSig(t) {
  try {
    const key = await crypto.subtle.importKey(
      "spki", b64bytes(LICENSE_PUBKEY_B64), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    const data = new TextEncoder().encode(`1|${t.code}|${t.plan}|${t.days}|${t.issued}|${t.nonce}`);
    return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, derToP1363(b64bytes(t.sig)), data);
  } catch (e) { return false; }
}
async function webDeviceId() {
  let seed = store.get("otvw:deviceSeed", "");
  if (!seed) {
    seed = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}-${Math.random()}`;
    store.set("otvw:deviceSeed", seed);
  }
  const input = new TextEncoder().encode(`${seed}|web|${location.origin}`);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", input));
  return Array.from(digest, b => b.toString(16).padStart(2, "0")).join("");
}
const license = { data: store.get("otvw:ticket", null) };
function withinLicenseGrace(now) {
  const last = +store.get("otvw:licenseVerifyAt", 0) || 0;
  return last > 0 && now >= last && now - last < LICENSE_OFFLINE_GRACE_MS;
}
function licenseNow() {
  const d = license.data;
  if (!d || !d.server_now || !d.verified_local) return Date.now();
  return d.server_now + Math.max(0, Date.now() - d.verified_local);
}
function isPremium() {
  const d = license.data;
  if (!d) return false;
  if (!withinLicenseGrace(Date.now())) return false;
  if (d.expiry_at < 0) return true;
  return licenseNow() < d.expiry_at;
}
function parseServerMs(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return value > 1e12 ? value : value * 1000;
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}
async function licenseRequest(action, code, currentCode) {
  const payload = { protocol: 2, action: action, code, deviceId: await webDeviceId() };
  if (action === "renew") payload.currentCode = currentCode;
  const r = await fetch(LICENSE_ENDPOINT, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return await r.json();
}
async function applyLicenseResponse(jo) {
  if (!jo || jo.ret !== 0 || !jo.ticket || !jo.licenseCode) return false;
  const t = jo.ticket;
  const activatedAt = parseServerMs(jo.activatedAt);
  const serverNow = parseServerMs(jo.serverNow);
  const expireAt = jo.expireAt == null ? -1 : parseServerMs(jo.expireAt);
  const shapeOk = t.v === 1 && t.code === jo.licenseCode && PLAN_DAYS[t.plan] === t.days &&
    String(t.issued).length === 10 && String(t.nonce).length >= 8 && String(t.sig).length >= 64 &&
    activatedAt != null && serverNow != null && expireAt != null;
  if (!shapeOk || !await verifyTicketSig(t)) return false;
  const localNow = Date.now();
  license.data = {
    code: jo.licenseCode, plan: t.plan, days: t.days,
    expiry_at: expireAt, activated_at: activatedAt,
    server_now: serverNow, verified_local: localNow,
  };
  store.set("otvw:ticket", license.data);
  store.set("otvw:licenseVerifyAt", localNow);
  renderMemberInline();
  return true;
}
async function activateCode(raw) {
  const code = String(raw || "").trim().toUpperCase().replace(/\s+/g, "");
  if (!/^OTV-[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$/.test(code))
    return { ok: false, msg: "卡密格式不正确（OTV-XXXXX-XXXXX）" };
  let jo = null;
  try {
    const current = license.data;
    const action = current && current.code !== code ? "renew" : "activate";
    jo = await licenseRequest(action, code, current ? current.code : null);
  } catch (e) { return { ok: false, msg: "网络不可用，请稍后重试" }; }
  if (jo && jo.ret === 0 && jo.ticket) {
    if (await applyLicenseResponse(jo)) return { ok: true };
    return { ok: false, msg: "票据校验失败，请重试" };
  }
  if (jo && jo.ret === 404) return { ok: false, msg: "卡密不存在，请检查输入" };
  if (jo && jo.ret === 403) return { ok: false, msg: "卡密已被封禁" };
  if (jo && jo.ret === 402) return { ok: false, msg: "该卡密绑定设备数已满（一码 2 台）" };
  if (jo && jo.ret === 409) return { ok: false, msg: "该续费卡已使用" };
  if (jo && jo.ret === 410) return { ok: false, msg: "终身会员无需续费" };
  if (jo && jo.ret === 426) return { ok: false, msg: "当前版本过旧，请更新后重试" };
  if (jo && jo.ret === 400) return { ok: false, msg: "卡密格式不正确（OTV-XXXXX-XXXXX）" };
  return { ok: false, msg: `激活失败（${jo && jo.ret != null ? jo.ret : "网络"}），请稍后重试` };
}
/* 成功复验刷新会员状态；网络/5xx 保留上次结果，不影响播放。 */
let licenseVerifyPromise = null;
function licenseReverify() {
  if (licenseVerifyPromise) return licenseVerifyPromise;
  licenseVerifyPromise = doLicenseReverify().finally(() => { licenseVerifyPromise = null; });
  return licenseVerifyPromise;
}
async function doLicenseReverify() {
  const d = license.data;
  if (!d) return;
  /* PostgreSQL CU 降耗：静默吊销复验最多每 6 小时一次；显式激活不受影响。 */
  const last = +store.get("otvw:licenseVerifyAt", 0) || 0;
  if (Date.now() - last < LICENSE_REVERIFY_MS) return;
  try {
    const jo = await licenseRequest("verify", d.code, null);
    if (jo && [403, 404, 406].includes(jo.ret)) {
      license.data = null;
      store.set("otvw:ticket", null);
      store.set("otvw:licenseVerifyAt", 0);
      renderMemberInline();
    } else if (jo && jo.ret === 405) {
      const expiry = parseServerMs(jo.expireAt) || licenseNow();
      license.data = { ...d, expiry_at: expiry };
      store.set("otvw:ticket", license.data);
      renderMemberInline();
    } else if (jo && jo.ret === 0) {
      await applyLicenseResponse(jo);
    }
  } catch (e) {}
}
/* ---------------- 导航（tab pill） ---------------- */
const PAGE_IDS = { live: "pLive", vod: "pVod", tv: "pTv", fav: "pFav", search: "pSearch" };
function switchTab(t) {
  if (state.tab === "tv" && t !== "tv") stopTvPlayback();
  if (t !== "search") store.set("otvw:lastTab", t);
  state.tab = t;
  /* App：搜索页 MainTabBar currentKey=""——无任何 tab 高亮 */
  $$("#tabpill .tab, #tabpill .tab-search").forEach(b => b.classList.toggle("on", t !== "search" && b.dataset.tab === t));
  $$(".page").forEach(p => p.hidden = p.id !== PAGE_IDS[t]);
  scrollTo(0, 0);
  $("#tabpill").classList.remove("hide");
  if (t === "live") initLive();
  if (t === "vod") initVod();
  if (t === "tv") initTv();
  if (t === "fav") renderFav();
}
$$("#tabpill button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
let pillHidden = false;
addEventListener("scroll", () => {
  const hide = scrollY > 300 * state.zoom;   /* chromeHidePx：300 设计px 阈值 */
  if (hide !== pillHidden) {
    pillHidden = hide;
    $("#tabpill").classList.toggle("hide", hide);
  }
}, { passive: true });

/* ================================================================
 * 足球（LiveScreen 复刻）
 * ================================================================ */
const live = { data: null, heroId: null, league: "all", loaded: false, lastOk: 0, refreshing: false,
  posterIdx: (Date.now() % 36) };
/* 88zhibo 动态返回的线路名称直接显示在播放器选择器中。 */
function srcLabel(src) {
  return String(src || "信号源");
}

/* 固定分类集（App LEAGUE_FILTERS v1.16）与 filterLeagues 语义 */
const LEAGUE_FILTERS = [["all", "全部"], ["important", "重要"],
  ["英超", "英超"], ["西甲", "西甲"], ["意甲", "意甲"], ["德甲", "德甲"], ["法甲", "法甲"],
  ["中超", "中超"], ["euro", "欧战"], ["national", "国家队"]];
const TOP5_RE = /^(英超|西甲|意甲|德甲|法甲|中超|足协杯)/;
const EURO_RE = /欧冠|欧联|欧协|欧罗巴|欧国联|欧会杯/;
const NAT_RE = /世预赛|欧预赛|欧洲杯|亚洲杯|美洲杯|世界杯|友谊赛|国家队|国联/;
const POPULAR_TEAMS = ["曼联","曼彻斯特联","曼城","曼彻斯特城","利物浦","阿森纳","切尔西","热刺","托特纳姆热刺","皇家马德里","皇马","巴塞罗那","巴萨","马德里竞技","马竞","拜仁","拜仁慕尼黑","多特蒙德","国际米兰","国米","AC米兰","尤文图斯","巴黎圣日耳曼","那不勒斯","罗马","勒沃库森"];
function isImportantMatch(m) { return POPULAR_TEAMS.some(t => (m.home || "").includes(t) || (m.away || "").includes(t)) || NAT_RE.test(m.league || ""); }
function matchLeagueFilter(m, key) {
  if (key === "all") return true;
  if (key === "important") return isImportantMatch(m);
  if (key === "euro") return EURO_RE.test(m.league);
  if (key === "national") return NAT_RE.test(m.league);
  if (key === "中超") return m.league.includes("中超") || m.league.includes("足协杯");
  return m.league.includes(key);
}
/* heroImportance：live+1000 有比分+100 五大联赛/中超+50 欧战国国赛+40 */
function heroImportance(m) {
  let s = 0;
  if (m.status === "live") s += 1000;
  if (m.hasScore) s += 100;
  if (TOP5_RE.test(m.league)) s += 50;
  if (NAT_RE.test(m.league)) s += 40;
  return s;
}
/* LiveRepository 主流赛事白名单（2026-08-28 收紧版），含三级回退 */
const NON_FOOTBALL_RE = /男篮|女篮|篮球|排球|网球|琼斯杯|NBL|NBA|CBA|WNBA|冰球|棒球|橄榄球|乒乓|羽毛/;
const EURO_CLUB_CUP_RE = /欧冠|欧联|欧协|欧会|欧超|世俱|欧国联|欧罗巴/;
const MAJOR_LEAGUE_RE = /^(英超|西甲|意甲|德甲|法甲|荷甲|葡超|中超)/;
const CHINA_COMP_RE = /中超|足协杯/;
const MAJOR_CUP_RE = /^(英联杯|足总杯|社区盾|国王杯|西班牙超级杯|西超杯|意大利杯|意超杯|德国杯|德超杯|法国杯|法超杯|超级杯)/;
const NAT_COMP_RE = /世预赛|欧预赛|欧国联|欧洲杯|美洲杯|亚洲杯|世界杯|友谊赛|国家队|亚运会|奥运会|金杯赛|非洲杯|麒麟杯/;
const TOP5_TEAMS = ["曼城", "曼彻斯特城", "利物浦", "阿森纳", "曼联", "曼彻斯特联", "切尔西", "热刺", "托特纳姆热刺", "纽卡斯尔联", "阿斯顿维拉", "布莱顿", "西汉姆联", "埃弗顿", "莱斯特城", "狼队", "水晶宫", "富勒姆", "布伦特福德", "伯恩茅斯", "诺丁汉森林", "南安普顿", "伊普斯维奇", "皇家马德里", "巴塞罗那", "马德里竞技", "塞维利亚", "比利亚雷亚尔", "瓦伦西亚", "毕尔巴鄂竞技", "皇家社会", "皇家贝蒂斯", "赫塔菲", "奥萨苏纳", "马洛卡", "塞尔塔", "阿拉维斯", "赫罗纳", "拉斯帕尔马斯", "巴列卡诺", "西班牙人", "莱万特", "阿尔梅里亚", "国际米兰", "AC米兰", "尤文图斯", "罗马", "那不勒斯", "亚特兰大", "拉齐奥", "佛罗伦萨", "都灵", "博洛尼亚", "乌迪内斯", "热那亚", "恩波利", "莱切", "萨索洛", "帕尔马", "蒙扎", "科莫", "卡利亚里", "维罗纳", "拜仁慕尼黑", "拜仁", "多特蒙德", "勒沃库森", "莱比锡红牛", "法兰克福", "斯图加特", "门兴格拉德巴赫", "沃尔夫斯堡", "弗赖堡", "霍芬海姆", "奥格斯堡", "云达不莱梅", "柏林联合", "波鸿", "海登海姆", "圣保利", "美因茨", "基尔", "科隆", "巴黎圣日耳曼", "马赛", "里昂", "摩纳哥", "里尔", "尼斯", "朗斯", "雷恩", "斯特拉斯堡", "图卢兹", "南特", "欧塞尔", "勒阿弗尔", "洛里昂", "蒙彼利埃", "布雷斯特", "兰斯"];
const NATIONAL_TEAMS = ["中国", "中国男足", "日本", "韩国", "巴西", "阿根廷", "法国", "德国", "西班牙", "意大利", "英格兰", "葡萄牙", "荷兰", "比利时", "克罗地亚", "乌拉圭", "哥伦比亚", "摩洛哥", "墨西哥", "美国", "沙特阿拉伯", "沙特", "卡塔尔", "伊朗", "澳大利亚", "丹麦", "瑞士", "瑞典", "挪威", "波兰", "奥地利", "塞尔维亚", "土耳其", "希腊", "捷克", "塞内加尔", "尼日利亚", "埃及", "加拿大", "智利", "厄瓜多尔", "巴拉圭", "秘鲁", "威尔士", "苏格兰", "乌克兰", "俄罗斯"];
function isMainstreamMatch(m) {
  const L = m.league || "";
  if (NON_FOOTBALL_RE.test(L) || NON_FOOTBALL_RE.test(m.home) || NON_FOOTBALL_RE.test(m.away)) return false;
  if (TOP5_RE.test(L)) return true;
  if (MAJOR_LEAGUE_RE.test(L)) return true;
  if (CHINA_COMP_RE.test(L)) return true;
  if (EURO_CLUB_CUP_RE.test(L)) return true;
  if (NAT_COMP_RE.test(L)) return true;
  if (MAJOR_CUP_RE.test(L)) {
    const hTop = TOP5_TEAMS.includes(m.home), aTop = TOP5_TEAMS.includes(m.away);
    const hNat = NATIONAL_TEAMS.includes(m.home), aNat = NATIONAL_TEAMS.includes(m.away);
    return (hTop || hNat) && (aTop || aNat);
  }
  return false;
}
/* matches 三级回退（LiveRepository.matches）：主流×今日/明日 → 全量主流 → 全部 */
function mainstreamFilter(list) {
  const today = fmtMD(0), tomorrow = fmtMD(1);
  const main = list.filter(isMainstreamMatch);
  if (main.some(m => m.date === today || m.date === tomorrow))
    return list.filter(m => isMainstreamMatch(m) && (m.date === today || m.date === tomorrow));
  if (main.length) return main;
  return list;
}
function fmtMD(offsetDays) {
  const d = new Date(Date.now() + offsetDays * 86400000 + 8 * 3600000);
  return `${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

/* App dateLabel：今日比赛 / 明日比赛 / 9月6日 比赛 */
function dateLabel(date) {
  if (date === fmtMD(0)) return "今日比赛";
  if (date === fmtMD(1)) return "明日比赛";
  const mm = (date || "").match(/^(\d{2})-(\d{2})$/);
  return mm ? `${+mm[1]}月${+mm[2]}日 比赛` : "比赛";
}
/* App matchTimeLabel：未开赛/完场 = 联赛 M月D日 time（全 join " "）；直播中 = 分钟 */
function matchTimeLabel(m) {
  if (m.status === "live") return m.minute ? m.minute + "'" : (m.time || "");
  const dm = (m.date || "").match(/^(\d{2})-(\d{2})$/);
  const dl = dm ? `${+dm[1]}月${+dm[2]}日` : (m.date || "");
  return [m.league, dl, m.time || ""].filter(x => x).join(" ");
}
/* 以源站完整北京时间日期为准；旧缓存仅在跨年邻近窗口内补年份。 */
function matchStartMillis(m) {
  const full = (m.date_full || "").trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const dm = (m.date || "").trim().match(/^(\d{2})-(\d{2})$/);
  if (!full && !dm) return null;
  const tm = (m.time || "").trim().match(/^(\d{1,2}):(\d{2})/); if (!tm) return null;
  const toMillis = (year, month, day) => Date.parse(
    `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(+tm[1]).padStart(2, "0")}:${tm[2]}:00+08:00`
  );
  if (full) return toMillis(+full[1], +full[2], +full[3]);
  const now = Date.now();
  const beijingYear = new Date(now + 8 * 3600000).getUTCFullYear();
  const candidates = [beijingYear - 1, beijingYear, beijingYear + 1]
    .map(year => toMillis(year, +dm[1], +dm[2]));
  const nearby = candidates.filter(ts => Math.abs(ts - now) <= 31 * 86400000);
  return nearby.length ? nearby.reduce((a, b) => Math.abs(a - now) <= Math.abs(b - now) ? a : b)
    : toMillis(beijingYear, +dm[1], +dm[2]);
}
function isClockHeldLiveMatch(m) {
  const status = String((m && m.status) || "").trim().toLowerCase();
  return ["postponed", "delayed", "pending", "cancelled", "canceled", "abandoned",
    "延期", "推迟", "待定", "取消", "腰斩"].includes(status);
}
function liveMatchDisplayStatus(m) {
  const status = String((m && m.status) || "").trim().toLowerCase();
  if (["postponed", "delayed", "延期", "推迟"].includes(status)) return "延期";
  if (["pending", "待定"].includes(status)) return "待定";
  if (["cancelled", "canceled", "取消"].includes(status)) return "取消";
  if (["abandoned", "腰斩"].includes(status)) return "腰斩";
  if (status === "live") return "直播中";
  if (isFinishedLiveMatch(m)) return "完场";
  return "未开赛";
}
function projectLiveMatchClock(m, nowMillis = Date.now()) {
  if (!m || isFinishedLiveMatch(m) || isClockHeldLiveMatch(m)) return m;
  const status = String(m.status || "").trim().toLowerCase();
  if (status !== "live" && status !== "upcoming") return m;
  const start = matchStartMillis(m);
  if (start == null) return m;
  const elapsed = Math.floor((nowMillis - start) / 60000);
  if (elapsed < 0) return status === "live" ? { ...m, status: "upcoming", minute: "" } : m;
  // The backend owns completion. The browser clock only promotes an upcoming
  // fixture during its plausible match window; it never creates a final state.
  const extendedTimeMatch = /杯|淘汰|决赛|半决赛|世界杯|欧洲杯|亚洲杯|美洲杯|欧冠|欧联|欧协|世俱杯|友谊赛/.test(m.league || "");
  const finishGrace = extendedTimeMatch ? 210 : 150;
  if (status !== "upcoming" || elapsed >= finishGrace) return m;
  const played = elapsed <= 45 ? elapsed : elapsed <= 60 ? 45 : elapsed - 15;
  return { ...m, status: "live", minute: String(Math.max(0, Math.min(90, played))) };
}
/* App heroList：国际米兰优先 → 今日/明日 → 重要度；前 6 */
function isMyTeam(m) { return (m.home || "").includes("国际米兰") || (m.away || "").includes("国际米兰"); }
function buildHeroList(all) {
  const today = fmtMD(0), tomorrow = fmtMD(1);
  const cmp = (a, b) => (isMyTeam(b) - isMyTeam(a)) || (heroImportance(b) - heroImportance(a));
  const tt = all.filter(m => m.date === today || m.date === tomorrow).sort(cmp);
  const others = all.filter(m => m.date !== today && m.date !== tomorrow).sort(cmp);
  return tt.concat(others).slice(0, 6);
}

async function initLive() {
  if (live.loaded) return;
  live.loaded = true;
  await refreshLive(false);
  /* 需求① + v1.23 实时刷新：前台驻留期每 15s 检查，距上次成功刷新 >25s 静默拉取
     （后端 40s 保热 + 30s 缓存 → 开赛约 1 分钟内上屏；旧 20s/60s 要等 2-5 分钟） */
  clearInterval(live.poll);
  live.poll = setInterval(() => refreshLive(true), 10000);
  window.addEventListener("focus", () => refreshLive(true));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && live.loaded) refreshLive(true);
  });
  clearInterval(live.clock);
  live.clock = setInterval(() => {
    let changed = false;
    for (const m of (live.data && live.data.matches) || []) {
      const projected = projectLiveMatchClock(m);
      if (projected !== m) { Object.assign(m, projected); changed = true; }
    }
    if (changed) renderLive();
  }, 1000);
}
async function refreshLive(silent) {
  if (live.refreshing) return;
  live.refreshing = true;
  try {
    /* App：失败 4s 重试 ×4（后端冷抓 20s+） */
    let data = null, lastErr = null;
    for (let i = 0; i < 4; i++) {
      try { data = await fetchJSON(BASE + "/api/matches?fresh=1", { timeout: 45000 }); break; }
      catch (e) { lastErr = e; await new Promise(r => setTimeout(r, 4000)); }
    }
    if (!data) throw lastErr || new Error("拉取失败");
    let ms = (data.matches || []).map(o => ({
      match_id: os(o.match_id), home: os(o.home), away: os(o.away),
      league: os(o.league), date: os(o.date), date_full: os(o.date_full), time: os(o.time),
      status: os(o.status), minute: os(o.minute),
      /* App MatchItem：比分读 score.h / score.a（与后端 {home,away} 键位保持同源语义） */
      hs: os((o.score || {}).h), as: os((o.score || {}).a),
      channels: o.channels || [],
    })).filter(m => m.match_id && m.home && m.away);
    ms = mainstreamFilter(ms);
    live.data = { matches: ms };
    live.lastOk = Date.now();
    $("#liveLoading").hidden = true;
    renderLive();
    scheduleVodWarmup();
  } catch (e) {
    if (!silent && !live.data) $("#liveLoading").textContent = "直播数据加载失败：" + e.message;
  } finally { live.refreshing = false; }
}

function isFinishedLiveMatch(m) {
  const status = String((m && m.status) || "").trim().toLowerCase();
  return status === "finished" || status === "ended" || status === "close";
}
function liveMatches() {
  const ms = (live.data && live.data.matches) || [];
  const visible = ms.filter(m => !isFinishedLiveMatch(m));
  return live.league === "all" ? visible : visible.filter(m => matchLeagueFilter(m, live.league));
}

function renderLive() {
  const data = live.data; if (!data) return;
  $("#leagueChips").innerHTML = LEAGUE_FILTERS.map(([key, label]) =>
    `<button class="chip ${key === live.league ? "on" : ""}" data-l="${key}">${label}</button>`).join("");
  $$("#leagueChips .chip").forEach(c => c.addEventListener("click", () => {
    live.league = c.dataset.l; renderLive();
  }));
  live.heroList = buildHeroList(liveMatches());
  if (!live.heroList.some(m => m.match_id === live.heroId)) live.heroId = live.heroList[0] && live.heroList[0].match_id;
  renderLiveHero();
  /* 日期分组：App groups（今日/明日组恒在，空组「暂无比赛」） */
  const byDate = new Map();
  byDate.set(fmtMD(0), []);
  byDate.set(fmtMD(1), []);
  for (const m of liveMatches()) {
    const k = m.date || "未知";
    if (!byDate.has(k)) byDate.set(k, []);
    byDate.get(k).push(m);
  }
  let html = "", gi = 0;
  byDate.forEach((g, date) => {
    const isFirst = gi++ === 0;
    const count = g.length ? `　${g.length} 场` : "";
    html += `<div class="row-sec-title live-sec">${esc(dateLabel(date))}<span class="sec-count">${count}</span></div>`;
    if (!g.length) {
      html += `<div class="live-empty">暂无比赛</div>`;
      return;
    }
    if (isFirst) {
      /* 需求⑦：今日比赛横排；2026-09-05 需求④：超一屏宽折两行（均分、时间顺序），未超出才单行 */
      /* 竖屏优化：2 列网格自适应宽度；横屏卡片区与日期标题共用 CSS 侧距 */
      if (state.portrait) {
        const half = Math.ceil(g.length / 2);
        const rows = [g.slice(0, half), g.slice(half)];
        html += `<div class="today-grid">`
          + rows.map(r => `<div class="today-grid-row">${r.map(gameCard).join("")}</div>`).join("")
          + `</div>`;
      } else {
        const pages = Array.from({length: Math.ceil(g.length / 8)}, (_, p) => g.slice(p * 8, p * 8 + 8));
        html += `<div class="today-pages">` + pages.map(page =>
          `<div class="today-page">${page.map(gameCard).join("")}</div>`).join("") + `</div>`;
      }
    } else {
      html += `<div class="live-grid" id="liveGrid">${g.map(gameCard).join("")}</div>`;
    }
  });
  html += `<div style="height:80px"></div>`;
  $("#liveSections").innerHTML = html;
  registerGrid("#liveGrid", state.portrait ? 320 : 320, 36, state.portrait ? 40 : 86);
  /* 修复（2026-09-05 需求⑪）：旧版把 dataset.mid（字符串）直接当比赛对象传给
     playLiveMatch → m.status/match_id 全 undefined → /api/stream/undefined 全线 404、
     播放必失败。改为按 mid 查回比赛对象。 */
  $$("#liveSections .gcard").forEach(c => c.addEventListener("click", () => {
    const m = liveMatches().find(x => x.match_id === c.dataset.mid);
    if (m) playLiveMatch(m);
  }));
  bindTeamImgs($("#liveSections"));

}

/* 队标加载后处理：后端找不到队标时返回 1x1 透明图 → 替换为首字占位（App 同款兜底） */
function bindTeamImgs(scope) {
  $$("img[data-team]", scope).forEach(im => {
    const name = im.dataset.team || "?";
    const fb = document.createElement("div");
    fb.className = im.dataset.fb || "st-fb";
    fb.textContent = name[0];
    const fail = () => im.replaceWith(fb);
    if (im.complete && im.naturalWidth > 0 && im.naturalWidth < 3) fail();
    else {
      im.addEventListener("error", fail);
      im.addEventListener("load", () => { if (im.naturalWidth < 3) fail(); });
    }
  });
}

/* App GameCard（TV 版 1:1）：黑80%卡 320×146 r16；中部顺序=联赛→LIVE→分钟→比分；
 * hasScore 显示「H - A」34 Bold（不分直播/完场），否则 VS（white60）；队名 fitSize=(92/len)∈[11,18]，>8 字折行 14/17 */
function gameCard(m) {
  const team = n => {
    const len = (n || "").length;
    const wrap = len > 8;
    const fitSize = Math.max(11, Math.min(18, Math.round(92 / Math.max(len, 1))));
    const fs = wrap ? 14 : fitSize, lh = wrap ? 17 : fitSize;
    return `<div style="width:96px;flex-shrink:0;display:flex;flex-direction:column;align-items:center">
      <div style="width:72px;height:72px;flex-shrink:0;display:flex;align-items:center;justify-content:center;position:relative">
        <span style="position:absolute;font-size:34px;font-weight:700;color:#fff">${esc((n || "?")[0])}</span>
        <img decoding="async" data-team="${esc(n)}" data-fb="gfb" src="${esc(BASE + "/api/team-icon-img?name=" + encodeURIComponent(n))}" style="position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:6px" alt="">
      </div>
      <div style="margin-top:4px;max-width:96px;text-align:center;font-size:${fs}px;line-height:${lh}px;color:#fff;
        display:-webkit-box;-webkit-line-clamp:${wrap ? 2 : 1};-webkit-box-orient:vertical;overflow:${wrap ? "hidden" : "clip"}">${esc(n)}</div>
    </div>`;
  };
  const liveNow = m.status === "live";
  const hasScore = m.hs !== "" && m.as !== "";
  let mid = "";
  if (m.league) mid += `<div style="font-size:20px;color:rgba(255,255,255,.6);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">${esc(m.league)}</div>`;
  if (liveNow) {
    mid += `<div class="live-badge">LIVE</div>`;
    if (m.minute) mid += `<div style="font-size:26px;font-weight:600;color:#fff">${esc(m.minute)}'</div>`;
  } else if (isClockHeldLiveMatch(m)) {
    mid += `<div style="font-size:30px;font-weight:700;color:rgba(255,255,255,.6)">${esc(liveMatchDisplayStatus(m))}</div>`;
  }
  if (!isClockHeldLiveMatch(m) && hasScore) mid += `<div style="font-size:34px;font-weight:700;color:#fff">${esc(m.hs)} - ${esc(m.as)}</div>`;
  else if (!liveNow && !isClockHeldLiveMatch(m)) mid += `<div style="font-size:30px;font-weight:700;color:rgba(255,255,255,.6)">VS</div>`;
  /* 2026-09-05：未开赛/完场卡片显示开赛时间（直播中已有 LIVE 徽章/分钟数） */
  if (!liveNow && m.time) mid += `<div style="font-size:20px;font-weight:500;color:rgba(255,255,255,.5)">${esc(m.time)}</div>`;
  return `<div class="gcard" data-mid="${esc(m.match_id)}">
      ${team(m.home)}
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;min-width:0">${mid}</div>
      ${team(m.away)}
    </div>`;
}

/* hero：海报带（海报池 fb-XX，启动随机固定）+ ScoreRow 上下两队 + 状态行 + CTA
   （App LiveHeroBand：start80/top170 宽760；ScoreRow h96×2；CTA top500 268×94） */
function renderLiveHero() {
  const m = live.heroList.find(x => x.match_id === live.heroId) || live.heroList[0];
  if (!m) {
    $("#liveHero").hidden = true;
    $("#liveScoreRow").innerHTML = "";
    $("#liveStatus").innerHTML = "";
    $("#liveDots").innerHTML = "";
    $("#liveCta").onclick = null;
    return;
  }
  $("#liveHero").hidden = false;
  heroSwap($("#liveHero"), BASE + "/assets/football/fb-" + String((live.posterIdx % 36) + 1).padStart(2, "0") + ".jpg");
  refade($("#liveHero").querySelector(".live-hero-content"));
  const scoreRow = (team, pts) => `<div class="score-team">
      <div class="st-box">
        <span class="st-fb">${esc((team || "?")[0])}</span>
        <img decoding="async" data-team="${esc(team)}" src="${esc(BASE + "/api/team-icon-img?name=" + encodeURIComponent(team))}" alt="">
      </div>
      <span class="st-name" style="font-size:${teamNameFont(team)}px">${esc(team)}</span>
      ${pts !== "" ? `<span class="score-x">${esc(pts)}</span>` : ""}
    </div>`;
  $("#liveScoreRow").innerHTML =
    scoreRow(m.home, m.hs) + scoreRow(m.away, m.as);
  bindTeamImgs($("#liveScoreRow"));
  /* App sb-meta：直播中 = LIVE 徽章 + 「league  minute'」（双空格 join）；否则 状态 + matchTimeLabel */
  const status = m.status === "live"
    ? `<span class="live-badge">LIVE</span><span class="ls-league">${esc([m.league, m.minute ? m.minute + "'" : m.time].filter(x => x).join("  "))}</span>`
    : `<span class="ls-time">${esc(liveMatchDisplayStatus(m))}</span><span class="ls-time">${esc(matchTimeLabel(m))}</span>`;
  $("#liveStatus").innerHTML = status;
  $("#liveDots").innerHTML = live.heroList.map((x, i) => `<i class="${x.match_id === live.heroId ? "on" : ""}"></i>`).join("");
  $$("#liveDots i").forEach((d, i) => d.addEventListener("click", () => {
    live.heroId = live.heroList[i] && live.heroList[i].match_id; renderLiveHero();
  }));
  $("#liveCta").onclick = () => playLiveMatch(m);
}
function teamNameFont(n) { return (n || "").length > 10 ? 42 : (n || "").length > 8 ? 50 : (n || "").length > 6 ? 60 : 76; }

/* App playMatch 门控：已结束提示；未开赛且距开赛 >10 分钟拦截 */
function playLiveMatch(m) {
  if (m.status === "finished") { hint("比赛已结束"); return; }  /* v1.23：后端完场值是 finished，旧版误查 ended 从未生效 */
  if (isClockHeldLiveMatch(m)) { hint(liveMatchDisplayStatus(m)); return; }
  if (m.status !== "live") {
    const start = matchStartMillis(m);
    if (start != null && Date.now() < start - 10 * 60000) { hint("比赛未开始"); return; }
  }
  openPlayer({ kind: "live", matchId: m.match_id, channels: m.channels || [],
    title: `${m.home} 对阵 ${m.away}`, urls: [], srcIdx: 0 });
}

/* ================================================================
 * 影视首页（VodScreen 复刻）
 * ================================================================ */
const vod = { loaded: false, car: [], cat: null, catSel: 0, sections: [], libLoading: false, libLoadedFor: null };

/* v1.22 网页版加载提速：目录类 JSON 60s 客户端缓存——buildCatalog 与 fetchLibBlocks
   会对同一 /hhkan/channel/{cid} 各发一次（切分类时又发），缓存后 tab 内往返不重拉 */
const _jsonCache = {};
/* 请求合并：首屏会同时由首页、片库和筛选逻辑请求同一资源。旧实现只缓存
 * 已完成的响应，两个调用在同一时刻仍会各自打一次上游；弱网下既变慢又更容易
 * 触发源站限流。in-flight promise 让同一 URL 在同一窗口内始终只产生一个请求。 */
const _jsonInflight = new Map();
async function fetchJSONCached(url, ttl = 60000, timeout = 60000) {
  const hit = _jsonCache[url];
  if (hit && Date.now() - hit[0] < ttl) return hit[1];
  const pending = _jsonInflight.get(url);
  if (pending) return pending;
  const request = fetchJSON(url, { timeout })
    .then(d => {
      _jsonCache[url] = [Date.now(), d];
      return d;
    })
    .finally(() => {
      if (_jsonInflight.get(url) === request) _jsonInflight.delete(url);
    });
  _jsonInflight.set(url, request);
  return request;
}

/* 首页与用户点进影视页常发生在同一网络窗口；复用同一请求而不重复抓取。 */
let _vodHomeInflight = null;
function normalizeVodHome(home) {
  if (!home) return home;
  return {
    ...home,
    /* 后端首页契约历史上用 title，网页渲染用 name；统一后避免热门块匹配失效。 */
    sections: (home.sections || []).map(s => ({ ...s, name: os(s.name || s.title) })),
  };
}
function fetchVodHome(category = "movie", refresh = false) {
  const suffix = refresh ? "&refresh=1" : "";
  if (refresh) {
    return fetchJSONCached(
      BASE + "/vod/home?category=" + encodeURIComponent(category) + suffix, 0, 90000)
      .then(home => normalizeVodHome({ ...home, sections: (home.sections || []).map(s => ({
        ...s, items: (s.items || []).map(it => normalizeDoubanItem(it, category)),
      })) }));
  }
  if (_vodHomeInflight) return _vodHomeInflight;
  /* 走同一份客户端缓存 + in-flight 表：旧实现是裸 fetchJSON，进影视页时
     buildCatalogProgressive / fetchLibBlocks / 预热三处会把 movie 分类同时打 3 次。 */
  const request = fetchJSONCached(BASE + "/vod/home?category=" + encodeURIComponent(category), 60000, 90000)
    .then(home => normalizeVodHome({ ...home, sections: (home.sections || []).map(s => ({ ...s, items: (s.items || []).map(it => normalizeDoubanItem(it, category)) })) }))
    .finally(() => {
      if (_vodHomeInflight === request) _vodHomeInflight = null;
    });
  _vodHomeInflight = request;
  return request;
}

/* App 目录（RemoteApiSource/HhkanSource.snapshot）：5 频道并发 → categories + normTitle 去重 */
const CH_CHANNELS = VOD_CATEGORIES;
async function buildCatalog() {
  const rs = await Promise.allSettled(CH_CHANNELS.map(async ([cid, name]) => {
    const d = await fetchJSONCached(BASE + "/vod/home?category=" + cid);
    return { cid, name, items: (d.sections || []).flatMap(s => s.items || []).map(it => normalizeDoubanItem(it, cid)) };
  }));
  const cats = [], items = [];
  for (const r of rs) {
    if (r.status !== "fulfilled") continue;
    const { cid, name, items: its } = r.value;
    cats.push({ id: "douban:" + cid, name });
    its.forEach(it => { it.categoryId = "douban:" + cid; items.push(it); });
  }
  if (!cats.length) return null;
  const seen = new Set(), deduped = [];
  for (const it of items) {
    const k = (it.title || "").replace(/\s+/g, "").toLowerCase();
    if (!k || seen.has(k)) continue;
    seen.add(k); deduped.push(it);
  }
  // 修复（2026-09-05 需求⑪）：字段名对齐 App 的 dedupedItems——旧版返回 deduped，
  // 而搜索本地过滤 / 全部影视页 / hero 降级链都读 dedupedItems → undefined.filter
  // 抛 TypeError（搜索页整个挂掉、「全部」目录页空白）
  return { cats, dedupedItems: deduped };
}

/* 首屏目录渐进聚合：不让最慢的一个频道阻塞已经返回的电影/剧集数据。 */
async function buildCatalogProgressive(onUpdate, refresh = false) {
  const parts = new Map();
  const snapshot = () => {
    const cats = [], items = [];
    for (const [cid, name] of CH_CHANNELS) {
      const part = parts.get(cid);
      if (!part) continue;
      cats.push({ id: "douban:" + cid, name });
      part.forEach(it => items.push({ ...it, categoryId: "douban:" + cid }));
    }
    const seen = new Set(), dedupedItems = [];
    for (const it of items) {
      const k = (it.title || "").replace(/\s+/g, "").toLowerCase();
      if (!k || seen.has(k)) continue;
      seen.add(k); dedupedItems.push(it);
    }
    return cats.length ? { cats, dedupedItems } : null;
  };
  await Promise.all(CH_CHANNELS.map(async ([cid]) => {
    try {
      const d = await fetchJSONCached(
        BASE + "/vod/home?category=" + cid + (refresh ? "&refresh=1" : ""),
        refresh ? 0 : 60000);
      parts.set(cid, (d.sections || []).flatMap(s => s.items || []).map(it => normalizeDoubanItem(it, cid)));
      const current = snapshot();
      if (current) onUpdate(current);
    } catch (e) {}
  }));
  return snapshot();
}

/*
 * 默认足球页稳定后，利用空闲期预热首页和最常访问的电影/电视剧目录。真正打开
 * 影视页时会复用同一 in-flight/cache；省流和 2G 网络不做预热，首屏不受影响。
 */
let vodWarmupScheduled = false;
function scheduleVodWarmup() {
  if (vodWarmupScheduled || vod.loaded) return;
  const net = navigator.connection;
  if (net && (net.saveData || /(^|-)2g/.test(net.effectiveType || ""))) return;
  vodWarmupScheduled = true;
  const warm = () => {
    fetchVodHome().then(home => {
      if (!home) return;
      home._ts = Date.now();
      store.set("otvw:home", home);
    }).catch(() => {});
    CH_CHANNELS.slice(0, 2).forEach(([cid]) => {
      fetchJSONCached(BASE + "/vod/home?category=" + cid).catch(() => {});
    });
  };
  if ("requestIdleCallback" in window) window.requestIdleCallback(warm, { timeout: 5000 });
  else setTimeout(warm, 1800);
}

/* App refreshHome hero 降级链：carousel≥3 → 首页热门板块合并 take8 → 目录前 6。
   2026-09-18：设备端/云端 /vod/home 的板块名是「最近热门 / 最新上映 / 豆瓣高分」，
   旧键表只有好好看时代的「近期热门…」，豆瓣源下恒不命中 → hero 只能靠目录兜底，
   目录一空 hero 就整条空掉（用户报障「影视界面崩了」）。两套键都留。 */
const HOT_KEYS = ["近期热门", "最近更新", "近期热播", "热播", "近期热门电影", "近期热门剧集",
  "最近热门", "最新上映", "豆瓣高分"];
function buildHeroes(home, cat) {
  if (home && home.carousel && home.carousel.length >= 3)
    return home.carousel.map(c => {
      const id = c.vid != null ? c.vid : c.id;
      return { id: String(id || "").startsWith("hhkan:") ? String(id) : "hhkan:" + id,
        title: c.title, bg: heroImg(c.backdrop || c.cover), tag: (c.tags || [])[0] || "" };
    }).filter(c => c.id !== "hhkan:");
  const hot = home && home.sections
    ? home.sections.filter(s => HOT_KEYS.some(k => s.name.includes(k)))
      .flatMap(s => (s.items || []).map(it => ({ id: it.id, title: it.title, bg: it.hero || it.cover, tag: s.name })))
      .filter((v, i, a) => a.findIndex(x => x.id === v.id) === i)
    : [];
  if (hot.length) return hot.slice(0, 8);
  /* 旧版 localStorage 缓存可能没有 dedupedItems（老接口叫 deduped）——直接 .filter 会抛
     TypeError，把整个影视页的重绘打断。 */
  const catItems = (cat && Array.isArray(cat.dedupedItems)) ? cat.dedupedItems : [];
  return catItems.filter(it => it.cover).slice(0, 6)
    .map(it => ({ id: it.id, title: it.title, bg: it.hero || it.cover, tag: "" }));
}

/* 分类页以好好看频道三栏目为主。旧版先拼首页/全站最新，分类会混入别类内容，
 * 且好好看接口只在“全挂”时才调用，造成栏目标题被吞。补充请求现在仅填补主源缺栏。 */
const LIB_HOT_SECS = {
  1: ["近期热门电影", "热门电影"],
  2: ["近期热门剧集", "热门剧集"],
  3: ["热播动漫", "热门动漫"],
  4: ["热播综艺纪录", "热门综艺"],
  6: ["近期热门短剧", "热门短剧"],
};
const BLOCK_ORDER = ["最近热门", "最新上映", "豆瓣高分"];
const SOURCE_BLOCK_ORDER = ["最近热门", "最新上映", "豆瓣高分"];
const VOD_LIB_PAGED_CATEGORIES = new Set(["movie", "tv", "anime", "variety", "short"]);
const VOD_LIB_PAGE_STATE = new Map();

function displayLibSectionName(name) {
  return name === "最新上映" ? "最近更新" : name;
}

function displayVodCategoryName(id, name) {
  return /(?:^|:)anime$/.test(String(id || "")) ? "国漫" : name;
}

function canonicalLibBlockName(raw) {
  const name = os(raw).replace(/\s+/g, "");
  if (/更新|连载|追更/.test(name)) return "最近更新";
  if (/热门|热播|推荐|人气|精选/.test(name)) return "最近热门";
  if (/最新|上线|上新|新片|新剧/.test(name)) return "最新上线";
  return "";
}

function libBlockMap(sections) {
  const map = Object.create(null), unknown = [];
  (sections || []).forEach(section => {
    const items = Array.isArray(section.items) ? section.items : [];
    if (!items.length) return;
    const name = canonicalLibBlockName(section.name || section.title);
    if (name) {
      if (!map[name] || items.length > map[name].length) map[name] = items;
    } else {
      unknown.push(items);
    }
  });
  /* 源站若只改了标题文案但仍保留三段布局，按原频道布局顺序补名，避免整组消失。 */
  SOURCE_BLOCK_ORDER.forEach(name => {
    if (!map[name] && unknown.length) map[name] = unknown.shift();
  });
  return map;
}

function completeLibBlocks(map) {
  return BLOCK_ORDER.map(name => ({ name, items: map[name] || [] }));
}

function categoryHomeHot(cid, homeSections) {
  const hints = LIB_HOT_SECS[cid] || [];
  if (!hints.length) return [];
  const hit = (homeSections || []).find(section => {
    const name = os(section.name || section.title);
    return hints.some(hint => name === hint || name.includes(hint));
  });
  return hit && Array.isArray(hit.items) ? hit.items : [];
}

async function fetchLibBlocks(cid, homeSections, refresh = false) {
  const doubanHome = await fetchJSONCached(
    BASE + "/vod/home?category=" + cid + (refresh ? "&refresh=1" : ""),
    refresh ? 0 : 60000, 30000);
  if (doubanHome.stale) refreshStaleVodHome(cid);
  const blocks = (doubanHome.sections || []).map(s => ({
    name: s.title || s.name,
    items: (s.items || []).map(it => normalizeDoubanItem(it, cid)),
  }));
  /* 豆瓣三栏都在就不用碰好好看（省一次上游请求）。 */
  if (blocks.length && blocks.every(b => b.items.length)) return blocks;

  const hkCid = HHKAN_CHANNELS[cid];
  if (!hkCid) return blocks;
  /* 动漫目录严格只使用 /vod/home?category=anime 的内容；HHKan 频道无法保证国漫语义。 */
  if (cid === "anime") return blocks;

  /* 豆瓣缺栏（或整页降级）时用好好看补位。所有路径均绑定 hkCid；
     不调用 /hhkan/latest 或 /hhkan/show/0，避免跨分类混入内容。好好看是唯一卡片源，这里是它
     在前端唯一的消费点——2026-09-30 之前这段是死代码（上面那行 return
     把它整个跳过了），所以页面上从没出现过好好看的内容。 */
  let primary = null;
  try {
    /* 好好看频道页是唯一自带“三栏目 + 当前分类”语义的接口，优先且限时等待。 */
    primary = await fetchJSONCached(BASE + "/hhkan/channel-sections/" + hkCid, 60000, 20000);
  } catch (e) {}
  const hkBlocks = libBlockMap(primary && primary.sections);
  /* 标题名对齐：好好看叫「最新上线/最近更新」，豆瓣叫「最新上映」。
     补位时保留豆瓣那一栏的名字，避免用户看到栏名忽变。 */
  const alias = { 最新上线: "最新上映", 最近更新: "最新上映" };
  const put = (name, items) => {
    if (!Array.isArray(items) || !items.length) return;
    const target = alias[name] || name;
    const cur = blocks.find(b => b.name === target);
    if (cur) { if (!cur.items.length) cur.items = items; }
    else blocks.push({ name: target, items });
  };

  const hk = items => (items || []).map(it => normalizeHhkanItem(it, cid));
  put("最近热门", hk(hkBlocks["最近热门"]));
  put("最新上线", hk(hkBlocks["最新上线"]));
  put("最近更新", hk(hkBlocks["最近更新"]));

  /* 三栏还缺就退到 /hhkan/show 的不同排序窗口，避免同栏重复。 */
  const tasks = [];
  const has = n => blocks.some(b => b.name === n && b.items.length);
  if (!has("最近热门")) {
    const hot = categoryHomeHot(cid, homeSections);
    if (hot.length) put("最近热门", hot.map(it => normalizeDoubanItem(it, cid)));
    else {
      tasks.push(fetchJSONCached(BASE + "/hhkan/show/" + hkCid + "?by=3&page=1")
        .then(d => ["最近热门", (d.items || []).map(it => normalizeHhkanItem(it, cid))]));
    }
  }
  if (!has("最新上映")) {
    tasks.push(fetchJSONCached(BASE + "/hhkan/channel/" + hkCid)
      .then(d => ["最新上线", (d.items || []).map(it => normalizeHhkanItem(it, cid))]));
  }
  if (!has("最近更新")) {
    /* 不再拿全站 /latest 混入别的分类，按当前频道的“最新”排序补位。 */
    tasks.push(fetchJSONCached(BASE + "/hhkan/show/" + hkCid + "?by=2&page=1")
      .then(d => ["最近更新", (d.items || []).map(it => normalizeHhkanItem(it, cid))]));
  }
  const settled = await Promise.allSettled(tasks);
  settled.forEach(result => {
    if (result.status === "fulfilled") put(result.value[0], result.value[1]);
  });
  return blocks;
}

/* 后端冷缓存先回快照并在后台抓当天数据；网页不能把 stale 响应当成最终结果。 */
const _staleVodRefreshes = new Map();
function refreshStaleVodHome(cid) {
  if (_staleVodRefreshes.has(cid)) return _staleVodRefreshes.get(cid);
  const request = (async () => {
    try {
      for (let i = 0; i < 30; i++) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const fresh = await fetchJSON(BASE + "/vod/home?category=" + encodeURIComponent(cid) + "&_refresh=" + Date.now(), { timeout: 30000 });
        if (!fresh.stale) return fresh;
      }
    } catch (e) {}
    return null;
  })().finally(() => _staleVodRefreshes.delete(cid));
  _staleVodRefreshes.set(cid, request);
  return request;
}

/* v1.25 需求①：板块 SWR 持久缓存（localStorage，10min 新鲜阈值）——
   旧值先上屏秒渲染；足够新鲜（≤10min 且含最近热门）不再回源，否则后台刷新覆盖 */
function libCacheKey(cid) { return "otvw:lib:v5:" + cid; }
async function fetchLibBlocksSWR(cid, homeSections, cb, refresh = false) {
  const cached = store.get(libCacheKey(cid), null);
  if (cached && cached.sections && cached.sections.length) {
    cb(cached.sections);
    const fresh = Date.now() - (cached.ts || 0) < 600000 &&
      BLOCK_ORDER.every(name => cached.sections.some(s => s.name === name && s.items && s.items.length));
    if (fresh && !refresh) return;
  }
  const sections = await fetchLibBlocks(cid, homeSections, refresh);
  if (sections.length) {
    try { store.set(libCacheKey(cid), { ts: Date.now(), sections }); } catch (e) {}
    cb(sections);
  }
  const fresh = _staleVodRefreshes.get(cid);
  if (fresh) {
    fresh.then(home => {
      if (!home || home.stale) return;
      const next = (home.sections || []).map(s => ({
        name: s.title || s.name,
        items: (s.items || []).map(it => normalizeDoubanItem(it, cid)),
      }));
      if (!next.length) return;
      try { store.set(libCacheKey(cid), { ts: Date.now(), sections: next }); } catch (e) {}
      cb(next);
    }).catch(() => {});
  }
}

/* App vodBrief：hero 详情补全 meta/desc/评分（缓存） */
const briefCache = {};
/* 同一 id 的详情并发去重：hero 轮播/重绘会在首个请求回来前重复调用，
   实测同一部片会同时打 5 次 /vod/detail（briefCache 只在成功后才写入）。 */
const briefInflight = {};
function vodIdentity(vid) {
  const value = String(vid || "");
  const hhkan = /^hhkan:(\d+)$/.exec(value);
  return hhkan ? { kind: "hhkan", id: hhkan[1] }
    : { kind: "douban", id: value.replace(/^douban:/, "") };
}
function vodDetailPath(vid) {
  const ref = vodIdentity(vid);
  return ref.kind === "hhkan" ? `/hhkan/detail/${ref.id}` : `/vod/detail/${ref.id}`;
}
function vodPlayPath(vid, pid, evid) {
  const ref = vodIdentity(vid);
  return ref.kind === "hhkan"
    ? `/hhkan/play/${ref.id}/${encodeURIComponent(pid)}/${evid}`
    : `/vod/play/${ref.id}/${encodeURIComponent(pid)}/${evid}`;
}
async function vodBrief(id) {
  if (briefCache[id] !== undefined) return briefCache[id];
  if (briefInflight[id]) return briefInflight[id];
  const request = (async () => {
    try {
      const ref = vodIdentity(id);
      const d = await fetchJSON(BASE + vodDetailPath(id) + "?defer_sources=1", { timeout: 60000 });
      if (!d.title) { briefCache[id] = null; return null; }
      /* 2026-09-18：详情接口已是豆瓣 subject 形态（summary/rating/year/regions/genres）。
         旧代码读好好看口径的 meta/desc/score——三个字段都取不到，简介恒为空、评分恒 0，
         hero 只剩「XXX · My TV 精选内容，一键播放。」占位。meta 拼法与 App 端保持一致。 */
      const v = {
        meta: ref.kind === "hhkan" ? (d.meta || [d.year, d.area, ...(d.tags || [])].filter(Boolean).join(" / "))
          : [d.year, (d.regions || []).join(" / "), (d.genres || []).join(" / ")].filter(Boolean).join(" / "),
        desc: d.summary || d.desc || "",
        rating: +d.rating || +d.score || 0,
      };
      briefCache[id] = v;
      return v;
    } catch (e) { return null; }
  })().finally(() => { delete briefInflight[id]; });
  briefInflight[id] = request;
  return request;
}

/* ---------------- 影视页门闸（2026-09-17 用户要求） ----------------
 * 「要影视界面加载完成、有图片和信息后才能进入，否则就在加载页面等待」。
 * 判定链：目录/首页数据到位 → 当前分类三块回执到位（空栏也算到位）→ 首屏封面预载完；
 * 数据 12s / 图片 10s 硬上限，图挂了或超时一律放行，绝不把用户永久扣在加载页。 */
const VOD_GATE_DATA_TIMEOUT = 12000;
const VOD_GATE_IMAGE_TIMEOUT = 10000;
let vodGateAttempt = 0;

function vodGateReady() {
  if (!vod.cat && !(vod.homeSections || []).length) return false;
  const sel = vodCategories()[vod.catSel] || { id: "" };
  /* 「全部 ›」网格只依赖目录条目；分类页要等当前分类三块回执（空栏也认） */
  if (sel.id === "" || !vod.cat) return true;
  return vod.libLoadedFor === sel.id;
}

/* 首屏要预载的图：hero 大图 + 当前分类三块各前 12 张（或「全部」网格前 18 张） */
function vodFirstScreenImages() {
  const urls = [];
  const push = u => { const v = img(u); if (v && urls.indexOf(v) < 0) urls.push(v); };
  const hero = (vod.heroes || [])[vod.heroIdx || 0];
  if (hero && hero.bg) push(hero.bg);
  const sel = vodCategories()[vod.catSel] || { id: "" };
  if (sel.id === "" || !vod.cat) {
    ((vod.cat && vod.cat.dedupedItems) || []).slice(0, 18).forEach(it => push(it.cover));
  } else {
    (vod.sections || []).forEach(sec => (sec.items || []).slice(0, 12).forEach(it => push(it.cover)));
  }
  return urls.slice(0, 40);
}

function preloadImg(url) {
  return new Promise(resolve => {
    if (!url) { resolve(false); return; }
    const im = new Image();
    const done = ok => { im.onload = im.onerror = null; resolve(ok); };
    im.onload = () => done(true);
    im.onerror = () => done(false);
    im.src = url;
    if (im.complete && im.naturalWidth > 0) done(true);
  });
}

function revealVod() {
  $("#pVod").classList.remove("gated");
  $("#vodLoading").hidden = true;
}

/* 失败放行：撤掉遮罩但保留错误文案（见 initVod 的失败分支） */
function failVodGate() {
  vodGateAttempt++;   /* 让还在等数据的门闸循环失效，别再回头撤掉错误文案 */
  $("#pVod").classList.remove("gated");
}

async function runVodGate() {
  const attempt = ++vodGateAttempt;
  const deadline = Date.now() + VOD_GATE_DATA_TIMEOUT;
  while (Date.now() < deadline) {
    if (vodGateReady()) break;
    await new Promise(r => setTimeout(r, 120));
  }
  if (attempt !== vodGateAttempt) return;          /* 期间又重新进过页 */
  await Promise.race([
    Promise.all(vodFirstScreenImages().map(preloadImg)),
    new Promise(r => setTimeout(r, VOD_GATE_IMAGE_TIMEOUT)),
  ]);
  if (attempt !== vodGateAttempt) return;
  revealVod();
}

async function refreshVodIncremental() {
  const applyCat = cat => {
    if (!cat) return;
    vod.cat = cat;
    try { store.set("otvw:cat", cat); } catch (e) {}
    renderVodHero(); renderCatChips(); renderVodLib();
  };
  const catP = buildCatalogProgressive(applyCat, true).then(applyCat).catch(() => null);
  const homeP = fetchVodHome("movie", true).then(home => {
    if (!home) return null;
    home._ts = Date.now();
    vod.car = (home.carousel || []).slice(0, 8);
    vod.homeSections = home.sections || [];
    try { store.set("otvw:home", home); } catch (e) {}
    renderVodHero(); renderVodLib();
    return home;
  }).catch(() => null);
  await Promise.all([catP, homeP]);
  renderHistRow();
  renderVodLib(true);
}

async function initVod() {
  /* 2026-09-21 用户要求：每次进影视页都回源校验一轮（源站新增影片/剧集要能看到）。
    本地缓存先上屏，增量结果后台合并；回源失败保留旧数据，不把页面清空。 */
  const refresh = true;
  if (vod.loaded) {
    if (refresh) refreshVodIncremental();
    return;
  }
  vod.loaded = true;
  /* 2026-09-17 用户要求：影视页要「数据 + 图片都就绪」才上屏，否则留在加载页等。
     gated 期间整页内容 display:none，只剩 #vodLoading，避免先出空壳再一张张补海报。 */
  $("#pVod").classList.add("gated");
  runVodGate();
  /* 需求①（v1.25 加载提速）：目录/首页数据 SWR 持久缓存——localStorage 旧值先上屏
     （二次进站秒开），后台拉新后重渲染（≤5min 视为新鲜直接用） */
  const catC = store.get("otvw:cat", null), homeC = store.get("otvw:home", null);
  const applyVod = (cat, home) => {
    /* 允许首页和目录分别到达：先来的结果立即可见，避免被最慢的源站拖住
     * 整个影视页；后到的结果只补齐对应区域。 */
    if (cat) vod.cat = cat;
    if (home) {
      home = normalizeVodHome(home);
      vod.car = (home.carousel || []).slice(0, 8);
      vod.homeSections = home.sections || [];
    }
    renderVodHero(); renderHistRow(); renderCatChips(); renderVodLib();
  };
  if (catC || homeC) applyVod(catC, homeC);
  let arrived = !!(catC || homeC);
  const catP = buildCatalogProgressive(cat => {
    arrived = true; applyVod(cat, null);
    try { store.set("otvw:cat", cat); } catch (e) {}
  }, refresh).then(cat => {
    if (!cat) return null;
    arrived = true; applyVod(cat, null);
    try { store.set("otvw:cat", cat); } catch (e) {}
    return cat;
  }).catch(() => null);
  const homeP = fetchVodHome("movie", refresh).then(home => {
    if (!home) return null;
    home._ts = Date.now(); arrived = true; applyVod(null, home);
    try { store.set("otvw:home", home); } catch (e) {}
    return home;
  }).catch(() => null);
  const [cat, home] = await Promise.all([catP, homeP]);
  if (refresh) renderVodLib(true);
  const homeFresh = homeC && Date.now() - (homeC._ts || 0) < 300000;
  if (!cat && !home && !catC && !homeC) {
    $("#vodLoading").textContent = "片库加载失败，请稍后重试";
    failVodGate();
    vod.loaded = false;
    setTimeout(() => { vod.loaded = false; }, 4000);
    return;
  }
  /* 失败时保留已先行绘制的缓存/部分结果；只有真正没有任何可用数据才进入失败态。 */
  if (!arrived && !homeFresh) {
    $("#vodLoading").textContent = "片库加载失败，请稍后重试";
    failVodGate();
    vod.loaded = false;
    setTimeout(() => { vod.loaded = false; }, 4000);
  }
}

/* App VodScreen chips：目录 categories + 末尾「全部 ›」；记住上次分类；点分类切三块，点「全部›」开全部页 */
function vodCategories() {
  const order = new Map(VOD_CATEGORIES.map(([id], index) => [id, index]));
  return vod.cat ? vod.cat.cats.map(c => ({ id: c.id, name: displayVodCategoryName(c.id, c.name) }))
    .sort((a, b) => (order.get(a.id.replace("douban:", "")) ?? 99) - (order.get(b.id.replace("douban:", "")) ?? 99))
    .concat([{ id: "", name: "全部 ›" }])
    : [{ id: "", name: "全部 ›" }];
}
function renderCatChips() {
  const cats = vodCategories();
  const saved = store.get("otvw:vodLastCat", null);
  if (saved !== null) {
    const i = cats.findIndex(c => c.id === saved);
    if (i >= 0) vod.catSel = i;
  }
  $("#catChips").innerHTML = cats.map((c, i) =>
    `<button class="chip ${i === vod.catSel ? "on" : ""} ${c.id === "" ? "all" : ""}" data-i="${i}">${esc(c.name)}</button>`).join("");
  $$("#catChips .chip").forEach(c => c.addEventListener("click", () => {
    const i = +c.dataset.i, cat = vodCategories()[i];
    if (cat.id === "") { openAll(0); return; }
    vod.catSel = i;
    store.set("otvw:vodLastCat", cat.id);
    renderCatChips();
    renderVodLib(true);
  }));
}

function renderVodLib(refresh = false) {
  /* 首页首屏内容真正上屏的时刻（方案 §7：home_first_content）。 */
  telemetryEmit(TELEMETRY_EVENTS.HOME_FIRST_CONTENT, { kind: "home" });
  const cats = vodCategories();
  const sel = cats[vod.catSel] || { id: "" };
  const host = $("#vodSections");
  if (sel.id === "") {
    /* 「全部」分类 = 目录去重条目 take 18（App gridItems） */
    const allItems = (vod.cat && Array.isArray(vod.cat.dedupedItems)) ? vod.cat.dedupedItems : [];
    const items = allItems.slice(0, 18);
    host.innerHTML = items.length
      ? `<div class="lib-grid" id="libGrid">${items.map(it => libCard(it)).join("")}</div><div style="height:80px"></div>`
      : `<div class="page-loading">暂无内容</div>`;
    registerGrid("#libGrid", state.portrait ? 205 : 232, 20, state.portrait ? 40 : 80);
    bindCards("#vodSections");
    return;
  }
  const cid = sel.id.replace("douban:", "");
  const mySel = vod.catSel;
  const paged = VOD_LIB_PAGED_CATEGORIES.has(cid);
  const columns = state.portrait ? 3 : 7;
  const paint = sections => {
    if (vod.catSel !== mySel) return;   /* 快速切分类防旧响应覆盖 */
    vod.sections = sections;
    /* 影视页门闸回执：当前分类三块已到（空栏也算「到」，否则会一直等到超时） */
    vod.libLoadedFor = sel.id;
    if (!sections.length) { host.innerHTML = `<div class="page-loading">— 加载中 …</div>`; return; }
    let html = "";
    sections.forEach((s, si) => {
      const displayName = displayLibSectionName(s.name);
      const items = Array.isArray(s.items) ? s.items : [];
      if (paged && items.length) {
        const key = `${cid}:${si}:${s.name}`;
        let pageState = VOD_LIB_PAGE_STATE.get(key);
        if (!pageState) pageState = { page: 0 };
        pageState.items = items;
        pageState.columns = columns;
        pageState.pageSize = columns * 2;
        pageState.page = Math.min(pageState.page, Math.max(0, Math.ceil(items.length / pageState.pageSize) - 1));
        VOD_LIB_PAGE_STATE.set(key, pageState);
        html += `<section class="lib-section" data-lib-key="${esc(key)}">
          <div class="row-sec-title" style="padding-top:${si === 0 ? 26 : 44}px">${esc(displayName)}</div>
          <div class="lib-page-grid"></div>
          <div class="lib-pager">
            <button type="button" class="lib-page-btn" data-lib-page-prev aria-label="上一页">上一页</button>
            <span class="lib-page-count" data-lib-page-count aria-live="polite"></span>
            <button type="button" class="lib-page-btn" data-lib-page-next aria-label="下一页">下一页</button>
          </div>
        </section>`;
        return;
      }
      html += `<div class="row-sec-title" style="padding-top:${si === 0 ? 26 : 44}px">${esc(displayName)}</div>
        ${items.length
          ? `<div class="hscroll poster-row">${items.map(it => libCard(it)).join("")}</div>`
          : `<div class="lib-empty">暂无${esc(displayName)}内容</div>`}`;
    });
    host.innerHTML = (html || `<div class="page-loading">暂无内容</div>`) + `<div style="height:80px"></div>`;
    bindCards("#vodSections");
    if (paged) {
      bindVodLibPager(host);
      host.querySelectorAll(".lib-section").forEach(section => bindVodLibPage(section, null));
    }
  };
  host.innerHTML = `<div class="page-loading">— 加载中 …</div>`;
  fetchLibBlocksSWR(cid, vod.homeSections, paint, refresh);
}

function bindVodLibPager(host) {
  host.querySelectorAll(".lib-section").forEach(section => {
    const key = section.dataset.libKey;
    const state = VOD_LIB_PAGE_STATE.get(key);
    if (!state) return;
    section.querySelector("[data-lib-page-prev]").addEventListener("click", () => bindVodLibPage(section, state.page - 1));
    section.querySelector("[data-lib-page-next]").addEventListener("click", () => bindVodLibPage(section, state.page + 1));
  });
  if (host.dataset.libPagerInteractionsBound) return;
  host.dataset.libPagerInteractionsBound = "true";
  host.addEventListener("keydown", event => {
    const card = event.target.closest && event.target.closest(".lib-page-grid .pcard");
    if (!card) return;
    const section = card.closest(".lib-section");
    const state = section && VOD_LIB_PAGE_STATE.get(section.dataset.libKey);
    const grid = card.closest(".lib-page-grid");
    if (!state || !grid) return;
    const cards = Array.from(grid.querySelectorAll(".pcard"));
    const index = cards.indexOf(card);
    if (index < 0) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      card.click();
      return;
    }
    let target = -1;
    if (event.key === "ArrowLeft") {
      if (index % state.columns > 0) target = index - 1;
      else if (state.page > 0) {
        event.preventDefault();
        const previousStart = (state.page - 1) * state.pageSize;
        const previousLength = Math.min(state.pageSize, state.items.length - previousStart);
        const row = Math.floor(index / state.columns);
        bindVodLibPage(section, state.page - 1, Math.min(row * state.columns + state.columns - 1, previousLength - 1));
        return;
      }
    } else if (event.key === "ArrowRight") {
      if (index % state.columns < state.columns - 1 && index + 1 < cards.length) target = index + 1;
      else if (state.page + 1 < Math.ceil(state.items.length / state.pageSize)) {
        event.preventDefault();
        const row = Math.floor(index / state.columns);
        bindVodLibPage(section, state.page + 1, row * state.columns);
        return;
      }
    } else if (event.key === "ArrowUp" && index >= state.columns) target = index - state.columns;
    else if (event.key === "ArrowDown" && index + state.columns < cards.length) target = index + state.columns;
    else if (event.key === "PageUp" && state.page > 0) {
      event.preventDefault();
      bindVodLibPage(section, state.page - 1, Number.MAX_SAFE_INTEGER);
      return;
    } else if (event.key === "PageDown" && state.page + 1 < Math.ceil(state.items.length / state.pageSize)) {
      event.preventDefault();
      bindVodLibPage(section, state.page + 1, 0);
      return;
    }
    if (target >= 0) {
      event.preventDefault();
      cards[target].focus();
    }
  });
  let swipeStart = null;
  host.addEventListener("touchstart", event => {
    const grid = event.target.closest && event.target.closest(".lib-page-grid");
    if (!grid || event.touches.length !== 1) { swipeStart = null; return; }
    const touch = event.touches[0];
    swipeStart = { grid, x: touch.clientX, y: touch.clientY };
  }, { passive: true });
  host.addEventListener("touchend", event => {
    if (!swipeStart) return;
    const start = swipeStart;
    swipeStart = null;
    const touch = event.changedTouches[0];
    if (!touch) return;
    const dx = touch.clientX - start.x, dy = touch.clientY - start.y;
    if (Math.abs(dx) < 48 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
    const section = start.grid.closest(".lib-section");
    const state = section && VOD_LIB_PAGE_STATE.get(section.dataset.libKey);
    if (!state) return;
    const nextPage = state.page + (dx < 0 ? 1 : -1);
    if (nextPage < 0 || nextPage >= Math.ceil(state.items.length / state.pageSize)) return;
    bindVodLibPage(section, nextPage);
  }, { passive: true });
}

function bindVodLibPage(section, page, focusIndex) {
  const state = VOD_LIB_PAGE_STATE.get(section.dataset.libKey);
  if (!state) return;
  const pages = Math.max(1, Math.ceil(state.items.length / state.pageSize));
  if (page !== null && page !== undefined) state.page = Math.max(0, Math.min(page, pages - 1));
  const start = state.page * state.pageSize;
  const items = state.items.slice(start, start + state.pageSize);
  const grid = section.querySelector(".lib-page-grid");
  grid.innerHTML = items.map(it => libCard(it, state.columns === 3 ? 21 : 24)).join("");
  const cards = Array.from(grid.querySelectorAll(".pcard"));
  cards.forEach(card => { card.tabIndex = 0; card.setAttribute("role", "button"); });
  bindCardElements(cards);
  section.querySelector("[data-lib-page-count]").textContent = `${state.page + 1} / ${pages}`;
  section.querySelector("[data-lib-page-prev]").disabled = state.page <= 0;
  section.querySelector("[data-lib-page-next]").disabled = state.page >= pages - 1;
  if (focusIndex !== null && focusIndex !== undefined && cards.length) {
    const index = focusIndex === Number.MAX_SAFE_INTEGER ? cards.length - 1 : Math.min(focusIndex, cards.length - 1);
    cards[index].focus();
  }
}

let vodLibPortrait = state.portrait;
addEventListener("resize", () => {
  if (vodLibPortrait === state.portrait) return;
  vodLibPortrait = state.portrait;
  setTimeout(() => {
    if (state.tab !== "vod" || !vod.sections) return;
    const selected = vodCategories()[vod.catSel];
    if (selected && VOD_LIB_PAGED_CATEGORIES.has(selected.id.replace("douban:", ""))) renderVodLib();
  }, 80);
});

/* 海报竖卡（232×352 r12 + remark 角标）。
 * 2026-09-17 用户要求：卡片下方不再有副行（年份/豆瓣评分），只留海报 + 片名；
 * 全部页 VcardCard 的评分由 allCard 单独渲染，不走这里。 */
function posterCard(it, titleSize) {
  return `<div class="pcard" data-vid="${esc(it.id)}">
    <div class="pc-img"><img loading="lazy" decoding="async" src="${esc(img(it.cover))}" alt="">${it.remark ? `<span class="pc-tag">${esc(it.remark)}</span>` : ""}</div>
    <div class="pc-title" style="font-size:${titleSize || 24}px">${esc(it.title)}</div>
  </div>`;
}
function libCard(it, titleSize) { return posterCard(it, titleSize); }
function bindCards(sel) {
  bindCardElements($$(sel + " .pcard"));
}
function bindCardElements(cards) {
  cards.forEach(c => c.addEventListener("click", () => {
    const t = c.querySelector(".pc-title");
    const im = c.querySelector(".pc-img img");
    openDetail(c.dataset.vid, { title: t ? t.textContent.trim() : "", cover: im ? im.src : "" });
  }));
}

/* 最近观看（historyFlow(12)；点卡直进播放器，App 影视#10） */
function renderHistRow() {
  const hist = getHistory().slice(0, 12);
  if (!hist.length) {
    $("#histRow").innerHTML = `<div class="row-sec-title home-sec">最近观看</div>
      <div class="hist-empty">最近还没有观看记录</div>`;
    return;
  }
  $("#histRow").innerHTML = `<div class="row-sec-title home-sec">最近观看</div>
    <div class="hscroll poster-row">${hist.map(h => histCard(h, "home")).join("")}</div>`;
  $$("#histRow .pcard").forEach(c => c.addEventListener("click", () => {
    const h = hist.find(x => String(x.id) === c.dataset.vid);
    if (h) playFromHistory(h);
  }));
}
/* App HistoryCard / HistoryVodCard：badge「上次看到 第N集 时间」右下角灰底 + 底部细进度条 */
function histCard(h, kind) {
  const p = getProgress("vod:" + h.id);
  const pctv = p && p.dur ? pct(p.pos / p.dur * 100) : 0;
  const badgeCls = kind === "home" ? "pc-hist-tag" : "pc-hist-tag fav";
  return `<div class="pcard" data-vid="${esc(h.id)}">
    <div class="pc-img"><img loading="lazy" decoding="async" src="${esc(img(h.cover))}" alt="">
      <div class="${badgeCls}">上次看到 第${(h.epIndex || 0) + 1}集 ${p ? fmtTime(p.pos) : ""}</div>
      <div class="pc-prog"><i style="width:${pctv}%"></i></div></div>
    <div class="pc-title" style="font-size:${kind === "home" ? 24 : 21}px">${esc(h.title)}</div>
  </div>`;
}

function renderVodHero() {
  const heroes = buildHeroes({ carousel: vod.car, sections: vod.homeSections }, vod.cat);
  vod.heroes = heroes;
  const c = heroes[vod.heroIdx || 0];
  if (!c) {
    clearHeroSwap($("#vodHero"));
    $("#vodDots").innerHTML = "";
    return;
  }
  heroSwap($("#vodHero"), img(c.bg));
  refade($("#vodHero").querySelector(".vod-hero-content"));
  $("#vodHeroTitle").textContent = c.title;
  /* App：meta 行先显 tag（单个），brief 到达后覆盖为 meta join " / "；评分拼行尾 */
  $("#vodHeroMeta").innerHTML = `${esc(c.tag || "")}${c.tag ? "" : ""}`;
  $("#vodHeroDesc").textContent = "";
  vodBrief(c.id).then(b => {
    if (!b) { $("#vodHeroDesc").textContent = c.title + " · My TV 精选内容，一键播放。"; return; }
    if (vod.heroes[vod.heroIdx || 0] && vod.heroes[vod.heroIdx || 0].id !== c.id) return;
    /* 2026-09-19：段数上限 6——豆瓣 regions+genres 最多可到 10 段
       （奥德赛：2026/美国/加拿大/冰岛/新西兰/摩洛哥/英国/动作/历史/奇幻），
       760 宽单行放不下会被省略号截断、并把评分挤出列外；与 App 同口径（take(6)） */
    const metaParts = b.meta ? b.meta.split("/").map(x => x.trim()).filter(Boolean) : [];
    const meta = metaParts.length ? metaParts.slice(0, 6).join(" / ") : (c.tag || "");
    $("#vodHeroMeta").innerHTML = `${esc(meta)}${b.rating > 0 ? `<span class="hm-score">★ ${b.rating.toFixed(1)}</span>` : ""}`;
    $("#vodHeroDesc").textContent = b.desc || c.title + " · My TV 精选内容，一键播放。";
  });
  $("#vodDots").innerHTML = heroes.map((_, i) => `<i class="${i === (vod.heroIdx || 0) ? "on" : ""}"></i>`).join("");
  $$("#vodDots i").forEach((d, i) => d.addEventListener("click", () => { vod.heroIdx = i; renderVodHero(); }));
  /* App 立即播放 → 详情页（不自动起播） */
  $("#vodHeroPlay").onclick = () => openDetail(c.id);
  /* 看过就换文案（用户 2026-09-17 要求）：有进度或历史记录 → 「继续播放」 */
  const seen = !!getProgress("vod:" + c.id) || getHistory().some(h => h.id === c.id);
  $("#vodHeroPlay").textContent = seen ? "继续播放" : "立即播放";
}

/* 历史卡直进播放（App 路由 player/{vodId}/{epIndex}：默认线路 0 + 该集续播） */
async function playFromHistory(h) {
  try {
    const d = await fetchJSON(BASE + vodDetailPath(h.id), { timeout: 60000 });
    if (!d.title) throw new Error(d.error || "未找到该影片");
    const src = (d.sources || []).map(line => ({ ...line, episodes: (line.episodes || []).map((ep, index) => ({
      ...ep, ep: ep.ep || ep.name || `第${index + 1}集`, pid: ep.pid != null ? ep.pid : line.id,
      vid: ep.vid != null ? ep.vid : index,
    })) }))[0];
    const eps = src ? src.episodes : [];
    if (!eps.length) { hint("该影片暂无可播放线路"); return; }
    let epIdx = h.epIndex || 0;
    if (epIdx >= eps.length) epIdx = 0;
    const ep = eps[epIdx];
    const prog = getProgress("vod:" + h.id);
    const epSaved = store.get("otvw:ep:" + h.id, -1);
    const resume = epSaved === epIdx && resumable(prog) ? prog.pos : 0;
    openPlayer({
      kind: "vod", vid: h.id, title: d.title, cover: d.cover || h.cover || "",
      epIdx, epName: ep.ep || `第${epIdx + 1}集`, epCount: eps.length,
      lineName: src.name, playRef: { pid: ep.pid, vid: ep.vid }, resume,
      lineIndex: 0, sources: d.sources || [],
    });
  } catch (e) { hint("打开播放失败：" + e.message); }
}

/* ================================================================
 * 全部影视（AllScreen 复刻）
 * ================================================================ */
const all = {
  cat: "", page: 1, hasMore: false, loading: false, filters: null,
  sel: { type: "", area: "", year: "", rating: "" }, by: "hot",
  pageRequest: null, filterRequest: null, filterSeq: 0,
  /* 筛选面板收起态（2026-09-18 需求）：记忆上次选择，默认展开 */
  filterOpen: store.get("otvw:filterOpen", true) !== false,
};
const ALL_CATS = [["movie", "电影"], ["tv", "电视剧"], ["anime", "国漫"], ["variety", "综艺"], ["short", "短剧"]];

async function openAll(cid) {
  /* App：无路由参数——进页恢复上次类别（allLastCat，默认「全部」） */
  all.pageRequest?.abort(); all.filterRequest?.abort();
  all.cat = cid ? String(cid) : ""; all.page = 1; all.hasMore = false; all.loading = false;
  all.sel = { type: "", area: "", year: "", rating: "" };
  $("#pAll").hidden = false; document.body.style.overflow = "hidden";
  $("#allGrid").innerHTML = ""; $("#allMore").hidden = true;
  const saved = store.get("otvw:allLastCat", null);
  if (saved !== null && ALL_CATS.some(([c]) => c === saved)) all.cat = saved;
  /* App AllScreen：无独立类别行——类别是筛选面板第一行 */
  if (!all.filters) all.filters = {};
  loadAllFilters();
  loadAllPage(true);
}
/* v1.20 修复：filters 拉取失败/空自动重试 ×4（间隔 2.5s），按所选类别拉取
   v1.25 需求①提速：filters 持久缓存（localStorage 24h）——有缓存秒渲染，
   仅缓存缺失或 >6h 才回源刷新（筛选选项几乎不变，无需频繁拉取） */
async function loadAllFilters() {
  const catId = all.cat || "movie";
  all.filterRequest?.abort();
  const controller = new AbortController();
  all.filterRequest = controller;
  const seq = ++all.filterSeq;
  const current = () => all.filterSeq === seq && all.filterRequest === controller && !controller.signal.aborted;
  const renderCurrent = () => { if (current()) renderAllFilters(); };
  const ck = "otvw:filters:" + catId;
  const cached = store.get(ck, null);
  if (cached && [(cached.types || []), (cached.areas || []), (cached.langs || []), (cached.years || [])].some(v => v.length)) {
    all.filters[catId] = cached;
    renderCurrent();
  }
  if (cached && Date.now() - (cached._ts || 0) < 6 * 3600000) {
    if (all.filterRequest === controller) all.filterRequest = null;
    return;
  }
  let attempt = 0;
  while (attempt < 4 && current()) {
    try {
      const f = await fetchJSON(BASE + "/vod/filters/" + catId, { timeout: 45000, signal: controller.signal });
      const supported = (f.types || []).length || (f.areas || []).length ||
        (f.langs || []).length || (f.years || []).length;
      if (f && (f.types || f.areas || f.langs || f.years) && !supported) {
        /* MacCMS 采集模式明确不提供组合筛选：空表是合法响应，不要按失败
         * 重试四轮，把“全部”页面白白拖慢十秒。renderAllFilters 会隐藏空行。 */
        all.filters[catId] = f;
        renderCurrent();
        break;
      }
      /* 有些分类只提供地区、语言或年份。旧逻辑只认 types，导致有效响应仍重试 4 轮。 */
      if (supported) {
        f._ts = Date.now();
        all.filters[catId] = f;
        try { store.set(ck, f); } catch (e) {}
        renderCurrent();
        break;
      }
    } catch (e) {
      if (!current()) return;
      all.filters[catId] = { types: [], areas: [], langs: [], years: [] };
    }
    attempt++;
    if (attempt < 4 && current()) await new Promise(r => setTimeout(r, 2500));
  }
  if (current() && !all.filters[catId]) renderAllFilters();
  if (all.filterRequest === controller) all.filterRequest = null;
}
function renderAllFilters() {
  const f = all.filters[all.cat || "movie"] || { types: [], areas: [], years: [], ratings: [] };
  const rows = [
    ["cat", "类别", ALL_CATS.map(([c, n]) => [c, n])],
    ["type", "类型", (f.types || []).map(v => [v, v])],
    ["area", "地区", (f.areas || []).map(v => [v, v])],
    /* 服务端 years 已含「更早」，这里补兜底后再去重，避免出现两个「更早」chip */
    ["year", "年份", Array.from(new Set((f.years || []).concat(["更早"]))).map(v => [v, v])],
    ["rating", "评分", (f.ratings || ["全部", "9+", "8+", "7+", "暂无评分"]).map(v => [v, v])],
    ["by", "排序", VOD_SORTS],
  ].filter(([, , values]) => values.length > 0);
  const panel = $("#filterPanel");
  /* 收起/展开开关（2026-09-18 用户要求「筛选组件要能够收起」）：放表头右侧——
     面板顶边已与左上角悬浮返回钮同顶，开关靠右就不会和返回钮抢地方。 */
  const open = all.filterOpen !== false;
  panel.className = "filter-panel" + (open ? "" : " collapsed");
  const activeCount = ["type", "area", "year", "rating"].filter(k => all.sel[k]).length +
    (all.by && all.by !== "hot" ? 1 : 0) + (all.cat && all.cat !== "movie" ? 1 : 0);
  panel.innerHTML =
    `<div class="fp-head"><button class="fp-toggle${activeCount ? " has-filter" : ""}" id="filterToggle">` +
    `筛选${activeCount ? "（已选 " + activeCount + "）" : ""}<span class="fp-caret">⌄</span></button></div>` +
    `<div class="fp-rows">` + rows.map(([k, label, arr]) =>
    `<div class="fp-row"><span class="fp-label">${label}</span>
      ${arr.map(([v, n]) => {
        const cur = k === "cat" ? all.cat : k === "by" ? all.by : all.sel[k];
        const on = String(cur) === String(v) || (k === "year" && v === "更早" && all.sel.year === "更早");
        return `<button class="fchip ${on ? "on" : ""}" data-k="${k}" data-v="${esc(v)}">${esc(n)}</button>`;
      }).join("")}
    </div>`).join("") + `</div>`;
  $("#filterToggle").addEventListener("click", () => {
    all.filterOpen = !(all.filterOpen !== false);
    try { store.set("otvw:filterOpen", all.filterOpen); } catch (e) {}
    renderAllFilters();
  });
  $$("#filterPanel .fchip").forEach(c => c.addEventListener("click", () => {
    const k = c.dataset.k, v = c.dataset.v;
    if (k === "cat") {
      if (all.cat === v) return;
      all.cat = v; all.page = 1; all.hasMore = false; all.loading = false;
      all.sel = { type: "", area: "", year: "", rating: "" };
      store.set("otvw:allLastCat", v);
      $("#allGrid").innerHTML = "";
      loadAllFilters(); loadAllPage(true);
      return;
    }
    if (k === "by") { all.by = v; }
    else { all.sel[k] = v; }
    all.page = 1; all.hasMore = false; $("#allGrid").innerHTML = "";
    renderAllFilters(); loadAllPage(true);
  }));
}
/* App showPage：请求序号防竞态 + distinctBy id + 触底自动加载（距底 4 项）
   v1.25 需求①提速：第一页 sessionStorage SWR（5min）——缓存先上屏再后台刷新 */
let allSeq = 0;
function allCacheKey() {
  return "otvw:show:" + [all.cat, all.by, all.sel.type, all.sel.area, all.sel.year, all.sel.rating].join("|");
}
async function loadAllPage(reset) {
  /* 重置（切分类/筛选/排序）优先取消旧页；翻页仍避免并发重复请求。 */
  if (reset) all.pageRequest?.abort();
  else if (all.loading) return;
  if (!reset && (!all.hasMore || all.page <= 1)) return;
  const seq = ++allSeq;
  const reqPage = all.page;
  const controller = new AbortController();
  all.pageRequest = controller;
  const current = () => allSeq === seq && all.pageRequest === controller && !controller.signal.aborted;
  all.loading = true;
  $("#allMore").hidden = false; $("#allMore").textContent = "加载中…";
  const paint = (items, hasMore, replace) => {
    if (!current()) return;
    if (replace) $("#allGrid").innerHTML = "";
    const exist = new Set($$("#allGrid .pcard").map(c => c.dataset.vid));
    const fresh = items.filter(it => !exist.has(String(it.id)));
    $("#allGrid").insertAdjacentHTML("beforeend", fresh.map(it => allCard(it)).join(""));
    bindCards("#allGrid");
    registerGrid("#allGrid", state.portrait ? 205 : 232, 20, state.portrait ? 40 : 60);
    all.hasMore = hasMore; all.page = reqPage + 1;
    $("#allMore").textContent = "加载更多…";
    $("#allMore").hidden = !all.hasMore;
  };
  try {
    /* 需求①提速：reset 时先渲染 sessionStorage 里的首页缓存（≤5min），后台刷新覆盖 */
    if (reset) {
      let hit = null;
      try { const v = sessionStorage.getItem(allCacheKey()); hit = v ? JSON.parse(v) : null; } catch (e) {}
      if (hit && Date.now() - (hit.ts || 0) < 300000 && hit.items && hit.items.length) {
        paint(hit.items, !!hit.hasMore, true);
      }
    }
    /* 「更早」必须原样传给后端（后端翻成 year_range=1,<今年-12>）：旧实现把它发成空串，
       等于用户选了「更早」却什么都没筛。 */
    const q = new URLSearchParams({ page: reqPage, sort: all.by, genre: all.sel.type, region: all.sel.area, rating: all.sel.rating, year: all.sel.year });
    const category = all.cat || "movie";
    const d = await fetchJSON(BASE + `/vod/show/${category}?` + q,
      { timeout: 60000, signal: controller.signal });
    if (!current()) return;
    const items = (d.items || []).map(it => normalizeDoubanItem(it, category));
    paint(items, !!d.has_more, reset);   /* reset=整页覆盖画（缓存占位被顶掉）；翻页=追加 */
    /* 上游降级（豆瓣被拦/超时）：有内容就说「可能不是最新」，没内容就说「暂时获取不到」——
       绝不把上游故障显示成「没有符合条件的内容」。 */
    if (d.upstream_available === false) {
      $("#allMore").hidden = false;
      $("#allMore").textContent = items.length
        ? "数据可能不是最新，正在重试…"
        : "暂时获取不到片库，请稍后重试";
      return;
    }
    if (reset && items.length) {
      try { sessionStorage.setItem(allCacheKey(), JSON.stringify({ ts: Date.now(), items, hasMore: !!d.has_more })); } catch (e) {}
    }
  } catch (e) {
    if (current()) { $("#allMore").textContent = "加载更多…"; $("#allMore").hidden = !all.hasMore; }
  } finally {
    if (all.pageRequest === controller) {
      all.pageRequest = null;
      all.loading = false;
    }
  }
}
/* 触底自动加载 */
let allScrollFrame = 0;
addEventListener("scroll", () => {
  if (allScrollFrame || $("#pAll").hidden) return;
  allScrollFrame = requestAnimationFrame(() => {
    allScrollFrame = 0;
    if ($("#pAll").hidden) return;
    if (scrollY + innerHeight >= document.documentElement.scrollHeight - 4 * (352 + 30) * state.zoom) loadAllPage(false);
  });
}, { passive: true });
$("#allMore").addEventListener("click", () => loadAllPage(false));

/* 全部页卡片（App VcardCard：副行只 ★ x.x 19px，无 year） */
function allCard(it) {
  const sub = (+it.score > 0) ? `<div class="pc-sub" style="font-size:19px">★ ${Number(it.score).toFixed(1)}</div>` : "";
  return `<div class="pcard" data-vid="${esc(it.id)}">
    <div class="pc-img"><img loading="lazy" decoding="async" src="${esc(img(it.cover))}" alt="">${it.remark ? `<span class="pc-tag">${esc(it.remark)}</span>` : ""}</div>
    <div class="pc-title">${esc(it.title)}</div>
    ${sub}
  </div>`;
}

/* ================================================================
 * 详情（DetailScreen 复刻）
 * ================================================================ */
let detail = null, dLineIdx = 0, dEpIdx = 0, dEpPage = 0, dResumeEp = -1, dResumePos = 0;
let detailRequest = null, detailSeq = 0;
let resolverState = { status: "IDLE", mytvId: null, preferred: null, backups: [] };
/* resolver 竞速的代次：每次 openDetail 自增，迟到响应据此自我作废。 */
let resolverSeq = 0;

/* ================================================================
 * Source Resolver 客户端（2026-09-28 优化方案 §5 / §6.3）
 *
 * 自建 VPS 实测：出口到全部影视源不可达，`/vod/resolve` 常态返回
 *   {status:"TEMPORARILY_UNAVAILABLE", preferred:null, backups:[]}
 * 所以 resolver 在网页版里是**加速器，不是闸门**：
 *   - 只有 status=MATCHED 且带 preferred 时才用它挑线路；
 *   - 任何其它状态（含超时、400/404、老后端没有该路由）都继续用
 *     /vod/detail 已经匹配出来的全部线路，UI 绝不把网络抖动说成没有这部片。
 *
 * 另一个现实约束：网页的目录/详情全是 douban:<id> 键，从不下发 mytv_id，
 * 而 /vod/resolve 只接受正整数 mytv_id。所以 canonicalMyTvId() 只在
 * 确实拿到规范 id 时才发请求，拿不到就安静跳过——绝不拿 douban id 去猜。
 * ================================================================ */
const RESOLVER_STATUS = Object.freeze({
  IDLE: "IDLE",
  RESOLVING: "RESOLVING",
  FAST_PATH_FAILED: "FAST_PATH_FAILED",
  FALLBACK_SEARCHING: "FALLBACK_SEARCHING",
  STALE_AVAILABLE: "STALE_AVAILABLE",
  TEMPORARILY_UNAVAILABLE: "TEMPORARILY_UNAVAILABLE",
  MATCHED: "MATCHED",
  UNSUPPORTED: "UNSUPPORTED",
});
const RESOLVER_TIMEOUT_MS = 6000;

function canonicalMyTvId(vid) {
  /* 网页侧只拿到 douban:<id>；没有显式 mytv_id 字段时返回 null，
     宁可跳过 resolver，也不把 douban id 当成 mytv_id 发出去。 */
  const raw = vid == null ? "" : String(vid).trim();
  const m = raw.match(/^mytv:(\d+)$/) || raw.match(/^(\d+)$/);
  if (!m || !m[1]) return null;
  const n = Number(m[1]);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

function readResolverDecision(payload) {
  /* 把后端契约折成前端内部状态。preferred 为 null 时**永远**回落到
     既有匹配结果，不清空、不报错。 */
  const p = payload && typeof payload === "object" ? payload : {};
  const status = typeof p.status === "string" ? p.status : RESOLVER_STATUS.TEMPORARILY_UNAVAILABLE;
  const backups = Array.isArray(p.backups) ? p.backups.filter(x => x && x.source_id) : [];
  const preferred = p.preferred && p.preferred.source_id ? p.preferred : null;
  return {
    status: status,
    preferred: preferred,
    backups: backups,
    resolveVersion: p.resolve_version == null ? "" : String(p.resolve_version),
  };
}

async function fetchResolverDecision(mytvId, episode) {
  const id = canonicalMyTvId(mytvId);
  if (!id) return readResolverDecision({ status: RESOLVER_STATUS.UNSUPPORTED });
  const ep = Number.isSafeInteger(episode) && episode > 0 ? episode : 1;
  const path = "/vod/resolve/" + encodeURIComponent(String(id)) + "?episode=" + ep;
  const raw = await fetchJSON(BASE + path, { timeout: RESOLVER_TIMEOUT_MS });
  return readResolverDecision(raw);
}

/* 按 resolver 候选在既有 sources 里找同源同集；找不到就返回 null——
   绝不凭空造一条线路出来（方案 §6.3：不伪造错误影片）。 */
function findCandidateLine(sources, candidate, episodeIndex) {
  if (!candidate || !candidate.source_id) return null;
  const list = Array.isArray(sources) ? sources : [];
  /* 线路对上：先按 source_id，再退到源名——自建后台的 source_id 是数据库 UUID，
     云端下发的是 hongniu 这类 slug，同名不同 id（vod_api._adopt_external_lines 同理）。 */
  const bySource = list.findIndex(line => line
    && (line.source_id === candidate.source_id || line.id === candidate.source_id
        || line.name === candidate.source_name));
  if (bySource < 0) return null;
  const line = list[bySource];
  const episodes = line.episodes || [];
  /* content_id 对得上就优先用它定位集数；同源同名但内容不同（重名片）时避免串集。 */
  const byContent = candidate.content_id
    ? episodes.findIndex(ep => ep && String(ep.content_id || ep.pid) === String(candidate.content_id))
    : -1;
  /* 集数优先级：content_id 命中 > 调用方指定的集 > 候选自带的 episode_index。
     episodeIndex 传 0/undefined 表示「调用方没有指定」，此时用候选自带的。 */
  const fromCaller = Number.isSafeInteger(episodeIndex) && episodeIndex > 0
    ? episodeIndex - 1 : -1;
  const fromCandidate = Number(candidate.episode_index || 1) - 1;
  const want = fromCaller >= 0 ? fromCaller : Math.max(0, fromCandidate);
  /* 实际命中的集下标：content_id 优先，其次调用方要的集，最后候选自带的集。 */
  const epIdx = byContent >= 0 ? byContent : Math.max(0, Math.min(want, episodes.length - 1));
  const ep = episodes[epIdx];
  if (!ep) return null;
  return { lineIdx: bySource, epIdx: epIdx, line: line, ep: ep };
}

/* resolver 只做「挑线路 / 提前预热」；它拿不到就原样保留既有匹配结果。 */
function applyResolverDecision(decision, d, episode) {
  if (!decision || !d) return null;
  /* keep：无论什么状态都保留 d.sources，这是「不误报没有片源」的底线 */
  const keep = Array.isArray(d.sources) ? d.sources : [];
  /* episode 传 0/undefined = 调用方没有指定，此时用候选自带的 episode_index。 */
  const ep = Number.isSafeInteger(episode) && episode > 0 ? episode : 0;
  if (decision.status !== RESOLVER_STATUS.MATCHED || !decision.preferred) {
    if (decision.status === RESOLVER_STATUS.STALE_AVAILABLE
        && decision.preferred) {
      /* 陈旧索引仍可用：先试历史线路，同时后台刷新。 */
      const hit = findCandidateLine(keep, decision.preferred, ep);
      if (hit) return hit;
    }
    return null;
  }
  return findCandidateLine(keep, decision.preferred, ep);
}

/* 详情打开时**并行**发起 resolver 与详情请求；失败/超时都不影响详情。 */
function startResolverRace(vid, episode) {
  metricMark("resolve");
  telemetryEmit(TELEMETRY_EVENTS.RESOLVE_START, { kind: "resolve" });
  /* 认领本次竞速：后续响应必须同时匹配 seq 与 vid，否则上一部片的
     迟到响应会写坏当前片的 resolverState，并拿当前片的 detail 去预热。 */
  const seq = ++resolverSeq;
  const mytvId = canonicalMyTvId(vid);
  resolverState = { status: RESOLVER_STATUS.RESOLVING, mytvId: mytvId,
    preferred: null, backups: [] };
  const mine = () => seq === resolverSeq && (!detail || detail.vid === vid);
  if (!mytvId) {
    /* 目录没下发 mytv_id：安静跳过，走既有 /vod/detail 全量匹配。 */
    resolverState.status = RESOLVER_STATUS.UNSUPPORTED;
    metricCloseResolve(RESOLVER_STATUS.UNSUPPORTED);
    return Promise.resolve(readResolverDecision({ status: RESOLVER_STATUS.UNSUPPORTED }));
  }
  return fetchResolverDecision(mytvId, episode)
    .then(decision => {
      /* 用户已经翻到别的片：本次结果作废，但不影响任何 UI（本来就没人等它）。 */
      if (!mine()) return decision;
      resolverState = {
        status: decision.status, mytvId: mytvId,
        preferred: decision.preferred, backups: decision.backups,
      };
      metricCloseResolve(decision.status);
      const hit = applyResolverDecision(decision, detail, dResumeEp + 1 || 1);
      if (hit) {
        telemetryEmit(TELEMETRY_EVENTS.RESOLVE_FIRST_SOURCE, {
          source_id: String(hit.line.id || hit.line.name || "").slice(0, 64),
        });
        /* 命中即预热该线路该集，点了就能起。 */
        if (hit.line.id != null && detail) prefetchPlay(detail.vid, hit.ep.pid, hit.ep.vid);
      }
      return decision;
    })
    .catch(e => {
      /* 同样只认自己那次竞速，避免上一部片的失败覆盖当前片的状态。 */
      if (!mine()) return readResolverDecision({ status: RESOLVER_STATUS.TEMPORARILY_UNAVAILABLE });
      resolverState = { status: RESOLVER_STATUS.TEMPORARILY_UNAVAILABLE, mytvId: mytvId,
        preferred: null, backups: [] };
      metricCloseResolve(RESOLVER_STATUS.TEMPORARILY_UNAVAILABLE);
      return readResolverDecision({ status: RESOLVER_STATUS.TEMPORARILY_UNAVAILABLE });
    });
}

/* 快速起播：已有 resolver 候选就直接用，不再等所有线路。 */
async function resolveFirstSource(ctx, options = {}) {
  /* 用户在详情页/「更多」面板手动选过线路（dLineIdx 变了、或续播集落在
     非第 1 集）时，playRef 才是他的明确选择——这时 resolver 只能预热，
     不能把播放目标换成 preferred 那条。只有「默认线路 + 默认集」才让
     resolver 决定起播线路，否则「当前播放 线路2/5」会悄悄播成另一条。 */
  const lines = pv.ctx === ctx ? pv.vodSources
    : (detail && String(detail.vid) === String(ctx.vid) ? detail.sources : ctx.sources || []);
  const userPicked = ctx.linePinned === true;
  const hit = !userPicked && resolverState.preferred
    ? findCandidateLine(lines, resolverState.preferred, ctx.epIdx + 1)
    : null;
  if (hit) {
    if (hit.epIdx !== ctx.epIdx) ctx.resume = 0;
    ctx.epIdx = hit.epIdx;
    ctx.epCount = (hit.line.episodes || []).length;
    ctx.epName = hit.ep.ep || hit.ep.name || `第${hit.epIdx + 1}集`;
    ctx.playRef = { pid: hit.ep.pid, vid: hit.ep.vid };
    ctx.lineIndex = hit.lineIdx;
    ctx.sourceKey = playerVodSourceKey(lines[hit.lineIdx], hit.lineIdx);
    if (pv.ctx === ctx) {
      pv.vodLineIdx = hit.lineIdx;
      if (detail && String(detail.vid) === String(ctx.vid)) {
        dLineIdx = hit.lineIdx; dEpIdx = hit.epIdx;
      }
      renderMorePanel();
    }
    return resolvePlayCached(ctx.vid, hit.ep.pid, hit.ep.vid, {
      force: !!options.force, signal: pv.playController && pv.playController.signal,
    });
  }
  const lineIndex = playerVodLineIndex(lines, ctx.playRef, ctx.sourceKey, ctx.lineIndex);
  const line = lines[lineIndex];
  if (line) {
    ctx.lineIndex = lineIndex;
    ctx.sourceKey = playerVodSourceKey(line, lineIndex);
    if (pv.ctx === ctx) { pv.vodLineIdx = lineIndex; renderMorePanel(); }
  }
  return resolvePlayCached(ctx.vid, ctx.playRef.pid, ctx.playRef.vid, {
    force: !!options.force, signal: pv.playController && pv.playController.signal,
  });
}

function retryDetail() {
  if (detail) openDetail(detail.vid);
}

async function openDetail(vid, cardHint, autoplay) {
  detailRequest?.abort();
  const controller = new AbortController();
  detailRequest = controller;
  const seq = ++detailSeq;
  const current = () => detailSeq === seq && !controller.signal.aborted;
  metricMark("detail");
  telemetryEmit(TELEMETRY_EVENTS.DETAIL_OPEN, { kind: "detail" });
  /* 与详情请求**并行**起跑，不新增串行等待（方案 §5：resolver 是加速器）。
     刻意不 await：/vod/resolve 在自建 VPS 上常态不可达，一旦在这里等它，
     线路早就到手的详情页反而要再陪它耗满 RESOLVER_TIMEOUT_MS。它到货后
     自己预热对应线路即可（见 startResolverRace），到得晚也不影响起播。 */
  startResolverRace(vid, 1).catch(() => {});
  $("#pDetail").hidden = false; document.body.style.overflow = "hidden";
  $("#dTitle").textContent = "加载中…";
  ["dMeta", "dScore", "dActors", "dDesc", "dResume", "dButtons", "dLines", "dLinesTitle"].forEach(id => $("#" + id).textContent = "");
  $("#dEpsWrap").hidden = true; $("#dPoster").removeAttribute("src");
  $("#dDouban").hidden = true;
  /* 需求①提速（v1.25）：点卡先上屏标题+封面（cardHint 来自卡片 DOM，零等待） */
  if (cardHint && cardHint.title) {
    $("#dTitle").textContent = cardHint.title;
    /* 走 img()：asset.doubanio.com 这类豆瓣图床需要中继补 Referer，直连必然空图 */
    if (cardHint.cover) $("#dPoster").src = img(cardHint.cover);
  }
  const dkey = "otvw:dt:" + vid;
  /* partial=true：只有元数据（第一段），线路还在匹配——此时不能拿「没有 sources」
     当成「选集越界」把续播状态清掉，也不能预解析起播集（无线路可解析）。 */
  const applyDetail = (d, partial) => {
    if (!current()) return;
    detail = d; detail.vid = vid; dLineIdx = 0; dEpIdx = 0; dEpPage = 0;
    /* App DetailViewModel：跨线路最近观看（updatedAt 最新）；pos>1s */
    dResumeEp = -1; dResumePos = 0;
    const prog = getProgress("vod:" + vid);
    const epSaved = store.get("otvw:ep:" + vid, -1);
    if (prog && epSaved >= 0 && resumable(prog)) { dResumeEp = epSaved; dResumePos = prog.pos; }
    const epCount = detail.sources[dLineIdx] ? detail.sources[dLineIdx].episodes.length : 0;
    if (epCount && dResumeEp >= epCount) dResumeEp = -1;
    dEpPage = dResumeEp >= 0 ? Math.floor(dResumeEp / 20) : 0;
    renderDetail();
    if (partial) return;
    /* v1.32 起播提速：详情上屏即后台预解析「将播放的那一集」（续播集或第 1 集，
     * 排名第 1 线路）——点播放时解析已在途/已完成，起播只剩中继握手 */
    try {
      const s0 = detail.sources[0];
      if (s0 && s0.episodes.length) {
        const idx = dResumeEp >= 0 ? Math.min(dResumeEp, s0.episodes.length - 1) : 0;
        prefetchPlay(vid, s0.episodes[idx].pid, s0.episodes[idx].vid);
      }
    } catch (e) {}
  };
  /* SWR：≤10min 的本地缓存直接用（详情页秒开）；10min~2h 先上屏再后台刷新 */
  let cached = null;
  try { const v = store.get(dkey, null); cached = v && v._d ? v : null; } catch (e) {}
  if (cached) {
    /* 缓存里可能残留上一轮的 source_state="unavailable"（第二段失败时打的标记）。
       重试/再进详情时必须先剥掉，否则按钮一上来就是「线路暂时不可用」，
       哪怕这次线路已经正常到货。 */
    delete cached._d.source_state;
    applyDetail(cached._d);
  }
  /* 2026-09-17 详情提速：详情页原先要等「多源匹配 + 线路测速」整段跑完才上屏
     （冷路径实测 12-30s，整页只有一个「加载中…」）。现在拆两段：
     ① `defer_sources=1` 只取豆瓣元数据（多数已随首页补详情落在服务端 7 天缓存里）
        → 海报/标题/meta/评分/演员/简介立刻上屏，主按钮显示「正在查找片源」；
     ② 再取完整详情（含 sources）→ 只补选集区与主按钮，不动已经上屏的信息。 */
  const ref = vodIdentity(vid);
  const normalizeDetail = raw => ({ ...raw, cover: raw.poster_url || raw.cover || "", score: +raw.rating || +raw.score || 0,
    area: (raw.area || (raw.regions || []).join(" / ")), tags: raw.tags || raw.genres || [],
    desc: raw.desc || raw.summary || "",
    meta: raw.meta || [raw.year, raw.area || (raw.regions || []).join(" / "), ...(raw.tags || raw.genres || [])].filter(Boolean).join(" / "),
    actors: Array.isArray(raw.actors) ? raw.actors.join(" / ") : (raw.actors || ""),
    sources: (raw.sources || []).map(line => ({ ...line, episodes: (line.episodes || []).map((ep, index) => ({
      ep: ep.name || ep.ep || `第${index + 1}集`, pid: ep.pid != null ? ep.pid : line.id, vid: ep.vid != null ? ep.vid : index,
    })) })) });
  let painted = false;
  try {
    const fast = await fetchJSON(BASE + vodDetailPath(vid) + "?defer_sources=1", { timeout: 60000, signal: controller.signal });
    if (!current()) return;
    if (!fast.title) throw new Error(fast.error || "未找到该影片");
    applyDetail(normalizeDetail(fast), true);
    painted = true;
    const rawDetail = await fetchJSON(
      BASE + (ref.kind === "hhkan" ? vodDetailPath(vid) : vodDetailPath(vid) + "?refresh=1"),
      { timeout: 90000, signal: controller.signal });
    if (!current()) return;
    const d = normalizeDetail(rawDetail);
    if (!d.title) throw new Error(d.error || "未找到该影片");
    try { store.set(dkey, { _ts: Date.now(), _d: d }); } catch (e) {}
    metricCloseDetail();
    /* 线路一到就上屏选集，绝不等 resolver。 */
    applyDetail(d);
    if (autoplay && current()) startPlayButton();
  } catch (e) {
    if (!current()) return;
    if (painted) {
      /* 第二段失败：保留已上屏的信息，落到可恢复文案（不误报「没有这部片」）。 */
      if (detail) { detail.source_state = "unavailable"; renderDetail(); }
      hint("线路暂时不可用，正在后台恢复");
    } else if (!cached) {
      $("#dTitle").textContent = "加载失败"; hint(e.message);
    }
  } finally {
    if (detailRequest === controller) detailRequest = null;
  }
}

function renderDetail() {
  const d = detail;
  $("#dPoster").src = img(d.cover);
  const doubanPoster = $("#dDouban");
  doubanPoster.hidden = true;
  doubanPoster.onload = null;
  doubanPoster.onerror = null;
  doubanPoster.removeAttribute("src");
  $("#dTitle").textContent = d.title || "未命名影片";
  /* App meta 行：meta.ifBlank { (year+area+tags) join " / " } */
  const meta = (d.meta || "").trim() || [os(d.year), os(d.area)].filter(Boolean).concat(d.tags || []).filter(Boolean).join(" / ");
  $("#dMeta").textContent = meta;
  $("#dScore").textContent = +d.score > 0 ? "★ 评分 " + Number(d.score).toFixed(1) : "";
  $("#dActors").textContent = d.actors ? "主演  " + d.actors : "";
  $("#dDesc").textContent = (d.desc || "").trim() || "暂无简介";
  $("#dResume").textContent = dResumeEp >= 0 ? `上次看到第${dResumeEp + 1}集 ${fmtTime(dResumePos)}` : "";
  const favs = getFavs().some(x => x.id === d.vid);
  /* App 按钮文案：「继续播放 第N集」/「立即播放」；「从头看」仅续播时 */
  const btns = [];
  /* 方案 §6.3：链路还在工作 → 「正在匹配最佳线路」；全挂 → 可恢复文案 + 重试。
     任何情况下都不把网络抖动说成「片库里没有这部片」。 */
  if (d.source_state === "matching") btns.push('<button class="d-btn primary" disabled>正在匹配最佳线路</button>');
  else if (d.source_state === "unavailable" || !d.sources.length) {
    btns.push('<button class="d-btn primary" id="dPlayBtn">线路暂时不可用，正在后台恢复</button>');
  }
  else btns.push(`<button class="d-btn primary" id="dPlayBtn">${dResumeEp >= 0 ? `继续播放 第${dResumeEp + 1}集` : "立即播放"}</button>`);
  btns.push(`<button class="d-btn ghost" id="dFavBtn">${favs ? "★ 已收藏" : "☆ 收藏"}</button>`);
  if (dResumeEp >= 0) btns.push(`<button class="d-btn ghost" id="dRestart">从头看</button>`);
  $("#dButtons").innerHTML = btns.join("");
  const playButton = $("#dPlayBtn");
  if (playButton) playButton.addEventListener("click", () => {
    /* 可恢复状态下这个按钮是「重试」而不是「立即播放」——线路可能已经恢复。 */
    if (detail.source_state === "unavailable" || !detail.sources.length) {
      hint("正在重新匹配线路…");
      retryDetail();
      return;
    }
    startPlayButton();
  });
  $("#dFavBtn").addEventListener("click", () => {
    const on = toggleFav({ id: d.vid, title: d.title, cover: d.cover, remark: "" });
    $("#dFavBtn").textContent = on ? "★ 已收藏" : "☆ 收藏";
    hint(on ? "已加入收藏" : "已取消收藏");
  });
  const r = $("#dRestart");
  if (r) r.addEventListener("click", () => {
    localStorage.removeItem("otvw:prog:vod:" + d.vid);
    localStorage.removeItem("otvw:ep:" + d.vid);
    dResumeEp = -1; dResumePos = 0; dEpIdx = 0; dEpPage = 0;
    renderDetail(); startPlayAt(0, 0);
  });
  renderDetailEps();
  /* 详情页去线路化：主动选源只保留在播放器「更多」面板内。 */
  const wrap = $("#dLinesRow");
  wrap.hidden = true;
  $("#dLines").innerHTML = "";
}

function isDetailEpisodeSet(eps) {
  if (eps.length <= 1) return false;
  const meta = (detail && detail.meta) || "";
  if (/电视剧|连续剧|网剧|短剧|动漫|综艺|美剧|英剧|韩剧|日剧|泰剧/.test(meta)) return true;
  if (eps.length > 4) return true;
  return eps.some(e => /第?\d+[\s]*[集期话章]|更新至|[上中下]集/.test((e && e.ep) || ""));
}

function renderDetailEps() {
  const s = detail.sources[dLineIdx];
  const eps = s ? s.episodes : [];
  const wrap = $("#dEpsWrap");
  /* HD中字/HD国语/正片等是电影播放版本，不是可交互的集数。 */
  if (!isDetailEpisodeSet(eps)) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const pageSize = 20, pageCount = Math.ceil(eps.length / pageSize);
  dEpPage = Math.min(dEpPage, pageCount - 1);
  $("#dEpPages").innerHTML = pageCount > 1 ? Array.from({ length: pageCount }, (_, p) =>
    `<button class="epp ${p === dEpPage ? "on" : ""}" data-p="${p}">${p * pageSize + 1}-${Math.min((p + 1) * pageSize, eps.length)}</button>`).join("") : "";
  $$("#dEpPages .epp").forEach(b => b.addEventListener("click", () => { dEpPage = +b.dataset.p; renderDetailEps(); }));
  const pageEps = eps.slice(dEpPage * pageSize, (dEpPage + 1) * pageSize);
  $("#dEpGrid").innerHTML = pageEps.map((e, i) => {
    const idx = dEpPage * pageSize + i;
    return `<button class="epc ${idx === dResumeEp ? "on" : ""}" data-i="${idx}">${esc(e.ep || idx + 1)}</button>`;
  }).join("");
  registerGrid("#dEpGrid", state.portrait ? 160 : 277, 20, state.portrait ? 40 : 80);
  $$("#dEpGrid .epc").forEach(b => b.addEventListener("click", () => {
    dEpIdx = +b.dataset.i; startPlayAt(dEpIdx, 0);
  }));
}

/* App 主按钮：resumeEp ≥0 播该集带位置，否则第 0 集 */
function startPlayButton() {
  if (dResumeEp >= 0) startPlayAt(dResumeEp, dResumePos);
  else startPlayAt(0, 0);
}
function startPlayAt(idx, resumePos) {
  const s = detail.sources[dLineIdx];
  const eps = s ? s.episodes : [];
  if (!eps.length) { hint("该线路暂无选集"); return; }
  if (idx >= eps.length) idx = 0;
  const ep = eps[idx];
  metricResetSession();
  metricMark("click");
  telemetryEmit(TELEMETRY_EVENTS.PLAY_CLICK, {
    kind: "click", episode_count: eps.length, line_count: (detail.sources || []).length,
    has_resume: resumePos > 0,
  });
  const prog = getProgress("vod:" + detail.vid);
  const epSaved = store.get("otvw:ep:" + detail.vid, -1);
  const resume = resumePos > 0 ? resumePos : (epSaved === idx && resumable(prog) ? prog.pos : 0);
  dEpIdx = idx;
  openPlayer({
    kind: "vod", vid: detail.vid, title: detail.title, cover: detail.cover,
    epIdx: idx, epName: ep.ep || `第${idx + 1}集`, epCount: eps.length,
    lineName: s.name, playRef: { pid: ep.pid, vid: ep.vid }, resume,
    lineIndex: dLineIdx, sourceKey: playerVodSourceKey(s, dLineIdx), sources: detail.sources,
    /* 详情页默认线路仍可由 resolver 优选；播放器内手动换线才锁定线路。 */
    linePinned: false,
  });
  /* v1.32 换集提速：起播同时预解析下一集（本线路），连播/手动换集秒切 */
  const nxt = eps[idx + 1];
  if (nxt) prefetchPlay(detail.vid, nxt.pid, nxt.vid);
}

/* ================================================================
 * 搜索（SearchScreen 复刻：本地目录 + 全站搜索，中文自动搜/字母显式搜）
 * ================================================================ */
const RESULT_CATS = ["全部", "电影", "电视剧", "国漫", "综艺", "短剧"];
const TV_TAG_RE = /欧美剧|国产剧|香港剧|台湾剧|韩国剧|日本剧|海外剧|泰剧|美剧|英剧|连续剧|电视剧|网剧/;
/* App classifyVod：meta 正则 + 集数 */
function classifyVod(meta, epCount) {
  meta = meta || "";
  if (meta.includes("短剧")) return "短剧";
  if (meta.includes("动漫")) return "国漫";
  if (meta.includes("综艺")) return "综艺";
  if (TV_TAG_RE.test(meta)) return "电视剧";
  if (epCount > 1) return "电视剧";
  return "电影";
}
const search = { q: "", remote: [], local: [], loading: false, loadingMore: false,
  lastSearched: "", seq: 0, explicit: 0, catMap: {}, timer: 0, page: 0, hasMore: false, moreError: "" };

function searchReset() {
  clearTimeout(search.timer); search.timer = 0; search.seq++;
  search.q = ""; search.remote = []; search.local = []; search.catMap = {};
  search.loading = false; search.loadingMore = false; search.lastSearched = "";
  search.page = 0; search.hasMore = false; search.moreError = ""; search.cat = "全部";
  $("#searchInput").value = ""; $("#searchClear").hidden = true;
  $("#searchGrid").innerHTML = ""; $("#searchTabs").innerHTML = "";
  $("#searchHint").hidden = true;
}
function searchResults() {
  /* remote 在前、local 在后当补充：同 id 时后端已识别的分类/排序胜出。
     本地目录按「首次出现的浏览频道」去重（buildCatalog），那是分区不是真实分类，
     local 排在前面会让错分类的副本遮住后端已认定 douban:anime 的同名条目。 */
  const allr = search.remote.concat(search.local).filter((v, i, a) => a.findIndex(x => x.id === v.id) === i);
  return allr;
}
function searchShown() {
  const rs = searchResults();
  if (search.cat === "全部") return rs;
  return rs.filter(it => search.catMap[it.id] === search.cat);
}
function renderSearch() {
  const rs = searchResults();
  if (search.q && rs.length) {
    $("#searchTabs").innerHTML = RESULT_CATS.map(catName => {
      const count = catName === "全部" ? rs.length : rs.filter(it => search.catMap[it.id] === catName).length;
      return `<button class="stab ${search.cat === catName ? "on" : ""}" data-c="${esc(catName)}">${esc(catName)}${count > 0 ? ` <span class="cnt">${count}</span>` : ""}</button>`;
    }).join("");
    $$("#searchTabs .stab").forEach(b => b.addEventListener("click", () => { search.cat = b.dataset.c; renderSearch(); }));
  } else $("#searchTabs").innerHTML = "";
  const shown = searchShown();
  if (!shown.length) {
    $("#searchGrid").innerHTML = "";
  } else {
    $("#searchGrid").innerHTML = shown.map(it => posterCard(it, 24)).join("");
    bindCards("#searchGrid"); registerGrid("#searchGrid", state.portrait ? 205 : 232, 20, state.portrait ? 40 : 70);
  }
  const hint = $("#searchHint");
  const empty = !shown.length && search.q
    ? (rs.length === 0 && search.loading ? "搜索中…" : rs.length === 0 ? "没有匹配的影片" : "该分类暂无结果") : "";
  if (search.loadingMore) {
    hint.textContent = empty ? `${empty}\n正在加载更多…` : "正在加载更多…";
    hint.hidden = false;
  } else if (search.moreError || search.hasMore) {
    hint.innerHTML = `${empty ? `<div>${esc(empty)}</div>` : ""}<button type="button" class="search-more" style="margin-top:18px;padding:12px 24px;border-radius:24px;background:rgba(255,255,255,.14);color:#fff">${search.moreError ? "加载失败，点击重试" : "加载更多搜索结果"}</button>`;
    hint.hidden = false;
    $(".search-more", hint).addEventListener("click", () => {
      search.moreError = "";
      doSearch(search.q, search.page + 1);
    });
  } else if (empty) {
    hint.textContent = empty;
    hint.hidden = false;
  } else hint.hidden = true;
}
/* 已知目录分类直接采用；目录外前 24 条拉详情归类（并发 6） */
async function classifyResults(items, seq = search.seq) {
  if (seq !== search.seq) return;
  const categories = { movie: "电影", tv: "电视剧", anime: "国漫", variety: "综艺", short: "短剧" };
  for (const it of items) {
    const m = /^douban:(movie|tv|anime|variety|short)$/.exec(it.categoryId || "");
    if (m) search.catMap[it.id] = categories[m[1]];
  }
  const targets = items.slice(0, 24).filter(it => search.catMap[it.id] === undefined);
  let i = 0;
  const worker = async () => {
    while (i < targets.length) {
      if (seq !== search.seq) return;
      const it = targets[i++];
      let cat = null;
      const m = /^hhkan:(\d+)$/.exec(it.categoryId || "");
      if (m && ["1", "2", "3", "4", "6"].includes(m[1])) {
        cat = { "1": "电影", "2": "电视剧", "3": "国漫", "4": "综艺", "6": "短剧" }[m[1]];
      } else {
        try {
          const d = await fetchJSON(BASE + vodDetailPath(it.id), { timeout: 45000 });
          if (d.title) cat = classifyVod(d.meta, (d.sources || []).reduce((n, s) => Math.max(n, (s.episodes || []).length), 0));
        } catch (e) {}
      }
      if (seq !== search.seq) return;
      if (cat) search.catMap[it.id] = cat;
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
  if (seq === search.seq) renderSearch();
}
async function doSearch(k, page = 1) {
  if (!k) return;
  const firstPage = page <= 1;
  if (!firstPage && (search.loadingMore || !search.hasMore)) return;
  const seq = firstPage ? ++search.seq : search.seq;
  if (firstPage) {
    search.loading = true; search.loadingMore = false; search.page = 0;
    search.hasMore = false; search.moreError = "";
  } else {
    search.loadingMore = true; search.moreError = "";
  }
  renderSearch();
  try {
    const d = await fetchJSON(BASE + "/api/search?q=" + encodeURIComponent(k) + "&page=" + page + "&limit=60", { timeout: 15000 });
    const items = (d.items || []).map(it => ({
      ...it,
      id: it.id || ("douban:" + it.douban_id),
      cover: it.poster_url || it.cover || "",
      categoryId: "douban:" + (it.category || "movie"),
    }));
    if (seq !== search.seq) return;
    search.remote = firstPage ? items : search.remote.concat(items)
      .filter((item, index, all) => all.findIndex(x => x.id === item.id) === index);
    search.loading = false; search.loadingMore = false; search.page = page;
    search.hasMore = !!d.has_more; search.moreError = "";
    renderSearch();
    classifyResults(firstPage ? items.concat(search.local) : items, seq);
  } catch (e) {
    if (seq === search.seq) {
      search.loading = false; search.loadingMore = false;
      if (!firstPage) search.moreError = "more";
      renderSearch();
    }
  }
}
function searchInputChanged(explicit) {
  const k = $("#searchInput").value.trim();
  const changed = k !== search.q;
  search.q = k;
  clearTimeout(search.timer); search.timer = 0;
  if (!k) {
    search.seq++; search.remote = []; search.local = []; search.catMap = {};
    search.loading = false; search.loadingMore = false; search.lastSearched = "";
    search.page = 0; search.hasMore = false; search.moreError = "";
    renderSearch(); return;
  }
  if (changed) {
    search.seq++; search.remote = []; search.catMap = {}; search.lastSearched = "";
    search.loading = false; search.loadingMore = false; search.page = 0;
    search.hasMore = false; search.moreError = ""; search.cat = "全部";
  }
  /* 本地目录即时过滤 */
  search.local = vod.cat ? vod.cat.dedupedItems.filter(it => {
    const t = it.title || "";
    return t.includes(k) || k.toLowerCase().split("").every(ch => t.toLowerCase().includes(ch));
  }) : [];
  renderSearch();
  classifyResults(search.local, search.seq);
  if (!explicit && k === search.lastSearched) return;
  if (explicit) { search.lastSearched = k; doSearch(k); return; }
  search.timer = setTimeout(() => {
    if (search.q !== k) return;
    search.lastSearched = k; doSearch(k);
  }, 450);
}
$("#searchInput").addEventListener("input", () => {
  $("#searchClear").hidden = !$("#searchInput").value;
  searchInputChanged(false);
});
$("#searchInput").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.target.blur(); searchInputChanged(true); }
});
$("#searchClear").addEventListener("click", searchReset);
search.cat = "全部";

/* ================================================================
 * 我的（FavoritesScreen 复刻：会员信息并入「我的收藏」标题行 + 收藏 + 历史，topPad 150）
 * ================================================================ */

/* 会员信息行（2026-09-19 需求④）：与 TV 版 / 移动版 App `MemberInlineInfo` 逐分支同构。
 *
 * App 基准（android + internal/mobile 的 ui/favorites/FavoritesScreen.kt）：
 *   when {
 *     d == null                       -> "加载中…"      White50
 *     expiryAt != null && now>=expiryAt -> "会员已过期"  #FF453A ; info = 套餐 + " · 续费可用"
 *     !isPremium()                    -> "等待联网校验"  #FF9F0A ; info = "联网后自动恢复"
 *     expiryAt == null                -> "终身会员"      #30D158 ; info = "终身 · 永久有效"
 *     else                            -> "会员有效"      #30D158 ; info = 套餐+" · 到期 yyyy-MM-dd · 剩余 N 天"
 *   }
 *   渲染形态 = 「状态点 + 状态 · info」+ 行尾「续费」；未激活显示主动会员/激活入口。
 *
 * 旧版是带背景框的三行大卡（标题/副行/卡密/徽章）且横屏竖屏两套布局，与两端 App 都不像；
 * 现统一为无框行内小字，横竖屏同构。 */
function memberStatus() {
  const d = license.data;
  if (d == null) return { status: "加载中…", color: "#8E8E93", info: "" };
  const exp = d.expiry_at;
  const hasExp = exp != null && exp >= 0;
  if (hasExp && licenseNow() >= exp)
    return { status: "会员已过期", color: "#FF453A", info: planName(d.plan) + " · 续费可用" };
  if (!isPremium())
    return { status: "等待联网校验", color: "#FF9F0A", info: "联网后自动恢复" };
  if (!hasExp)
    return { status: "终身会员", color: "#30D158", info: "终身 · 永久有效" };
  const dt = new Date(exp);
  const ymd = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  const left = Math.max(0, Math.ceil((exp - licenseNow()) / 86400000));
  return { status: "会员有效", color: "#30D158",
    info: `${planName(d.plan)} · 到期 ${ymd} · 剩余 ${left} 天` };
}
function planName(plan) {
  return { monthly: "月卡", quarterly: "季卡", yearly: "年卡", lifetime: "终身", weekly: "周卡" }[plan] || plan;
}
function renderMemberInline() {
  const el = $("#memberInline");
  if (!el) return;
  /* 会员激活是可选功能；未激活时仍提供主动入口，播放不受会员状态影响。 */
  const st = memberStatus();
  el.hidden = false;
  if (!license.data) {
    el.innerHTML = `<button class="mi-renew" id="miRenew">会员 / 激活</button>`;
    $("#miRenew").addEventListener("click", openMemberModal);
    return;
  }
  el.innerHTML = `
    <span class="mi-dot" style="background:${st.color}"></span>
    <span class="mi-text">${esc(st.status)}${st.info ? " · " + esc(st.info) : ""}</span>
    <button class="mi-renew" id="miRenew">续费</button>`;
  $("#miRenew").addEventListener("click", openMemberModal);
}

/* ---------------- Windows 桌面版版本 / 检查更新 ----------------
 * 仅当后端 /api/health 返回 desktop:true 时渲染（由 EXE 注入 OTV_DESKTOP 决定）。
 * 网页版不返回该字段，整行隐藏 —— 网页随服务器更新，没有"手动检查"这回事。
 * 「检查更新」只读状态：真实下载/校验/生效由 launcher.py 的后台线程完成，
 * 这里通过重新探测 /api/health 的 hotVersion 反映结果，避免前端重复实现下载链路。 */
let verState = { checking: false };
async function renderVersionRow() {
  const el = $("#verInline");
  if (!el) return;
  let h = null;
  try {
    h = await fetchJSON(BASE + "/api/health", 0, 8000);
  } catch (e) { /* 健康探测失败就不显示这一行，不影响其他功能 */ }
  if (!h || !h.desktop) { el.hidden = true; el.innerHTML = ""; return; }
  const hot = Number(h.hotVersion || 0);
  el.hidden = false;
  const label = hot > 0 ? "已更新到 v" + hot : "已是最新版本";
  /* 只在结构缺失时建一次；之后只改文字与 disabled。
     之前每次都重建 innerHTML，切页/轮询会把按钮从 DOM 里换掉，
     光标形状（hover/pointer/disabled）在重建瞬间闪一下——
     2026-09-30 用户反馈"更新弹窗的光标逻辑有问题"，根因在此。 */
  if (!el.firstElementChild) {
    el.innerHTML = `<span class="vi-text"></span><button class="vi-btn" id="viCheck"></button>`;
    $("#viCheck").addEventListener("click", checkForUpdate);
  }
  el.querySelector(".vi-text").textContent = label;
  const btn = $("#viCheck");
  btn.textContent = verState.checking ? "检查中…" : "检查更新";
  btn.disabled = !!verState.checking;
}
async function checkForUpdate() {
  if (verState.checking) return;
  verState.checking = true;
  hint("正在检查更新…");
  await renderVersionRow();
  // 桌面端后台线程会自行下载；等它一个周期再刷新显示。
  verState.timer = setTimeout(() => {
    verState.checking = false;
    verState.timer = null;
    renderVersionRow();
  }, 6000);
}

/* ---------------- 会员续费 / 兑换弹窗（需求④） ----------------
 * App 对应物 = PaywallScreen：四张套餐卡（月/季/年/终身）+ 选中后「续费」按钮 + 底部
 * 「输入卡密激活」。两端 App 的续费按钮当前走 RenewalGateway（handler 未注入 → 返回 false），
 * 页面据此提示「续费通道准备中，可先用输入卡密激活」——网页版沿用同口径，避免自造一条
 * 与 App 不一致的支付通道；真正的续费下单接口接通后只改这里一处。 */
const MEMBER_PLANS = [
  { plan: "monthly", name: "月卡", price: "¥9.9", sub: "30 天" },
  { plan: "quarterly", name: "季卡", price: "¥29.9", sub: "90 天" },
  { plan: "yearly", name: "年卡", price: "¥99.9", sub: "365 天" },
  { plan: "lifetime", name: "终身", price: "¥128", sub: "一次买断" },
];
let mmSelected = "monthly";
function openMemberModal() {
  const st = memberStatus();
  $("#mmSub").textContent = license.data
    ? `${st.status}${st.info ? " · " + st.info : ""}`
    : "尚未开通 · 会员服务为可选功能";
  renderMemberModal();
  $("#mmMsg").textContent = "";
  $("#memberModal").hidden = false;
}
function closeMemberModal() { $("#memberModal").hidden = true; }
function renderMemberModal() {
  $("#mmPlans").innerHTML = MEMBER_PLANS.map(p => `
    <div class="mm-plan ${p.plan === mmSelected ? "on" : ""}" data-plan="${p.plan}">
      <div class="p-name">${esc(p.name)}</div>
      <div class="p-price">${esc(p.price)}</div>
      <div class="p-sub">${esc(p.sub)}</div>
    </div>`).join("");
  $$("#mmPlans .mm-plan").forEach(c => c.addEventListener("click", () => {
    mmSelected = c.dataset.plan;
    renderMemberModal();
    $("#mmMsg").textContent = "";
  }));
  const p = MEMBER_PLANS.find(x => x.plan === mmSelected) || MEMBER_PLANS[0];
  const btn = $("#mmRenewBtn");
  btn.textContent = `续费 ${p.name}`;
  $("#mmRenewNote").textContent = `${p.name} · ${p.price}`;
}
/* 续费按钮：App RenewalGateway.request() 未注入 handler → false，页面给卡密兜底提示。
   网页版无发卡平台下单通道，同样只提示；待接口接通后在此处替换为真实下单跳转。 */
function mmRenew() {
  const p = MEMBER_PLANS.find(x => x.plan === mmSelected) || MEMBER_PLANS[0];
  $("#mmMsg").textContent = `续费通道准备中，可先购买${p.name}卡密后用下方输入框兑换`;
  const inp = $("#mmInput");
  if (inp) inp.focus();
}
async function mmSubmit() {
  const btn = $("#mmBtn");
  btn.disabled = true; btn.textContent = "兑换中…";
  const r = await activateCode($("#mmInput").value);
  btn.disabled = false; btn.textContent = "兑换";
  if (r.ok) {
    $("#mmInput").value = "";
    $("#mmMsg").textContent = "";
    hint("兑换成功，有效期已顺延");
    renderMemberInline();
    openMemberModal();   /* 状态行同步刷新 */
  } else $("#mmMsg").textContent = r.msg;
}
(function bindMemberModal() {
  const modal = $("#memberModal");
  if (!modal) return;
  /* 弹窗内容每次打开重建（套餐卡选中态），静态骨架在这里一次性装好 */
  const plans = $("#mmPlans");
  plans.insertAdjacentHTML("afterend",
    `<div class="mm-renew-row">
       <button class="mm-renew-btn" id="mmRenewBtn">续费月卡</button>
       <span class="mm-renew-note" id="mmRenewNote"></span>
     </div>`);
  $("#mmRenewBtn").addEventListener("click", mmRenew);
  $("#mmBtn").addEventListener("click", mmSubmit);
  $("#mmInput").addEventListener("keydown", e => { if (e.key === "Enter") mmSubmit(); });
  $("#mmClose").addEventListener("click", closeMemberModal);
  /* 点暗区关闭（与 App 浮层点按关闭同语义） */
  modal.addEventListener("click", e => { if (e.target === modal) closeMemberModal(); });
})();

function renderFav() {
  renderMemberInline();
  renderVersionRow();
  const favs = getFavs(), hist = getHistory().slice(0, 10);
  $("#favEmpty").hidden = favs.length > 0;
  $("#favHistSec").hidden = hist.length === 0;   /* App：历史记录标题仅历史非空时渲染 */
  $("#favHistEmpty").hidden = hist.length > 0;
  $("#favRow").innerHTML = favs.map(it => posterCard(it, 21)).join("");
  bindCards("#favRow");
  $("#histRowFav").innerHTML = hist.map(h => histCard(h, "fav")).join("");
  $$("#histRowFav .pcard").forEach(c => c.addEventListener("click", () => {
    const h = hist.find(x => String(x.id) === c.dataset.vid);
    if (h) playFromHistory(h);
  }));
}

/* ================================================================
 * 电视（TvScreen 复刻：浏览 chips+网格 / 全屏播放 + FAB + 两级侧边栏）
 * ================================================================ */
const tv = { loaded: false, groups: [], gi: 0, cur: null, hls: null, playing: false,
  lineIdx: 0, urls: [], playUrl: "", autoSwapped: false, autoSkips: 0, sidebarOpen: false, fabTimer: 0 };
/* LineSpeed.FAIL_MS 同值；TTFB 测速——浏览器直连上游无 CORS 头，经同源 relay 测量 */
const LINE_FAIL = 999999;
const lineScores = {};
async function probeLine(u) {
  if (lineScores[u] !== undefined && lineScores[u] < LINE_FAIL) return lineScores[u];
  const t0 = performance.now();
  const ctrl = new AbortController();
  const kill = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetch(relay(u), { signal: ctrl.signal, cache: "no-store" });
    clearTimeout(kill);
    if (lineScores[u] === undefined || lineScores[u] >= LINE_FAIL) lineScores[u] = Math.round(performance.now() - t0);
    try { if (r.body && r.body.cancel) r.body.cancel(); } catch (e) {}
  } catch (e) {
    clearTimeout(kill);
    if (lineScores[u] === undefined) lineScores[u] = LINE_FAIL;
  }
  return lineScores[u];
}
/* IptvRepository.probeAll：并发测一组线路 */
async function probeAll(urls) {
  const fresh = [...new Set(urls)].filter(u => lineScores[u] === undefined);
  let i = 0;
  const CONN = 6;
  await Promise.all(Array.from({ length: CONN }, async () => {
    while (i < fresh.length) { await probeLine(fresh[i++]); }
  }));
}
/* IptvRepository.rankUrls：好线升序 + 未测 + 死线垫底（≥2 条有分才重排） */
function rankUrls(urls) {
  if (urls.length < 2) return urls;
  const scored = urls.filter(u => lineScores[u] !== undefined);
  if (scored.length < 2) return urls;
  const good = scored.filter(u => lineScores[u] < LINE_FAIL).sort((a, b) => lineScores[a] - lineScores[b]);
  const dead = scored.filter(u => lineScores[u] >= LINE_FAIL);
  return good.concat(urls.filter(u => lineScores[u] === undefined), dead);
}

async function initTv() {
  if (tv.loaded) return;
  tv.loaded = true;
  $("#tvLoading").textContent = "— 频道表加载中 …";
  try {
    const d = await fetchJSON(BASE + "/api/iptv", { timeout: 40000 });
    if (!d.groups || !d.groups.length) throw new Error(d.error || "频道表为空");
    tv.groups = d.groups;
    $("#tvLoading").hidden = true;
    renderTvGroupChips();
    renderTvChannels();

    /* 上次频道自动续播 */
    const last = store.get("otvw:tv:last", null);
    if (last && tv.groups[last.gi] && tv.groups[last.gi].channels[last.ci]) {
      playTvChannel(last.gi, last.ci, true);
    }
  } catch (e) {
    $("#tvLoading").textContent = "直播源加载失败\n正在自动重试，请稍候…";
    setTimeout(() => { tv.loaded = false; }, 4000);
  }
}
function renderTvGroupChips() {
  $("#tvGroupChips").innerHTML = tv.groups.map((g, i) =>
    `<button class="chip ${i === tv.gi ? "on" : ""}" data-gi="${i}">${esc(g.name)}</button>`).join("");
  $$("#tvGroupChips .chip").forEach(c => c.addEventListener("click", () => {
    tv.gi = +c.dataset.gi; renderTvGroupChips(); renderTvChannels();
    scrollTo(0, 0);
  }));
}
/* App LogoPlaceholder：首二字 + 4 色板按 name.hashCode 取模 */
function logoPalette(name) {
  const palettes = [["#2E4A6B", "#121E2E"], ["#50345C", "#1A1220"], ["#2A5248", "#101E19"], ["#5C4630", "#201710"]];
  let h = 0;
  for (let i = 0; i < (name || "").length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return palettes[Math.abs(h) % palettes.length];
}
function chLogoHTML(ch) {
  const [c1, c2] = logoPalette(ch.name);
  const fb = `<div class="ch-fb" style="background:linear-gradient(160deg,${c1},${c2})">${esc((ch.name || "?").slice(0, 2))}</div>`;
  if (!ch.logo) return fb;
  return `<img loading="lazy" decoding="async" src="${esc(img(ch.logo))}" onerror="this.outerHTML='${fb.replace(/"/g, "&quot;")}'" alt="">`;
}
function chRowHTML(gi, ci, ch, playing) {
  return `<div class="ch-row ${playing ? "on" : ""}" data-gi="${gi}" data-ci="${ci}">
    ${chLogoHTML(ch)}
    <div class="ch-name">${esc(ch.name)}</div>
    ${playing ? `<span class="ch-playing">正在播放</span>` : ""}</div>`;
}
function renderTvChannels() {
  const g = tv.groups[tv.gi];
  /* App 浏览态 ChannelRow playing=false——浏览网格不显示「正在播放」 */
  $("#tvChannels").innerHTML = g.channels.map((ch, ci) => chRowHTML(tv.gi, ci, ch, false)).join("");
  bindTvRows("#tvChannels");
}
function bindTvRows(sel) {
  $$(sel + " .ch-row").forEach(r => r.addEventListener("click", () => {
    playTvChannel(+r.dataset.gi, +r.dataset.ci, false);
  }));
}

function tvAttach(url) {
  const v = $("#tvVideo");
  if (tv.hls) { tv.hls.destroy(); tv.hls = null; }
  tv.started = false;
  const onPlaying = () => { tv.started = true; $("#tvBuf").hidden = true; };
  v.removeEventListener("playing", onPlaying);
  v.addEventListener("playing", onPlaying);
  if (window.Hls && Hls.isSupported()) {
    tv.hls = new Hls({ maxBufferLength: 24, maxMaxBufferLength: 60 });
    tv.hls.loadSource(url); tv.hls.attachMedia(v);
    tv.hls.on(Hls.Events.ERROR, (_, d) => {
      if (!d.fatal) return;
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR && !String(d.details || "").includes("manifest")) { tv.hls.startLoad(); return; }
      tvNextLine();
    });
  } else { v.src = url; v.onerror = () => tvNextLine(); }
  v.play().catch(() => {});
  $("#tvBuf").hidden = false;
  /* 电视#5：3.5s 仍缓冲 → 自动换更快线（<0.55×当前）；12s 未起播 → 自动跳台（≤3 次） */
  clearTimeout(tv.swapTimer); clearTimeout(tv.skipTimer);
  tv.swapTimer = setTimeout(() => {
    if (tv.autoSwapped || tv.started || !tv.cur) return;
    const ch = tv.groups[tv.cur.gi] && tv.groups[tv.cur.gi].channels[tv.cur.ci];
    if (!ch) return;
    const ranked = rankUrls(ch.urls);
    const curScore = lineScores[tv.playUrl] || LINE_FAIL;
    const best = ranked.find(u => u !== tv.playUrl && (lineScores[u] || LINE_FAIL) < LINE_FAIL &&
      (lineScores[u] === undefined ? false : lineScores[u] < curScore * 0.55));
    if (best) {
      tv.autoSwapped = true;
      hint("已自动切换最快线路");
      tvAttach(relay(best));
      tv.playUrl = best;
    }
  }, 3500);
  tv.skipTimer = setTimeout(() => {
    if (tv.started || !tv.cur) return;
    if (tv.autoSkips >= 3) { hint("多个频道播放失败，请手动选台"); tvOpenSidebar(); return; }
    tv.autoSkips++;
    hint("该频道不可用，自动切换下一台…");
    const all = tv.groups.flatMap((g, gi) => g.channels.map((ch, ci) => ({ ch, gi, ci })));
    const idx = all.findIndex(x => x.gi === tv.cur.gi && x.ci === tv.cur.ci);
    const nx = all[(idx + 1) % all.length];
    playTvChannel(nx.gi, nx.ci, false);
  }, 12000);
}
function tvNextLine() {
  tv.lineIdx++;
  const ch = tv.cur && tv.groups[tv.cur.gi] && tv.groups[tv.cur.gi].channels[tv.cur.ci];
  if (!ch) return;
  const urls = tv.urls.length ? tv.urls : ch.urls;
  if (tv.lineIdx < urls.length) {
    setTimeout(() => tvAttach(relay(urls[tv.lineIdx])), 400);
  } else {
    $("#tvBuf").hidden = true;
    $("#tvErrMsg").textContent = `「${ch.name}」全部线路播放失败，请换台或稍后重试`;
    $("#tvErr").hidden = false;
  }
}
function playTvChannel(gi, ci, silent) {
  const ch = tv.groups[gi].channels[ci];
  tv.cur = { gi, ci }; tv.lineIdx = 0; tv.autoSwapped = false;
  tv.urls = ch.urls.slice();
  tv.playUrl = tv.urls[0];
  store.set("otvw:tv:last", { gi, ci });
  tv.playing = true;
  $("#pTv").hidden = false; $("#tvPlay").hidden = false;
  $("#tvErr").hidden = true;
  tvAttach(relay(tv.playUrl));
  /* App startPlay：换台反馈 hint 频道名；后台补测本频道全部线路 */
  hint(ch.name);
  probeAll(tv.urls);
  if (state.portrait) {
    /* 需求12⑧：竖屏电视播放器=顶部 16:9 视频 + 下方常驻频道列表（无 FAB/浮层） */
    $("#tvSidebar").hidden = false;
    tv.sidebarOpen = true;
    renderTvSidebar();
  } else if (tv.sidebarOpen) renderTvSidebar();
  else renderTvChannels();
  fz($("#tvFab"));
}
function stopTvPlayback() {
  const v = $("#tvVideo");
  try { v.pause(); v.removeAttribute("src"); v.load(); } catch (e) {}
  if (tv.hls) { tv.hls.destroy(); tv.hls = null; }
  clearTimeout(tv.swapTimer); clearTimeout(tv.skipTimer); clearTimeout(tv.fabTimer);
  $("#tvPlay").hidden = true; $("#tvSidebar").hidden = true; tv.sidebarOpen = false;
  $("#tvFab").hidden = true;
  tv.playing = false; tv.cur = null;
}
/* 移动#2：触碰屏幕浮现 FAB，3s 无操作隐藏；侧边栏开时点空白关闭（竖屏=常驻列表，无 FAB） */
function tvPokeHandle() {
  if (state.portrait) return;
  const fab = $("#tvFab");
  fab.hidden = false;
  fz(fab);
  clearTimeout(tv.fabTimer);
  tv.fabTimer = setTimeout(() => { fab.hidden = true; }, 3000);
}
function tvOpenSidebar() {
  if (state.portrait) { renderTvSidebar(); return; }   /* 竖屏常驻列表，无浮层语义 */
  tv.sidebarOpen = true;
  $("#tvSidebar").hidden = false;
  $("#tvFab").hidden = true;
  clearTimeout(tv.fabTimer);
  renderTvSidebar();
}
function tvCloseSidebar() {
  if (state.portrait) return;
  tv.sidebarOpen = false;
  $("#tvSidebar").hidden = true;
}
$("#tvFab").addEventListener("click", () => tvOpenSidebar());
$("#tvPlay").addEventListener("click", e => {
  if (e.target.id === "tvVideo") {
    if (tv.sidebarOpen) tvCloseSidebar(); else tvPokeHandle();
  }
});
/* 播放态侧边栏：一级分组 = 浏览分组（电视#7 记忆），点分组切列表不关闭 */
function renderTvSidebar() {
  const gi = tv.gi;
  const g = tv.groups[gi] || tv.groups[0];
  $("#tvsGroups").innerHTML = tv.groups.map((grp, i) =>
    `<button class="gchip ${i === gi ? "on" : ""}" data-gi="${i}">${esc(grp.name)}</button>`).join("");
  $$("#tvsGroups .gchip").forEach(c => c.addEventListener("click", () => {
    tv.gi = +c.dataset.gi; renderTvSidebar(); renderTvGroupChips();
  }));
  $("#tvsList").innerHTML = g.channels.map((ch, ci) =>
    chRowHTML(gi, ci, ch, !!(tv.cur && tv.cur.gi === gi && tv.cur.ci === ci))).join("");
  bindTvRows("#tvsList");
}

/* ================================================================
 * 播放器（PlayerScreen 复刻：影视 tvOS 控制层 + 直播信号源条 + 手势）
 * ================================================================ */
const MAX_RECOVERY_ATTEMPTS = 4;
const pv = { hls: null, video: null, ctx: null, sources: [], srcIds: [], srcNames: [], sourceRefs: [], hideTimer: 0, progTimer: 0,
  stallTimer: 0, speedIdx: 2, pausedAt: -1, controls: false, picker: false,
  recoveryGen: 0, recoveryAttempts: 0, recovering: false, loadGen: 0, playerSession: 0,
  playController: null, detailController: null, detailRequest: null,
  vodSources: [], vodLineIdx: 0, vodSourceStatus: "idle", vodSourceMessage: "", panelOpen: false,
  routeLinesExpanded: false,
  loadPending: false };
/* PlayerViewModel.SPEED_STEPS */
const SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0];
/* AspectMode：原始 / 裁切填充 / 16:9 / 4:3 */
const ASPECTS = ["原始", "裁切填充", "16:9", "4:3"];

function openPlayer(ctx) {
  if (pv.ctx && pv.ctx.kind === "vod") stopVodMediaPipeline();
  pv.playController?.abort(); pv.playController = null;
  pv.detailController?.abort(); pv.detailController = null; pv.detailRequest = null;
  pv.playerSession++;
  const session = pv.playerSession;
  const sources = ctx.kind === "vod" ? initialPlayerVodSources(ctx) : [];
  pv.ctx = { ...ctx, sources };
  pv.sources = []; pv.srcIds = []; pv.srcNames = []; pv.sourceRefs = [];
  if (ctx.kind === "vod") pv.playController = new AbortController();
  pv.srcIdx = 0; pv.speedIdx = 2; pv.pausedAt = -1;
  pv.recoveryGen++; pv.recoveryAttempts = 0; pv.recovering = false;
  pv.loadGen++;
  pv.aspectIdx = store.get("otvw:aspect", 0);
  pv.controls = false; pv.picker = false;
  pv.vodSources = sources;
  if (ctx.kind === "vod") pv.sources = [];
  pv.vodLineIdx = playerVodLineIndex(sources, pv.ctx.playRef, pv.ctx.sourceKey, pv.ctx.lineIndex);
  pv.vodSourceStatus = ctx.kind === "vod" ? (sources.length ? "refreshing" : "loading") : "idle";
  pv.vodSourceMessage = ctx.kind === "vod"
    ? (sources.length ? "正在更新可用信号源…" : "正在加载可用信号源…") : "";
  pv.panelOpen = ctx.kind === "vod" && !!state.portrait;
  pv.routeLinesExpanded = false;
  pv.loadPending = ctx.kind === "vod";
  $("#pPlayer").hidden = false; document.body.style.overflow = "hidden";
  $("#pError").hidden = true; $("#bufSpin").hidden = false;
  $("#morePanel").hidden = !pv.panelOpen; $("#srcPickerWrap").hidden = true;
  $("#controls").hidden = true; $("#ctlBack").hidden = true;
  syncPlayerSourcePanel();
  $("#cTitle").textContent = ctx.title;
  $("#cMeta").textContent = "";
  setPlayIcon(false);
  const v = $("#video"); pv.video = v;
  applyAspect();
  /* 竖屏播放器（2026-09-20 v3）：控制条覆盖视频底部；播放信息与更多选项下沉 */
  if (state.portrait) {
    renderMorePanel();
    $("#morePanel").hidden = false;
    showControls();
  }
  if (ctx.kind === "vod") {
    renderMorePanel();
    refreshPlayerEpisodes({ session });
  }
  try {
    if (ctx.kind === "vod") resolveVodAndPlay(pv.ctx, pv.loadGen, { force: false });
    else if (ctx.kind === "live") resolveLiveAndPlay(pv.ctx, session);
  } catch (e) { playerFatal(e.message); }
}

function playerVodSourceKey(line, index) {
  if (!line) return "";
  if (line.id != null && String(line.id)) return "id:" + String(line.id);
  const first = (line.episodes || []).find(ep => ep && ep.pid != null);
  return [line.source_id || "", line.source_name || line.name || "", first ? first.pid : index]
    .map(value => String(value == null ? "" : value)).join("|");
}
function playerVodEpisodeIndex(line, playRef) {
  const episodes = (line && line.episodes) || [];
  if (!playRef) return -1;
  return episodes.findIndex(ep => ep && String(ep.pid) === String(playRef.pid)
    && String(ep.vid) === String(playRef.vid));
}
function playerVodEpisodeIndexByName(line, epName) {
  const name = String(epName || "").trim();
  if (!name) return -1;
  const episodes = (line && line.episodes) || [];
  const matches = episodes.reduce((indices, ep, index) => {
    if (String((ep && (ep.ep || ep.name)) || "").trim() === name) indices.push(index);
    return indices;
  }, []);
  return matches.length === 1 ? matches[0] : -1;
}
function playerVodEpisodeIndexForRefresh(line, playRef, epName) {
  const name = String(epName || "").trim();
  if (!name) return playerVodEpisodeIndex(line, playRef);
  const byName = playerVodEpisodeIndexByName(line, name);
  if (byName >= 0) return byName;
  const byRef = playerVodEpisodeIndex(line, playRef);
  const referencedName = byRef >= 0
    ? String(((line.episodes || [])[byRef] || {}).ep || ((line.episodes || [])[byRef] || {}).name || "").trim()
    : "";
  return byRef >= 0 && referencedName === name ? byRef : -1;
}
function exactPlayerVodLineIndex(lines, playRef, sourceKey, referenceLine) {
  if (sourceKey) {
    const byKey = lines.reduce((matches, line, index) => {
      if (playerVodSourceKey(line, index) === sourceKey) matches.push(index);
      return matches;
    }, []);
    if (byKey.length === 1) return byKey[0];
  }
  if (referenceLine) {
    const referenceName = String(referenceLine.name || "").trim();
    const referenceSource = String(referenceLine.source_id || "");
    if (referenceName) {
      const bySourceAndName = lines.reduce((matches, line, index) => {
        if (String(line.name || "").trim() === referenceName
            && (!referenceSource || String(line.source_id || "") === referenceSource)) matches.push(index);
        return matches;
      }, []);
      if (bySourceAndName.length === 1) return bySourceAndName[0];
    }
    if (referenceSource) {
      const sourceMatches = lines.reduce((matches, line, index) => {
        if (String(line.source_id || "") === referenceSource) matches.push(index);
        return matches;
      }, []);
      if (sourceMatches.length === 1) return sourceMatches[0];
    }
  }
  const byPlayRef = lines.reduce((matches, line, index) => {
    if (playerVodEpisodeIndex(line, playRef) >= 0) matches.push(index);
    return matches;
  }, []);
  if (byPlayRef.length === 1) return byPlayRef[0];
  return -1;
}
function playerVodLineIndex(lines, playRef, sourceKey, preferredIndex) {
  const exact = exactPlayerVodLineIndex(lines, playRef, sourceKey);
  if (exact >= 0) return exact;
  return Number.isInteger(preferredIndex) && preferredIndex >= 0 && preferredIndex < lines.length
    ? preferredIndex : 0;
}
function normalizePlayerVodSources(raw) {
  return (raw.sources || []).map(line => ({
    ...line,
    episodes: (line.episodes || []).map((ep, index) => ({
      ...ep,
      ep: ep.name || ep.ep || `第${index + 1}集`,
      pid: ep.pid != null ? ep.pid : line.id,
      vid: ep.vid != null ? ep.vid : index,
    })),
  })).filter(line => line.episodes.length > 0);
}
function initialPlayerVodSources(ctx) {
  const currentDetail = detail && String(detail.vid) === String(ctx.vid) ? detail.sources : null;
  let lines = ctx.sources && ctx.sources.length ? ctx.sources
    : currentDetail && currentDetail.length ? currentDetail : [];
  lines = lines.map(line => ({
    ...line,
    episodes: (line.episodes || []).map((ep, index) => ({
      ...ep, ep: ep.ep || ep.name || `第${index + 1}集`,
      pid: ep.pid != null ? ep.pid : line.id, vid: ep.vid != null ? ep.vid : index,
    })),
  })).filter(line => line.episodes.length > 0);
  if (!lines.length && ctx.playRef) {
    lines = [{ id: `current:${ctx.playRef.pid}`, name: ctx.lineName || "当前信号源",
      episodes: [{ pid: ctx.playRef.pid, vid: ctx.playRef.vid, ep: ctx.epName || "当前集" }] }];
  }
  return lines;
}
function playerSessionIsCurrent(session, ctx) {
  return pv.playerSession === session && pv.ctx === ctx && ctx && ctx.kind === "vod";
}
function stopVodMediaPipeline() {
  pv.playController?.abort(); pv.playController = null;
  pv.recoveryGen++;
  pv.recovering = false;
  pv.loadPending = false;
  clearInterval(pv.progTimer); pv.progTimer = 0;
  clearInterval(pv.stallTimer); pv.stallTimer = 0;
  clearTimeout(pv.hideTimer); pv.hideTimer = 0;
  if (pv.hls) { pv.hls.destroy(); pv.hls = null; }
  const v = pv.video || $("#video");
  if (!v) return;
  try { v.pause(); } catch (e) {}
  v.controls = false;
  v.onloadedmetadata = v.onplaying = v.onpause = v.onended = v.ontimeupdate = v.onerror = null;
  try { v.removeAttribute("src"); v.load(); } catch (e) {}
}
function refreshPlayerEpisodes(options = {}) {
  const ctx = pv.ctx;
  if (!ctx || ctx.kind !== "vod") return Promise.resolve(null);
  const session = options.session == null ? pv.playerSession : options.session;
  const expectedLoadGen = options.loadGen;
  const isCurrentRequest = request => pv.playerSession === session
    && (!request || (pv.detailRequest && pv.detailRequest.promise === request))
    && (expectedLoadGen == null || pv.loadGen === expectedLoadGen)
    && !controller.signal.aborted;
  if (pv.detailRequest && pv.detailRequest.session === session && !options.force)
    return pv.detailRequest.promise;
  if (pv.detailRequest) pv.detailController?.abort();
  const controller = new AbortController();
  pv.detailController = controller;
  pv.vodSourceStatus = pv.vodSources.length ? "refreshing" : "loading";
  pv.vodSourceMessage = pv.vodSources.length ? "正在更新可用信号源…" : "正在加载可用信号源…";
  renderMorePanel();
  const ref = vodIdentity(ctx.vid);
  const path = vodDetailPath(ctx.vid) + (ref.kind === "hhkan" ? "?enrich=1" : "?refresh=1");
  let request = null;
  request = fetchJSON(BASE + path, { timeout: 90000, signal: controller.signal })
    .then(raw => {
      if (!isCurrentRequest(request) || !pv.ctx || pv.ctx.kind !== "vod") return null;
      if (!raw || !raw.title) throw new Error((raw && raw.error) || "信号源详情不可用");
      const fresh = {
        ...raw,
        cover: raw.poster_url || "",
        score: +raw.rating || 0,
        area: (raw.regions || []).join(" / "),
        tags: raw.genres || [],
        desc: raw.summary || "",
        meta: [raw.year, ...(raw.regions || []), ...(raw.genres || [])].filter(Boolean).join(" / "),
        actors: (raw.actors || []).join(" / "),
        sources: normalizePlayerVodSources(raw),
      };
      const oldLines = pv.vodSources;
      const oldHasRealLines = oldLines.some(line => !String(line.id || "").startsWith("current:"));
      const oldLine = oldLines[pv.vodLineIdx];
      const sourceKey = (pv.ctx && pv.ctx.sourceKey) || playerVodSourceKey(oldLine, pv.vodLineIdx);
      let currentLineMissing = false;
      let currentEpisodeMissing = false;
      if (fresh.sources.length) {
        pv.vodSources = fresh.sources;
        const exactIndex = exactPlayerVodLineIndex(fresh.sources, pv.ctx.playRef, sourceKey, oldLine);
        if (exactIndex >= 0) pv.vodLineIdx = exactIndex;
        else if (oldLine) {
          currentLineMissing = true;
          pv.vodSources = [...fresh.sources, oldLine];
          pv.vodLineIdx = pv.vodSources.length - 1;
          pv.vodSourceMessage = "线路列表已更新，保留当前播放线路";
        } else pv.vodLineIdx = 0;
      } else if (!oldLines.length) pv.vodSources = [];
      pv.vodSourceStatus = fresh.sources.length ? "ready" : (oldHasRealLines ? "error" : "empty");
      if (fresh.sources.length && !pv.vodSourceMessage.startsWith("线路列表已更新"))
        pv.vodSourceMessage = playerVodReadyMessage();
      else if (!fresh.sources.length) pv.vodSourceMessage =
        oldHasRealLines ? "刷新未返回可用信号源，保留当前线路" : "暂未匹配到其他信号源，可点击当前线路重试";
      if (detail && String(detail.vid) === String(ctx.vid)) {
        detail = { ...detail, ...fresh, vid: ctx.vid };
      }
      let selected = pv.vodSources[pv.vodLineIdx];
      const selectedName = String(pv.ctx.epName || "").trim();
      const refIndex = playerVodEpisodeIndex(selected, pv.ctx.playRef);
      const refName = refIndex >= 0
        ? String(((selected.episodes || [])[refIndex] || {}).ep
            || ((selected.episodes || [])[refIndex] || {}).name || "").trim()
        : "";
      const refMatchesSelectedName = !selectedName || refName === selectedName;
      let episodeIndex = playerVodEpisodeIndexForRefresh(selected, pv.ctx.playRef, selectedName);
      /* A detail refresh can race the first play request. If the refreshed
         catalogue changed its playRef while the current request is pending,
         keep the old line/ref until that request settles; otherwise the video
         can attach the old URL while the panel and recovery state point at the
         new episode reference. A later refresh can safely remap by episode. */
      if ((episodeIndex < 0 || !refMatchesSelectedName) && pv.loadPending && !options.allowPendingRemap
          && oldLine && selected !== oldLine) {
        const currentName = String(pv.ctx.epName || "").trim();
        const nameStillExists = currentName && selected && selected.episodes.some(ep =>
          String(ep.ep || ep.name || "").trim() === currentName);
        currentEpisodeMissing = !nameStillExists;
        pv.vodSources = [...pv.vodSources, oldLine];
        pv.vodLineIdx = pv.vodSources.length - 1;
        selected = oldLine;
        episodeIndex = playerVodEpisodeIndexForRefresh(selected, pv.ctx.playRef, pv.ctx.epName);
        pv.vodSourceMessage = "线路列表已更新，保留当前播放线路";
      }
      if (episodeIndex < 0 && oldLine && selected !== oldLine) {
        currentEpisodeMissing = true;
        pv.vodSources = [...pv.vodSources, oldLine];
        pv.vodLineIdx = pv.vodSources.length - 1;
        selected = oldLine;
        episodeIndex = playerVodEpisodeIndexForRefresh(selected, pv.ctx.playRef, pv.ctx.epName);
      }
      if (episodeIndex >= 0) {
        const current = selected.episodes[episodeIndex];
        pv.ctx.epIdx = episodeIndex;
        pv.ctx.epCount = selected.episodes.length;
        pv.ctx.epName = current.ep || `第${episodeIndex + 1}集`;
        pv.ctx.playRef = { pid: current.pid, vid: current.vid };
        pv.ctx.sourceKey = playerVodSourceKey(selected, pv.vodLineIdx);
        pv.ctx.lineIndex = pv.vodLineIdx;
      }
      if (currentLineMissing || currentEpisodeMissing || episodeIndex < 0) {
        pv.vodSourceStatus = "error";
        pv.vodSourceMessage = currentEpisodeMissing || episodeIndex < 0
          ? `当前集“${pv.ctx.epName || "所选集"}”已不在刷新后的线路中，保留原播放选择`
          : "当前线路已不在刷新后的信号源列表中，保留原播放选择";
      } else if (fresh.sources.length) {
        pv.vodSourceStatus = "ready";
        if (!pv.vodSourceMessage.startsWith("线路列表已更新"))
          pv.vodSourceMessage = playerVodReadyMessage();
      }
      if (detail && String(detail.vid) === String(ctx.vid)) {
        detail.sources = pv.vodSources;
        dLineIdx = pv.vodLineIdx;
        dEpIdx = pv.ctx.epIdx;
        renderDetailEps();
      }
      renderMorePanel();
      return fresh;
    })
    .catch(error => {
      if (!isCurrentRequest(request) || (error && error.name === "AbortError")) return null;
      pv.vodSourceStatus = "error";
      pv.vodSourceMessage = pv.vodSources.length
        ? "信号源更新失败，保留现有线路，可点击线路重试"
        : "信号源加载失败，请重试";
      renderMorePanel();
      return null;
    })
    .finally(() => {
      if (pv.detailRequest && pv.detailRequest.promise === request) {
        pv.detailRequest = null;
        if (pv.detailController === controller) pv.detailController = null;
      }
    });
  pv.detailRequest = { session, promise: request };
  return request;
}

/* 播放地址解析缓存（v1.32 性能）：{key: {t,d} 已完成 | {p} 在途}，
 * 详情打开即预解析起播集、播放中预解析下一集 → 点播/换集秒起。
 * 在途 Promise 去重防并发重复打后端；TTL 略短于后端 play 解析缓存（5min）。 */
const playResolve = { map: new Map(), TTL: 4 * 60 * 1000, MAX: 40, seq: 0 };
function resolvePlayCached(vid, pid, evid, options = {}) {
  const key = `${vid}:${pid}:${evid}`;
  const hit = playResolve.map.get(key);
  if (hit && !options.force) {
    if (hit.p && !(hit.signal && hit.signal.aborted)) return hit.p; /* 在途：直接复用 */
    if (hit.p && hit.signal && hit.signal.aborted) playResolve.map.delete(key);
    if (Date.now() - hit.t < playResolve.TTL) return Promise.resolve(hit.d);
    playResolve.map.delete(key);
  }
  if (options.force) playResolve.map.delete(key);
  const token = ++playResolve.seq;
  const playPath = vodPlayPath(vid, pid, evid) + (options.force ? "?fresh=1" : "");
  const p = fetchJSON(BASE + playPath, { timeout: 60000, signal: options.signal })
    .then(d => {
      if (d && d.url && !d.sources) d.sources = [{ name: d.name || "自动优选", url: d.url }];
      if (!d || !d.sources || !d.sources.length) throw new Error((d && d.error) || "未解析到播放地址");
      const current = playResolve.map.get(key);
      if (current && current.token === token) playResolve.map.set(key, { token, t: Date.now(), d });
      if (playResolve.map.size > playResolve.MAX)                  /* FIFO 淘汰最旧 */
        playResolve.map.delete(playResolve.map.keys().next().value);
      return d;
    })
    .catch(e => {
      const current = playResolve.map.get(key);
      if (current && current.token === token) playResolve.map.delete(key);
      throw e;
    });
  playResolve.map.set(key, { token, p, signal: options.signal || null });
  return p;
}
function bustResolvedPlay(ctx) {
  if (!ctx || !ctx.playRef) return;
  playResolve.map.delete(`${ctx.vid}:${ctx.playRef.pid}:${ctx.playRef.vid}`);
}
function prefetchPlay(vid, pid, evid) {
  if (pid == null || evid == null) return;
  resolvePlayCached(vid, pid, evid).catch(() => {});
}

async function resolveVodAndPlay(ctx, loadGen = pv.loadGen, options = {}) {
  pv.loadPending = true;
  metricMark("prepare");
  telemetryEmit(TELEMETRY_EVENTS.PLAYER_PREPARE, { kind: "prepare" });
  try {
    /* resolver 已给出首选线路时直接用它起播，不再等所有线路（方案 §5 快速起播）。 */
    const d = await resolveFirstSource(ctx, options);
    if (!playerSessionIsCurrent(pv.playerSession, ctx) || pv.loadGen !== loadGen) return;
    pv.loadPending = false;
    pv.vodSourceStatus = "ready";
    pv.vodSourceMessage = playerVodReadyMessage();
    pv.sources = (d.sources || []).filter(x => x && x.url && x.alive !== false).map(x => x.url);
    if (!pv.sources.length) throw new Error("未解析到播放地址");
    pv.srcIdx = 0;
    renderMorePanel();
    vodAttach(pv.sources[0], ctx.resume || 0);
    pushHistory({ id: ctx.vid, title: ctx.title, cover: ctx.cover || "", epIndex: ctx.epIdx || 0 });
    if ("mediaSession" in navigator)
      navigator.mediaSession.metadata = new MediaMetadata({ title: ctx.title, artist: ctx.epName || "" });
  } catch (e) {
    if (!playerSessionIsCurrent(pv.playerSession, ctx) || pv.loadGen !== loadGen) return;
    pv.loadPending = false;
    const lines = pv.vodSources || [];
    const next = chooseNextVodLine(ctx, lines);
    if (next) {
      const failedIndex = playerVodLineIndex(lines, ctx.playRef, ctx.sourceKey, ctx.lineIndex);
      const failedKey = playerVodSourceKey(lines[failedIndex], failedIndex);
      const nextLine = lines[next.index];
      const nextCtx = {
        ...ctx,
        failedLineIds: [...(ctx.failedLineIds || []), failedKey],
        playRef: next.playRef,
        sourceKey: playerVodSourceKey(nextLine, next.index),
        lineIndex: next.index,
        linePinned: true,
      };
      pv.ctx = nextCtx;
      pv.vodLineIdx = next.index;
      pv.loadPending = true;
      dLineIdx = next.index;
      renderMorePanel();
      resolveVodAndPlay(nextCtx, loadGen, { force: true });
      return;
    }
    playerFatal(e, loadGen);
  }
}

/* 后端解析并探测当前比赛的全部线路；选择器只展示返回的 HLS 可用项。 */
async function resolveLiveAndPlay(ctx, session = pv.playerSession) {
  if (!ctx || !ctx.matchId) { playerFatal("比赛数据缺失，请刷新重试"); return; }
  try {
    const matchId = encodeURIComponent(ctx.matchId);
    const d = await fetchJSON(BASE + `/api/stream/${matchId}?src=all&fresh=1`, { timeout: 60000 });
    if (pv.playerSession !== session || pv.ctx !== ctx) return;
    const seen = new Set();
    const routes = (Array.isArray(d.sources) ? d.sources : []).filter(route => {
      const source = String((route && route.source) || "").trim();
      if (!route || route.player !== "m3u8" || !route.url || !source || seen.has(source)) return false;
      seen.add(source);
      return true;
    });
    if (!routes.length) throw new Error((d && d.error) || "没有检测到可播放线路");
    pv.sources = routes.map(route => route.url);
    pv.srcIds = routes.map(route => String(route.source));
    pv.srcNames = routes.map(route => String(route.name || route.source));
    pv.sourceRefs = routes.map(route => route.referer || "https://www.88zhibo.tv/");
    pv.srcIdx = 0;
    renderSrcPicker();
    attachPlayer(relay(pv.sources[0], pv.sourceRefs[0]), 0);
  } catch (e) {
    if (pv.playerSession !== session || pv.ctx !== ctx) return;
    playerFatal(e && e.message ? e.message : "信号尚未开播（开赛后自动可用），可稍后重试");
  }
}

function renderSrcPicker() {
  if (!pv.ctx || pv.ctx.kind !== "live") return;
  $("#srcPicker").innerHTML = `<div class="sp-title">信号源</div><div class="sp-chips">` +
    pv.srcNames.map((n, i) =>
      `<button class="spchip ${i === pv.srcIdx ? "on" : ""}" data-i="${i}">${esc(srcLabel(n))}</button>`).join("") + `</div>`;
  $$("#srcPicker .spchip").forEach(c => c.addEventListener("click", async () => {
    pv.srcIdx = +c.dataset.i;
    renderSrcPicker();
    $("#bufSpin").hidden = false;
    const gen = ++pv.recoveryGen;
    try {
      const url = await resolveFreshLiveSource(pv.srcIdx);
      if (gen === pv.recoveryGen) attachPlayer(relay(url, pv.sourceRefs[pv.srcIdx]), 0);
    } catch (e) { if (gen === pv.recoveryGen) recoverPlayback("手动切源失败", gen); }
  }));
  $("#srcPickerWrap").hidden = !pv.picker;
  syncPlayerBackBtn();
}

async function resolveFreshLiveSource(index) {
  const ctx = pv.ctx;
  if (!ctx || ctx.kind !== "live") throw new Error("not live");
  const src = pv.srcIds[index];
  if (!src) throw new Error("missing live source");
  const matchId = encodeURIComponent(ctx.matchId || "");
  const d = await fetchJSON(BASE + `/api/stream/${matchId}?src=${encodeURIComponent(src)}&fresh=1`, { timeout: 60000 });
  if (pv.ctx !== ctx || pv.srcIds[index] !== src) throw new Error("live source changed");
  if (!d.url) throw new Error(d.error || "no url");
  pv.sources[index] = d.url;
  pv.sourceRefs[index] = d.referer || "https://www.88zhibo.tv/";
  return d.url;
}

function attachPlayer(url, resume, opts) {
  const v = pv.video, ctx = pv.ctx;
  const gen = ++pv.recoveryGen;
  const opt = opts || {};
  pv.recovering = false;
  /* 起播瞬间引擎降级（hls.js 不可用 → 原生 HLS，或直连 → 中继）都记一笔。 */
  const engine = window.Hls && Hls.isSupported() ? "hlsjs" : "native";
  telemetryEmit(TELEMETRY_EVENTS.ENGINE_FALLBACK, { engine: engine, kind: "player" });
  $("#bufSpin").hidden = false;
  if (pv.hls) { pv.hls.destroy(); pv.hls = null; }
  v.removeAttribute("src");
  if (window.Hls && Hls.isSupported()) {
    /* v1.22 卡顿优化：maxBufferLength 30→45——经中继的直播分片供给有波动，
       更大前向缓冲在 CDN 抖动时不易穿透成可感知暂停 */
    pv.hls = new Hls({ maxBufferLength: 45, maxMaxBufferLength: 90 });
    pv.hls.loadSource(url); pv.hls.attachMedia(v);
    pv.hls.on(Hls.Events.ERROR, (_, d) => {
      if (!d.fatal || gen !== pv.recoveryGen) return;
      /* 直连失败（CDN 要 Referer / 不给 CORS）→ 同一线路先回中继，别浪费一次恢复额度 */
      if (opt.direct && opt.raw) {
        attachPlayer(relay(opt.raw, "none"), resume, { raw: opt.raw });
        return;
      }
      recoverPlayback(`HLS ${d.type || "error"}/${d.details || "unknown"}`, gen);
    });
  } else {
    v.src = url;
    v.onerror = () => {
      if (gen !== pv.recoveryGen) return;
      if (opt.direct && opt.raw) { attachPlayer(relay(opt.raw, "none"), resume, { raw: opt.raw }); return; }
      recoverPlayback("原生播放器错误", gen);
    };
  }
  v.onloadedmetadata = () => {
    if (gen !== pv.recoveryGen) return;
    $("#bufSpin").hidden = true;
    if (resume > 0 && ctx.kind === "vod" && (!v.duration || resume < v.duration - 30)) {
      v.currentTime = resume; hint("已从 " + fmtTime(resume) + " 继续播放");
    }
  };
  v.onplaying = () => {
    if (gen !== pv.recoveryGen) return;
    pv.recoveryAttempts = 0; pv.recovering = false;
    $("#bufSpin").hidden = true; setPlayIcon(true);
    metricBuffer(false);
    if (metrics.marks.click != null) {
      const first = metricDelta(metrics.marks.click);
      telemetryEmit(TELEMETRY_EVENTS.FIRST_FRAME, {
        kind: "playback", first_frame_ms: first || 0,
        total_click_to_frame_ms: first || 0, prepare_ms: metricDelta(metrics.marks.prepare) || 0,
      });
      metrics.marks.click = null;
    } else {
      metricBuffer(false);
    }
  };
  v.onpause = () => { if (gen === pv.recoveryGen) setPlayIcon(false); };
  v.onended = () => { if (gen === pv.recoveryGen) onEnded(); };
  v.ontimeupdate = () => { if (gen === pv.recoveryGen) updatePb(); };
  v.play().catch(() => {});
  clearInterval(pv.progTimer);
  pv.progTimer = setInterval(() => saveCurProgress(), 5000);
  clearInterval(pv.stallTimer);
  let lastPos = -1, stuckTicks = 0;
  pv.stallTimer = setInterval(() => {
    if (gen !== pv.recoveryGen || !pv.ctx || v.paused || v.ended) return;
    const buffering = v.readyState < 3;
    /* readyState<3 = 正在缓冲；这只在起播/续播窗口内才上报，避免长视频常态抖动刷屏。 */
    if (metrics.marks.click != null) metricBuffer(buffering);
    stuckTicks = buffering || Math.abs(v.currentTime - lastPos) < 0.05 ? stuckTicks + 1 : 0;
    lastPos = v.currentTime;
    if (stuckTicks >= 8) { stuckTicks = 0; recoverPlayback("播放 8 秒无进展", gen); }
  }, 1000);
  applyAspect();
}

async function recoverPlayback(reason, expectedGen) {
  if (!pv.ctx || pv.recovering || (expectedGen != null && expectedGen !== pv.recoveryGen)) return;
  const recoveryGen = pv.recoveryGen;
  const ctx = pv.ctx;
  pv.recovering = true;
  const attempt = pv.recoveryAttempts++;
  telemetryEmit(TELEMETRY_EVENTS.AUTO_SWITCH, {
    attempt: attempt + 1, error_class: classifyTelemetryError(new Error(reason)),
    kind: "recovery",
  });
  const recoveryLimit = ctx.kind === "vod"
    ? Math.max(MAX_RECOVERY_ATTEMPTS, pv.vodSources.length + 2) : MAX_RECOVERY_ATTEMPTS;
  if (attempt >= recoveryLimit) {
    pv.recovering = false;
    playerFatal("播放持续卡顿，自动恢复已达上限");
    return;
  }
  const resume = ctx.kind === "vod" && pv.video ? Math.max(0, pv.video.currentTime - 2) : 0;
  hint(`正在恢复播放（${attempt + 1}/${recoveryLimit}）`);
  try {
    if (ctx.kind === "live") {
      if (attempt === 1 && pv.sources.length > 1) pv.srcIdx = (pv.srcIdx + 1) % pv.sources.length;
      const url = await resolveFreshLiveSource(pv.srcIdx);
      if (pv.ctx !== ctx || pv.recoveryGen !== recoveryGen) return;
      pv.recovering = false;
      attachPlayer(relay(url, pv.sourceRefs[pv.srcIdx]), 0);
      renderSrcPicker();
      return;
    }
    if (attempt >= 2) {
      const lines = pv.vodSources || [];
      const next = chooseNextVodLine(ctx, lines);
      if (next) {
        const failedIndex = playerVodLineIndex(lines, ctx.playRef, ctx.sourceKey, ctx.lineIndex);
        const failedKey = playerVodSourceKey(lines[failedIndex], failedIndex);
        const nextLine = lines[next.index];
        const nextCtx = { ...ctx,
          failedLineIds: [...(ctx.failedLineIds || []), failedKey],
          playRef: next.playRef, linePinned: true, lineIndex: next.index,
          sourceKey: playerVodSourceKey(nextLine, next.index),
        };
        if (pv.ctx !== ctx || pv.recoveryGen !== recoveryGen) return;
        pv.ctx = nextCtx;
        pv.vodLineIdx = next.index;
        dLineIdx = next.index;
        bustResolvedPlay(ctx);
        const switched = await resolvePlayCached(ctx.vid, next.playRef.pid, next.playRef.vid,
          { force: true, signal: pv.playController && pv.playController.signal });
        if (pv.ctx !== nextCtx || pv.recoveryGen !== recoveryGen) return;
        const urls = (switched.sources || []).filter(x => x && x.url && x.alive !== false).map(x => x.url);
        if (!urls.length) throw new Error("下一线路没有播放地址");
        pv.sources = urls; pv.srcIdx = 0;
        pv.recovering = false;
        renderMorePanel();
        vodAttach(urls[0], resume);
        return;
      }
    }
    if (attempt === 0 || attempt === 1) {
      bustResolvedPlay(ctx);
      const d = await resolvePlayCached(ctx.vid, ctx.playRef.pid, ctx.playRef.vid,
        { force: true, signal: pv.playController && pv.playController.signal });
      if (pv.ctx !== ctx || pv.recoveryGen !== recoveryGen) return;
      const fresh = (d.sources || []).filter(x => x && x.url && x.alive !== false).map(x => x.url);
      if (!fresh.length) throw new Error("未解析到播放地址");
      pv.sources = fresh;
      if (attempt === 1 && fresh.length > 1) pv.srcIdx = (pv.srcIdx + 1) % fresh.length;
    } else if (attempt === 2 && pv.sources.length > 1) {
      pv.srcIdx = (pv.srcIdx + 1) % pv.sources.length;
    }
    pv.recovering = false;
    vodAttach(pv.sources[pv.srcIdx], resume);
  } catch (e) {
    if (pv.ctx !== ctx || pv.recoveryGen !== recoveryGen) return;
    pv.recovering = false;
    recoverPlayback(reason + ": " + e.message, pv.recoveryGen);
  }
}

function chooseNextVodLine(ctx, lines) {
  if (!ctx || !ctx.playRef || !Array.isArray(lines)) return null;
  const currentIndex = playerVodLineIndex(lines, ctx.playRef, ctx.sourceKey, ctx.lineIndex);
  const tried = new Set([...(ctx.failedLineIds || []).map(String)]);
  if (currentIndex >= 0) tried.add(playerVodSourceKey(lines[currentIndex], currentIndex));
  else tried.add(String(ctx.playRef.pid));
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (!line) continue;
    const episodes = line.episodes || [];
    const lineId = playerVodSourceKey(line, index);
    if (tried.has(lineId)) continue;
    const ep = episodes[ctx.epIdx] || episodes[0];
    if (ep && ep.pid != null && ep.vid != null)
      return { index, playRef: { pid: ep.pid, vid: ep.vid } };
  }
  return null;
}

function nextSource() {
  pv.srcIdx++;
  telemetryEmit(TELEMETRY_EVENTS.AUTO_SWITCH, {
    attempt: pv.srcIdx + 1, kind: "source_switch",
  });
  if (pv.srcIdx < pv.sources.length) {
    hint("线路失效，切换下一个源");
    if (pv.ctx.kind === "vod") vodAttach(pv.sources[pv.srcIdx], 0);
    else attachPlayer(relay(pv.sources[pv.srcIdx], pv.sourceRefs[pv.srcIdx]), 0);
    if (pv.ctx.kind === "live") renderSrcPicker();
  } else if (pv.ctx.kind === "vod" && pv.ctx.epIdx < pv.ctx.epCount - 1) {
    hint("本集所有源失败，尝试下一集");
    pv.ctx.epIdx++;
    startPlayFromCtx();
  } else {
    playerFatal("播放失败：所有线路均不可用");
  }
}
/* 换集/换线：openPlayer 复位会清 srcIdx——换线保进度（App 跨线路续播） */
function startPlayFromCtx(keepPos) {
  const c = pv.ctx;
  const pos = keepPos && pv.video && pv.video.duration && pv.video.currentTime > 1 &&
    pv.video.currentTime < pv.video.duration - 30 ? pv.video.currentTime : 0;
  const line = pv.vodSources[pv.vodLineIdx];
  const ep = line && line.episodes && line.episodes[c.epIdx];
  openPlayer({ ...c, sources: pv.vodSources, lineIndex: pv.vodLineIdx,
    sourceKey: playerVodSourceKey(line, pv.vodLineIdx),
    playRef: ep ? { pid: ep.pid, vid: ep.vid } : c.playRef, resume: pos });
}
function playerSourceName(line, index) {
  return String((line && (line.name || line.source_name)) || `信号源${index + 1}`);
}
function playerVodReadyMessage() {
  return pv.vodSources.length === 1
    ? "仅有 1 个信号源，可再次点击当前线路重试"
    : `${pv.vodSources.length} 个可用信号源，点击切换；再次点击当前线路可重试`;
}
function startVodPlayback(ctx, options = {}) {
  if (!ctx || ctx.kind !== "vod" || !pv.ctx || pv.ctx.kind !== "vod") return;
  if (!options.skipSave) saveCurProgress();
  const resume = Number.isFinite(options.resume) ? Math.max(0, options.resume) : 0;
  pv.playController?.abort();
  stopVodMediaPipeline();
  pv.loadGen++;
  const loadGen = pv.loadGen;
  pv.recoveryGen++;
  pv.recoveryAttempts = 0;
  pv.recovering = false;
  pv.sources = [];
  pv.srcIdx = 0;
  pv.ctx = { ...ctx, resume };
  const playCtx = pv.ctx;
  pv.loadPending = true;
  pv.playController = new AbortController();
  pv.vodLineIdx = playerVodLineIndex(pv.vodSources, pv.ctx.playRef,
    pv.ctx.sourceKey, pv.ctx.lineIndex);
  pv.ctx.lineIndex = pv.vodLineIdx;
  pv.ctx.sourceKey = playerVodSourceKey(pv.vodSources[pv.vodLineIdx], pv.vodLineIdx);
  if (detail && String(detail.vid) === String(pv.ctx.vid)) {
    dLineIdx = pv.vodLineIdx;
    dEpIdx = pv.ctx.epIdx;
    renderDetailEps();
  }
  $("#pError").hidden = true;
  $("#bufSpin").hidden = false;
  setPlayIcon(false);
  renderMorePanel();
  syncPlayerSourcePanel();
  const force = options.force !== false;
  const identity = vodIdentity(playCtx.vid);
  if (force && identity.kind === "douban") {
    const session = pv.playerSession;
    refreshPlayerEpisodes({ session, force: true, loadGen, allowPendingRemap: true }).then(fresh => {
      if (!playerSessionIsCurrent(session, playCtx) || pv.loadGen !== loadGen) return;
      /* A manual retry must not silently use a removed or stale selection when
         detail refresh failed. An empty source list can still use an explicit
         current-line reference, but a failed/ambiguous remap must stop here. */
      if (!fresh || pv.vodSourceStatus === "error") {
        const message = pv.vodSourceMessage || "信号源详情刷新失败，请重试";
        playerFatal(message, loadGen);
        pv.vodSourceStatus = "error";
        pv.vodSourceMessage = message;
        renderMorePanel();
        return;
      }
      pv.vodSourceStatus = "loading";
      pv.vodSourceMessage = `正在解析 ${playerSourceName(pv.vodSources[pv.vodLineIdx], pv.vodLineIdx)}…`;
      renderMorePanel();
      resolveVodAndPlay(playCtx, loadGen, { force: true });
    });
    return;
  }
  resolveVodAndPlay(playCtx, loadGen, { force });
}
function selectVodLine(index) {
  if (!pv.ctx || pv.ctx.kind !== "vod" || !Number.isInteger(index)) return false;
  const line = pv.vodSources[index];
  const episodes = (line && line.episodes) || [];
  if (!line || !episodes.length) {
    pv.vodSourceStatus = "empty";
    pv.vodSourceMessage = "该信号源没有可播放集";
    renderMorePanel();
    return false;
  }
  const current = pv.ctx;
  const retryingCurrent = index === pv.vodLineIdx;
  const currentName = String(current.epName || "").trim();
  let epIdx = currentName
    ? episodes.findIndex(ep => String(ep.ep || ep.name || "").trim() === currentName) : -1;
  if (epIdx < 0) epIdx = Math.max(0, Math.min(Number(current.epIdx) || 0, episodes.length - 1));
  const ep = episodes[epIdx];
  const sameEpisode = epIdx === current.epIdx;
  const v = pv.video;
  const resume = sameEpisode && v && v.duration && v.currentTime > 1
    && v.currentTime < v.duration - 30 ? v.currentTime : 0;
  const nextCtx = {
    ...current,
    lineIndex: index,
    sourceKey: playerVodSourceKey(line, index),
    lineName: playerSourceName(line, index),
    epIdx,
    epCount: episodes.length,
    epName: ep.ep || ep.name || `第${epIdx + 1}集`,
    playRef: { pid: ep.pid, vid: ep.vid },
    linePinned: true,
    failedLineIds: [],
  };
  pv.vodLineIdx = index;
  pv.vodSourceStatus = "loading";
  pv.vodSourceMessage = retryingCurrent ? "正在重新加载当前信号源…"
    : `正在切换到 ${nextCtx.lineName}…`;
  startVodPlayback(nextCtx, { resume, force: true });
  return true;
}
function playerFatal(msg, expectedLoadGen) {
  if (expectedLoadGen != null && expectedLoadGen !== pv.loadGen) return;
  if (pv.ctx && pv.ctx.kind === "vod") {
    const v = pv.video;
    if (v && v.duration && v.currentTime > 1 && v.currentTime < v.duration - 30)
      pv.ctx.resume = v.currentTime;
    saveCurProgress();
    stopVodMediaPipeline();
    pv.loadPending = false;
  }
  /* 埋点只取错误分类，不取原始文案（可能含上游拓扑）。 */
  const errClass = msg instanceof Error ? classifyTelemetryError(msg) : "unknown";
  telemetryEmit(TELEMETRY_EVENTS.FATAL, { error_class: errClass, kind: "player" });
  $("#bufSpin").hidden = true;
  $("#peMsg").textContent = (msg && msg.message) || msg || "播放失败";
  $("#pError").hidden = false;
  $("#controls").hidden = true;
  $("#ctlBack").hidden = true; $("#srcPickerWrap").hidden = true;
  if (pv.ctx && pv.ctx.kind === "vod") {
    pv.vodSourceStatus = "error";
    pv.vodSourceMessage = "当前播放失败，可切换信号源或重试当前线路";
    if (state.portrait) pv.panelOpen = true;
    renderMorePanel();
    syncPlayerSourcePanel();
  } else {
    $("#morePanel").hidden = true;
    $("#mpToggle").hidden = true;
  }
}
$("#peRetry").addEventListener("click", () => {
  const c = pv.ctx;
  if (c && c.kind === "vod") {
    const v = pv.video;
    const resume = v && v.duration && v.currentTime > 1 && v.currentTime < v.duration - 30
      ? v.currentTime : (c.resume || 0);
    startVodPlayback({ ...c, linePinned: true }, { resume, force: true });
  } else if (c) openPlayer(c);
});
$("#peBack").addEventListener("click", closePlayer);

function onEnded() {
  saveCurProgress(true);
  const c = pv.ctx;
  if (c.kind === "vod" && c.epIdx < c.epCount - 1) { hint("自动播放下一集"); pv.ctx.epIdx++; startPlayFromCtx(); }
  else closePlayer();
}

function saveCurProgress(clear) {
  const c = pv.ctx;
  if (!c || c.kind !== "vod" || !pv.video || !pv.video.duration) return;
  if (clear) { localStorage.removeItem("otvw:prog:vod:" + c.vid); return; }
  store.set("otvw:ep:" + c.vid, c.epIdx);
  saveProgress("vod:" + c.vid, pv.video.currentTime, pv.video.duration);
}
document.addEventListener("visibilitychange", () => { if (document.hidden) saveCurProgress(); });
addEventListener("pagehide", () => saveCurProgress());

/* 控制条 */
function setPlayIcon(playing) {
  $("#cPlayIco").innerHTML = playing ? `<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>` : `<path d="M8 5v14l11-7z"/>`;
}
/* 5s 无操作自动隐藏（App tick 5000ms）；竖屏常驻控制条只收浮层（需求12⑥） */
/* 返回钮与「控制条/信号源条」同现同隐（index.html 注释：返回钮在控制条或信号源条可见时出现在左上角）。
   横屏直播播放态视频铺满整屏，信号源条上的返回钮是唯一可见的退出入口。 */
function syncPlayerBackBtn() {
  if (!pv.ctx) return;
  $("#ctlBack").hidden = !(pv.picker || pv.controls);
}
function syncPlayerSourcePanel() {
  const isVod = !!(pv.ctx && pv.ctx.kind === "vod");
  const isPortrait = !!state.portrait;
  $("#mpToggle").hidden = !isVod || isPortrait || pv.panelOpen;
  $("#mpToggle").setAttribute("aria-expanded", isVod && pv.panelOpen ? "true" : "false");
  if (isVod) $("#morePanel").hidden = !pv.panelOpen;
  else if (!isPortrait) $("#morePanel").hidden = true;
}
function setPlayerSourcePanel(open) {
  if (!pv.ctx || pv.ctx.kind !== "vod") return;
  pv.panelOpen = state.portrait ? true : !!open;
  if (pv.panelOpen) renderMorePanel();
  syncPlayerSourcePanel();
  if (pv.panelOpen) syncPlayerRouteDisclosure();
}
$("#mpToggle").addEventListener("click", () => setPlayerSourcePanel(true));
$("#mpClose").addEventListener("click", () => setPlayerSourcePanel(false));
$("#mpLinesToggle").addEventListener("click", () => {
  pv.routeLinesExpanded = !pv.routeLinesExpanded;
  syncPlayerRouteDisclosure();
});
addEventListener("resize", syncPlayerRouteDisclosure);
$("#mpSourceRetry").addEventListener("click", () => refreshPlayerEpisodes({ force: true }));
function armHide() {
  clearTimeout(pv.hideTimer);
  pv.hideTimer = setTimeout(() => {
    if (pv.video && pv.video.paused) return;
    if (pv.picker) { pv.picker = false; $("#srcPickerWrap").hidden = true; syncPlayerBackBtn(); }
    else if (pv.controls && !state.portrait) hideControls();
  }, 5000);
}
function showControls() {
  if (!pv.ctx) return;
  if (pv.ctx.kind === "live" && !state.portrait) { pv.picker = true; renderSrcPicker(); armHide(); return; }
  pv.controls = true;
  $("#controls").hidden = false;
  /* 竖屏：信息面板常驻于视频下方，控制浮层只覆盖视频区域 */
  if (!state.portrait && !(pv.ctx.kind === "vod" && pv.panelOpen)) $("#morePanel").hidden = true;
  $("#ctlBack").hidden = false;
  armHide();
}
function hideControls() {
  pv.controls = false;
  $("#controls").hidden = true;
  $("#ctlBack").hidden = true;
}
$("#cPlayBtn").addEventListener("click", () => {
  const v = pv.video; if (!v) return;
  v.paused ? v.play().catch(() => {}) : v.pause();
  if (pv.ctx && pv.ctx.kind === "vod") showControls(); else armHide();
});
function updatePb() {
  const v = pv.video; if (!v || pv.seeking) return;
  const isLive = pv.ctx && pv.ctx.kind === "live";
  if (v.duration && !isLive) setPlayerProgress(v.currentTime / v.duration);
  setPlayerPositionText(v.currentTime);
  $("#cDur").textContent = isLive ? "直播" : "-" + fmtTime((v.duration || 0) - v.currentTime);
  /* 观看时长里程碑：每次播放会话各报一次（直播不报时长）。 */
  if (!isLive) {
    if (v.currentTime >= 1800) metricMilestone("PLAY_30MIN");
    else if (v.currentTime >= 300) metricMilestone("PLAY_5MIN");
  }
}
function setPlayerProgress(value) {
  const fraction = Math.max(0, Math.min(1, Number(value) || 0));
  const rail = $("#cFill").parentElement;
  rail.style.setProperty("--progress-scale", fraction);
  rail.style.setProperty("--progress-position", (fraction * 100) + "%");
}
function setPlayerPositionText(seconds) {
  const label = fmtTime(seconds);
  const position = $("#cPos");
  if (position.textContent !== label) position.textContent = label;
}
/* 进度条：点按 + 拖动 scrub（App detectTapGestures + detectHorizontalDragGestures） */
(function () {
  const track = $("#cTrack");
  const rail = track.querySelector(".ctl-rail");
  let scrub = null, trackRect = null, scrubFrame = 0;
  const fracOf = (e, rect) => {
    return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  };
  const renderScrub = () => {
    scrubFrame = 0;
    if (scrub == null || !pv.video) return;
    setPlayerProgress(scrub);
    setPlayerPositionText(scrub * pv.video.duration);
  };
  const queueScrubRender = () => {
    if (!scrubFrame) scrubFrame = requestAnimationFrame(renderScrub);
  };
  const cancelScrub = () => {
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
    scrubFrame = 0;
    scrub = null; trackRect = null; pv.seeking = false;
    rail.classList.remove("seeking");
    if (pv.video && pv.video.duration) {
      setPlayerProgress(pv.video.currentTime / pv.video.duration);
      setPlayerPositionText(pv.video.currentTime);
    }
  };
  track.addEventListener("pointerdown", e => {
    const v = pv.video; if (!v || !v.duration) return;
    /* Map the actual visible rail endpoints to 0..100%; side padding only
       enlarges the hit target and naturally clamps to the nearest endpoint. */
    trackRect = rail.getBoundingClientRect();
    if (!trackRect.width) { trackRect = null; return; }
    track.setPointerCapture(e.pointerId);
    pv.seeking = true;
    rail.classList.add("seeking");
    scrub = fracOf(e, trackRect);
    queueScrubRender();
  });
  track.addEventListener("pointermove", e => {
    if (scrub == null || !trackRect) return;
    scrub = fracOf(e, trackRect);
    queueScrubRender();
  });
  track.addEventListener("pointerup", e => {
    if (scrub == null || !trackRect) return;
    const v = pv.video;
    if (!v || !v.duration) { cancelScrub(); return; }
    scrub = fracOf(e, trackRect);
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
    scrubFrame = 0;
    renderScrub();
    const targetTime = scrub * v.duration;
    scrub = null; trackRect = null; pv.seeking = false;
    rail.classList.remove("seeking");
    v.currentTime = targetTime;
    if (pv.ctx && pv.ctx.kind === "vod") showControls();
  });
  track.addEventListener("pointercancel", cancelScrub);
  track.addEventListener("lostpointercapture", () => { if (scrub != null) cancelScrub(); });
})();

/* 画幅（App aspectConstraint：原始 contain / 裁切填充 cover / 16:9 / 4:3 约束比例） */
function applyAspect() {
  const v = pv.video; if (!v) return;
  v.classList.remove("asp-fit", "asp-zoom", "asp-169", "asp-43");
  v.classList.add(["asp-fit", "asp-zoom", "asp-169", "asp-43"][pv.aspectIdx] || "asp-fit");
}
function closePlayer() {
  saveCurProgress();
  clearInterval(pv.progTimer);
  clearInterval(pv.stallTimer);
  clearTimeout(pv.hideTimer);
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  pv.playController?.abort(); pv.playController = null;
  pv.detailController?.abort(); pv.detailController = null; pv.detailRequest = null;
  if (pv.ctx && pv.ctx.kind === "vod") stopVodMediaPipeline();
  else {
    if (pv.hls) { pv.hls.destroy(); pv.hls = null; }
    if (pv.video) { pv.video.pause(); pv.video.removeAttribute("src"); pv.video.load(); pv.video.controls = false; }
  }
  $("#pPlayer").hidden = true; $("#morePanel").hidden = true; $("#mpToggle").hidden = true;
  $("#ctlBack").hidden = true;
  document.body.style.overflow = "";
  pv.recoveryGen++; pv.loadGen++; pv.playerSession++; pv.ctx = null;
  pv.controls = false; pv.picker = false; pv.recovering = false; pv.panelOpen = false;
  pv.vodSources = []; pv.vodLineIdx = 0; pv.vodSourceStatus = "idle"; pv.vodSourceMessage = "";
}

/* 需求12⑦：竖屏全屏按钮——全屏走系统原生播放器（video 全屏 + 原生控件） */
$("#ctlFull").addEventListener("click", async () => {
  const v = pv.video;
  if (!v) return;
  try {
    if (v.webkitEnterFullscreen) { v.webkitEnterFullscreen(); return; }   /* iOS 原生全屏播放器 */
    if (document.fullscreenElement) { await document.exitFullscreen(); return; }
    await v.requestFullscreen();
  } catch (e) {
    try { await $("#pPlayer").requestFullscreen(); } catch (e2) { hint("当前浏览器不支持全屏"); }
  }
});
document.addEventListener("fullscreenchange", () => {
  const v = pv.video;
  if (v) v.controls = !!document.fullscreenElement;   /* 全屏=交给系统播放器控件 */
});

/* 播放选项面板（右侧悬浮栏 / 竖屏视频下方）；线路项始终来自真实 detail sources。 */
function mpApplyEpisode(idx) {
  const s = pv.vodSources[pv.vodLineIdx];
  const eps = (s && s.episodes) || [];
  if (!pv.ctx || pv.ctx.kind !== "vod" || typeof idx !== "number" || idx < 0 || idx >= eps.length) return false;
  const ep = eps[idx];
  saveCurProgress();
  dEpIdx = idx; dEpPage = Math.floor(idx / 10);
  pv.ctx = {
    ...pv.ctx, epIdx: idx, epCount: eps.length,
    epName: ep.ep || ep.name || `第${idx + 1}集`,
    playRef: { pid: ep.pid, vid: ep.vid },
    lineIndex: pv.vodLineIdx,
    sourceKey: playerVodSourceKey(s, pv.vodLineIdx),
    linePinned: true,
  };
  if (detail && String(detail.vid) === String(pv.ctx.vid)) {
    dLineIdx = pv.vodLineIdx;
    detail = { ...detail, sources: pv.vodSources };
  }
  return true;
}

function renderMorePanel() {
  const c = pv.ctx; if (!c) return;
  const playerDetail = c.kind === "vod" && detail && String(detail.vid) === String(c.vid) ? detail : null;
  const line = c.kind === "vod" ? pv.vodSources[pv.vodLineIdx] : null;
  const eps = (line && line.episodes) || [];
  $("#mpPoster").src = img(c.cover || "");
  $("#mpTitle").textContent = c.title || "影片";
  $("#mpMeta").textContent = c.kind === "vod" ? (playerDetail ? playerDetail.meta || "" : "") : "足球直播";
  $("#mpPanelHead").hidden = c.kind !== "vod";
  const engineName = (window.Hls && Hls.isSupported()) ? "hls.js" : "原生 HLS";
  $("#mpLine").textContent = c.kind === "vod"
    ? (line ? `${playerSourceName(line, pv.vodLineIdx)} · ${pv.sources.length ? `播放地址 ${pv.srcIdx + 1}/${pv.sources.length}` : "正在解析"}` : "尚未选择信号源")
    : `线路${pv.srcIdx + 1}/${pv.sources.length} · ${engineName}`;
  $("#mpDesc").textContent = c.kind === "vod" && playerDetail ? playerDetail.desc || "" : "";
  $("#mpLinesSec").hidden = c.kind !== "vod";
  $("#mpSourceRetry").hidden = c.kind !== "vod";
  if (c.kind === "vod") {
    const state = pv.vodSourceStatus || "loading";
    const statusText = pv.vodSourceMessage || (state === "loading" || state === "refreshing"
      ? "正在加载可用信号源…" : state === "empty" ? "暂未匹配到可用信号源" : "");
    $("#mpSourceState").textContent = statusText;
    $("#mpSourceState").dataset.state = state;
    $("#mpSourceRetry").hidden = state !== "error" && state !== "empty";
    $("#mpSourceRetry").textContent = "重新检测信号源";
    $("#mpLines").innerHTML = pv.vodSources.map((source, i) => {
      const name = playerSourceName(source, i);
      const selected = i === pv.vodLineIdx;
      return `<button class="lchip ${selected ? "on" : ""}" data-i="${i}"
        aria-current="${selected ? "true" : "false"}"
        aria-label="${esc(selected ? `${name}，当前信号源，再次点击重试` : `切换到${name}`)}"
        title="${esc(selected ? "再次点击重试当前信号源" : `切换到${name}`)}">${esc(name)}</button>`;
    }).join("");
    $$("#mpLines .lchip").forEach(ch => ch.addEventListener("click", () => selectVodLine(+ch.dataset.i)));
    syncPlayerRouteDisclosure();
  } else {
    $("#mpSourceState").textContent = "";
    $("#mpSourceState").dataset.state = "";
    $("#mpLines").innerHTML = "";
    pv.routeLinesExpanded = false;
    $("#mpLines").classList.remove("expanded");
    $("#mpLinesToggle").hidden = true;
    $("#mpLinesToggle").setAttribute("aria-expanded", "false");
  }
  /* 选集（电影不显示） */
  const isMovie = c.epCount <= 1 && !/电视剧|连续剧|网剧|短剧|动漫|综艺|美剧|英剧|韩剧|日剧|泰剧/.test(playerDetail && playerDetail.meta || "");
  const showEps = c.kind === "vod" && !isMovie && eps.length > 1;
  $("#mpEpsWrap").hidden = !showEps;
  if (showEps) {
    const pageSize = 10, pageCount = Math.ceil(eps.length / pageSize);
    const page = Math.floor(c.epIdx / pageSize);
    $("#mpEpPages").innerHTML = eps.length >= 20 ? Array.from({ length: pageCount }, (_, p) =>
      `<button class="epp ${p === page ? "on" : ""}" data-p="${p}">第${p * pageSize + 1}-${Math.min((p + 1) * pageSize, eps.length)}集</button>`).join("") : "";
    $$("#mpEpPages .epp").forEach(b => b.addEventListener("click", () => {
      if (mpApplyEpisode(+b.dataset.p * pageSize)) startVodPlayback(pv.ctx, { resume: 0, force: true, skipSave: true });
    }));
    /* 2026-09-19 集数不全修复：与详情页同口径——<20 集不分页全量展示
       （旧版 11~19 集按 10 分页落在第 2 页，但分页 chip 要 ≥20 才渲染，
       前 10 集永远点不到；App 两端同款已同修） */
    const pageEps = eps.length >= 20 ? eps.slice(page * pageSize, (page + 1) * pageSize) : eps;
    $("#mpEpGrid").innerHTML = pageEps.map((e, i) => {
      const idx = eps.length >= 20 ? page * pageSize + i : i;
      return `<button class="epc ${idx === c.epIdx ? "on" : ""}" data-i="${idx}">${idx + 1}</button>`;
    }).join("");
    $$("#mpEpGrid .epc").forEach(b => b.addEventListener("click", () => {
      if (mpApplyEpisode(+b.dataset.i)) startVodPlayback(pv.ctx, { resume: 0, force: true, skipSave: true });
    }));
  }
  /* 倍速 */
  $("#mpSpeed").innerHTML = SPEEDS.map((s, i) =>
    `<button class="lchip ${i === pv.speedIdx ? "on" : ""}" data-i="${i}">${s.toFixed(1)}x</button>`).join("");
  $$("#mpSpeed .lchip").forEach(ch => ch.addEventListener("click", () => {
    pv.speedIdx = +ch.dataset.i;
    if (pv.video) pv.video.playbackRate = SPEEDS[pv.speedIdx];
    renderMorePanel();
  }));
  /* 画幅 */
  $("#mpAspect").innerHTML = ASPECTS.map((a, i) =>
    `<button class="lchip ${i === pv.aspectIdx ? "on" : ""}" data-i="${i}">${a}</button>`).join("");
  $$("#mpAspect .lchip").forEach(ch => ch.addEventListener("click", () => {
    pv.aspectIdx = +ch.dataset.i;
    store.set("otvw:aspect", pv.aspectIdx);
    applyAspect(); renderMorePanel();
  }));
  /* 直播不可变速：不渲染倍速区（App PlayerMoreSections 同口径） */
  $("#mpSpeedSec").hidden = c.kind === "live";
  /* 竖屏（v3）：面板平铺常驻，任何状态重渲后保持可见 */
  if (state.portrait && c.kind === "vod") pv.panelOpen = true;
  if (state.portrait) $("#morePanel").hidden = false;
  if (c.kind === "vod") syncPlayerSourcePanel();
}
function syncPlayerRouteDisclosure() {
  const routes = $("#mpLines"), toggle = $("#mpLinesToggle");
  if (!routes || !toggle) return;
  const buttons = Array.from(routes.querySelectorAll(".lchip"));
  buttons.forEach(button => { button.hidden = false; });
  /* 面板被 hidden 时测量值全为 0；等打开或 resize 后再按真实宽度排版。 */
  routes.classList.add("expanded");
  const routeWidth = routes.getBoundingClientRect().width;
  if (!routeWidth) {
    routes.classList.toggle("expanded", !!pv.routeLinesExpanded);
    return;
  }
  const rowTops = [], buttonRows = [];
  buttons.forEach(button => {
    const top = button.offsetTop;
    let row = rowTops.findIndex(existing => Math.abs(existing - top) <= 1);
    if (row < 0) { row = rowTops.length; rowTops.push(top); }
    buttonRows.push(row);
  });
  const hasMore = rowTops.length > 2;
  const expanded = hasMore && !!pv.routeLinesExpanded;
  routes.classList.toggle("expanded", expanded);
  if (!expanded) buttons.forEach((button, index) => { button.hidden = buttonRows[index] >= 2; });
  toggle.hidden = !hasMore;
  toggle.textContent = expanded ? "⌃" : "⌄";
  toggle.setAttribute("aria-label", expanded ? "收起信号源" : "展开信号源");
  toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
}
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => pv.video && pv.video.play());
  navigator.mediaSession.setActionHandler("pause", () => pv.video && pv.video.pause());
}

/* 手势层（App PlayerScreen：单击=浮层开关；双击左右 1/3=±15s、中央=播放暂停；
 * 横滑=±120s 快进预览；竖滑左半亮度/右半音量；直播仅单击+竖滑） */
(function () {
  const layer = $("#video");
  let down = null, mode = null, seekStart = 0, seekCur = 0, vbKind = null, vbStart = 0.5, vbCur = 0.5;
  let lastTap = 0, lastX = 0, lastY = 0, tapPending = 0;
  const ind = () => $("#gestInd");
  /* 简 4（2026-09-19 v3 终定，App 同构）：双击/拖动快进退 = 无底色双三角 + 目标时间；
     亮度/音量保持毛玻璃胶囊并补图标 */
  const TRI_F = '<svg class="gi-tri" viewBox="0 0 60 60"><path d="M10 12l18 18-18 18z"/><path d="M32 12l18 18-18 18z"/></svg>';
  const TRI_B = '<svg class="gi-tri" viewBox="0 0 60 60"><path d="M50 12L32 30l18 18z"/><path d="M28 12L10 30l18 18z"/></svg>';
  const ICO_SUN = '<svg class="gi-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.2" fill="#fff" stroke="none"/><path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5 5l1.8 1.8M17.2 17.2L19 19M19 5l-1.8 1.8M6.8 17.2L5 19"/></svg>';
  const ICO_SPK = '<svg class="gi-ico" viewBox="0 0 24 24"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4z" fill="#fff" stroke="none"/><path d="M15.5 9a4.2 4.2 0 0 1 0 6M18 6.6a7.6 7.6 0 0 1 0 10.8"/></svg>';
  function showInd(html, plain) { const el = ind(); el.innerHTML = html; el.classList.toggle("gi-plain", !!plain); el.hidden = false; }
  function hideInd() { ind().hidden = true; }
  let indTimer = 0;
  function flashInd(html) { showInd(html, true); clearTimeout(indTimer); indTimer = setTimeout(hideInd, 700); }
  function fmtT(s) { return fmtTime(s); }
  const seekInd = (fwd, sec) => (fwd ? TRI_F : TRI_B) + `<span class="gi-time">${fmtT(sec)}</span>`;
  /* 长按倍速（2026-09-21）：按住画面左/右任一侧 ~0.45s → 3.0x 快进播放，松手回用户倍速。
     只认触屏/手写笔（桌面鼠标按住不该改倍速）；直播不可变速（App PlaybackPolicy.LONG_PRESS_SPEED 同口径）。 */
  const LP_SPEED = 3.0, LP_DELAY = 450;
  let lpTimer = 0, lpActive = false;
  function lpStart() {
    lpTimer = 0;
    if (lpActive || !pv.video || !pv.ctx || pv.ctx.kind !== "vod") return;
    lpActive = true;
    pv.video.playbackRate = LP_SPEED;
    showInd(`<div class="gi-speed">${LP_SPEED.toFixed(1)}x</div><div class="gi-sub">倍速播放中</div>`, true);
  }
  function lpStop() {
    if (lpTimer) { clearTimeout(lpTimer); lpTimer = 0; }
    if (!lpActive) return;
    lpActive = false;
    if (pv.video) pv.video.playbackRate = SPEEDS[pv.speedIdx];
    hideInd();
  }
  layer.addEventListener("pointerdown", e => {
    if (!pv.ctx || pv.ctx.kind !== "vod" && pv.ctx.kind !== "live") return;
    down = { x: e.clientX, y: e.clientY, t: Date.now() };
    mode = null;
    /* 长按计时只给触屏/手写笔起；一动就撤（见 pointermove） */
    if (pv.ctx.kind === "vod" && e.pointerType !== "mouse") lpTimer = setTimeout(lpStart, LP_DELAY);
  });
  layer.addEventListener("pointermove", e => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y;
    const v = pv.video;
    /* 长按中途滑动 = 用户要的是拖动，撤掉计时（已生效的长按保持到松手） */
    if (!lpActive && lpTimer && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) { clearTimeout(lpTimer); lpTimer = 0; }
    if (!mode) {
      if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) && pv.ctx.kind === "vod" && v && v.duration) {
        mode = "seek"; seekStart = v.currentTime; seekCur = seekStart;
      } else if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx)) {
        mode = "vb";
        vbKind = down.x < innerWidth / 2 ? "brightness" : "volume";
        vbStart = vbKind === "brightness" ? (pv.bright || 1) : (v ? v.volume : 1);
        vbCur = vbStart;
      }
    }
    if (mode === "seek" && v && v.duration) {
      seekCur = Math.max(0, Math.min(v.duration - 1, seekStart + dx / innerWidth * 120));
      showInd(seekInd(seekCur >= seekStart, seekCur), true);
    } else if (mode === "vb") {
      vbCur = Math.max(0.02, Math.min(1, vbStart - dy / innerHeight * 1.2));
      if (vbKind === "brightness") {
        pv.bright = vbCur;
        v.style.filter = `brightness(${vbCur})`;
      } else if (v) { v.volume = vbCur; v.muted = false; }
      showInd(`<div class="gi-label">${vbKind === "brightness" ? ICO_SUN : ICO_SPK}${vbKind === "brightness" ? "亮度" : "音量"}</div>
        <div class="gi-bar"><i style="width:${Math.round(vbCur * 100)}%"></i></div>`, false);
    }
  });
  const end = e => {
    if (!down) return;
    const dx = e.clientX - down.x, dy = e.clientY - down.y;
    /* 长按倍速已接管本次手势：还原倍速后直接收尾，不再落进单击/双击判定 */
    const wasLongPress = lpActive;
    lpStop();
    if (wasLongPress) { down = null; mode = null; return; }
    if (mode === "seek" && pv.video) { pv.video.currentTime = seekCur; hideInd(); }
    else if (mode === "vb") hideInd();
    else if (Math.abs(dx) < 12 && Math.abs(dy) < 12) {
      /* 单击 / 双击判定 */
      const now = Date.now();
      const isDouble = now - lastTap < 300 && Math.abs(e.clientX - lastX) < 40 && Math.abs(e.clientY - lastY) < 40;
      clearTimeout(tapPending);
      if (isDouble) {
        lastTap = 0;
        if (pv.ctx && pv.ctx.kind === "vod") {
          const third = innerWidth / 3;
          if (e.clientX < third) {
            pv.video.currentTime = Math.max(0, pv.video.currentTime - 10);
            flashInd(seekInd(false, pv.video.currentTime));
          } else if (e.clientX > third * 2) {
            pv.video.currentTime = Math.min(pv.video.duration - 1, pv.video.currentTime + 10);
            flashInd(seekInd(true, pv.video.currentTime));
          } else { pv.video.paused ? pv.video.play().catch(() => {}) : pv.video.pause(); }
        }
      } else {
        lastTap = now; lastX = e.clientX; lastY = e.clientY;
        tapPending = setTimeout(() => {
          if (!pv.ctx) return;
          if (pv.ctx.kind === "live") { pv.picker = !pv.picker; renderSrcPicker(); if (pv.picker) armHide(); }
          /* 竖屏（v3）：更多面板平铺常驻，点视频统一开/关控制浮层 */
          else { pv.controls ? hideControls() : showControls(); }
        }, 280);
      }
    }
    down = null; mode = null;
  };
  layer.addEventListener("pointerup", end);
  layer.addEventListener("pointercancel", () => { lpStop(); down = null; mode = null; hideInd(); });
})();
$("#ctlBack").addEventListener("click", closePlayer);

/* 推送页返回（全部/详情共用；左缘右滑手势同走这里） */
function closePush() {
  /* 退出片库/详情后不让慢请求继续占带宽，更不允许回写已经离开的页面。 */
  all.pageRequest?.abort(); all.filterRequest?.abort();
  all.pageRequest = null; all.filterRequest = null; all.loading = false;
  detailRequest?.abort(); detailRequest = null; detailSeq++;
  $("#pAll").hidden = true; $("#pDetail").hidden = true;
  document.body.style.overflow = "";
  if (state.tab === "vod") renderHistRow();
}
$$(".back-btn").forEach(b => b.addEventListener("click", closePush));

/* ---------------- 启动 ---------------- */
fit();
metricMark("app");
telemetryEmit(TELEMETRY_EVENTS.APP_START, { kind: "app", portrait: !!state.portrait });
/* 深链：#vod/#tv/#fav/#search 直达对应页；#detail/{vid} 直达详情（默认足球） */
const bootHash = (location.hash || "").replace("#", "");
if (bootHash.startsWith("detail/")) {
  switchTab("vod");
  openDetail(bootHash.slice(7));
} else {
  switchTab(PAGE_IDS[bootHash] ? bootHash : "live");
}
fetch(BASE + "/api/health").then(r => r.ok).catch(() => hint("后端连接异常，部分功能不可用", 3500));
licenseReverify();
setInterval(licenseReverify, LICENSE_REVERIFY_MS);
let backgroundedAt = 0;
function refreshVisibleContent() {
  if (document.hidden) return;
  licenseReverify();
  if (state.tab === "live") refreshLive(true);
  if (pv.ctx && pv.ctx.kind === "live" && backgroundedAt && Date.now() - backgroundedAt >= 30000) {
    recoverPlayback("长时间后台后返回", pv.recoveryGen);
  }
  backgroundedAt = 0;
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshVisibleContent();
  else backgroundedAt = Date.now();
});
addEventListener("online", refreshVisibleContent);
addEventListener("focus", refreshVisibleContent);
addEventListener("pageshow", refreshVisibleContent);
