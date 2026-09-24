# My TV 点播 Resolver 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让规范 mytv_id 在 1.5 秒内优先得到可信的点播线路，并以短期缓存、健康排序和受限回查避免慢源造成空详情或长等待。

**Architecture:** 在现有静态 vod-lines 索引和并发源注册表之上增加 Resolver 服务。它先通过 metadata center 把 mytv_id 显式映射到 douban_id，再查 last-good/热缓存/静态索引；只有快路径没有可信结果时才并行查询至多 3 个已启用源，最后才做限时回退搜索。新增 API 只返回线路引用和状态，播放 URL 仍由播放端点即时解析。

**Tech Stack:** Python 3、ThreadPoolExecutor、现有 Hhkan/MacCms adapters、metadata center HTTP API、PostgreSQL 与进程内 LRU。

**Spec:** docs/superpowers/specs/2026-09-24-mytv-vod-resolver-design.md

## Global Constraints

- 新 API 为 GET /vod/resolve/{mytv_id}?episode=1；响应含 mytv_id、episode、resolve_version、preferred、backups、cache 和 status。
- 单源软超时为 600–800ms；最多并发查询健康排序靠前的 3 个启用来源；总快路径预算为 1.2–1.5s。
- 动态标题匹配最低置信度沿用 AUTO_MATCH_THRESHOLD=80；低置信度近似标题不能生成可播放线路。
- 成功结果缓存 30 分钟至 6 小时；来源健康排序约 5 分钟；失败缓存 15–60 秒，不长期缓存空结果。
- 初版复用现有 PostgreSQL 和小型进程 LRU；没有内存和并发证据前不引入 Redis。
- 正常视频流量由客户端直连；只有直连不适用或失败时使用现有受控 relay。
- 不新增未授权来源、不绕过反爬/鉴权、不记录签名播放 URL 或授权信息。

## Review Focus

- mytv_id 缺失、重复映射或过期索引误连影片：Task 1/2 用明确映射正反例覆盖。
- 单个慢源拖住首条线路：Task 2 使用可控延迟源验证先返回时间和后台任务收尾。
- 403、429、404、5xx 与空结果被当作有效线路：Task 3 为每种响应固定状态及缓存时间。
- 失败负缓存阻碍线路恢复：Task 3 使用假时钟验证短 TTL 到期重试。
- 临时签名 URL 落入日志或长期缓存：Task 4 检查事件、缓存及错误输出中不存在敏感字段。

## Dependencies

- selfhosted retirement 计划向 web-api 容器注入内部 metadata API origin：OTV_METADATA_API_URL=http://metadata-api:8094。
- Android 与 Web 计划只消费本文定义的 JSON/API 状态；发生合同调整时先同步更新本计划和对应测试，再改客户端。
- CloudBase 点播回退路径属于本计划，旧 APK 不在验收范围。

---

### Task 1: 定义 Resolver API 合同及 metadata ID 映射

**Files:**
- Create: tools/backend-src/vod_resolver.py
- Create: tools/backend-src/metadata_client.py
- Modify: tools/backend-src/vod_api.py
- Modify: tools/backend-src/proxy.py
- Modify: tools/backend-src/tests/test_vod_api.py
- Create: tools/backend-src/tests/test_vod_resolver_contract.py
- Create: tools/backend-src/tests/test_metadata_client.py

**Interfaces:**
- Consumes: mytv_id、episode、metadata center detail、现有静态线路索引及 SourceRegistry。
- Produces: ResolveResult(status, mytv_id, episode, resolve_version, preferred, backups, cache)。
- Candidate 至少携带 source_id、source_name、content_id、episode_index、match_score、quality；不得含长期存储的完整 playback URL。

- [ ] **Step 1: 先写接口和映射失败测试**

```python
def test_resolver_maps_mytv_id_through_metadata_center():
    result = resolver.resolve(mytv_id=10_000_123, episode=1)
    assert result.mytv_id == 10_000_123
    assert index.lookup_was_called_with("1292052")

def test_missing_or_ambiguous_mapping_never_guesses_a_line():
    result = resolver.resolve(mytv_id=99, episode=1)
    assert result.preferred is None
    assert result.status in {"RESOLVING", "TEMPORARILY_UNAVAILABLE"}
```

补充未知 mytv_id、同一 external id 指向冲突、过期 index_version、episode 小于 1/超过总集数、单集与剧集集数映射错误测试。测试使用本地假 MetadataClient 和静态索引，不访问 VPS。

