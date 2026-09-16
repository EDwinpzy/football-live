# MyTV 豆瓣刮削后本地搜索后台完整方案

> 版本：v1.0  
> 日期：2026-09-13  
> 范围：仅后台数据与搜索逻辑，不包含 UI、遥控器焦点、软键盘布局、Compose 交互实现。  
> 核心原则：豆瓣只负责刮削与 Metadata 获取；刮削成功后的数据持久化为 MyTV 自有 Media Catalog；搜索只查询 MyTV 本地 Catalog。

---

## 1. 目标

本方案解决 MyTV 在“豆瓣刮削完成之后”对本地媒体目录进行高质量搜索的问题。

目标能力：

- 中文标题精确搜索
- 中文标题前缀搜索
- 中文标题包含搜索
- 拼音全拼搜索
- 拼音前缀搜索
- 拼音首字母搜索
- 拼音首字母前缀搜索
- 数字 / 季数归一搜索
- 别名搜索
- 原标题 / 外文名搜索
- 演员 / 导演搜索
- 类型 / 标签搜索
- 中文错字模糊搜索
- 拼音少字母 / 多字母 / 错字母模糊搜索
- 首字母有限容错
- FTS5 辅助全文搜索
- 本地缓存与低延迟
- 搜索索引可独立重建
- 三端统一调用同一搜索 API
- 搜索逻辑可随 Python backend 热更新

明确不做：

- 不让搜索依赖在线豆瓣请求
- 不在搜索时重新刮削
- 不把输入法逻辑和 SearchEngine 混在一起
- 不使用 Elasticsearch / OpenSearch / Meilisearch / Typesense
- 不把 SearchIndex 固化进 APK assets 作为唯一索引源
- 不让播放源决定搜索目录
- 不把 UI 排序、焦点、键盘交互写入搜索后端

---

# 2. 现有工程约束

MyTV 当前适合承载本方案的基础条件：

```text
Android App
    │
    ├── Kotlin / Compose
    │
    └── Chaquopy Python Backend
              │
              └── 127.0.0.1:8090
```

后端模块可以随 backend zip 热更新，因此以下能力适合集中放到 Python backend：

```text
DoubanScraper
MediaNormalizer
MediaRepository
SearchIndexer
QueryNormalizer
SearchEngine
Ranker
FuzzyMatcher
Search API
```

数据库文件属于 App 运行时数据，不属于 backend zip，不应因为热更新被覆盖。

推荐数据库位置：

```text
App private storage
└── media/
    └── mytv.db
```

---

# 3. 总体架构

```text
                     豆瓣
                      │
                      ▼
                DoubanScraper
                      │
                      ▼
                MediaNormalizer
                      │
                      ▼
              MyTV Media Catalog
                      │
              SQLite / mytv.db
                      │
        ┌─────────────┼─────────────┐
        │             │             │
      media         person         alias
        │             │             │
        └─────────────┼─────────────┘
                      │
                      ▼
                 SearchIndexer
                      │
             ┌────────┴────────┐
             │                 │
        media_search        media_fts
             │                 │
             └────────┬────────┘
                      ▼
                 SearchEngine
                      │
        ┌─────────────┼─────────────┐
        │             │             │
      Title         Pinyin        Initial
        │             │             │
      Alias         Person          FTS
        │             │             │
        └─────────────┼─────────────┘
                      ▼
                CandidateMerge
                      │
                      ▼
                    Rank
                      │
             results insufficient
                      │
                      ▼
                 FuzzyRecall
                      │
                      ▼
                    Top N
                      │
                      ▼
               GET /api/search
```

---

# 4. 数据生命周期

## 4.1 新媒体第一次进入

```text
原始媒体条目
    ↓
识别豆瓣条目
    ↓
豆瓣刮削
    ↓
MediaNormalizer
    ↓
BEGIN TRANSACTION
    ↓
写 media
写 aliases
写 people
写 genres
写关联关系
    ↓
SearchIndexer
    ↓
写 media_search
更新 media_fts
    ↓
COMMIT
```

一旦 COMMIT 成功：

> 该影片已经成为 MyTV 自有媒体数据。

后续：

```text
首页
详情
筛选
搜索
收藏
历史
```

全部读取本地 Catalog。

---

## 4.2 Metadata 刷新

只有以下情况重新刮削：

- 首次加入
- 用户显式刷新
- Metadata 缺少关键字段
- 条目版本过旧且后台策略允许更新
- 管理工具要求重新刮削

刷新流程：

```text
scrape
  ↓
normalize
  ↓
update media
  ↓
update aliases / people / genres
  ↓
rebuild search index for media_id
  ↓
update FTS
```

