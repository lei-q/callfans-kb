# 查询与回写协议 v1（callfans-agent）

> 实现代码：`kb/tools.py`（查询/决策）、`kb/ingest.py`（摄取）、`kb/rollup.py`（压缩）。
> 回答六问中的问题 4（AI 执行任务时怎么查询思考）、问题 5（结果怎么回写）、问题 6（隔离与并发）。

## 1. 工具总览（Agent 调用面）

图谱永远不进上下文，只通过以下原子工具按需取用：

| 工具 | 签名 | 返回 | 成本 |
| --- | --- | --- | --- |
| `get_persona` | `(account_id)` | 人设卡（含兴趣树） | ~200 token |
| `get_account_state` | `(account_id)` | 状态/风险/今日计数/最近行为/最近摘要 | ~200 token |
| `get_topic_context` | `(topic_id)` | 话题标签/父话题/适用规则/近期帖子 | ~150 token |
| `find_interaction_targets` | `(account_id, limit=5)` | 候选对象（强联结=历史互动，弱联结=兴趣重合） | ~100 token |
| `search_knowledge` | `(query, k=5)` | 关键词命中（v2 换向量检索） | ~50 token/条 |
| `check_rules` | `(account_id, action, topic_id?)` | 规则闸门裁决 | 0（纯代码） |
| `decide` | `(account_id, action, topic_id?, llm?)` | 两段式完整决策 | 见下 |
| `submit_result` | `(result_dict)` | 回写统计（含去重数） | 0（写路径） |

## 2. 决策协议：两段式

```text
任务触发
   │
   ▼
阶段一 check_rules ──── 纯代码，毫秒级，零 LLM 成本
   │  规则来源：Rule 节点 × RULE_CHECKERS 注册表
   │  不过关 → 直接返回 blocked（附违反原因），流程终止
   ▼
阶段二 decide ────────── LLM 按需介入
   │  1) build_decision_context：按优先级组装子图，token 预算化
   │  2) llm(context) → 结构化决策 JSON
   │  3) 校验输出 + 补充 knowledge_refs（决策依据）
   ▼
结构化决策 ──→ 执行器（Appium/uiautomator2/adb）
```

确定性判断（限流期、频控、话题匹配、冷却）永远在阶段一用代码完成；
LLM 只负责模糊判断（写什么内容、怎么措辞、和谁互动）。

### 决策输出 JSON

```json
{
  "status": "approved",
  "action": "post",
  "content_type": "text_with_image",
  "topic": "topic:beauty.skincare",
  "persona_fidelity_score": 0.92,
  "generated_content": "……",
  "rationale": "话题与人设兴趣匹配，规则闸门已通过",
  "decision_id": "dec:9f3a…",
  "knowledge_refs": ["per:beauty-lily", "topic:beauty.skincare", "rule:…"],
  "context_tokens": 789
}
```

`llm=None` 时走确定性桩（`_stub_decision`），用于离线验证闭环；
接真模型只需传入一个 `context -> dict` 回调，输出校验只强制 `action` 字段。

## 3. 上下文预算

`build_decision_context` 按**块优先级**组装，超出预算从尾部裁剪：

| 优先级 | 块 | 内容 |
| --- | --- | --- |
| 1 | `persona_card` | 人设卡（我是谁） |
| 1 | `hard_rules` | 适用规则文本（不能违反什么） |
| 2 | `account_state` | 当前状态（现在能做什么） |
| 2 | `topic_context` | 话题与近期帖子（在什么语境下做） |
| 3 | `recent_actions` | 最近行为摘要（刚做过什么，避免重复） |
| 4 | `candidates` | 互动候选（和谁做） |

默认预算 2000 token。超预算时由代码裁剪而不是让 LLM 自己取舍——
保证每次决策的上下文构成是确定、可审计的。

## 4. 回写协议（执行结果 → 知识库）

