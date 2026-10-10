# 知识库 Schema v1（callfans-agent）

> 实现代码：`kb/schema.py`（权威定义）。本文档解释设计决策。
> 核心原则：**Schema 即过滤器** —— 不符合本定义的数据在入库前被直接拒绝，
> 这是"精简出重要信息"的最硬手段。

## 0. 设计原则

1. **Schema 即过滤器**：只允许 6 种节点、9 种边、4 种事件；LLM 抽取的候选事实凡不符合本体的一律丢弃。
2. **只存影响决策的事实**：一条数据入库的标准是"会影响某个未来决策"（发不发帖、发什么、和谁互动、怎么避风控）。
3. **行为聚合有界压缩**：同一关系的重复互动只累加 `count`，不产生新记录；图谱永不因流水而膨胀。
4. **一切写入皆事件**：写入唯一合法形态是 `Event`，幂等、可回放、可审计。
5. **MVP 进程内实现，接口按可换 Neo4j 设计**：`KnowledgeStore` 的方法签名即生产图数据库的读写面。

## 1. ID 规范（全局唯一，入库强校验）

| 类型 | 格式 | 示例 |
| --- | --- | --- |
| Subject | `subj:{slug}`（跨平台主体） | `subj:lily_beauty` |
| Account | `acc:{platform}:{handle}` | `acc:xiaohongshu:lily_beauty` |
| Persona | `per:{slug}` | `per:beauty-lily` |
| Topic | `topic:{a}.{b}.{c}`（层级用 `.` 编码） | `topic:tech.ai.llm` |
| Post | `post:{platform}:{native_id}` | `post:xiaohongshu:p001` |
| Rule | `rule:{slug}` | `rule:xhs-new-account` |
| ActionRecord | `act:{uuid}` 或 `act:{业务ID}` | `act:demo-101` |
| Episode | `ep:{主体slug}.{周期}`（跨平台稳定） | `ep:lily_beauty.2026W41` |
| MemoryNote | `note:{事实指纹}` | `note:857ef3acec` |

统一 ID 是**联结（问题 3）的地基**：所有数据源入库前先消歧到这套 ID，
跨本体（人设/领域/关系）的查询都从 `Account`、`Topic` 两个枢纽节点出发。

## 2. 节点类型（9 种）

> 2026-10-11（主体模型改造，docs/05 §3/§11 第 2 步）：新增 **Subject 记忆主体**。
> 记忆归属规则：`HAS_PERSONA` / `HAS_EPISODE` / `HAS_MEMORY` 的起点全部是
> Subject（数字生命）；`Account` 退化为它在某平台的**操作句柄**
> （`HAS_HANDLE: Subject→Account`，1:N）。同一数字生命跨平台共享
> 记忆/人设/情景；频控与行为流水仍是账号级。MemoryNote 节点只存客观字段
> （fact/category，全局指纹去重），**主观状态（status/pinned/confidence/ts）
> 在 HAS_MEMORY 边上、按主体私有**——同一条事实被多主体共享节点、各持状态。

| 类型 | 用途 | 关键属性 | 冷热 |
| --- | --- | --- | --- |
| Subject | 记忆主体（数字生命/人/agent/项目），记忆与人设的归属者 | `kind(persona/person/agent/project/client), name, created_at, summary_day:*`（rollup 产物，跨平台汇入） | 温 |
| Account | 平台操作句柄（或外部用户） | `platform, status, risk_level, created_at, cooldown_until` | 温 |
| Persona | 人设定义（多账号可复用同一人设） | `name, age_band, occupation, location, mbti, language_style, bio` | 冷（稳定低频更新） |
| Topic | 层级话题（层级在 ID 里，不存 parent 边） | `label` | 冷 |
| Post | 已发布内容（只存 digest，不存全文） | `platform, published_at, content_type, digest, views, likes, comments` | 温 |
| Rule | 平台规则/风控约束（含共识确证的公共规则） | `name, kind, params, actions, platform, hard, text, status, confirmations, effective_at, jitter_window` | 冷 |
| ActionRecord | 行为流水（热区数据，rollup 后归档） | `action, outcome, topic_id, digest, decision_id, archived, ts` | 热→冷 |
| Episode | 情景记忆：周/月/年卷积叙事（docs/03） | `level, period, narrative, coverage, drift, drift_detail, confidence, ts` | 冷 |
| MemoryNote | 语义记忆：稳定事实/教训/里程碑/承诺（节点=客观 fact/category；主观状态在 HAS_MEMORY 边上） | `fact, category` | 冷（pinned 不衰减） |

Post 只存 `digest` 不存全文——原始数据在图谱外归档（冷区），
图谱里的事实自带压缩语义（问题 2 的"结构即压缩"）。

## 3. 边类型（13 种，三类联结）