必须同一事务完成。

---

## 4.3 删除媒体

删除主记录必须同时删除：

```text
media_alias
media_person
media_genre
media_search
media_fts 对应文档
search_history 中无效引用可保留或置空
```

禁止产生“搜索有结果但详情不存在”的幽灵记录。

---

# 5. 数据库设计

推荐使用 SQLite。

建议开启：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
```

---

## 5.1 media

```sql
CREATE TABLE media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    douban_id TEXT UNIQUE,

    media_type TEXT NOT NULL,

    title TEXT NOT NULL,
    original_title TEXT,

    year INTEGER,

    season INTEGER,
    episode_count INTEGER,

    rating REAL,
    rating_count INTEGER,

    poster TEXT,
    backdrop TEXT,

    overview TEXT,

    country TEXT,
    language TEXT,

    release_date TEXT,

    status TEXT NOT NULL DEFAULT 'active',

    scraped_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_media_douban_id
ON media(douban_id);

CREATE INDEX idx_media_title
ON media(title);

CREATE INDEX idx_media_year
ON media(year);

CREATE INDEX idx_media_type
ON media(media_type);
```

`media_type` 建议：

```text
movie
tv
variety
anime
documentary
other
```

---

## 5.2 media_alias

```sql
CREATE TABLE media_alias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    media_id INTEGER NOT NULL,

    alias TEXT NOT NULL,
    alias_type TEXT,

    FOREIGN KEY(media_id)
        REFERENCES media(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_media_alias_media
ON media_alias(media_id);

CREATE INDEX idx_media_alias_text
ON media_alias(alias);
```

示例：

```text
庆余年 第二季

庆余年2
庆余年第二部
庆余年 S2
```

---

## 5.3 person

```sql
CREATE TABLE person (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    name TEXT NOT NULL,
    name_original TEXT,

    UNIQUE(name, name_original)
);

CREATE INDEX idx_person_name
ON person(name);
```

---

## 5.4 media_person

```sql
CREATE TABLE media_person (
    media_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,

    role_type TEXT NOT NULL,

    character_name TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY(media_id, person_id, role_type),

    FOREIGN KEY(media_id)
        REFERENCES media(id)
        ON DELETE CASCADE,

    FOREIGN KEY(person_id)
        REFERENCES person(id)
        ON DELETE CASCADE
);
```

`role_type`：

```text
actor
director
writer
producer
other
```

---

## 5.5 genre

```sql
CREATE TABLE genre (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
```

---

## 5.6 media_genre

```sql
CREATE TABLE media_genre (
    media_id INTEGER NOT NULL,
    genre_id INTEGER NOT NULL,

    PRIMARY KEY(media_id, genre_id),

    FOREIGN KEY(media_id)
        REFERENCES media(id)
        ON DELETE CASCADE,

    FOREIGN KEY(genre_id)
        REFERENCES genre(id)
        ON DELETE CASCADE
);
```

---

# 6. 搜索索引表

## 6.1 media_search

`media_search` 只保存搜索派生字段。

```sql
CREATE TABLE media_search (
    media_id INTEGER PRIMARY KEY,

    title_norm TEXT NOT NULL,

    title_pinyin TEXT,
    title_initials TEXT,
    title_number_norm TEXT,
    title_number_pinyin TEXT,
    title_number_initials TEXT,

    original_title_norm TEXT,

    aliases_norm TEXT,
    aliases_pinyin TEXT,
    aliases_initials TEXT,

    people_norm TEXT,
    people_pinyin TEXT,
    people_initials TEXT,

    genre_norm TEXT,

    search_version INTEGER NOT NULL DEFAULT 1,

    FOREIGN KEY(media_id)
        REFERENCES media(id)
        ON DELETE CASCADE
);

CREATE INDEX idx_search_title_norm
ON media_search(title_norm);

CREATE INDEX idx_search_title_pinyin
ON media_search(title_pinyin);

CREATE INDEX idx_search_title_initials
ON media_search(title_initials);

CREATE INDEX idx_search_title_number
ON media_search(title_number_norm);
```

---

## 6.2 为什么 SearchIndex 与 Media 分开

这样可以做到：

```text
Media 数据不变
    ↓
搜索算法升级
    ↓
rebuildSearchIndex()
```

例如：

```text
V1
只有 title_pinyin

V2
加入 initials

V3
加入 people_pinyin

V4
加入数字季数归一
```

无需重新刮削豆瓣。

---

# 7. SearchDocument 示例

豆瓣标准化数据：

```json
{
  "title": "庆余年 第二季",
  "original_title": "",
  "aliases": [
    "庆余年2",
    "庆余年第二部"
  ],
  "actors": [
    "张若昀",
    "李沁"
  ],
  "genres": [
    "剧情",
    "古装"
  ],
  "year": 2024,
  "season": 2
}
```

生成：

```json
{
  "title_norm": "庆余年第二季",

  "title_pinyin": "qingyuniandierji",

  "title_initials": "qyndej",

  "title_number_norm": "庆余年2",

  "title_number_pinyin": "qingyunian2",

  "title_number_initials": "qyn2",

  "aliases_norm": "庆余年2|庆余年第二部",

  "aliases_pinyin": "qingyunian2|qingyuniandierbu",

  "aliases_initials": "qyn2|qyndeb",

  "people_norm": "张若昀|李沁",

  "people_pinyin": "zhangruoyun|liqin",

  "people_initials": "zry|lq",

  "genre_norm": "剧情|古装"
}
```

---

# 8. SearchIndexer

建议接口：

```python
class SearchIndexer:

    def build(self, media_id: int) -> SearchDocument:
        ...

    def rebuild_one(self, media_id: int):
        ...

    def rebuild_all(self):
        ...
```

数据来源只能读取本地 Catalog：

```text
media
media_alias
person
media_person
genre
media_genre
```

不在索引阶段访问外部网络。

---

# 9. 文本归一化

所有索引字段和查询字段必须走同一个 Normalizer。

推荐：

```python
normalize_text(text)
```

处理：

1. Unicode NFKC
2. lowercase
3. 全角转半角
4. 去首尾空格
5. 合并连续空格
6. 清理无意义标点
7. 中英文标点统一
8. 可选繁简归一
9. 数字格式归一
10. 季 / 部 / Season 归一
11. 保留有意义的英文、数字、汉字

示例：

```text
"  庆余年 · 第二季  "
↓

"庆余年第二季"
```

```text
"THE BATMAN"
↓

"thebatman"
```

---

# 10. 数字与季数归一

影视搜索必须做。

统一规则：

```text
第一季        → 1
第二季        → 2
第三季        → 3

第一部        → 1
第二部        → 2
第三部        → 3

Season 2      → 2
Season2       → 2
S02           → 2
S2            → 2
```

例如：

```text
庆余年 第二季
```

派生：

```text
庆余年2
qingyunian2
qyn2
```

查询：

```text
qyn2
```

应高置信度命中：

```text
庆余年 第二季
```

---

# 11. 拼音生成

后台必须独立于输入法生成搜索拼音。

即：

```text
PinyinIme
≠
Search Pinyin Engine
```

输入法负责输入。

SearchIndexer / SearchEngine 自己负责：

```text
中文 → 全拼
中文 → 首字母
```

推荐统一函数：

```python
to_pinyin(text)
to_initials(text)
```

要求：

- lowercase
- 不保留声调
- 汉字之间不加空格
- 数字原样保留
- 英文字母 lowercase
- 多音字允许词级修正

例如：

```text
庆余年
→ qingyunian
→ qyn

重庆森林
→ chongqingsenlin
→ cqsl
```

---

# 12. 多音字策略

第一版不要设计得过重。

推荐优先级：

```text
词级特殊映射
    ↓
常规拼音库
    ↓
默认读音
```

维护：

```python
PINYIN_OVERRIDES = {
    "重庆": "chongqing",
    ...
}
```

SearchIndex 可以在未来支持备用拼音：

```text
primary_pinyin
alternate_pinyin
```

但 V1 非必须。

---

# 13. QueryNormalizer

统一入口：

```python
class QueryNormalizer:

    def parse(self, raw: str) -> SearchQuery:
        ...
```

建议模型：

```python
@dataclass
class SearchQuery:
    raw: str
    normalized: str
    query_type: str
    length: int
    contains_han: bool
    contains_latin: bool
    contains_digit: bool
```

---

# 14. QueryType

至少支持：

```text
HAN
LATIN
INITIAL
INITIAL_NUMERIC
MIXED
NUMERIC
EMPTY
```

示例：

```text
庆余年
→ HAN

qingyunian
→ LATIN

qyn
→ INITIAL

qyn2
→ INITIAL_NUMERIC

庆余年2
→ MIXED

2024
→ NUMERIC
```

注意：

> LATIN 与 INITIAL 不能只靠长度绝对判断。

建议由 SearchEngine 同时进行轻量候选判断：

```text
qyn
优先 initial
同时允许 pinyin prefix 低权召回

qingyu
优先 pinyin
```

---

# 15. TV 软键盘的后台契约

本方案不负责 UI 和输入法交互，但后台必须满足 TV 拼音输入场景。

无论前端输入框传入：

```text
庆余年
qingyunian
qingyu
qyn
qyn2
```

SearchEngine 都必须直接处理。

不要求前端先把：

```text
qyn
```

转换成：

```text
庆余年
```

才搜索。

即：

```text
TV Pinyin Input
      │
      │ raw query
      ▼
GET /api/search?q=qyn
      │
      ▼
SearchEngine
```

后台必须具备拼音搜索能力。

---

# 16. Recall Engine

建议拆成独立 Channel：

```text
SearchEngine

├── TitleRecall
├── AliasRecall
├── PinyinRecall
├── InitialRecall
├── OriginalTitleRecall
├── PersonRecall
├── GenreRecall
├── FtsRecall
└── FuzzyRecall
```

每个 Recall 返回：

```python
Candidate(
    media_id=...,
    score=...,
    matched_field=...,
    match_type=...
)
```

---

# 17. TitleRecall

支持：

```text
EXACT
PREFIX
CONTAINS
```

示例：

```text
庆余年
→ exact

庆余
→ prefix

余年
→ contains
```

---

# 18. AliasRecall

同样支持：

```text
EXACT
PREFIX
CONTAINS
```

例如：

```text
庆余年2
仙三
复联
权游
```

Alias 是影视搜索质量的重要来源。

---

# 19. PinyinRecall

支持：

```text
EXACT
PREFIX
CONTAINS
```

优先级：

```text
exact > prefix > contains
```

例如：

```text
qingyunian
→ exact

qingyu
→ prefix
```

`contains` 权重必须明显低于 `prefix`，防止过量误召回。

---

# 20. InitialRecall

支持：

```text
EXACT
PREFIX
```

默认不做普通 contains。

例如：

```text
qyn
```

可命中：

```text
庆余年
qyn
```

也可命中：

```text
庆余年第二季
qyndej
```

但前者分数更高。

---

# 21. OriginalTitleRecall

用于：

```text
The Batman
Oppenheimer
Interstellar
```

即使中文标题为：

```text
新蝙蝠侠
奥本海默
星际穿越
```

输入英文也应命中。

---

# 22. PersonRecall

搜索演员 / 导演：

```text
张若昀
zhangruoyun
zry
```

均可召回关联作品。

优先级：

```text
中文姓名 exact
>
姓名拼音 exact
>
姓名拼音 prefix
>
姓名首字母 exact
```

人物匹配分数必须低于标题匹配。

---

# 23. GenreRecall

例如：

```text
科幻
悬疑
犯罪
古装
```

用于低权重辅助召回。

不要让 Genre 结果压过明确的标题结果。

---

# 24. FTS5

建立：

```sql
CREATE VIRTUAL TABLE media_fts
USING fts5(
    media_id UNINDEXED,
    title,
    aliases,
    people,
    genre,
    overview
);
```

FTS5 负责：

```text
演员
导演
类型
简介
较长自然文本关键词
```

不负责：

```text
qyn
qingyu
qyn2
```

这些由结构化 `media_search` 负责。

---

# 25. Candidate Merge

同一影片可能被多个 Recall 命中：

```text
title prefix
pinyin prefix
alias exact
```

必须合并。

建议：

```python
@dataclass
class Candidate:
    media_id: int
    best_score: float
    matched_fields: list[str]
    reasons: list[str]
```

Merge 规则：

```text
同 media_id
    ↓
保留最高基础分
    ↓
记录全部 match reason
    ↓
可增加 small multi-hit bonus
```

例如：

```text
best_score = 100

同时命中 3 个 channel
multi_hit_bonus = +3
```

Bonus 必须很小。

---

# 26. Ranking

V1 建议使用完全可解释的规则分。

推荐初始值：

| 匹配方式 | Score |
|---|---:|
| 标题完全匹配 | 120 |
| Alias 完全匹配 | 115 |
| 标题前缀 | 108 |
| 首字母完全匹配 | 105 |
| 全拼完全匹配 | 102 |
| 全拼前缀 | 96 |
| 首字母前缀 | 92 |
| 原标题完全匹配 | 90 |
| 标题包含 | 82 |
| Alias 前缀 | 80 |
| Alias 包含 | 76 |
| 人物中文完全匹配 | 68 |
| 人物拼音完全匹配 | 62 |
| 人物首字母完全匹配 | 58 |
| Genre | 45 |
| FTS | 35~60 |
| Fuzzy | 30~60 |

最终：

```text
FinalScore =
BaseMatchScore
+ MultiHitBonus
+ OptionalBusinessBonus
```

V1 推荐：

```text
OptionalBusinessBonus = 0
```

先把文本相关性调稳。

---

# 27. 后续业务 Bonus

V2 可增加：

```text
WatchHistoryBonus
PopularityBonus
RecentlyAddedBonus
RatingBonus
```

限制：

```text
所有业务 Bonus 合计
不得超过强文本匹配层级之间的主要差值
```

原则：

> 热门不能压过明确标题匹配。

---

# 28. Fuzzy Search 总体策略

Fuzzy 是 fallback，不是主搜索。

流程：

```text
普通 Recall
    ↓
结果 >= FUZZY_MIN_RESULTS
    ↓
直接返回

结果不足
    ↓
FuzzyRecall
```

建议：

```python
FUZZY_MIN_RESULTS = 8
```

---

# 29. 中文 Fuzzy

使用：

```text
Levenshtein Distance
```

阈值：

```text
长度 <= 2
不做 fuzzy

长度 3~4
distance <= 1

长度 5~8
distance <= 2

长度 > 8
distance <= 3
```

例如：

```text
斗罗大路
≈
斗罗大陆
```

---

# 30. 拼音 Fuzzy

用于：

```text
qingyunian
qinyunian

zhangruoyun
zhangruoyn
```

推荐：

```text
长度 <= 4
distance <= 1

长度 5~8
distance <= 1

长度 > 8
distance <= 2
```

拼音 Fuzzy 的权重低于正常拼音 prefix。

---

# 31. 首字母 Fuzzy

首字母非常短，必须严格限制。

推荐：

```text
长度 <= 3
禁止编辑距离
只 Exact / Prefix

长度 4~6
distance <= 1

长度 > 6
distance <= 2
```

例如：

```text
qyn
```

不能因为 fuzzy 扩成大量：

```text
xyn
qxn
qyj
```

---

# 32. 模糊分数计算

可使用：

```text
Similarity =
1 - distance / max(len(query), len(candidate))
```

例如：

```python
fuzzy_score = BASE_FUZZY * similarity
```

再做字段权重：

```text
title fuzzy
>
alias fuzzy
>
pinyin fuzzy
>
person fuzzy
```

---

# 33. 搜索主流程

推荐伪代码：

```python
def search(raw_query: str, limit: int = 30):

    query = query_normalizer.parse(raw_query)

    if query.query_type == "EMPTY":
        return []

    pool = CandidatePool()

    if query.query_type in ("HAN", "MIXED"):
        pool.add(title_recall(query))
        pool.add(alias_recall(query))

    if query.query_type in ("LATIN", "INITIAL", "INITIAL_NUMERIC", "MIXED"):
        pool.add(pinyin_recall(query))
        pool.add(initial_recall(query))
        pool.add(original_title_recall(query))

    pool.add(person_recall(query))

    if query.length >= 2:
        pool.add(genre_recall(query))

    if query.length >= 2:
        pool.add(fts_recall(query))

    candidates = pool.merge()

    if len(candidates) < FUZZY_MIN_RESULTS:
        pool.add(fuzzy_recall(query))
        candidates = pool.merge()

    ranked = ranker.rank(candidates, query)

    return ranked[:limit]
```

---

# 34. 查询短词保护

短 Query 很容易污染。

建议：

```text
长度 = 1
只 Exact / Prefix
不做 FTS
不做 Fuzzy

长度 = 2
允许部分 Prefix
不做宽泛 Fuzzy

长度 >= 3
正常搜索
```

例如：

```text
三
```

不应该触发全库编辑距离。

---

# 35. SearchCache

SQLite 是事实源。

SearchCache 是加速层。

建议启动时加载：

```text
media_id
title_norm
title_pinyin
title_initials
title_number_norm
title_number_pinyin
title_number_initials
aliases_*
people_*
```

结构：

```python
class SearchCache:
    docs_by_id
    title_exact_map
    initial_exact_map
    pinyin_exact_map
```

---

# 36. Cache 更新

新增影片：

```text
DB transaction commit
    ↓
cache.upsert(media_id)
```

刷新影片：

```text
DB update
    ↓
cache.upsert(media_id)
```

删除影片：

```text
DB delete
    ↓
cache.remove(media_id)
```

索引版本变化：

```text
rebuild all
    ↓
reload cache
```

---

# 37. SearchIndex 版本

定义：

```python
SEARCH_INDEX_VERSION = 1
```

`media_search` 中保存：

```text
search_version
```

启动检查：

```text
当前代码 SEARCH_INDEX_VERSION
!=
数据库现有 version
    ↓
后台 rebuild
```

不重新访问豆瓣。

---

# 38. 数据库 Schema Version

另外维护：

```text
DB_SCHEMA_VERSION
```

两者职责：

```text
DB_SCHEMA_VERSION
= 表结构版本

SEARCH_INDEX_VERSION
= 搜索派生算法版本
```

不要混用。

---

# 39. Migration

推荐：

```text
migrations/
├── 001_init.sql
├── 002_add_media_search.sql
├── 003_add_person_index.sql
└── 004_add_number_normalization.sql
```

每次启动：

```text
读取 schema_version
    ↓
顺序执行 migration
    ↓
成功后更新版本
```

禁止：

```text
发现结构不一致
→ 删除数据库重建
```

因为 Media Catalog 是长期资产。

---

# 40. 搜索 API

统一：

```http
GET /api/search?q={query}&limit={limit}
```

默认：

```text
limit = 30
max limit = 100
```

返回：

```json
{
  "query": "qyn",
  "normalized": "qyn",
  "total": 2,
  "results": [
    {
      "id": 1001,
      "title": "庆余年",
      "year": 2019,
      "poster": "...",
      "mediaType": "tv"
    },
    {
      "id": 1002,
      "title": "庆余年 第二季",
      "year": 2024,
      "poster": "...",
      "mediaType": "tv"
    }
  ]
}
```

正式环境不必返回：

```text
score
matchedBy
distance
```

---

# 41. Debug API

开发环境推荐：

```http
GET /api/search/debug?q=qyn
```

返回：

```json
{
  "raw": "qyn",
  "normalized": "qyn",
  "queryType": "INITIAL",

  "candidates": [
    {
      "mediaId": 1001,
      "title": "庆余年",
      "finalScore": 105,
      "matches": [
        {
          "channel": "initial",
          "field": "title_initials",
          "type": "exact",
          "score": 105
        }
      ]
    }
  ]
}
```

用途：

- 为什么 A 排在 B 前面
- 为什么某部片没召回
- 是否错误判定 QueryType
- 是否错误生成拼音
- 是否错误进入 Fuzzy

---

# 42. 搜索日志

建议：

```sql
CREATE TABLE search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    query TEXT NOT NULL,
    normalized_query TEXT,

    result_count INTEGER NOT NULL,

    duration_ms INTEGER,

    query_type TEXT,

    created_at INTEGER NOT NULL
);
```

建议默认只本地保存有限数量：

```text
最近 1000~5000 条
```

避免数据库无限增长。

---

# 43. 搜索历史

如果业务需要最近搜索：

```sql
CREATE TABLE search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    query TEXT NOT NULL,

    media_id INTEGER,

    clicked INTEGER NOT NULL DEFAULT 0,

    searched_at INTEGER NOT NULL
);
```

搜索引擎本身不依赖该表。

---

# 44. 无结果分析

重点记录：

```text
query
normalized_query
query_type
result_count = 0
```

后续可以发现：

```text
仙三
复联
权游
HP
```

等高频 Alias。

再补：

```text
media_alias
```

---

# 45. SearchIndexer 与刮削事务

推荐：

```python
def save_scraped_media(payload):

    with db.transaction():

        media_id = media_repo.upsert(payload)

        alias_repo.replace(media_id, payload.aliases)

        person_repo.replace(media_id, payload.people)

        genre_repo.replace(media_id, payload.genres)

        search_indexer.rebuild_one(media_id)

        fts_repo.rebuild_one(media_id)

    search_cache.upsert(media_id)