- [ ] **Step 2: 运行失败测试确认目前没有规范解析合同**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_contract.py tools/backend-src/tests/test_metadata_client.py -q

Expected: FAIL，指出 ResolverResult、元数据显式映射或 /vod/resolve 路由尚未实现。

- [ ] **Step 3: 实现 metadata client、Resolver 类型和路由**

```python
return {
    "mytv_id": item.mytv_id,
    "episode": requested_episode,
    "resolve_version": index.version,
    "preferred": candidate.to_public_dict() if candidate else None,
    "backups": [line.to_public_dict() for line in backups],
    "cache": {"kind": cache_kind, "age_ms": cache_age_ms},
    "status": status,
}
```

MetadataClient 只调用内部 metadata-api 的 detail 接口，并从 external_ids.douban 显式取得 douban_id。解析路由拒绝空、非数字和无效 episode 参数；缺映射时返回定义好的不可用/补查状态，不构造猜测 ID。响应固定含 mytv_id、episode、resolve_version、preferred、backups、cache、status。

- [ ] **Step 4: 运行 API 与 ID 映射合同并提交**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_contract.py tools/backend-src/tests/test_metadata_client.py tools/backend-src/tests/test_vod_api.py -q

Expected: PASS；响应字段名、状态集合和 HTTP 错误码与客户端计划一致。提交消息：feat(vod): add mytv id resolver contract。

### Task 2: 实现 last-good、静态索引和快速首线

**Files:**
- Modify: tools/backend-src/vod_resolver.py
- Modify: tools/backend-src/vod_sources.py
- Modify: tools/backend-src/proxy.py
- Create: tools/backend-src/tests/test_vod_resolver_fast_path.py
- Modify: tools/backend-src/tests/test_vod_source_racing.py

**Interfaces:**
- Consumes: Task 1 的 ResolveResult 与已有 SourceRegistry、VodHistory、vod-lines 数据。
- Produces: first trusted preferred、可增量追加的 backups、cache 字典和耗时分段。

- [ ] **Step 1: 为缓存命中、竞速和状态时限写失败测试**

覆盖 last-good 优先、静态索引命中、索引无该 episode、三个来源中 650ms 首个可信结果不等待 2 秒慢源、总预算 1.5 秒、超时候选不返回、失败任务有界结束。用事件屏障/假时钟避免依赖真实网络抖动。

- [ ] **Step 2: 运行测试确认缺少按 mytv_id/episode 的快速路径**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_fast_path.py tools/backend-src/tests/test_vod_source_racing.py -q

Expected: 至少“首个可信结果先返回”与 episode 对应缓存测试 FAIL。

- [ ] **Step 3: 实现快速候选选择和有限竞速**

```python
candidate = last_good.get(key) or hot_cache.get(key) or static_index.get(key)
if is_trusted(candidate, threshold=AUTO_MATCH_THRESHOLD):
    return matched(candidate, cache_kind=cache_kind(candidate))
results = race(enabled_sources[:3], per_source_timeout=0.700, budget=1.500)
return first_trusted_or_continue(results, bounded_backup_executor)
```

键值使用规范 mytv_id + episode + resolve_version。顺序查询最近成功线路、热缓存和已验证静态索引。静态数据缺失时只并行调用健康排序靠前的最多 3 个启用来源；每源软超时设为 700ms、快路径硬预算设为 1.5s。用现有 AUTO_MATCH_THRESHOLD=80 过滤候选。preferred 一旦可信即返回；未完成工作只允许通过有界 executor 后台补充 backups，并在请求取消、关闭和异常时回收任务。剧集源按服务端已验证的 episode_index 映射，不用客户端数组顺序猜。

- [ ] **Step 4: 运行快路径测试并提交**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_fast_path.py tools/backend-src/tests/test_vod_source_racing.py tools/backend-src/tests/test_vod_api.py -q

Expected: PASS；慢源不影响首线时延，缺集、过期静态索引和低分候选不产生 preferred。提交消息：feat(vod): return first trusted source within budget。

### Task 3: 加入延迟回退、负缓存及来源熔断恢复

**Files:**
- Modify: tools/backend-src/vod_resolver.py
- Modify: tools/backend-src/vod_sources.py
- Modify: tools/backend-src/tests/test_vod_cloud_source_fallback.py
- Modify: tools/backend-src/tests/test_vod_source_resilience.py
- Create: tools/backend-src/tests/test_vod_resolver_fallback.py