| 边 | 方向 | 语义 | 联结类型 |
| --- | --- | --- | --- |
| `HAS_HANDLE` | Subject→Account | 主体在某平台的操作句柄（1:N） | 本体定义（强） |
| `HAS_PERSONA` | Subject→Persona | 主体绑定的唯一人设（跨平台共享） | 本体定义（强） |
| `INTERESTED_IN` | Persona→Topic | 人设兴趣树 | 本体定义（强） |
| `FOLLOWS` | Account→Topic | 行为聚合的话题关注（`count`） | 行为聚合 |
| `INTERACTS_WITH` | Account→Account | 行为聚合的互动（`count/weight`） | 行为聚合 |
| `PUBLISHED` | Account→Post | 发帖关系 | 本体定义 |
| `ABOUT` | Post→Topic | 帖子话题 | 本体定义 |
| `APPLIES_TO` | Rule→Topic | 规则适用话题（预留） | 本体定义 |
| `PERFORMED` | Account→ActionRecord | 行为流水 | 热区数据 |
| `DECIDED_VIA` | ActionRecord→任意 | **决策依据（provenance）** | 决策追溯 |
| `HAS_EPISODE` | Subject→Episode | 主体的情景记忆（docs/03） | 记忆联结 |
| `HAS_MEMORY` | Subject→MemoryNote | 主体的语义记忆；边携带主观状态 `status/pinned/confidence/ts` | 记忆联结 |
| `COMMITTED_IN` | MemoryNote→Post | 承诺在哪条内容中做出 | 记忆联结 |
| `FULFILLED_BY` | MemoryNote→任意 | 承诺由哪次行为兑现（闭环） | 记忆联结 |

所有边统一携带元属性：`ts, confidence, source, event_id`；
聚合边额外携带 `count, weight, last_active`。

`DECIDED_VIA` 是可解释性的关键：每条行为都能回答"当时依据了哪些知识节点"，
出问题可回放，这也是回写闭环（问题 5）的审计基础。

## 4. 事件模型（写入协议）

写入唯一合法形态（`kb/schema.py::Event`）：

```text
Event {
  event_id:  全局唯一 —— 幂等去重的依据
             由 action_id 派生（如 "act:demo-101:03"）→ 同一执行结果重试天然幂等
  partition: 分区键 = account_id → 同账号串行写、跨账号并发写
  kind:      upsert_node | set_prop | upsert_edge | incr_edge
  payload:   按 kind 定型
  ts:        事件时间
}
```

四种 kind 覆盖全部写入需求：

- `upsert_node`：建节点或合并属性（`None` 表示不覆盖）
- `set_prop`：单属性更新，旧值自动进 `history`（保留最近 10 条，可回溯纠错）
- `upsert_edge`：建边或更新属性；**冲突仲裁：ts 大者胜，同 ts 比 confidence**
- `incr_edge`：聚合边计数累加（`count += n`）——有界压缩的核心

## 5. 冷热分层与归档

| 区 | 内容 | 生命周期 |
| --- | --- | --- |
| 热区 | 未归档 `ActionRecord`（约当天流水） | rollup 后置 `archived=True` |
| 温区 | Post、聚合边、日摘要 `summary_day:*` | 长期，低频更新 |
| 冷区 | 原始数据（帖子全文、页面快照） | 图谱外对象存储，只留 digest |

rollup 见 `kb/rollup.py`：日流水 → `summary_day:YYYYMMDD` 写入账号属性 →
明细归档（保留可追溯，但不进热区查询）。

## 6. 规则的表达方式

规则 = **一条 Rule 数据**（`kind + params + actions + platform`）+ **一个代码 checker**
（`kb/tools.py::RULE_CHECKERS` 注册表）。v1 内置四种：

| kind | 语义 | params |
| --- | --- | --- |
| `min_account_age_days` | 新号限流期 | `days` |
| `rate_limit_per_day` | 每日频控 | `actions, max` |
| `topic_overlap` | 话题必须落在人设兴趣树内 | — |
| `cooldown` | 风控冷却期 | — |

确定性判断全部在代码层执行（阶段一闸门），不消耗 LLM；
新增规则只需加一条数据 + 一个函数，不动存储结构。

## 7. 生产替换路径（MVP → 生产）

| MVP（本实现） | 生产替换 | 接口变化 |
| --- | --- | --- |
| `KnowledgeStore`（进程内 dict） | Neo4j / Postgres | 无（方法签名即读写面） |
| `WriteQueue`（线程队列 + 单消费者） | Kafka / 分区 worker（每账号分区一个消费者） | 无 |
| `search_knowledge` 关键词匹配 | 向量检索（embedding 二值量化） | 无（返回结构一致） |
| `history` 内存保留 10 条 | 图库版本表 / 事件日志回放 | 无 |

## 8. v2 预留

- Topic/Persona 节点增加 `embedding` 属性，`search_knowledge` 换向量召回
- 聚合边增加时间衰减（`weight × 0.95/周`），自动弱化陈旧关系
- Rule 增加 `APPLIES_TO` 精细作用域与有效期（`effective_from/until`）
- 人设演化：Persona 增加版本链，回答"这个账号的人设什么时候变的"
- 多平台：ID 里的 `platform` 段天然支持扩展到 TikTok / Twitter