```

注意：

> Cache 更新必须发生在 DB commit 成功之后。

---

# 46. 一致性检查

启动时可以轻量检查：

```text
media active count
vs
media_search count
```

发现：

```text
media 存在
media_search 缺失
```

自动：

```text
rebuild missing index
```

FTS 同理。

---

# 47. 异常降级

## SearchIndex 缺失

```text
media_search 某条缺失
```

处理：

```text
跳过该派生索引
必要时异步 rebuild
```

不能导致整个 Search API 500。

---

## FTS5 不可用

降级：

```text
Structured Search only
```

核心标题 / 拼音 / 首字母仍正常。

---

## Fuzzy 异常

降级：

```text
关闭 Fuzzy
返回普通 Recall
```

---

## Cache 加载失败

降级：

```text
直接 SQLite structured query
```

---

# 48. 性能目标

建议：

| 场景 | 目标 |
|---|---:|
| 标题 Exact | < 10ms |
| 标题 Prefix | < 20ms |
| 拼音 / 首字母 | < 30ms |
| Person Recall | < 30ms |
| FTS5 | < 50ms |
| Fuzzy fallback | < 80ms |
| 总体 P95 | < 100ms |

数据量：

```text
1,000
10,000
50,000
```

都应该在测试范围内。

---

# 49. 内存目标

不要把：

```text
poster
overview
backdrop
```

加载进 SearchCache。

SearchCache 只保存：

```text
media_id
normalized strings
small metadata
```

搜索结果确定后：

```text
media_id list
    ↓