```text
执行结果 result（执行器产出）
   │
   ▼ extract_events_from_result（kb/ingest.py）
   │  规则映射优先（互动→incr_edge、发帖→Post 节点……）
   │  LLM 兜底抽取可选（llm 回调），候选事实必须过 Schema 校验
   │  事件 ID 由 action_id 派生 → 同一结果重试幂等
   ▼ WriteQueue.submit（kb/store.py）
   │  event_id 去重（重复直接丢弃）
   │  partition = account_id 分区入队
   ▼ WriteQueue.drain → store.apply
   │  边冲突仲裁：ts 大者胜，同 ts 比 confidence
   │  属性更新：旧值进 history（保留 10 条）
   ▼
知识库更新完成，返回 {events, accepted, duplicates}
```

### 分型写入语义

| 结果类型 | 写入方式 | 压缩效果 |
| --- | --- | --- |
| 行为流水 | 新建 ActionRecord + PERFORMED + DECIDED_VIA | 热区数据，rollup 后归档 |
| 互动 | `incr_edge`（count += 1） | **有界**：50 次互动仍是 1 条边 |
| 话题参与 | `incr_edge` FOLLOWS | 同上 |
| 发帖 | Post 节点 + PUBLISHED + ABOUT | 只存 digest，不膨胀 |
| 矛盾信息 | 仲裁：ts/confidence 胜出，败者保留在 history | 可回溯纠错 |

## 4.1 执行层适配协议（kb/executor.py）

适配 `demo/xhs.py` 这类独立 CLI 执行器（调度平台 argv 拉起、标题/正文 base64、
自身不产出结构化结果）。双向桥：

```text
方向一（决策→命令）
  tools.decide() ──▶ plan() ──▶ ExecutionSpec ──▶ build_command() ──▶ xhs.py argv
                     │ 规则闸门拦截 → status=blocked，不生成命令
                     │ 决策内容 split_title_content：首行截 20 字为标题，余为正文
                     ▼ base64 编码，与 xhs.py _decode_from_base64 兼容

方向二（结果→回写）
  执行 outcome ──▶ report() ──▶ result dict ──▶ submit_result（第 4 节协议）
                     │ action_id = act:xhs-<job_id>（清洗为合法 ID）
                     │ 同一 job 重试 → 同一事件 ID → 队列层自动去重
                     │ 失败也回写（outcome=failed，不建 Post 节点）
```

关键约定：

- **幂等锚点 job_id**：`plan(job_id=...)` 与调度平台的任务 ID 对齐；平台重试
  同一 job 不会造成互动/发帖重复计数（FakeRunner Demo 实测 12 事件全部去重）
- **Post 节点稳定派生**：`post:<平台>:<job_id>`，后续补报数据指向同一节点
- **runner 注入**：测试用 FakeRunner，生产直接 subprocess；不需要 sma_autoui
  即可验证全链路
- **stats 补报**：事件 ID 派生自 action_id，同 job 重提 stats 更新会被去重丢弃；
  需要更新数据时用新的 job_id（如 `job-001-stats`）触发独立回写

## 5. Rollup 协议（结果压缩更新）

`daily_rollup(store, account_id)`（建议每日定时执行）：

1. 收集该账号全部未归档 ActionRecord
2. 生成日摘要写入 `summary_day:YYYYMMDD`（`summarizer` 钩子可接 LLM 叙事摘要）
3. 计数快照写入 `summary_counts:YYYYMMDD`
4. 明细标记 `archived=True` —— 保留在图谱中可追溯，但不再进入热区查询

### 5.1 层级 rollup 与记忆（详见 docs/03）

已实现周/月/年卷积（`period_rollup`）：

```text
日 → 周（summary_week:2026W41）→ 月（summary_month:202610）→ 年
每层同时写入 Episode 节点（情景记忆，含人设漂移检查 drift）
```

配套：
- `consolidate_memory`：周度记忆固化，情景 → MemoryNote 语义记忆
  （事实指纹 ID 幂等；category=fact/lesson/milestone/promise）
- `fulfill_promise`：承诺兑现闭环（FULFILLED_BY 边）
- `KBTools.recall_memory`：三因子（relevance×recency×importance）
  记忆召回 + 未兑现承诺置顶，作为决策上下文 memory 块