**Interfaces:**
- Consumes: 快路径无可信线路的结果、现有 source adapters 与健康历史。
- Produces: 有明确状态的受限回退搜索、短负缓存和恢复后可重试结果。

- [ ] **Step 1: 写错误分类、匹配反例和缓存到期测试**

```python
first = resolver.resolve(mytv_id=10_000_123, episode=1)
assert first.status == "TEMPORARILY_UNAVAILABLE"
clock.advance(seconds=15)
assert resolver.resolve(mytv_id=10_000_123, episode=1).attempts == 2
```

用 fake adapter 覆盖 403、429、404、5xx、连接超时、空 JSON、空 episodes、线路恢复和任务取消。匹配正例覆盖片名/原名/别名/繁简/年份/类型/季集/主演；反例覆盖同名不同年份（差距大于 1）、季号不符和低于分数 80。断言空结果只保留 15 秒，success TTL 默认 30 分钟，health ranking TTL 为 5 分钟。

- [ ] **Step 2: 运行失败测试**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_fallback.py tools/backend-src/tests/test_vod_source_resilience.py -q

Expected: FAIL，现有 detail() 仍会等待所有源或旧 CloudBase 回退测试仍期望调用旧入口。

- [ ] **Step 3: 实现快路径失败后的宽松搜索和恢复**

```python
if not preferred:
    status = "FALLBACK_SEARCHING"
    preferred = next((c for c in search_fallback(subject) if match_score(subject, c) >= 80), None)
    status = "MATCHED" if preferred else "TEMPORARILY_UNAVAILABLE"
    cache.put(key, preferred, ttl_seconds=1800 if preferred else 15)
```

仅在第一波快路径无可信候选后，按已审核元数据的片名、原名、别名、繁简、年份、类型、季/集和主演构造 subject，并复用 match_score。对外可播放阈值为 80；每种反例都证明低分候选不会起播。状态按实际进程区分 RESOLVING、FAST_PATH_FAILED、FALLBACK_SEARCHING、STALE_AVAILABLE、TEMPORARILY_UNAVAILABLE、MATCHED。连续失败降权或短期熔断；退出熔断须至少两次探测成功。无线路时响应“线路暂不可用，可重试”，并在短负 TTL 到期后允许重新解析。删除 CloudBase 线路兜底期望，改为断言不存在 CloudBase URL/请求。

- [ ] **Step 4: 运行故障矩阵及全后端回归**

Run: python -m pytest tools/backend-src/tests/test_vod_resolver_fallback.py tools/backend-src/tests/test_vod_source_resilience.py tools/backend-src/tests/test_vod_cloud_source_fallback.py tools/backend-src/tests -q

Expected: PASS；每类故障都有断言，回退/熔断不泄漏线程，CloudBase 静态兜底不可达。提交消息：feat(vod): bound fallback search and source recovery。

### Task 4: 增加匿名化指标及真实样本基准

**Files:**
- Modify: tools/backend-src/vod_resolver.py
- Modify: tools/backend-src/proxy.py
- Modify: tools/backend-src/tests/test_vod_resolver_contract.py
- Create: tools/backend-src/tests/test_vod_resolver_metrics.py
- Create: docs/04-records/2026-09-24-vod-resolver-benchmark.md

**Interfaces:**
- Consumes: 请求/缓存/来源/匹配/错误阶段事件。
- Produces: aggregate request, p50/p95/p99, match/cache hit, timeout/error/circuit and metadata version metrics；无用户身份或签名 URL。

- [ ] **Step 1: 写隐私与统计合同测试**

```python
assert "source_id" in event and "elapsed_ms" in event
assert not any(secret in repr(event) for secret in ("Cookie", "token=", "license_key", "https://media"))
```

在捕获结构日志与 metrics 响应后断言存在 source_id、cache_kind、candidate_count、elapsed_ms、error_category、metadata_version；断言不存在签名 URL、Cookie、卡密、授权头、个人 ID。并发请求下计数无负值或重复关闭。

- [ ] **Step 2: 实现聚合指标**

```python
event = {"source_id": source_id, "cache_kind": cache_kind,
         "candidate_count": count, "elapsed_ms": elapsed,
         "error_category": category, "metadata_version": version}
metrics.observe(event)
```

只输出匿名化计数、耗时直方图/分位数和来源状态；采样记录不包含标题、播放 URL、IP 或稳定设备标识。指标查询复用项目已有 health/ops 访问控制，不公开管理凭证。

- [ ] **Step 3: 用真实可测合法内容基准**