批量查询 media
    ↓
构造响应
```

---

# 50. API 安全与边界

Search API 只允许：

```text
GET
query string
limit
optional type filter
```

参数约束：

```text
q 最大长度，例如 128
limit 最大 100
```

禁止把 SQL 片段从前端传入。

所有 SQLite 查询参数化。

---

# 51. 可选过滤条件

后续可扩展：

```http
GET /api/search?q=qyn&type=tv&year=2024&limit=30
```

但 SearchEngine 和 filter 分开：

```text
Recall
    ↓
Rank
    ↓
Filter / Constraint
```

或在 Candidate 阶段做结构化约束。

---

# 52. 三端统一

TV、Mobile、Web 都调用：

```text
/api/search
```

不在三端分别实现：

```text
拼音
首字母
模糊算法
排序
```

这样保证：

```text
同一个 qyn
```

三端返回一致。

---

# 53. 热更新边界

适合通过 Python backend 热更新的：

```text
SearchIndexer
QueryNormalizer
Recall
Rank
Fuzzy
Search API
Score 权重
Alias 规则
拼音修正规则
SearchIndex version
```

不应由热更新覆盖的：

```text
mytv.db
Media Catalog
用户数据
搜索历史
```

数据库 Migration 必须向前兼容。

---

# 54. 推荐代码目录

```text
backend/
│
├── scraper/
│   ├── douban.py
│   └── normalizer.py
│
├── media/
│   ├── database.py
│   ├── models.py
│   ├── repository.py
│   ├── alias_repository.py
│   ├── person_repository.py
│   └── genre_repository.py
│
├── search/
│   ├── engine.py
│   ├── models.py
│   ├── indexer.py
│   ├── normalizer.py
│   ├── pinyin.py
│   ├── number_normalizer.py
│   ├── ranker.py
│   ├── cache.py
│   ├── fuzzy.py
│   │
│   └── recall/
│       ├── title.py
│       ├── alias.py
│       ├── pinyin.py
│       ├── initial.py
│       ├── original_title.py
│       ├── person.py
│       ├── genre.py
│       └── fts.py
│
├── migrations/
│   ├── 001_init.sql
│   └── ...
│
└── proxy.py
```

`proxy.py` 只负责：

```text
HTTP route
参数校验
调用 service
返回 JSON
```

不要把搜索算法全部塞进 `proxy.py`。

---

# 55. 核心接口建议

```python
class MediaRepository:
    def get(self, media_id: int):
        ...

    def batch_get(self, media_ids: list[int]):
        ...

    def upsert(self, media):
        ...