- `KBTools.audit_decision`：失真审计（引用存在性 + 保真度）
- LLM 钩子（kb/llm.py）：`make_period_summarizer`（周期叙事）、
  `make_memory_extractor`（记忆固化抽取）；实机验证见
  `demo/run_memory_llm_demo.py`；LLM 抽取的 MemoryNote 候选在摄取层
  归一化（指纹 ID + 自动 HAS_MEMORY 边，详见 docs/03 §1.3）

### 5.2 决策预算分级

`decide(budget_tokens=None)` 时按动作风险取预算：
post=3000 / comment=2000 / follow=1000 / like=800 / browse=600。
预算不足按 BLOCK_PRIORITY 裁剪（memory 块位于 account_state 之后）。

### 5.3 公共记忆（共识确证）

`report_signal`：账号上报异常信号 → candidate（不进决策上下文）
→ N 账号独立确证 → confirmed 全局规则 + `effective_at` 灰度生效
（各账号按确定性哈希在 jitter_window 内错峰采纳）。

## 6. 并发与隔离

### MVP（当前实现）

```text
读路径：直接调用，无锁（单进程内存图）
写路径：WriteQueue —— submit 线程安全 + event_id 幂等
        drain 单消费者顺序执行（同分区天然串行）
```

### 生产目标形态

```text
            ┌─→ 只读查询服务（无状态，N 副本水平扩）──→ Agent 任务并发读
知识图谱（主）─┤
            └─→ 分区写入（Kafka，partition=account_id）
                  ├─ worker-1 ──→ 账号分区 1..k   （分区内串行）
                  ├─ worker-2 ──→ 账号分区 k+1..2k（跨分区并发）
                  └─ worker-N …
```

- **数据隔离**：按账号/平台图分区，单账号脏数据不污染全局
- **服务隔离**：查询、写入、LLM 调用、云机执行四类进程独立部署，各自限流熔断
- **任务隔离**：每个账号任务独立 worker，Agent 决策间不共享内存，只经图谱通信
- **幂等**：`event_id = action_id 派生`，消息队列重复投递/网络重试零副作用
- **乐观并发**：节点带 `version`，冲突重读重算，不用悲观锁
- **背压即业务**：每账号频控与随机延迟（反风控要求）天然构成写入限速，
  并发上限 = 账号数 × 频控，不会失控

## 7. LLM 接入层（kb/llm.py）

三个钩子的真实实现已就绪，传输协议为 OpenAI Chat Completions 兼容
（DeepSeek / Qwen / GLM / OpenAI / vLLM 等均支持，零第三方依赖）：

```python
from kb.llm import LLMClient, make_decision_llm, make_extract_llm, make_summarizer

client = LLMClient()                       # 配置：环境变量 CALLFANS_LLM_*
tools.decide(aid, "post", topic, llm=make_decision_llm(client))
tools.submit_result(result, llm=make_extract_llm(client))
daily_rollup(store, aid, summarizer=make_summarizer(client))
```

配置（环境变量或构造参数）：`CALLFANS_LLM_BASE_URL` / `CALLFANS_LLM_API_KEY` / `CALLFANS_LLM_MODEL`。

### 可靠性设计（实机验证过）

| 场景 | 行为 |
| --- | --- |
| 传输错误（429/5xx） | 指数退避重试（1s, 3s） |
| 推理模型思考链吃掉输出预算 | 空输出时 max_tokens 自动加倍重试 |
| LLM 返回垃圾 JSON / 非法字段 | 决策降级桩、抽取候选被 Schema 过滤，业务不中断 |
| 关键字段被篡改 | action/topic 以调用方上下文为准，不信任 LLM 复述 |

### 钩子汇总

| 钩子 | 工厂函数 | 传给 | 说明 |
| --- | --- | --- | --- |
| 决策 | `make_decision_llm(client)` | `decide(llm=…)` | 生成符合人设的内容，temperature 0.8 |
| 抽取 | `make_extract_llm(client)` | `submit_result(llm=…)` | 仅当 result 带 `raw_text` 时调用，temperature 0.2 |
| 摘要 | `make_summarizer(client)` | `daily_rollup(summarizer=…)` | 叙事化日小结 |

实机验证：`demo/run_llm_demo.py`（含故障降级验证）。
