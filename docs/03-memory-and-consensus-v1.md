# 长期记忆与共识协议 v1（callfans-agent）

对应 2026-10-09 讨论的四个问题：个体记忆沉淀 / 隔离与公共记忆 / 检索 / 压缩不失真。

## 0. 分层记忆模型

| 层 | 内容 | 载体 | 生命周期 |
| --- | --- | --- | --- |
| 工作记忆 | 未归档行为明细 + 账号状态 | ActionRecord 热区 | 滚动（日 rollup 后归档） |
| 情景记忆 | 周/月/年卷积叙事 | `Episode` 节点 + `summary_*` 属性 | 永久（叙事，人设漂移可回溯） |
| 语义记忆 | 沉淀的稳定事实/教训/里程碑/承诺 | `MemoryNote` 节点 | 永久；`pinned` 里程碑不衰减 |
| 公共记忆 | 平台规则、大事件、跨账号教训 | 确证后的 `Rule` 节点 | 全矩阵共享，灰度生效 |

"完整记忆"≠"保存一切"：原始层只存指针（source_uri），图谱只存结构与叙事，
叙事连续性（兑现承诺、引用历史内容）才是"有血有肉"的来源。

## 1. 个体记忆：层级卷积 + 固化（kb/rollup.py）

### 1.1 层级 rollup

```text
日：daily_rollup   行为流水 → summary_day:YYYYMMDD（已有）
周：period_rollup(level="week")   日摘要 → summary_week:2026W41 + Episode
月：period_rollup(level="month")  周摘要 → summary_month:202610 + Episode
年：period_rollup(level="year")   月摘要 → summary_year:2026 + Episode
```

- 每层卷积同时做**人设漂移检查**：期内动作话题分布 vs 人设兴趣重合度，
  漂移分写入 Episode（>20% 在叙事中标记），人设演化靠属性 history 回溯
- 跨月周（ISO 周一归属月）由 `_week_month` 统一裁决
- 周期 key 规范：week=`YYYYWww`、month=`YYYYMM`、year=`YYYY`

### 1.2 记忆固化（consolidate_memory）

每周跑一次：读近期 Episode / 帖子表现 / 失败记录 → 沉淀 MemoryNote。

- extractor 钩子可接 LLM；失败/未传回退确定性兜底（高赞帖、话题重心、失败教训）
- **事实指纹 ID**：`note:md5(fact)[:10]`——同一事实重复抽取指向同一节点，天然幂等
- category：`fact`（稳定事实）/ `lesson`（教训）/ `milestone`（里程碑，pinned）/
  `promise`（对粉丝的承诺，status=open → 进入兑现追踪）

### 1.3 承诺追踪（连续性闭环）

```text
承诺产生（两条路径）：
  a. 反馈抽取：raw_text 中的承诺性反馈 → LLM 抽取钩子
  b. 生成埋坑：决策内容里新埋的承诺（"下周实测"）→ track_promises
     （ExecutorAdapter.report 发布成功后自动扫描标题+正文）
   → MemoryNote(category=promise, status=open) + COMMITTED_IN → 帖子
决策置顶：build_decision_context 的 memory 块，open_promises 排最前
兑现闭环：fulfill_promise(note_id, by_ref)
   → status=fulfilled + FULFILLED_BY → 兑现它的帖子/行为
```

track_promises 的 scanner 钩子（make_promise_scanner）可接 LLM；
缺省用确定性关键词兑底（未来时点标记 × 承诺动词，保守取向：
宁漏判不误报）。指纹 ID 保证重复扫描不膨胀。

**摄取归一化（kb/ingest.py，LLM 实机验证后加固）**：LLM 抽取的
MemoryNote 候选 ID 统一重写为事实指纹（note_id_of），引用它的边同步
重映射；HAS_MEMORY 归属边由系统确定性补齐（不依赖 LLM 自觉）；
ts/pinned/confidence 等结构性字段由系统补默认值；
COMMITTED_IN/FULFILLED_BY 错指另一条 MemoryNote 的候选边直接丢弃；
WriteQueue.drain 对端点不合法的候选事件只计 rejected 不中断业务。

### 1.4 时效淘汰与人可读摘要（借鉴 Proma 原则）

- `expire_stale_notes`（周维护自动执行）：fact/lesson 类语义记忆超过 90 天
  未更新且未 pin → status=stale，保留可追溯但**不再进入召回**；承诺有独立
  生命周期、pinned 里程碑永不淘汰
- `memory_digest`：账号记忆的 Markdown 摘要（CLI `kb memory` / GUI 检查器 /
  人工导出），遵循"分层路由 + 索引只做路由"——情景记忆只列周期与一行摘要
  （按 Episode ID 下钻全文），语义记忆全文，承诺置顶

## 2. 隔离与公共记忆（kb/consensus.py）

- **私有记忆严格分区**：账号只写自己分区（WriteQueue partition=account_id）。
  隔离不仅是数据卫生，是风控刚需——平台通过账号间行为关联识别矩阵
- **公共区只由确证机制写入**：单账号遭遇的异常只是候选信号（candidate）；
  N 个（默认 2）独立账号确证才固化为全局规则。confirmations 按账号去重（幂等）
- **灰度生效**：confirmed 后设 `effective_at`；每个账号按确定性哈希
  `(rule_id, account_id)` 在 jitter_window 内错峰采纳——全员同日改变行为
  模式本身就是机器特征。无 status 字段的种子规则视为始终生效（向后兼容）
- 候选信号不进决策上下文（hard_rules 只含对该账号当前生效的规则）

## 3. 检索（kb/search.py）

- **只索引摘要层**：label/digest/fact/narrative/summary_*；原始层不付检索成本
- **bigram 倒排索引**：零依赖中文语义粗召回（"防晒"→ 防/晒/防晒），
  接口形状与向量检索一致，接 embedding 时替换实现（索引层不变）
- **分层下钻**（drill_down）：年/月摘要定位 → 周摘要 → 日明细 →
  高风险决策才按 source_uri 下钻原始层。年度定位只需 2-3 跳
- 入口是**决策需求**派生的 query（action + topic label），预算固定 top-k

## 4. 压缩不失真（kb/tools.py）

原则：**失真不可消除，只能管理；不可追溯的失真才可怕**。

1. **结构化数据零压缩**：数字/时间戳/计数永远精确存原值，失真只发生在
   自然语言摘要区，且永远有 source_uri 指回原始层
2. **三因子记忆召回**：relevance × recency × importance（pinned 加权），
   未兑现承诺置顶（连续性优先于相关度）
3. **风险分级预算**：`RISK_BUDGETS = {post: 3000, comment: 2000, follow: 1000,
   like: 800, browse: 600}`——高风险动作允许更大预算/更细记忆；
   预算不够时按 BLOCK_PRIORITY 从低优先块裁剪
4. **失真审计**（audit_decision）：决策引用的知识节点存在性 + 保真度 +
   内容长度检查；配合 DECIDED_VIA 回写形成"摘要质量 → 决策依据 → 审计反馈"
   闭环

## 5. 验证

`python3 demo/run_memory_demo.py`（无 LLM/真机）覆盖 10 项：
周/月卷积、漂移检查、固化、承诺兑现闭环、倒排召回 + 下钻、
候选→确证→灰度、跨层联动（确证规则真实拦截发帖）、预算分级、审计、幂等。