```

```python
class SearchIndexer:
    def rebuild_one(self, media_id: int):
        ...

    def rebuild_all(self):
        ...
```

```python
class SearchEngine:
    def search(self, query: str, limit: int = 30):
        ...
```

```python
class Ranker:
    def rank(self, candidates, query):
        ...
```

---

# 56. 测试用例

必须有自动化测试。

## 中文

```text
庆余年
→ 庆余年

庆余
→ 庆余年
→ 庆余年 第二季

余年
→ 庆余年
```

---

## 拼音

```text
qingyunian
→ 庆余年

qingyu
→ 庆余年
→ 庆余年 第二季
```

---

## 首字母

```text
qyn
→ 庆余年
→ 庆余年 第二季
```

第一条必须排在第二条前。

---

## 数字季数

```text
qyn2
→ 庆余年 第二季
```

```text
庆余年2
→ 庆余年 第二季
```

---

## 人物

```text
张若昀
→ 张若昀参演作品
```

```text
zhangruoyun
→ 张若昀参演作品
```

```text
zry
→ 张若昀参演作品
```

---

## 原标题

```text
oppenheimer
→ 奥本海默
```

```text
batman
→ 新蝙蝠侠 / 对应 Batman 条目
```

---

## 中文 Fuzzy

```text
斗罗大路
→ 斗罗大陆
```

---

## 拼音 Fuzzy

```text
qinyunian
→ 庆余年
```

---

## 首字母保护

```text
qyn
```

不得因为 fuzzy 返回大量只有一位差异的无关结果。

---

## 空 Query

```text
""
"   "
```

必须：

```text
[]
```

或返回明确空结果结构。

---

# 57. 排序验收

以：

```text
qyn
```

为例：

```text
庆余年
```

必须高于：

```text
庆余年 第二季
```

因为：

```text
qyn == qyn
```

属于 Initial Exact。

而：

```text
qyndej startsWith qyn
```

属于 Initial Prefix。

---

# 58. 数据一致性验收

以下场景必须通过：

```text
新增影片
→ 立即可搜

