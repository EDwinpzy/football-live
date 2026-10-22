/* 足球直播网页：只包含足球赛程、信号源选择和直播播放。 */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const BASE = (location.pathname.replace(/\/[^/]*$/, "") || "").replace(/\/+$/, "");
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  const LIVE_FILTERS = [["all", "全部"], ["important", "焦点赛事"], ["英超", "英超"],
    ["西甲", "西甲"], ["意甲", "意甲"], ["德甲", "德甲"], ["法甲", "法甲"],
    ["中超", "中超"], ["euro", "欧战"], ["national", "国家队"]];
  const IMPORTANT_TEAMS = ["曼联", "曼彻斯特联", "曼城", "曼彻斯特城", "利物浦", "阿森纳", "切尔西",
    "热刺", "托特纳姆热刺", "皇家马德里", "皇马", "巴塞罗那", "巴萨", "马德里竞技", "马竞",
    "拜仁", "拜仁慕尼黑", "国际米兰", "国米", "AC米兰", "尤文图斯", "巴黎圣日耳曼"];
  const IMPORTANT_COMPETITIONS = /世预赛|欧预赛|欧洲杯|亚洲杯|美洲杯|世界杯|友谊赛|国家队|欧国联/;
  const NON_FOOTBALL = /男篮|女篮|篮球|排球|网球|冰球|棒球|橄榄球|乒乓|羽毛/;
  const state = { matches: [], filter: "all", heroIndex: 0, refreshTimer: 0 };
  const player = { match: null, session: 0, sources: [], selected: -1, hls: null, retry: 0, mediaRetry: 0, tried: new Set() };
  let hintTimer = 0;
  let refreshInFlight = false;

  function hint(message, duration = 2800) {
    const node = $("#hint");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { node.hidden = true; }, duration);
  }

  async function fetchJson(url, timeoutMs = 12000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally { clearTimeout(timer); }
  }

  function dateKey(offset = 0) {
    const date = new Date(Date.now() + offset * 86400000 + 8 * 3600000);
    return `${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
  }
  function dateTitle(date) {
    if (date === dateKey()) return "今日比赛";
    if (date === dateKey(1)) return "明日比赛";
    const match = String(date || "").match(/^(\d{2})-(\d{2})$/);
    return match ? `${+match[1]}月${+match[2]}日` : "比赛赛程";
  }
  function scoreOf(match) {
    const score = match.score || {};
    return { home: score.h == null ? score.home : score.h, away: score.a == null ? score.away : score.a };
  }
  function finished(match) { return ["finished", "ended", "close"].includes(String(match.status || "").toLowerCase()); }
  function held(match) { return ["postponed", "cancelled", "abandoned", "pending"].includes(String(match.status || "").toLowerCase()); }
  function important(match) {
    const teams = `${match.home || ""} ${match.away || ""}`;
    return IMPORTANT_TEAMS.some(team => teams.includes(team)) || IMPORTANT_COMPETITIONS.test(match.league || "");
  }
  function matchesForView() {
    return state.matches.filter(match => !NON_FOOTBALL.test(`${match.league || ""} ${match.home || ""} ${match.away || ""}`)
      && !finished(match)).filter(match => {
        if (state.filter === "all") return true;
        if (state.filter === "important") return important(match);
        if (state.filter === "euro") return /欧冠|欧联|欧协|欧罗巴|欧国联|欧会杯/.test(match.league || "");
        if (state.filter === "national") return IMPORTANT_COMPETITIONS.test(match.league || "");
        if (state.filter === "中超") return /中超|足协杯/.test(match.league || "");
        return String(match.league || "").includes(state.filter);
      });
  }
  function statusText(match) {
    if (match.status === "live") return match.minute ? `LIVE · ${match.minute}'` : "LIVE";
    if (match.status === "postponed") return "延期";
    if (match.status === "cancelled") return "已取消";
    if (match.status === "abandoned") return "中断";
    if (match.status === "pending") return "待定";
    return match.time || "赛前";
  }
  function matchStartMillis(match) {
    const full = String(match.date_full || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    const short = String(match.date || "").match(/^(\d{2})-(\d{2})$/);
    const time = String(match.time || "").match(/^(\d{1,2}):(\d{2})/);
    if ((!full && !short) || !time) return null;
    const date = full ? full.slice(1).map(Number) : [new Date(Date.now() + 8 * 3600000).getUTCFullYear(), ...short.slice(1).map(Number)];
    return Date.parse(`${date[0]}-${String(date[1]).padStart(2, "0")}-${String(date[2]).padStart(2, "0")}T${String(+time[1]).padStart(2, "0")}:${time[2]}:00+08:00`);
  }
  function canSelect(match) {
    if (held(match)) return false;
    if (match.status === "live") return true;
    const start = matchStartMillis(match);
    return start == null || Date.now() >= start - 10 * 60000;
  }
  function teamMarkup(name) {
    const text = String(name || "?");
    const imageUrl = `${BASE}/api/team-icon-img?name=${encodeURIComponent(text)}`;
    return `<span class="team"><img class="team-logo" src="${esc(imageUrl)}" alt="" data-team-fallback="${esc(text)}"><span class="team-name">${esc(text)}</span></span>`;
  }
  function bindTeamFallbacks(scope = document) {
    $$('img[data-team-fallback]', scope).forEach(image => {
      const fallback = () => {
        const node = document.createElement("span");
        node.className = "team-fallback";
        node.textContent = (image.dataset.teamFallback || "?").slice(0, 1);
        image.replaceWith(node);
      };
      image.addEventListener("error", fallback, { once: true });
      image.addEventListener("load", () => { if (image.naturalWidth > 0 && image.naturalWidth < 3) fallback(); }, { once: true });
    });
  }
  function matchCard(match) {
    const score = scoreOf(match);
    const hasScore = score.home !== "" && score.home != null && score.away !== "" && score.away != null;
    const middle = hasScore ? `${esc(score.home)} - ${esc(score.away)}` : "VS";
    const dateTime = [match.date && dateTitle(match.date), match.time].filter(Boolean).join(" · ");
    return `<button class="match-card" type="button" data-mid="${esc(match.match_id)}" ${canSelect(match) ? "" : "disabled"}>
      <span class="match-meta"><span>${esc(match.league || "足球")}</span><span class="match-status ${match.status === "live" ? "live" : ""}">${esc(statusText(match))}</span></span>
      <span class="match-teams">${teamMarkup(match.home)}<span class="match-score">${middle}</span>${teamMarkup(match.away)}</span>
      <span class="match-time">${esc(dateTime || "点击选择直播信号")}</span>
    </button>`;
  }
  function renderFilters() {
    $("#leagueChips").innerHTML = LIVE_FILTERS.map(([key, label]) =>
      `<button class="filter-chip ${key === state.filter ? "on" : ""}" type="button" data-filter="${esc(key)}">${esc(label)}</button>`).join("");
    $$(".filter-chip").forEach(button => button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      renderMatches();
    }));
  }
  function heroCandidates() {
    const matches = matchesForView();
    return [...matches.filter(match => match.status === "live"), ...matches.filter(match => match.status !== "live")].slice(0, 5);
  }
  function renderHero() {
    const candidates = heroCandidates();
    const hero = $("#liveHero");
    if (!candidates.length) { hero.hidden = true; return; }
    state.heroIndex = Math.max(0, Math.min(state.heroIndex, candidates.length - 1));
    const match = candidates[state.heroIndex];
    const score = scoreOf(match);
    $("#liveHeroImg").src = `${BASE}/assets/football/fb-${String((Number(match.match_id) % 36) + 1).padStart(2, "0")}.jpg`;
    $("#heroLeague").textContent = match.league || "足球直播";
    $("#heroTeams").innerHTML = `<span>${esc(match.home)}</span><span class="versus">VS</span><span>${esc(match.away)}</span>`;
    const hasScore = score.home !== "" && score.home != null && score.away !== "" && score.away != null;
    $("#heroStatus").textContent = `${statusText(match)}${hasScore ? ` · ${score.home} - ${score.away}` : ""}`;
    $("#heroWatch").disabled = !canSelect(match);
    $("#heroWatch").onclick = () => startMatch(match);
    $("#heroDots").innerHTML = candidates.map((_, index) =>
      `<button class="${index === state.heroIndex ? "on" : ""}" type="button" aria-label="第 ${index + 1} 场焦点比赛" data-hero="${index}"></button>`).join("");
    $$("#heroDots button").forEach(button => button.addEventListener("click", () => {
      state.heroIndex = Number(button.dataset.hero) || 0;
      renderHero();
    }));
    hero.hidden = false;
  }
  function renderMatches() {
    renderFilters();
    renderHero();
    const matches = matchesForView();
    const liveCount = matches.filter(match => match.status === "live").length;
    $("#liveCount").textContent = liveCount ? `${liveCount} 场正在直播` : `${matches.length} 场可关注`;
    const groups = new Map();
    for (const key of [dateKey(), dateKey(1)]) groups.set(key, []);
    for (const match of matches) {
      const key = match.date || "其他日期";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(match);
    }
    let html = "";
    for (const [date, items] of groups) {
      if (!items.length && date !== dateKey() && date !== dateKey(1)) continue;
      html += `<section class="date-group"><h2 class="date-heading">${esc(dateTitle(date))}<span>${items.length} 场</span></h2>`;
      html += items.length ? `<div class="match-grid">${items.map(matchCard).join("")}</div>`
        : `<div class="empty-state">暂无比赛</div>`;
      html += `</section>`;
    }
    if (!html) html = `<div class="empty-state">当前没有可播放的比赛</div>`;
    $("#liveSections").innerHTML = html;
    bindTeamFallbacks($("#liveSections"));
    $$("#liveSections .match-card:not(:disabled)").forEach(button => button.addEventListener("click", () => {
      const match = state.matches.find(item => String(item.match_id) === button.dataset.mid);
      if (match) startMatch(match);
    }));
  }
  async function refreshMatches(silent = false) {
    if (refreshInFlight) return;
    refreshInFlight = true;
    $("#refreshButton").disabled = true;
    try {
      const payload = await fetchJson(`${BASE}/api/matches?fresh=1`, 45000);
      state.matches = (Array.isArray(payload.matches) ? payload.matches : []).map(match => ({
        ...match,
        match_id: String(match.match_id || ""),
        home: String(match.home || ""),
        away: String(match.away || ""),
        league: String(match.league || ""),
        date: String(match.date || ""),
        status: String(match.status || ""),
        time: String(match.time || ""),
        minute: String(match.minute || ""),
      })).filter(match => match.match_id && match.home && match.away);
      state.heroIndex = 0;
      $("#liveLoading").hidden = true;
      renderMatches();
      if (!silent) hint("赛程已更新");
    } catch (error) {
      if (!silent || !state.matches.length) {
        $("#liveLoading").hidden = false;
        $("#liveLoading").textContent = `赛程加载失败：${error.name === "AbortError" ? "请求超时" : error.message}`;
      }
    } finally {
      refreshInFlight = false;
      $("#refreshButton").disabled = false;
    }
  }

  function setPlayerLoading(loading) { $("#playerSpinner").hidden = !loading; }
  function playerMessage(message) {
    $("#playerErrorText").textContent = message;
    $("#playerError").hidden = false;
    setPlayerLoading(false);
  }
  function destroyPlayerSource() {
    if (player.hls) { player.hls.destroy(); player.hls = null; }
    const video = $("#video");
    try { video.pause(); } catch (_) {}
    video.removeAttribute("src");
    video.load();
  }
  function relayUrl(url, referer) {
    return `${BASE}/api/relay?u=${encodeURIComponent(url)}&ref=${encodeURIComponent(referer || "https://www.88zhibo.tv/")}`;
  }
  function sourceMarkup(source, index) {
    const selected = index === player.selected;
    return `<button class="source-chip ${selected ? "on" : ""}" type="button" data-source="${index}" ${selected ? "disabled" : ""}>${esc(source.name || `线路 ${index + 1}`)}</button>`;
  }
  function renderSources(message = "") {
    $("#sourceState").textContent = message;
    $("#srcPicker").innerHTML = player.sources.map(sourceMarkup).join("") || `<span class="empty-state">暂无可用信号源</span>`;
    $$("#srcPicker .source-chip").forEach(button => button.addEventListener("click", () => selectSource(Number(button.dataset.source), true)));
  }
  async function selectSource(index, refresh, automatic = false) {
    const source = player.sources[index];
    if (!source || !player.match) return;
    if (!automatic) player.tried = new Set();
    player.tried.add(index);
    player.selected = index;
    player.retry = 0;
    player.mediaRetry = 0;
    const session = player.session;
    destroyPlayerSource();
    $("#playerError").hidden = true;
    setPlayerLoading(true);
    renderSources("正在连接线路…");
    try {
      let resolved = source;
      if (refresh) {
        const id = encodeURIComponent(player.match.match_id);
        const src = encodeURIComponent(source.source || "");
        resolved = await fetchJson(`${BASE}/api/stream/${id}?src=${src}&fresh=1`, 60000);
      }
      if (session !== player.session) return;
      if (!resolved.url) throw new Error(resolved.error || "线路解析失败");
      player.sources[index] = { ...source, ...resolved };
      renderSources("正在播放");
      const video = $("#video");
      const url = relayUrl(resolved.url, resolved.referer || source.referer);
      if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({ maxBufferLength: 45, maxMaxBufferLength: 90, liveSyncDurationCount: 3 });
        player.hls = hls;
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (session !== player.session) return;
          video.play().catch(() => hint("线路已连接，点击播放器开始观看"));
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (session !== player.session || !data.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR && player.retry < 2) {
            player.retry += 1;
            hls.startLoad(-1);
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR && player.mediaRetry < 1) {
            player.mediaRetry += 1;
            hls.recoverMediaError();
          } else advanceSource("当前线路中断，正在尝试下一条");
        });
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url;
        video.play().catch(() => hint("线路已连接，点击播放器开始观看"));
      } else {
        throw new Error("当前浏览器无法播放 HLS 直播");
      }
    } catch (error) {
      if (session !== player.session) return;
      if (player.sources.some((_, sourceIndex) => !player.tried.has(sourceIndex))) {
        advanceSource("当前线路不可用，正在尝试下一条");
      } else {
        playerMessage(error.message || "信号源连接失败");
        renderSources("线路连接失败");
      }
    }
  }
  function advanceSource(message) {
    const next = player.sources.findIndex((_, index) => !player.tried.has(index));
    if (next < 0) { playerMessage("所有信号源暂不可用，请稍后重试"); renderSources("暂无可播放线路"); return; }
    hint(message, 1800);
    selectSource(next, true, true);
  }
  async function startMatch(match) {
    if (!canSelect(match)) {
      hint(held(match) ? statusText(match) : "比赛还未开始");
      return;
    }
    clearPlayer();
    player.match = match;
    const session = player.session;
    $("#playerTitle").textContent = `${match.home} 对阵 ${match.away}`;
    $("#playerMeta").textContent = [match.league, statusText(match)].filter(Boolean).join(" · ");
    $("#player").hidden = false;
    document.body.style.overflow = "hidden";
    setPlayerLoading(true);
    renderSources("正在查找可用线路…");
    try {
      const payload = await fetchJson(`${BASE}/api/stream/${encodeURIComponent(match.match_id)}?src=all&fresh=1`, 60000);
      if (session !== player.session) return;
      const seen = new Set();
      player.sources = (Array.isArray(payload.sources) ? payload.sources : []).filter(source => {
        const id = String(source && source.source || "").trim();
        if (!source || source.player !== "m3u8" || !source.url || !id || seen.has(id)) return false;
        seen.add(id);
        return true;
      });
      if (!player.sources.length) throw new Error(payload.error || "当前没有可用的直播信号源");
      player.selected = -1;
      player.tried = new Set();
      renderSources(`${player.sources.length} 条可用线路`);
      await selectSource(0, false);
    } catch (error) {
      if (session !== player.session) return;
      player.sources = [];
      renderSources("稍后可以重试");
      playerMessage(error.message || "未找到可用信号源");
    }
  }
  function clearPlayer() {
    player.session += 1;
    destroyPlayerSource();
    player.match = null;
    player.sources = [];
    player.selected = -1;
    $("#player").hidden = true;
    $("#playerError").hidden = true;
    document.body.style.overflow = "";
    renderSources("");
  }
  async function checkDesktopUpdate() {
    const button = $("#updateButton");
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      const health = await fetchJson(`${BASE}/api/health`, 8000);
      $("#updateStatus").textContent = health.desktop && Number(health.hotVersion) > 0
        ? `热更新 v${Number(health.hotVersion)}` : "客户端已是最新状态";
      hint("已请求桌面客户端检查更新");
    } catch (_) { hint("更新状态暂时不可用"); }
    finally { button.disabled = false; button.textContent = "检查更新"; }
  }
  async function init() {
    $("#refreshButton").addEventListener("click", () => refreshMatches(false));
    $("#playerClose").addEventListener("click", clearPlayer);
    $("#playerRetry").addEventListener("click", () => {
      if (player.sources.length) selectSource(Math.max(0, player.selected), true);
      else if (player.match) startMatch(player.match);
    });
    $("#updateButton").addEventListener("click", checkDesktopUpdate);
    $("#player").addEventListener("click", event => { if (event.target === $("#player")) clearPlayer(); });
    $("#video").addEventListener("playing", () => { setPlayerLoading(false); $("#playerError").hidden = true; });
    $("#video").addEventListener("waiting", () => setPlayerLoading(true));
    $("#video").addEventListener("error", () => {
      if (!player.hls && $("#video").currentSrc && !$("#player").hidden) advanceSource("当前线路播放失败，正在切换");
    });
    addEventListener("keydown", event => { if (event.key === "Escape" && !$("#player").hidden) clearPlayer(); });
    try {
      const health = await fetchJson(`${BASE}/api/health`, 8000);
      if (health.desktop) {
        $("#updateStatus").textContent = Number(health.hotVersion) > 0 ? `热更新 v${Number(health.hotVersion)}` : "桌面版";
        $("#updateStatus").hidden = false;
        $("#updateButton").hidden = false;
      }
    } catch (_) { /* 赛程请求会显示实际连接状态 */ }
    await refreshMatches(true);
    state.refreshTimer = setInterval(() => {
      if (!document.hidden) refreshMatches(true);
    }, 20000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshMatches(true);
    });
    addEventListener("online", () => refreshMatches(true));
  }
  init();
})();