从仓库已启用的来源中预先固定一组影片与剧集正例/反例；记录样本数量、metadata/index 版本、来源、设备网络和每次结果。目标首条可信线路 P50 ≤ 500ms、P95 ≤ 1.5s，匹配率至少 99%。未达到即报告基线和失败来源，不使用 mock 结果替代真实基准；不得超过源站访问限制。

- [ ] **Step 4: 运行完整 Resolver/后端测试并提交证据**

Run: python -m pytest tools/backend-src/tests -q

Expected: PASS；benchmark 只报告实测数据，指标与日志不含敏感信息。提交消息：test(vod): record resolver latency and match benchmark。

### Task 5: 由统一 HTTP 入口挂接 API、退役 CloudBase 请求并服务哈希资源

**Files:**
- Modify: tools/backend-src/proxy.py
- Modify: tools/build_vod_line_index.py
- Modify: tools/backend-src/tests/test_endpoint_migration_contract.py
- Modify: tools/backend-src/tests/test_vod_storage_contract.py
- Modify: tools/backend-src/tests/test_vod_source_publish_contract.py
- Create: tools/backend-src/tests/test_current_backend_has_no_cloudbase.py

**Interfaces:**
- Consumes: Resolver API、Web 静态资源构建目录和 selfhosted deployment environment。
- Produces: 自建 proxy 路由 /vod/resolve、无 CloudBase 运行时调用，以及供 Web builder 接入的静态资源服务合同：dist/index.html、dist/assets/app.<sha256>.js、dist/assets/style.<sha256>.css、manifest.json。

- [ ] **Step 1: 为路由、CloudBase 零请求和缓存策略写合同测试**

```python
response = client.get("/vod/resolve/10000123?episode=1")
assert response.json()["mytv_id"] == 10000123
assert "tcloudbase" not in response.text.lower()
```

用本地假 metadata-api/source server 覆盖 /vod/resolve 映射与状态；断言 activate、hotupdate、IPTV、metadata、Web、VOD 和 object API 只走配置的自建 origin。静态文件测试验证 HTML 为 no-store、内容哈希 JS/CSS 为 immutable、无 hash 的 JS/CSS 不超过 300 秒；媒体分片沿用短缓存且不被误判成不可变 Web 资源。fake requester 断言运行期间没有 CloudBase 域名或 CloudBase 环境配置读取。

- [ ] **Step 2: 运行测试并确认当前中央 proxy 仍包含旧路由**

Run: python -m pytest tools/backend-src/tests/test_current_backend_has_no_cloudbase.py tools/backend-src/tests/test_endpoint_migration_contract.py tools/backend-src/tests/test_vod_storage_contract.py tools/backend-src/tests/test_vod_source_publish_contract.py -q

Expected: FAIL，至少一个业务路径仍选择 CloudBase/bridge 或静态资源不满足新缓存合同。

- [ ] **Step 3: 收敛 proxy 路由到自建入口**

```python
if is_html(path):
    headers["Cache-Control"] = "no-store"
elif is_content_hashed_asset(path):
    headers["Cache-Control"] = "public, max-age=31536000, immutable"
else:
    headers["Cache-Control"] = route_cache_policy(path)
```

将 Resolver route 接到 Task 1 服务。移除 proxy.py 内 CloudBase 点播、激活、热更新、IPTV、对象镜像和 Web 回退请求；每个业务 endpoint 使用 selfhosted API origin，不从测试/CloudBase 变量推断默认入口。同步改写 build_vod_line_index.py 的当前操作说明，索引由合规开发机生成并经 selfhosted SSH/SCP/对象写入接口发布到 VPS，禁止回到 CloudBase bucket。静态 handler 接受约定的 dist/index.html、dist/assets/<name>.<sha256>.js|css 与 manifest.json 布局，拒绝路径穿越；HTML no-store，哈希资源 immutable，普通静态资源和视频 manifest/segment 使用各自 TTL。API timeout 与媒体 relay timeout 独立。

- [ ] **Step 4: 运行所有 proxy 合同和后端回归**

Run: python -m pytest tools/backend-src/tests/test_current_backend_has_no_cloudbase.py tools/backend-src/tests/test_endpoint_migration_contract.py tools/backend-src/tests/test_vod_api.py tools/backend-src/tests -q

Expected: PASS；只有 Web build/archive fixture、迁移工具和退役记录可包含 CloudBase 字样，正常运行配置和响应 URL 不包含 CloudBase。提交消息：refactor(backend): route current services through self-hosted API。