重新刮削
→ 新标题立即可搜
→ 旧无效 alias 不再出现

删除影片
→ 搜索立即消失

SearchIndex rebuild
→ 不改变 media 主数据

APK 升级
→ media 数据保留

Backend 热更新
→ media 数据保留
```

---

# 59. 性能验收

构造至少：

```text
1k
10k
50k
```

Media 数据集。

分别测试：

```text
中文 exact
中文 prefix
pinyin exact
pinyin prefix
initial exact
initial prefix
person
FTS
fuzzy
```

记录：

```text
P50
P95
P99
```

核心要求：

```text
P95 < 100ms
```

---

# 60. 第一阶段实施范围

建议第一阶段只实现最有价值的核心：

```text
SQLite Catalog

media
media_alias
person
media_person
genre
media_genre

media_search

SearchIndexer

Normalize

Pinyin

Initials

Number Normalize

TitleRecall
AliasRecall
PinyinRecall
InitialRecall

CandidateMerge

Rank

/api/search
```

暂缓：

```text
复杂 FTS
复杂个性化排序
高级自然语言
向量搜索
云端索引
```

---

# 61. 第二阶段

加入：

```text
PersonRecall
GenreRecall
FTS5
SearchCache
Debug API
Search Log
```

---

# 62. 第三阶段

加入：

```text
中文 Fuzzy
拼音 Fuzzy
有限 Initial Fuzzy
Alias 数据补全
无结果分析
```

---

# 63. 第四阶段

根据真实使用数据决定是否增加：

```text
WatchHistoryBonus
PopularityBonus
RecentlyAddedBonus
```

不建议在没有搜索日志之前过早做复杂排序。

---

# 64. 核心验收指标

最终至少满足：

```text
庆余年        ✅
庆余          ✅
余年          ✅

