# Task 1 完成报告：Resolver API 合同及 metadata ID 映射

## 状态

DONE_WITH_CONCERNS

实现仅通过 metadata center detail 返回的 `external_ids.douban` 做 ID 映射，并通过静态线路索引的精确 Douban ID 键查找。缺失、歧义、身份冲突、未知 ID 或未配置线路均不猜测索引键，也不返回候选。过期但仍有候选的索引结果标记为 `STALE_AVAILABLE`。

新增 `GET /vod/resolve/{mytv_id}?episode=1`，固定返回 `mytv_id`、`episode`、`resolve_version`、`preferred`、`backups`、`cache`、`status`。接口拒绝空或非数字 ID、空/非数字/小于 1 的集数；元数据已知总集数时拒绝越界集数。剧集线路缺少所请求的集数时不会把最后一集夹取为替代项。候选只返回来源、内容、集数索引、匹配分和质量字段，不包含线路播放 URL。

MetadataClient 仅请求 `OTV_METADATA_API_URL` 下的 `/metadata/{mytv_id}` detail 接口。全部 metadata HTTP 测试使用本地假服务。

## TDD 证据

先新增合同测试，再按任务要求运行：

```text
python -m pytest tools/backend-src/tests/test_vod_resolver_contract.py tools/backend-src/tests/test_metadata_client.py -q
```

初始 RED：收集失败，明确提示 `vod_resolver` 和 `metadata_client` 模块尚不存在（2 个 `ModuleNotFoundError`，退出码 1）。

## 验证结果

任务指定的 Resolver、metadata client 和 API 合同测试：

```text
python -m pytest tools/backend-src/tests/test_vod_resolver_contract.py tools/backend-src/tests/test_metadata_client.py tools/backend-src/tests/test_vod_api.py -q
32 passed in 1.72s
```

整套后端测试运行一次：

```text
python -m pytest tools/backend-src/tests -q
393 passed, 5 errors
```

5 个错误都来自既有 `test_license_restore_contract.py` 的类初始化：测试尝试读取后台 `worker/index.js`，该工作树中的路径无法解析（错误路径显示为乱码并抛出 `FileNotFoundError`）。失败不在本任务的文件或代码路径中。

## 发布集成注意项

本任务没有部署、发布或访问生产 metadata/VPS。运行时使用的 backend-src 可加载新增模块；发布前仍需由对应任务的文件所有者确认 release hot-update ZIP 和 admin required-entry 清单包含 `metadata_client.py` 与 `vod_resolver.py`。这两处清单属于并行的 Web/自托管任务，本任务依照文件所有权没有修改它们。

本任务只变更后端 API 与 Python 测试，没有视觉界面改动；目标状态通过本地 HTTP 合同测试和 URL-free 响应断言验证。

## 提交

提交主题：`feat(vod): add mytv id resolver contract`