qingyunian    ✅
qingyu        ✅

qyn           ✅
qyn2          ✅

庆余年2       ✅

张若昀        ✅
zhangruoyun   ✅
zry           ✅

qinyunian     ✅  # 拼音轻微错误

斗罗大路      ✅  # 中文轻微错误
```

---

# 65. 最终职责边界

```text
豆瓣
=
Metadata Provider
```

```text
DoubanScraper
=
数据获取
```

```text
MediaNormalizer
=
字段标准化
```

```text
MyTV Media Catalog
=
本地事实数据源
```

```text
SearchIndexer
=
生成搜索派生字段
```

```text
SearchEngine
=
召回 + 合并 + 排序 + Fuzzy
```

```text
FTS5
=
辅助全文召回
```

```text
PinyinIme / 前端输入法
=
只产生用户输入
```

SearchEngine 不依赖输入法。

即：

```text
前端传 qyn
```

后台自己就必须知道如何搜索：

```text
庆余年
```

---

# 66. 最终技术选型

```text
Metadata
豆瓣刮削
    ↓
Persistence
SQLite
    ↓
Catalog
media / alias / person / genre
    ↓
Search Index
media_search
    ↓
Text Search
SQLite FTS5
    ↓
Pinyin
预生成全拼 + 首字母
    ↓
Fuzzy
Levenshtein fallback
    ↓
Ranking
Rule-based Score
    ↓
Cache
Python Memory SearchCache
    ↓
API
Python Backend / proxy.py
    ↓
Clients
TV + Mobile + Web
```

---

# 67. 最终结论

MyTV 后台搜索应正式采用：

> **豆瓣刮削 → 本地 SQLite Media Catalog → SearchIndexer → 本地 SearchEngine**

搜索运行时：

- 不请求豆瓣
- 不重新刮削
- 不依赖输入法把拼音先转成中文
- 不依赖外部搜索服务

TV 端即使直接把：

```text
qyn
qyn2
qingyu
qinyunian
```

传给 `/api/search`，后台也必须独立完成：

```text
Query Normalize
→ 拼音 / 首字母识别
→ Structured Recall
→ Candidate Merge
→ Rank
→ Fuzzy fallback
→ Top N
```

这套搜索系统的事实数据源始终只有：

```text
MyTV Media Catalog
```

而豆瓣只是 Catalog 的 Metadata 获取入口。
