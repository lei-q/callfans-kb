# 需求评审与重定位 v1（callfans-agent → 通用记忆内核）

> 评审对象：2026-10-10 提出的「我想要实现一个拥有长期记忆的 agent 工具」一句话需求
> 评审方法：6 视角独立评分 → 6 轮对抗反驳 → 3 份通用化架构方案 → 人工综合裁决
> 本文所有分数与结论**已按对抗反驳修正**；被推翻的原始论断在第 9 节留档，不作为决策依据。
> 代码事实核查基于 `/Users/lay/.proma/agent-workspaces/callfans-agent/workspace-files`（下称「工作区」）。

## 0. 结论摘要

1. **需求原样值 5.5 分，按本轮修订值 7.5 分。** 分差来自定位与范围，不来自代码质量。
2. **最大的一条判断修正是：项目的已有资产被严重低估了。** 工作区里 5351 行 Python 是跑绿的（四个离线 demo 实机复核通过），Tauri 桌面壳已编译完成，`lark-cli` 已以用户身份完成 13+ 企业级 scope 的 OAuth 授权。评审组最初把「代码不存在」当成事实，是工具盲区（Spotlight 不索引点目录 `.proma`）造成的误判。
3. **真正的架构级未决问题只有一个：记忆主体模型。** 需求第 4 条「知识库作为 agent 的行为动机」与「每个账号都是独特的数字生命」这两句话，在当前 Schema 下互相冲突，且冲突已导致一个可复现的 bug（`kb/rollup.py:309`）。这一条必须在写第一行 TypeScript 之前定案。
4. **通用化的答案是「薄内核 + 领域包」**，而切分线不是猜的——`deepseek_markdown` §2/§4 的软著申请本体已经独立长成了与社交矩阵完全相同的骨架，两个领域一个模式，证据在仓库里。
5. **能力 1（全域扫描）与能力 2（三库）都该大幅降级**：前者靠已授权的 CLI 委托 + MCP 生态供给，后者靠实现三个已成事实的标准（SKILL.md / MCP / AGENTS.md），不自建生态。

### 0.1 本轮定案（2026-10-10，用户确认）

| # | 议题 | 定案 | 详见 |
| --- | --- | --- | --- |
| 1 | 主体模型 | **方案 B**：MemoryNote 节点只存 `fact`（保持全局指纹），归属与主观状态（`status`/`pinned`/`ts`/`confidence`）移到 `HAS_MEMORY` **边上** | §3.5 |
| 2 | 性格层怎么涌现 | **独立 trait 层**，载体是 `MemoryNote{category:'trait'}`（不新增节点类型）；不直接改 Persona 属性 | §3.8 |
| 3 | 技术栈 | **Python 内核 + TS 前端**。5351 行零重写，「零第三方依赖」保住，存储与分发的 spike 全部取消 | §7.1 |
| 4 | pack-sdk | **不投**那 2.3 人月。通用化从**产品能力**降级为**架构属性**：保持接缝清晰，不建框架 | §6.5 / §6.7 |
| 5 | 代码位置 | `kb/` 与 `demo/` 已拷入本工程（5351 行，diff 与源一致），**在工程内改造** | §2 |
| 6 | 视图层 | **TUI 不做**（之后有需要再说）；操作面 = CLI + Web 控制台，Linux 服务器部署用 `kb serve --no-browser` + systemd（`docs/04 §6`） | §7 / §10.1 |

**代码已入库并复核（2026-10-10，新位置实机跑绿）：**

| demo | 结果 |
| --- | --- |
| `run_demo.py` | 节点 43 / 边 53；队列 submitted=127 / duplicated=24 / applied=127；rollup 1892→234 字符 |
| `run_memory_demo.py` | 10 项全覆盖；300 token 硬预算只保留 `hard_rules`+`persona_card`；`audit_decision ok=True`；重复固化 44→44 零膨胀 |
| `run_executor_adapter_demo.py` | job_id 幂等锚点；`split_title_content` 首行 20 字截断 |
| `run_sqlite_demo.py` | 双进程 applied=4 / duplicated=4；`count=3` 精确 +1；新进程读回 nodes=31 / edges=39 / events=86；maintenance 幂等 |
| `python3 -m kb status` | 后端 memory，CLI 冒烟通过 |

> **这是改造前的回归基线。任何一次重构后这四个 demo 必须仍然全绿。**

## 1. 定位与评分

### 1.1 定位（用户 2026-10-10 确认）

初期满足**自己用**，中后期升级成**团队内部工具**。不做对外产品（至少现阶段不做）。

这个定位把两处设计的优先级反转了，必须记下来：

| 机制 | 单人阶段 | 团队阶段 |
| --- | --- | --- |
| 共识确证（`kb/consensus.py`） | 几乎无用（N=1，quorum 凑不齐） | **最值钱的语义** |
| 跨进程幂等（`store_sqlite.py:174`） | 无用 | 必需 |
| 记忆 scope 隔离 | 弱需求 | **合规刚需**（A 部门数据不得影响 B 部门决策） |

⚠️ **现在就改、以后改很贵的一条**：把共识 quorum 从「N 个独立**账号**」改为「N 个独立**来源**」（`kb/consensus.py:70` 的 `confirmations` 目前按 `account_id` 去重）。改完之后单人阶段多数据源独立印证也能晋升信念，公共记忆层不会在 v1 就死掉，团队版语义无缝升级。

### 1.2 评分

**六视角原始分 → 对抗反驳后修正分：**

| 视角 | 原始 | 修正 | 反驳结论 |
| --- | --- | --- | --- |
| 产品定位与切入点 | 3.5 | **5.5** | refuted=True：头号 fatalFlaw（代码不存在）是假的；四个 demo 实机跑绿 |
| 数据接入与合规 | 3.5 | **5.0** | refuted=True：35–55 人月是「自建 Glean 级连接器套件」的价格，不是需求的属性；lark-cli 已授权 |
| 自我迭代闭环的风险 | 4.0 | **6.0** | refuted=False，但两个 blocker 的机制都指错了地方，严重度虚高 |
| 范围与工程可行性 | 4.0 | **5.5** | refuted=True：两套互相矛盾的估算口径，只有悲观那套在驱动分数 |
| 记忆系统 | 4.5 | **5.5** | refuted=False，但严重度轴塌了一半，且有两处硬错误、一处编造机制 |
| 通用化架构 | 6.5 | **5.0** | refuted=True：**唯一被下调的视角**。「七成机制领域无关」无出处，且其中相当部分在桌面单机形态下根本不运行 |
| **均值** | **4.33** | **5.42** | 反驳使分歧从 3.0 收敛到 1.0 |

**最终评分（人工综合裁决，纳入反驳后新核实的事实）：**

| 情形 | 分 |
| --- | --- |
| 需求按原样（定位缺失、能力 1 无边界、主动扫描语义未定） | **5.5** |
| **本轮修订后**（定位钉死 + 主动扫描语义确定 + 能力降级 + 保留三视图） | **7.5** |
| 天花板（补齐主体模型 + 双时态 + 信任分级 + 删除语义 + Goal 层 + 记忆评测） | **8.5–9.0** |

### 1.3 为什么修正后仍然只有 7.5

反驳环节把「严重度通胀」压下去了，但下面这些缺口是**反驳者自己在试图推翻时 grep 复核确认的**（在 docs/01–04 全文中出现次数为 0）：

`trust` / `scope` / `双时态` / `valid_from` / `valid_to` / `Session` / `Goal` / `Intention` / `Drive` / `动机` / `DERIVED_FROM` / `last_recalled` / `tombstone` / `delete` / `MCP` / `skill` / `hook` / `OAuth` / `凭证` / `互联网`

也就是说：**缺陷是真的，但它们是设计阶段的工作，不是需求阶段的罪名。** 这是本次评审方法论上最重要的一条自我修正——评审组最初把「一句话需求」当架构文档打分，再按架构缺陷扣分。7.5 的含义是「方向对、资产足、范围已收敛，剩下的是一次严肃的记忆层重新规格化」。

## 2. 事实核查（本轮修正）

| 项 | 评审组最初的说法 | 核实结果 |
| --- | --- | --- |
| `kb/` 代码 | 本机不存在，所有「已验证」不可核验 | **存在**，工作区 18 个模块 / 5351 行；`docs/` 目录是 docs-only 镜像，`AGENTS.md` 与原件 diff 一致 |
| 四个离线 demo | 不能作为决策依据 | **实机跑绿**：`run_demo`（12 事件首次全 accepted、重试全 duplicated、并发 3 账号去重、rollup 1894→234 字符）；`run_memory_demo`（10 项全覆盖，含灰度抖动 lily=2812s / kai=1516s、300 token 硬预算裁剪、重复固化 44→44 零膨胀）；`run_sqlite_demo`（双进程 applied=4/duplicated=4、count=3 精确 +1、新进程读回 nodes=31/edges=39/events=86）；`run_executor_adapter_demo`（job_id 幂等锚点） |
| GUI 成本 | 4.5–8 人月纯成本项 | **Tauri 已做完并编译过**：`tauri-app/src-tauri/target` 2.2GB、`binaries/callfans` sidecar、四平台 conf、图标与 capabilities 齐备 |
| 企业云连接器 | 每个 1–2 人月 + 企业应用审批流 | **已授权**：`lark-cli` v1.0.97 以用户身份完成 OAuth，scope 覆盖 消息/群组、云文档、云空间、多维表格、电子表格、日历、邮箱、任务、知识库、妙记、OKR、审批、通讯录。边际成本 = 写一个「已授权 CLI → Event 契约」适配器（天到周量级） |
| 凭证保管 | 全系统唯一凭证是明文环境变量 | 不完整：`AGENTS.md:37` 已有成文密钥卫生策略（「凭证由 CLI 自身安全存储；不要把 token/cookie/OAuth code 写入本仓库任何文件或 mcp.json」） |
| 「主动扫描」语义 | 未定义，是两个不同产品 | **用户 2026-10-10 定案**：用户圈定范围后 agent 定期执行。且 `AGENTS.md:52` 早已是成文策略（「历史会话仅在用户授权后作为分批、限量的补充证据，不得全量扫描」） |
| 合规炸弹 | 企业凭证 × 自由接 LLM = 数据出域 | **读法错误**：需求写的是「需提供凭证」= BYO 凭证的个人/prosumer 工具，不是厂商托管企业租户的 SaaS。你不保管凭证、不走企业应用审批（飞书=spawn lark-cli，GitHub=spawn gh，个人云=rclone）。风险仍在但量级小一个数量级 |
| Schema 文档漂移 | `docs/01 §0` 6/9 vs §2/§3 8/13 自相矛盾 | **成立，但降级**：代码是 8/13（`kb/schema.py:17-18` / `:39-53`），与 §2/§3 一致。所以这是 §0 散文陈述过期，不是定义与校验之间失去一致性。修文档即可 |

## 3. 主体模型：从 Account 到 Subject（核心架构决定）

> 这是本轮唯一的架构级未决问题，也是需求第 4 条能否成立的前提。

### 3.1 用户的问题

「这批要维护的账号，每个都有专属的人物背景，每个性格都是独特的，目标是把他们打造成有血有肉的、具有真人感的数字生命。这些账号是不是也应该有自己的记忆空间？当前的设计能满足吗？」

**答案：应该有，当前设计给了一半，另一半是矛盾的，而且矛盾已经产生了一个可复现的 bug。**

### 3.2 代码级核查

| 记忆类型 | 是否已是每账号独立空间 | 证据 |
| --- | --- | --- |
| 情景记忆 Episode | ✅ 是 | `kb/rollup.py:179` `ep_id = f"ep:{_account_slug(account_id)}.{period}"` —— handle 编在 ID 里 |
| 写入分区 | ✅ 是 | `kb/schema.py:64` `Event.partition`；WriteQueue 按分区串行 |
| 语义记忆 MemoryNote | ❌ **不是** | `kb/rollup.py:198-200` `note_id_of(fact) = "note:" + md5(fact)[:10]` —— **全局内容指纹，与账号无关**。归属只靠 `HAS_MEMORY: Account→MemoryNote` 边 |
| 人设 Persona | ❌ **与「每个性格独特」直接冲突** | `docs/01 §2` 明写「多账号可复用同一人设」；`kb/tools.py:414` 注释「persona_id 已存在则复用」；且 Persona 标注为**冷（稳定低频更新）**，属性是 name/age_band/occupation/mbti/language_style/bio —— 一张**设定表**，不是**成长中的性格** |
| Persona 是否持有记忆 | ❌ 否 | `HAS_EPISODE` / `HAS_MEMORY` 的起点都是 **Account**（`kb/schema.py:49-50`）。性格设定与人生经历挂在两个互不相连的节点上 |

### 3.3 由此产生的三个具体后果

**(1) 跨账号记忆串号 —— 可复现 bug**

`kb/rollup.py:309`：

```python
account_id = next(s for s, _, _ in store.in_edges(note_id, "HAS_MEMORY"))
```

`fulfill_promise` 取**第一个** owner。若一条 MemoryNote 被 A、B 两个账号共享（同一句 fact 文本 → 同一指纹 → 同一节点）：

- A 兑现承诺 → `set_prop status=fulfilled` 写在**共享节点**上 → `open_promises(store, B)`（`kb/rollup.py:292` 遍历 B 的 `HAS_MEMORY`）也看不到这条了 → **B 从没做过，但 B 的承诺被标记为已兑现**
- 反向同理：A、B 都发了「下周实测防晒」→ 一个节点 → 任一个兑现即两个都算兑现

**(2) 隐式风控指纹 —— 与 `docs/03 §2` 的立论冲突**

`docs/03 §2` 说隔离是风控刚需（平台通过账号间行为关联识别矩阵）。但一个**跨账号共享的 MemoryNote 节点**，在图里就是一条账号间的结构性关联：两个账号只要产出过一句相同文本，就被连起来了。你会小心地不建 `INTERACTS_WITH`，但 `HAS_MEMORY` 指向同一节点是一条**你没打算建的隐式关联边**。

**(3) 跨平台失忆 —— 与 `docs/01 §8` 的 v2 预留冲突**

Account ID = `acc:{platform}:{handle}`（`kb/schema.py:26`），记忆挂在 Account 上。所以同一个人格在 TikTok 和小红书上是**两个 Account 节点 = 两个互不相通的记忆空间**。而 `docs/01 §8` 写着「多平台：ID 里的 platform 段天然支持扩展到 TikTok / Twitter」——**它不是天然支持，是天然分裂**。一个换了平台就失忆的东西不是数字生命，是每平台一个副本。

### 3.4 结论：矛盾，且矛盾点精确定位

```text
现状：Account(平台句柄) ── 同时是 ──> 记忆主体 + 分区键 + 操作对象
      Persona(设定表)   ── 冷、可复用、无记忆、与 Account 只有一条 HAS_PERSONA

需要：Subject(数字生命) ── 记忆主体 + 分区键
      ├── 设定层 declared（出身/职业/MBTI —— 冷）
      ├── 性格层 emergent（从经历长出来的偏好/口头禅/价值观 —— 温、演化）
      ├── 情景记忆（我的经历 —— 私有、第一人称）
      ├── 语义记忆（我相信什么 —— 私有、带来源）
      ├── 关系记忆（我和具体某人的事 —— 不能只有 count）
      └── 承诺（我说过要做什么 —— 私有）
      Account = Subject 在某平台上的一个句柄（1 Subject : N Account）
```

**改动量评估：是主体模型的重新定位，不是加字段。但机械部分比看起来少：**

- `partition` 已经是 Event 上的一个字符串字段（`kb/schema.py:64`），从 `account_id` 换成 `subject_id` 是机械改动
- `HAS_EPISODE` / `HAS_MEMORY` 起点类型改成 Subject 是机械改动
- 真正需要设计的是：Subject↔Account 的 1:N 关系、性格层怎么从记忆中涌现、MemoryNote ID 的 scope 化

### 3.5 MemoryNote 的修法 —— **已定案：方案 B**（2026-10-10）

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| A（否决） | `note:{scope}:{fingerprint}` —— ID 里带主体 | 简单，但丢掉了跨主体去重（同一条客观事实无法被多主体共享，而这正是共识机制的基础） |
| **B（定案）** | 节点只存 `fact`（保持全局指纹），**把归属与主观状态移到 `HAS_MEMORY` 边上**（`status` / `pinned` / `ts` / `confidence` 都在边上） | 保留客观事实的跨主体共享，同时让主观状态私有。承诺尤其应该这样——承诺本质是**主体特有的义务**，不该被内容指纹全局化。顺带修掉 `rollup.py:309`：status 在边上，`fulfill_promise` 必须显式指定 subject，不能 `next(...)` 猜 |

**落地时的具体改动面（Python 侧）：**

| 位置 | 现状 | 改动 |
| --- | --- | --- |
| `kb/rollup.py:271-279` | `upsert_node` 写 `status`/`pinned`/`confidence`/`ts` 到节点 props，再 `upsert_edge` 建 `HAS_MEMORY` | 节点只写 `fact`/`category`；四个主观字段改写进 `HAS_MEMORY` 边的 props |
| `kb/rollup.py:289-299` `open_promises` | 遍历 `HAS_MEMORY` 拿 dst，再读**节点** props 判 `category`/`status` | `category` 仍读节点（客观），`status` 改读**边** props |
| `kb/rollup.py:302-314` `fulfill_promise` | `next(...)` 猜第一个 owner | 签名改为 `fulfill_promise(store, note_id, subject_id, by_ref)`，`set_prop` 改为 `upsert_edge` 更新该 subject 的边 |
| `kb/rollup.py:320-333` `record_promise` | 同 `:271` | 同上 |
| `kb/tools.py:366-377` 记忆摘要 | 读节点 props 的 `status`/`pinned` | 改读边 props |
| `kb/rollup.py:373` `expire_stale_notes` | 遍历节点 | 改为遍历边（淘汰是主体特有的：同一条事实对 A 过期不代表对 B 过期） |

> 注意 `EDGE_TYPES["HAS_MEMORY"] = ("Account", "MemoryNote")`（`kb/schema.py:50`）—— 主体模型改造后起点类型要变成 `Subject`，这是 §3.4 的机械改动之一。

### 3.6 关系记忆 vs 有界聚合（真人感的真实张力）

`INTERACTS_WITH` 只有 `count` / `weight` / `last_active`（`kb/store.py:128-135`）。有界聚合是可扩展性必需的（`docs/01 §0.3`：50 次互动仍是 1 条边），但**真人感来自「我记得她妈妈的病」，不是「互动 47 次」**。

解法不是放弃聚合，而是**把 `docs/03` 的层级 rollup 从时间维度复制到关系维度**：聚合边保留 count 做频控，另外为每对关系维护一个**有界的显著记忆集**（MemoryNote scoped to `(subject, counterpart)`），靠固化机制保持有界——不是无限记，是记「最显著的 N 条 + 定期卷积成关系叙事」。

> 时间维度已有 rollup，关系维度还没有。这是同一个思想的对称补全，不是新机制。

### 3.7 通用性：数字生命不是特例，是一般情形

「persona 是一个**没有自己进程的记忆主体**（被 agent 扮演）」——这不是社交矩阵的特例，而是**代理主体（delegated subjecthood）**的一般情形：品牌人格客服、虚拟主播、游戏 NPC、企业里「以某个 IP 身份发言」、法律领域「以某个当事人视角」。

所以 `Scope` 应该是内核一等公民，带 `kind`：

```text
Scope{ kind: person | team | agent | persona | project | client, id, parent }
```

- `partition = scope_id`（Account 退化为 social pack 里 Scope 的一个子类）
- 读路径做最小权限过滤：persona scope 只读自己 + 继承的矩阵级已确证规则，**不读兄弟 persona 的私有 scope**（这同时满足风控刚需与团队版合规刚需）

### 3.8 性格层怎么涌现 —— **已定案：三层人格模型**（2026-10-10）

> 用户原话：「我不清楚，哪个能实现『数字生命会长大』就用哪个。」

**结论：独立 trait 层，不直接改 Persona 属性。但 trait 不是新节点类型——它是 `MemoryNote{category:'trait'}`，复用已有机制。**

两个候选方案的差别不在实现成本，而在**「长大」是可审计的成长还是不可控的漂移**：

| | (a) 从 Episode 卷积出 Persona 属性增量 | **(b) 独立 trait 层（定案）** |
| --- | --- | --- |
| 载体 | `set_prop` 直接改 Persona 字段 | `MemoryNote{category:'trait'}` |
| 「为什么变」 | **不可回答**：history 只留 10 条（`kb/store.py:17`），而性格是慢变量，半年演化追溯不了 | 每条 trait 带 `DERIVED_FROM` → 支撑它的 Episode / ActionRecord |
| 变化惯性 | 无（一次 rollup 就能改） | 有：candidate → K 个**独立周期**确证 → belief；反证只降权不翻转 |
| 用户能否纠正 | 只能手动改回字段 | **能否决 → 持久墓碑**（「她不是这样的人」） |
| 人设会不会崩 | 会（LLM 直接改 `mbti`/`bio`） | 不会：trait 与设定层冲突时**不覆盖，产生 Contradiction 暴露给用户** |
| 新增机制 | 需要新的写入路径 | **零**——`category` 已有 fact/lesson/milestone/promise（`kb/rollup.py:273`），加一个 `trait` |

**三层人格模型：**

```text
Persona  设定层 declared  冷  运营/用户写的  agent 不得自动修改
   ↕ 冲突时设定层胜出，冲突本身暴露给用户（人设护栏）
Trait    性格层 emergent  温  MemoryNote{category:'trait'} + DERIVED_FROM 证据 + confidence
   ↑ 由 consolidate_memory 从 Episode 提名 candidate，走第 8 节晋升管线
Episode  经历层           已有（kb/rollup.py:179）
```

**四个接入点（改动量都很小）：**

1. **提名** —— `consolidate_memory`（`kb/rollup.py:264-286`）已在跑周度固化，加一个 trait 抽取器：从本期 Episode + 行为分布提名 candidate（例：「最近三次都在评论区主动安慰人 → candidate: 共情倾向高」）。确定性兜底与现有 `_stub_extractor` 同款，LLM 钩子走 `make_memory_extractor`（`kb/llm.py`）。
2. **晋升** —— 复用 §1.1 已定案的那条改动：quorum 从「N 个独立**账号**」改为「N 个独立**来源**」，这里的独立来源就是**时间上的独立周期**。同一条 trait 语义在 K 个不同周期出现 → 晋升为 belief。
3. **影响行为** —— `BLOCK_PRIORITY`（`kb/tools.py:29`）的 `persona_card` 块 = 设定层 + **已晋升的 trait**。这是「性格影响行为」的唯一接入点，改动是一个 builder 函数（`kb/tools.py:48-53` `get_persona`）。
4. **演化可见** —— trait 的 confidence 时间序列 → GUI 画「性格演化曲线」，用户能看到「她这半年共情倾向从 0.4 升到 0.8，证据是这 7 条」。这是 `docs/04 §1.3` 三栏控制台「检查器」栏的自然扩展。

**验收标准**（因为「没有 benchmark 的独特设计等于没有独特设计」）：

- 注入 90 天合成流水（前 30 天偏 A 行为、后 60 天偏 B 行为）→ trait 应在**第 4–6 周出现 candidate、第 8–10 周晋升**，且 `DERIVED_FROM` 能指回具体 Episode
- **反证注入**：晋升后注入 10 条反向行为 → confidence 衰减但**不立即翻转**（惯性可测）
- **人设护栏**：注入与 Persona 声明冲突的 trait → 产生 Contradiction，**不覆盖设定层**

> **关键区别：「会长大」不是让 Persona 字段变化，而是让 trait 层的证据链增长。** 前者是不可控漂移，后者是可审计成长。这个区别就是「数字生命」和「随机游走」的区别。

**顺带解决 §5 缺口 6**：trait 走 MemoryNote 通道，就自动获得 `HAS_MEMORY` 边上的主观状态（§3.5 方案 B），因此可以带 `due_at`、可以被淘汰、可以被否决——而 Persona 字段三样都做不到。

## 4. 多 agent 协作：台账 / 幂等 / 租约 / 共识 四层辨析

> 用户 2026-10-10 提问：「单人阶段 agent 知道昨天哪个账号发了；团队版两个 agent 共同维护一批账号时要相互告知，否则重复发。到此我对共识确证的理解和你一致吗？」

**不一致。而且按这个理解实现，你举的例子会失败。** 你把三个不同的机制混成了一个：

| 机制 | 你的例子对应哪个 | 一致性 | 阈值 | 延迟 | 现状 |
| --- | --- | --- | --- | --- | --- |
| **共享工作台账** | 「昨天谁发了，今天先安排没发的」 | 强一致、立即可见 | 无 | 0 | ✅ 已有：`ActionRecord` + `PERFORMED` + `get_account_state()` 的 `today_action_counts`（`kb/tools.py:72`） |
| **跨进程幂等** | 「两个 agent 相互告知」 | 唯一约束 | 无 | 0 | ✅ 已有且实测：`store_sqlite.py:174` `INSERT OR IGNORE`（双进程 applied=4/duplicated=4） |
| **共识确证** | —— | 最终一致 | **N≥2 独立确证** | `effective_at` + `jitter_window` | ✅ 已有，但**只作用于 Rule**（`kb/consensus.py` 全 91 行都是 Rule-scoped） |

### 4.1 为什么用共识做「今天谁发了」会失败

`kb/consensus.py:72`：`status = "confirmed" if confirmed else "candidate"`
`kb/consensus.py:40-41`：`if status != "confirmed": return False`
`docs/03 §2`：「候选信号不进决策上下文」

单个 agent 上报「账号 X 今天发过了」→ 只有 1 个确证 → `status=candidate` → `rule_active_for()` 返回 False → **对决策不可见** → 另一个 agent 照发 → **重复发帖，正是你想避免的**。

「今天谁发了」根本不需要共识。它需要的是**你已经有的那个台账**：`build_decision_context` 的 `account_state` 块已经包含「今日行为/最近行为」（`kb/tools.py:356-357`），agent B 读共享存储就已经看得到 agent A 做过什么。

### 4.2 团队版真正缺的是「租约（claim）」，不是共识

**已核实：`kb/` 全库没有 claim / lease / reserve 机制。**

竞态如下：

```text
两个 agent 同时为同一账号决策
  → 都读 today_action_counts（都不含对方尚未执行的动作）
  → 都通过 _check_rate_limit（kb/tools.py:550-552）
  → 各自生成【不同】的 job_id
  → 各自执行 → 双发
```

`event_id` 幂等只在 **job_id 相同**时生效（`kb/executor.py:88-90`）。台账去重的是**记录**，不是**动作**。

**修法（小，且是内核原语）**：原子 claim —— `claim(subject_id, action, period)`，靠唯一键实现（SQLite `INSERT OR IGNORE` on `(subject, action, period)`，Neo4j 唯一约束）。胜者执行，败者跳过。外加租约过期以处理 agent 崩溃。

> 这个机制对任何多 agent 系统都成立，**属于内核，不属于领域包**。

### 4.3 回答「记忆层是不是只服务单人或团队的 agent」

不是。记忆层服务的是**主体（Scope）**，而主体有三类，这正是你问题里在绕的东西：

1. **人（用户）** —— 你自己
2. **agent（执行体）** —— 有进程
3. **persona（数字生命）** —— **没有自己的进程，是被 agent 扮演的记忆主体**

三类主体之间的记忆流动规则不同：

| 关系 | 规则 |
| --- | --- |
| persona ↔ persona | **禁止**共享私有记忆（风控刚需 + 数字生命的个体性） |
| agent ↔ agent（服务同一批 persona） | **必须**共享工作台账（强一致）+ claim 租约（防双发） |
| agent/persona → 公共知识 | 走共识确证（candidate → N 独立来源确证 → 灰度生效），有阈值、有延迟 |

## 5. 记忆层的六个缺口（反驳后仍然成立）

按「必须在写第一行 TS 之前决定」排序。严重度已按对抗反驳修正。

| # | 缺口 | 证据 | 严重度 | 为什么现在必须定 |
| --- | --- | --- | --- | --- |
| 1 | **主体模型**（第 3 节） | `kb/schema.py:49-50`、`kb/rollup.py:198-200,309` | blocker | partition 键与记忆归属是存储层地基，事后改=重做 |
| 2 | **无双时态**：没有 valid time（事实何时成立）与 transaction time（agent 何时得知）的区分 | 全文 grep `valid_from`/`valid_to` = 0；边只带一个 `ts`（`kb/schema.py` EDGE 元属性） | blocker | 能力 1 批量扫描落地后，晚入库的旧事实会吃掉早入库的新事实；且无法回答「agent 当时以为什么」 |
| 3 | **无删除语义**：事件日志不可删 + 指纹无 tombstone | `docs/04 §4`「事件即真相，KBEvent 保留全部历史」；grep `tombstone`/`delete` = 0 | blocker | 必然 bug：用户删掉「我住在杭州」→ 下次 `consolidate_memory` 重读租房合同、换措辞再抽 → md5 不同 → 连去重都命不中 → **被删事实以新 ID 复活** |
| 4 | **无来源信任分级** | grep `trust`/`scope` = 0；边元属性 `source` 是字符串不是信任等级；`docs/03 §1.3` 明写 confidence「由系统补默认值」 | blocker | 能力 1（扫互联网）× 能力 2（skills/MCP 有真实副作用）= 持久化投毒管道。原业务爆炸半径是「最多发一条帖子」，通用工具是「你的文件系统和企业云凭证」 |
| 5 | **无 DERIVED_FROM 边** | `kb/schema.py:39-53` 指向 MemoryNote 的只有 HAS_MEMORY / COMMITTED_IN / FULFILLED_BY | major | `audit_decision` 只能查图谱内部一致性，**查不出「摘要把 3000 说成了 30000」**，因为原始值不在图谱里也没有边指回去。与 `docs/03 §4.1`「永远有 source_uri 指回原始层」自相矛盾：派生链是断的 |
| 6 | **承诺缺 due_at / 状态过少 / 豁免淘汰** | `kb/rollup.py:276` `status = "open" if promise else "stable"`；`docs/03 §1.4`「承诺有独立生命周期、pinned 永不淘汰」；`kb/tools.py:359` open_promises 置顶 | major | 「下周实测」没有截止时间 → 永不判逾期 → 永不 stale → **半年后决策上下文最贵的槽位堆满早已无意义的 open 承诺**，把真正相关的记忆挤出预算。缺 conditional / violated / expired / cancelled / renegotiated |

> **2026-10-10 18:00 实测命中缺口 2**：`demo/run_memory_demo.py` 第 5 步兑现帖曾硬编码 `ts = 2026-10-09 18:00`，第 8 步断言依赖它落在 `get_account_state` 的滚动 24h 频控窗内——该日期 +24h 后断言**永久失败**（当天 17:2x 跑绿、18:19 跑红，源头工作区同样红）。这不是业务代码 bug，而是测试把「事实何时发生」（valid time）与「何时检查」（transaction time）混在同一个墙钟上——正是缺口 2 的活例子。已修：`day_ts` 锚定当前周（周一为第 1 天，周/月 key 随之派生）+ 兑现帖 `ts = now`，四个 demo 恢复全绿且跨日稳定。教训对内核同样成立：双时态不是学术洁癖，是「时间一过、断言/决策静默漂移」这类不可复现故障的唯一解。

**补充两条非 blocker 但值得记下的：**

- **指纹宽度**：`md5(fact)[:10]` = 40 bit，按生日界约 **120 万条 note 时碰撞概率接近 50%**，碰撞后果是两个不同事实被静默合并成同一节点且不报错。修法是一行常量（改 64 bit 以上 + 先做措辞归一化），所以不是 blocker，但**必须在能力 1 把量级推到百万之前改掉**。
- **embedding 不能推到 v2**：语义去重（指纹的补救）和矛盾检测都依赖语义相似度，而 v1 只有 bigram 倒排（`kb/search.py`）+ md5 指纹。`docs/01 §8` 把 embedding 列为 v2 —— 这意味着 **v1 无法实现自己最需要的两个机制**。

## 6. 通用化架构

### 6.1 切分线（三方案一致）

**判断一段逻辑归内核还是领域包的试金石：把「账号/帖子/人设」换成「案件/证据/当事人」后它还成立吗。**

| 归内核（域无关） | 归领域包（业务语义） |
| --- | --- |
| Event 协议 + `event_id` 幂等 + 分区写队列（`kb/store.py:166-218`） | 节点/边类型表、ID 规范（`kb/schema.py:17-53`） |
| 四原语 apply + 冲突仲裁 + history（`kb/store.py:78-163`） | `RULE_CHECKERS`（`kb/tools.py:577-579`） |
| **`incr_edge` 有界聚合**（`kb/store.py:123-136`） | `BLOCK_PRIORITY`（`kb/tools.py:29`） |
| 层级 rollup（日→周→月→年→Episode） | `RISK_BUDGETS`（post=3000/comment=2000/…） |
| 三因子召回 + 承诺置顶 | 执行器适配（`kb/executor.py` → `demo/xhs.py`） |
| `DECIDED_VIA` 溯源 + `audit_decision` | 人设漂移检查的**指标定义**（机制留内核） |
| 共识确证 + 灰度采纳（`kb/consensus.py`） | 领域专属的记忆固化策略 |
| 两段式决策（确定性闸门 0 token → LLM 模糊判断） | **claim 租约的具体资源键**（机制留内核） |

> `docs/01 §6`「规则 = 一条 Rule 数据 + 一个代码 checker」已经证明「扩展点做成数据」这个范式在本仓跑得通。把它从**规则**推到**本体本身**，就是通用化的全部动作，不需要发明新东西。

### 6.2 「Schema 即过滤器」的重构

三份方案在这点上给出了同一个答案的两种表述，合并采纳：

- **决策图保持 100% 严格**（上下文只读 schema 合格事实，`docs/01 §0` 的严格性一点不丢）
- **过滤器从「入库口」搬到「派生出口」**（Sediment 方案的核心洞察）：原始输入你不理解，所以无法校验它；但你自己的 prompt 要求 LLM 产出的结构，你能 100% 校验。所以严格性该施加在 **derive 的输出契约**上，而不是在 ingest 对不可信原始数据做本体裁决（后者只能丢弃或强塞）
- **被拒候选带 `source_uri` 进冷区隔离区而非丢弃**，`schema_version` 升级后可重放再抽取（`docs/01 §5` 的冷区已经在设计里，这是路由改动不是新子系统）
- 再加一个 **schema 发现循环**：观察反复出现的被拒候选类型，主动建议本体扩充 —— 过滤器反转成生长机制

### 6.3 溯源锚点：从「会烂的指针」到哈希

`docs/03 §4.1` 说「永远有 source_uri 指回原始层」，但 **`source_uri` 是个会烂的指针**（文件被改、网页 404、邮件被删，digest 就成了无源之水）。

改为 `blob_hash + byte_range + quote`（内容寻址）：源变了 hash 就不匹配，派生产物自动标 `stale_by_source` 并踢出上下文。**docs 的信任锚是指针，应该换成哈希。**

配套硬约束：**citation 强制** —— 每条答案必须带 `blob_hash:byte_range:quote`，quote 与 byte_range 内容不符即**拒答而非硬答**。

### 6.4 三份方案的取舍

| 方案 | 核心赌注 | 量级 | 裁决 |
| --- | --- | --- | --- |
| **Strata（A）薄内核 + 领域包** | docs 里跑通的机制没有一条真属于社交矩阵；属于它的只有本体、ID 格式、预算数字、规则函数体、执行器 | 14.5 人月（全量） | **✅ 采纳其切分线与两条判据；❌ 否决其 pack-sdk（见 §6.5 第 8 条）** |
| **Engram（B）带时钟的守护进程 + MCP 窗口** | 2026 年 harness 已商品化，唯一稀缺的是「会在没人提问时自己思考的记忆」 | 15.5 人月 / MVP 7 | 部分采纳（见 6.5） |
| **Sediment（C）schema-on-derive** | `docs/01 §0.1` 把过滤器放在入库口是**搞错了位置**，不是搞错了必要性 | MVP 3.2 人月 | 嫁接三条核心机制 |

**为什么骨架是 A 而不是 B**：B 自己的头号 kill criterion 是「如果用户不是已经在日常使用 Claude Code/Cursor/Cline，寄生优势归零，应转 A」。用户 2026-10-10 明确要**自带 GUI+TUI+CLI 的独立 App**，即自己拥有宿主 —— 寄生不是策略，A 才是对的框架。

**但 A 的 pack-sdk 部分被否决**（定案 4）。所以最终骨架是：**A 的切分线 + A 的判据 + C 的过滤器位置与溯源锚点 + B 的时钟，落地形态是 Python 内核（§7.1），领域差异用「可注入注册表」而不是「领域包机制」承载。**

### 6.5 从落选方案嫁接

**从 Engram（B）：**

1. **守护进程必须拥有时钟。** 这是 B 最有价值的一条洞察：`docs/03 §1` 的层级卷积、§1.4 的时效淘汰、`docs/04 §2` 的定时维护，全都发生在「没人在问」的时候。一个只被调用才动的记忆系统必然退化成笔记本。**核心必须自带调度器，不能依赖宿主 hook 触发固化。**
2. **MCP 作为「额外的窗口」而不是「产品本身」。** 一旦核心是 headless service，暴露一个 MCP server 近乎免费，可以在自建 App 的同时从 Claude Code 拿到日常使用流量与真实反馈（`docs/03 §1.3` 承认承诺抽取靠「宁漏判不误报」的关键词兜底，那条质量**只能在真实使用中调**）。
3. **警惕**：`@modelcontextprotocol/server-memory` 就是 9 个 CRUD 工具 + JSONL 实体图，无时间模型、无固化、无溯源、无排序、无 schema，README 自称「A basic implementation」。「记忆 MCP server」这个说法本身不值钱，值钱的是背后那个有时钟的账本。

**从 Sediment（C）：**

4. 过滤器搬到派生出口（6.2）
5. `blob_hash + byte_range + quote` 溯源锚点 + citation 强制拒答（6.3）
6. **go/no-go 观测门**：citation 强制下拒答率 > 40% → 证据定位能力不足，派生层价值前提不成立，退回纯检索线；50 条评测集实测抽取 precision < 75% → 停止派生层投入（「有引用但引用是错的」比「没有引用」危害更大）

**从 Strata（A）自己 —— 两条判据，其中第二条已被用户接受（定案 4）：**

7. **判据一（保留）**：做第二个领域时**不许改内核签名**。若发现必须改 `partitionOf` / 晋升管线 / 漂移指标 / 上下文预算器 中任何一个的签名，或代码里出现 `if domain == '...'` 这类按领域分支的逻辑 —— 判定接缝画错了，此时正确动作**不是继续打磨抽象**，而是承认「一个写得很好的 social-matrix 应用 + 一份可复制的模式文档」比一个框架好。
8. **判据二（已接受，2026-10-10：不投 pack-sdk 的 2.3 人月）**：如果三年里只有你自己写领域包，通用化的全部成本（pack-sdk、doctor、一致性套件、声明式 gate 求值器、ABI 版本协商、脚手架，约 2.3 人月一次性投入 + 此后每次改内核的长期税）**只有在存在第三方 pack 作者时才回本**。没有的话，manifest 就是你写给自己的间接层。

**因此明确不做 / 仍然做：**

| 不建（通用化投资） | 仍然建（不是通用化投资，是修 bug 或你自己的目标所需） |
| --- | --- |
| pack-sdk、manifest schema、doctor 校验器 | **主体模型**：Subject/Scope + MemoryNote 状态移到边上（§3.4/§3.5）—— 修 §3.3 的三个后果 |
| 22 项一致性套件、kernelAbi 版本协商 | **注册表化**：把 `NODE_TYPES`/`ID_PATTERNS`/`EDGE_TYPES`（`kb/schema.py:17-53`）、`BLOCK_PRIORITY`（`kb/tools.py:29`）、`RISK_BUDGETS`、`RULE_CHECKERS`（`kb/tools.py:577`）从**模块级常量**改为**可注入的注册表对象** |
| 脚手架、Tier0/1/2 三档能力、声明式 gate 求值器 | **claim 租约**（§4.2）—— 团队版必需，且是内核原语 |
| 领域包市场 / 第三方分发 | **三层人格模型**（§3.8）+ 第 8 节的阻尼机制 |

> 注册表化那一行的成本是**小时级**，不是人月级：`RULE_CHECKERS` 本来就是一个 dict，其余几个也是纯数据结构。把它们从 `kb/schema.py` 的模块常量变成 `KBTools` / `open_kb()` 的参数，不引入任何新机制。这与 pack-sdk 的区别是：**前者让你能换领域，后者让别人能换领域。** 只要前者。

> 判据二是对「通用化」这个目标本身最诚实的警告。**代价已被知悉并接受**：将来若要开放第三方领域包，那 2.3 人月还是要投；收益是现在不为一个可能永远不存在的生态付税。

### 6.6 跨域接口：只允许 4 个动词

```text
remember / recall / commit / rewind      （+ audit 作为横切）
```

领域侧（注册表实例）只回答三个问题：**什么值得记、上下文怎么裁、哪些是不可违反的硬规则**。

如果一个新域需要第 5 个动词，说明内核抽象错了，**必须回炉而不是加接口** —— 这是防止内核退化成「又一个框架」的硬约束。

### 6.7 通用性的验证方式（按定案 4 修订）

原方案要求「做出 3 个官方领域包」并配 pack-sdk。**pack-sdk 已否决**，验证方式相应收敛：

1. **社交矩阵**（现有，四个 demo 已跑绿）
2. **软著申请**（`deepseek_markdown` §2–4 已有实体/类层次/属性关系/规则实例/校验器草稿，几乎白送）—— **硬编码为第二套注册表实例**，不建 pack 机制
3. 第三个领域**等真实需求出现再做**（rule of three）

第 2 项的价值**不是证明框架可用**（没有框架了），而是**检验接缝画错了没有** —— 这是判据一（§6.5 第 7 条）的唯一执行方式。如果做第二个领域时发现必须改内核签名，那时**再**决定要不要投 pack-sdk：**用证据决定，不用现在决定。**

**硬性纪律保留**：任何单个领域提出的内核签名改动一律禁止，必须两个领域都需要才改内核。

> 你不是为所有场景设计，你是为**接缝**设计，然后用 2 个领域验证接缝。

**对「如何适用于各类业务场景」这个探讨方向的最终答复**：通用化从**产品能力**降级为**架构属性**。保持接缝清晰，不建框架。

选下一个域用「**记忆密度 × 错误代价**」两轴，不要用市场规模：社交矩阵（高×高，封号）、销售跟进（高×高，丢单）、软著/合规申报（中×高，补正返工）、客服（中×中）、个人笔记（高×低）。**低错误代价的域不需要事件溯源、审计和 rewind，因此付不出溢价 —— 而那正是你唯一的技术壁垒所在。**

## 7. 能力清单逐条处置

| # | 原需求 | 处置 | 理由 |
| --- | --- | --- | --- |
| 1 | 主动扫描本地/个人云/企业云/互联网 | **降级**：一等公民只有 本地文件系统 + MCP sink + 手工导入；云端走已授权 CLI 委托（lark-cli / gh / rclone）与现成 MCP server | 自建全域连接器是 Glean 级投入（几百人年）；Rewind 因为通用采集太难 pivot 去做硬件。而你的环境里企业云授权**已经付过钱了** |
| 1a | 「主动扫描」语义 | **定案**：用户圈定范围 + 定期执行；agent 永不自主扩大范围；运行时按需单点拉取每次进审计日志并在 GUI 可见 | 用户 2026-10-10 确认；`AGENTS.md:52` 早已是成文策略 |
| 2 | skills 库 / mcp 库 / hook 库 | **降级为纯标准兼容**（20–30% 精力）：实现 SKILL.md + MCP + 对齐 Claude Code 命名的 hook 总线，做现有目录的消费端与聚合器 | 标准战争已结束：SKILL.md 约 45 家客户端（含 OpenAI Codex / Gemini CLI / VS Code / Cursor / JetBrains / TRAE / Mistral）；MCP 进 Linux Foundation，TS SDK 月下载约 5 亿；AGENTS.md 6 万+ 项目；分发渠道过剩（`npx skills` 约 79 宿主、官方 Registry、`.mcpb`）。**2026 年自造技能格式 ≈ 2016 年自造 `.vsix`** |
| 2a | 三库的差异化 | **真正的空白在三库 × 知识库的交叉处**：hooks 自动回写生活痕迹、KB 固化产物反向生成 SKILL.md、KB 状态驱动三库场景化挂载 | 这个飞轮没有任何标准或现成产品覆盖，就是能力 4 的独有叙事 |
| 3 | 扩容/备份/迁移 | **保留但收敛**：只做事件流导出/重放/rewind + 单文件备份；MVP 砍掉 Neo4j 与多后端 | `docs/04 §4`「事件即真相」已经让备份=拷事件流，这是最便宜的一条能力 |
| 4 | 生活痕迹反馈 + 自动迭代 + 正反馈 | **保留为旗舰，但必须补负反馈** | 见第 5 节缺口 3/4/5 与第 8 节的阻尼机制 |
| 5 | 自由接入 LLM | **卫生因子**，0.5–1 人月 | `docs/02 §7` 的 OpenAI 兼容路线已经对；2026 年这不是卖点 |
| 视图 | GUI + TUI + CLI | **保留独立 App；TUI 不做**（2026-10-10 定案，之后有需要再说）。Linux 服务器部署的操作面 = `kb serve --no-browser` + systemd（`docs/04 §6` 已有此形态）；Tauri 壳已编译完成，成本远低于评审组最初估算 | 纪律：核心从第一天就是 headless service，壳零业务逻辑（`docs/04 §1.2`「一个核心三种壳」）。**GUI 的不可替代用途是记忆审查面**——每周「我学到的 N 件事」diff 队列、单条记忆一键「这是错的」→ 墓碑 + 级联切除。这是 Claude Code 给不了你的，也是自建 App 的正当理由 |
| 栈 | TypeScript | **改为 Python 内核 + TS 前端**（定案 3，2026-10-10） | 见 §7.1。这个决定把「零第三方依赖」保住了，并取消了原计划第一周的全部存储/分发 spike |

### 7.1 技术栈定案：Python 内核 + TS 前端（2026-10-10）

**这不是改变方向，是追认已经建好的东西。** 三条证据都在仓库里：

- `tauri-app/src-tauri/binaries/callfans` 已经是 sidecar 形态的 **Python 二进制**
- `docs/04 §1.3` 早已写明「Tauri 桌面壳 = sidecar 拉起 `kb serve`（sqlite 后端）+ WebView 加载同一页面，**前端零重写**」
- `docs/04 §1.2`「一个核心（kb core），三种壳（CLI / serve / GUI）」—— 接缝就是 HTTP API

**收益：**

| 项 | 说明 |
| --- | --- |
| 5351 行零重写 | 四个 demo 已在本工程实机跑绿（§0.1） |
| 「零第三方依赖」保住了 | `kb/` 只用 stdlib，neo4j 是懒加载可选依赖（`kb/factory.py:31`）。本文 §7 原来说这条在 TS 侧必须显式放弃 —— **现在不用放弃** |
| 存储与分发的 spike 全部取消 | 原第 10.3 条的三题（`bun:sqlite` vs `better-sqlite3` vs `node:sqlite`、`sqlite-vec` 能否 `loadExtension`、能否进单文件二进制）**在 Python 侧不存在**：stdlib `sqlite3` + PyInstaller 四平台已经跑通（`docs/04 §6`，`.github/workflows/release.yml`） |
| 图数据库问题消失 | `kb/store_sqlite.py` 的 nodes/edges 物化表 + 应用层遍历已经是终态，不需要 Kuzu / DuckDB。（顺带记录 TS 侧的核查结论以免将来重走：**「桌面单机 + 嵌入式图数据库」是伪组合** —— Kuzu npm 停在 0.11.3 整年未更且公司 2025 年停摆、Neo4j 需 JVM server 无法随 App 分发、hnswlib-node 已死于 2024-03） |
| 执行层桥零新增成本 | `docs/02 §4.1` 的 `build_command()` → argv subprocess（`kb/executor.py:115`）本来就是跨进程形态 |

**代价（必须显式管理）：**

1. **前后端接缝升级为版本化契约。** `kb serve` 的 HTTP API（`docs/04 §1.2` 那张端点表）从「内部实现细节」变成**跨语言 ABI**。建议：从 Python 侧生成 OpenAPI / JSON Schema，TS 侧类型由它生成，**不手写两遍**。这是本决定唯一的新增工程债，必须在第一次改 API 之前立起来，否则两端会各自漂移。
2. **两套工具链的 CI**：Python（demo 冒烟 + PyInstaller）+ TS（前端构建 + Tauri bundler）。`release.yml` 已有前者，需加后者。
3. **embedding 能力**（§5 补充条：不能推到 v2）在 Python 侧更容易：`sqlite-vec` 有 Python binding，本地小模型可走 ONNX Runtime；比 Node 侧的 `transformers.js` 成熟。

**TS 侧只承担两件事**：GUI / Web 控制台（React + WebView——Tauri 桌面壳与 Linux 无头服务器加载同一页面，`kb/static/index.html` 现成）、MCP server 门面（若要暴露记忆给 Claude Code 等宿主，见 §6.5 第 2 条）。TUI 不做（2026-10-10 定案）。

**内核、记忆引擎、决策引擎、摄取、维护任务全部留在 Python。**

## 8. 让「正反馈」收敛：必须补的阻尼机制

用户原话「形成正反馈」在中文语境里是「形成良性循环」，愿望没问题。但**只有正反馈的系统会指数放大误差**，所以设计目标必须写成「**形成带负反馈的收敛回路**」。

`docs/03 §2` 的共识机制（candidate → N 独立确证 → `effective_at` + `jitter_window` 错峰采纳）**就是标准的阻尼设计**，只是它现在只作用于 Rule。把它泛化成全域**信念晋升管线**：

```text
一次性观察 → candidate（影子记忆，不进高风险动作的决策上下文）
  → K 个独立【来源】确证（独立性按来源根/模态/时间去重）
    → belief（进召回，confidence = f(确证数, 来源信任)）
      → 高频预测验证
        → consolidated（可 gate 行为，采纳时沿用 effective_at + 灰度）

降级路径（同等重要）：
  矛盾 → 降回 candidate
  预测失败 → 衰减
  用户否决 → 持久墓碑（任何后续自动写入不得覆盖）
```

配套五条：

1. **`origin` 一等公民字段** ∈ `{user_asserted, first_party_observation, corroborated_extraction, single_source_web, agent_inference}`，按来源类设 confidence 上限：`single_source_web` 永远到不了 belief 层，`agent_inference` **不得自我确证**。摄取路径**禁止直接创建 Rule/Constraint 节点**。抽取环节对扫描内容做指令剥离/沙箱化（抽取 LLM 无工具权限、内容仅作数据），防间接提示注入。
2. **`pinned` 只能由用户或晋升管线终态授予，LLM 抽取最多提名。** 现状是 milestone 类自动 pinned（`kb/rollup.py:275`）+ pinned 永不衰减 + 三因子召回加权置顶 —— **把系统里最强的正反馈权重交给了最不可信的判断者**。
3. **`AFFECTED_BY` 反向索引**（节点 → 引用过它的全部决策 → 那些决策回写的全部结果），把「按事件流重放切除某来源及其派生」做成一等维护命令 `forget --source X`。现状只有前向 `DECIDED_VIA`，发现记错之后无法评估爆炸半径、无法做记忆手术。
4. **派生深度标记 + 自产比例上限**：每条 MemoryNote 标记派生深度（0=原始观察，1=观察的摘要，2=摘要的抽取…），召回预算对深度 ≥2 的自产记忆设占比上限，把「决策上下文自产比例」作为**常驻健康指标**。这是对回音室唯一的度量手段。
5. **预测核销回路**：belief 若蕴含可测预期（「该时段发帖会被限流」「用户周三在办公室」），维护任务对照后续 ActionRecord/外部事实核销，成功升 confidence、失败触发降级。**这是把「生活痕迹反馈」从叙事循环改造成真值循环的关键一步，也是唯一能让系统越用越对而不是越用越自信的机制。**

**人工复核闸门**：频率 ∝ 自治度 × 动作不可逆度（发帖可删 / 邮件不可撤 / 交易不可逆，分级 gate）。社交矩阵 7×24 无人值守是最高危形态，企业 copilot 每轮有人审是最低危形态 —— 这个旋钮做成部署期配置，是通用工具适配不同业务的核心机制。

## 9. 留档：被对抗反驳推翻的论断

**以下论断在评审过程中被提出，经反驳与代码核实后不成立，记录以免将来重复。**

| 被推翻的论断 | 事实 |
| --- | --- |
| 「信念修正 = last-write-wins，最新污染自动战胜更早的真信念」 | **机制指错了地方。** `docs/01 §4` 的「ts 大者胜」明文只限定 `upsert_edge`（`kb/store.py:115`）；MemoryNote 的 fact 是**节点**，ID 是内容指纹，两条矛盾事实 md5 不同 → 两个不同节点 → **谁也没覆盖谁**。真正的病是「**矛盾并存 + 无矛盾检测**」，与 LWW 是两种病。「废掉 ts-wins 仲裁」是对着一条不存在的代码路径开刀 |
| 「恶意网页可升级成 Rule 直接改写行为」 | `docs/03 §2` 第一句就是「公共区只由确证机制写入」，Rule 必须 N≥2 独立确证（`kb/consensus.py:19,71`）。提出者自己在 strengths 里把这条列为「文档中唯一完整的阻尼设计」，转头在 flaws 里当它不存在。（真正该问的是 sybil：同一网页被多账号抓取算不算独立？—— 这个问题有效，但没被提出） |
| 「批量扫描会按文件 mtime 覆盖记忆」 | docs 全文 `mtime` 出现 0 次；`ts` 是系统补的入库时刻。且「ts 大者胜」只作用于同端点同类型的边 |
| 「importance 因子因 confidence 是常量而失效」 | `docs/03 §4.2` 原文是「importance（**pinned** 加权）」，`docs/01 §2` 标注「pinned 不衰减」，milestone→pinned。提出者把 §1.3「LLM 候选缺字段由系统补默认值」误读成「pinned 永远常量」 |
| 「通用工具主用法是自然语言问句，这个入口无法构造」 | `docs/04 §1.1` 有 `kb query "防晒"`、§1.2 有 `GET /search?q=`、§1.3 明写「⌘K 命令框：自然语言检索知识库」。提出者只读了 `docs/03 §3` 的决策上下文召回入口 |
| 「Schema 即过滤器会按设计拒绝一切通用数据入库」 | `docs/01 §0.1` 原句主语是「**LLM 抽取的候选事实**」，是事实抽取过滤器不是摄取过滤器。真问题是**类型词汇表缺 Document/Person/Message**，不是内容过滤 |
| 「`kb/` 代码不存在，所有『已验证』不可核验，排期按零实现重算（3–5 倍差距）」 | **假。** 见第 2 节。这是工具盲区（Spotlight 不索引点目录）被当成了世界事实，且指路牌就在被要求阅读的目录里（`docs/.idea/workspace.xml` 的 `last_opened_file_path` 明写着工作区路径） |
| 「docs 里约七成机制已经是接口化的领域无关设计」 | 「七成」无任何出处或清点过程。且其中相当一部分（Kafka 背压、跨进程 event_id 去重、N≥2 共识、jitter_window 灰度）在 `docs/04 §1` 描述的桌面单机形态下**根本不运行**。用目标形态里跑不到的机器凑出的比例不能当资产计价 |
| 「22–41 人月，单人 12 个月内不会有任何日用闭环」 | 提出者自己的 stage-1 行项目加总是 7–11.5 人月，而 mustFix 声称这一段是 3–4 人月（差 2.5–3 倍）；keyClaims 说 >12 个月，mustFix #2 说 6–8 周即可日用 —— **同一份评审里日用闭环既需要 >12 个月又需要 6–8 周**。且自陈行项目单价是 pre-AI 口径，却用它算总量、定分数，同时用 post-AI 口径写建议 |
| 「选 TS 意味着放弃已跑通的 Python 打包通路重做 Tauri/Electron」 | Tauri 已经做完且本机编译过（`tauri-app/src-tauri/target` 2.2GB、`binaries/callfans` sidecar） |
| 「正反馈这个词用错了，扣 2 分」 | 中文语境里「形成正反馈」九成九是「形成良性循环」。把措辞双关当设计缺陷扣分是修辞不是分析。**警告本身有价值**（若实现者按控制论字面理解确实会做出发散系统），但那是「需要提醒」不是「需要扣分」 |

**方法论教训（比任何单条结论都重要）：**

评审组系统性地犯了三个错，且六个视角各自都犯：

1. **把「一句话需求」当架构文档打分，再按架构缺陷扣分。** 没人会在一句话需求里写「请包含矛盾隔离与来源信任分级」——那是设计阶段的工作。
2. **severity 通胀。** 多条被标为 blocker 的缺陷，其补救措施是「写一份路线图」或「不做这个功能」。靠删除即可消解的缺陷是**范围错误**，不是致命缺陷。当一半以上的 blocker 靠写清单就消失时，severity 分级已经不再携带信息。
3. **视角帝国主义。** 每个评审者都先把自己关注的维度声明为决定性权重（数据接入视角自称占 60%+，产品视角把「给谁用」设为唯一变量），再用这个权重给整个需求打不及格分。
4. **一题多计制造重量。** 数据接入视角 6 条 flaw + 5 条 fix + 5 条 generalization 共 16 个条目，去重后约 3 个独立问题。

## 10. 定案记录与新的未决问题

### 10.1 已定案（2026-10-10）

| 原未决问题 | 结论 |
| --- | --- |
| 主体模型：Subject 独立于 Account？MemoryNote 方案 A 还是 B？ | **Subject 独立；方案 B**（状态移到 `HAS_MEMORY` 边上）。落地改动面见 §3.5 |
| 性格层怎么涌现？ | **独立 trait 层**，载体 `MemoryNote{category:'trait'}`，三层人格模型见 §3.8 |
| 第一周 spike 三题（SQLite 驱动 / 扩展加载 / 单文件分发） | **取消**。定案 3 之后这三题在 Python 侧不存在（stdlib `sqlite3` + PyInstaller 已跑通） |
| TS 全量重写 vs Python 内核 + TS 前端 | **Python 内核 + TS 前端**（§7.1） |
| 是否接受 Strata 判据二、要不要投 2.3 人月 pack-sdk | **接受判据二，不投 pack-sdk**（§6.5 第 8 条 / §6.7） |
| 代码位置 | `kb/` + `demo/` 已入本工程，在工程内改造（§0.1） |
| TUI 做不做（第二批定案） | **不做**。操作面 = CLI + Web 控制台；Linux 无头部署形态 `docs/04 §6` 已就绪（`serve --no-browser` + systemd） |

### 10.2 新的未决问题（按必须决定的先后排序）

1. **HTTP API 契约怎么生成。** 这是定案 3 引入的**唯一新增工程债**，且必须在第一次改 API 之前立起来，否则 Python 与 TS 两端会各自漂移。建议：Python 侧产出 OpenAPI/JSON Schema，TS 侧类型由它生成，不手写两遍。—— 需要定：用什么生成（pydantic？手写 schema？从 `kb/serve.py` 的路由表反推？）
2. **Subject 的 ID 规范与 Subject↔Account 的 1:N 表达。** `subj:{slug}`？还是 `subj:{platform-agnostic-handle}`？1:N 用新边 `HAS_HANDLE: Subject→Account` 还是把 platform 降为 Account 的属性？这决定 `partition` 键的形态，进而决定 §4.2 claim 租约的资源键
3. **trait 层的晋升阈值 K。** 几个独立周期算确证？（§3.8 接入点 2）K 太大 → 性格永远长不大；K 太小 → 一次异常就改性格。建议 K=3 起步，用 §3.8 的验收标准实测调
4. **TUI 到底做不做 —— 已定案（2026-10-10）：不做。** CLI（`kb/cli.py`，六条命令齐备）+ Web 控制台（`kb serve`；Linux 无头部署 `--no-browser` + systemd，`docs/04 §6`）已覆盖本地与远程两种操作面；TUI 是第二个消费同一 HTTP API 的命令行壳，增量价值不明确，之后有真实需要再做
5. **第二个领域（软著申请）什么时候做。** 它是判据一（§6.5 第 7 条）的唯一执行方式。太早做 → 第一个领域的改造还没稳定，接缝判断失真；太晚做 → 内核签名已被单领域假设固化，改起来贵。建议：§11 的第 2–6 步做完之后、第 7 步之前

## 11. 改造顺序（在工程内，每步后四个 demo 必须仍全绿）

> 回归基线见 §0.1。**任何一步跑不绿就停下修，不要带着红往前走。**
> 反驳环节有一条结论直接决定了这个顺序：§5 的缺口 2/3/4/5（双时态、删除语义、信任分级、DERIVED_FROM）**全部落在同一条写入/检索路径上，会在一次重设计里同时定形，不可能各付全价** —— 所以它们合并为一步（第 5 步），不要拆成四次改动。

| 步 | 内容 | 为什么在这个位置 | 新增验收 |
| --- | --- | --- | --- |
| 1 ✅ | **立 HTTP API 契约**（OpenAPI 生成 + TS 类型生成）—— **已完成（2026-10-10，v0.4.0）**：`kb/api_schema.py`（spec 事实源，18 操作/37 schema，契约版本独立于应用 tag）→ `docs/api/openapi.json` → `web/src/types/api.d.ts`（openapi-typescript 生成，tsc 冒烟过）；契约测试 `demo/run_api_contract_demo.py` **双后端（内存+SQLite）**起真实 serve 全操作实测——closed schema 端点严格校验、开放 schema 端点（节点属性扩展口）校验必填与已声明字段、负例按 Error schema 校验、approved/blocked/幂等/非空记忆数组显式断言；`.github/workflows/ci.yml` 在 push/PR 到 main 时跑五 demo + 双重漂移检查（spec 重导出 diff + TS 类型重生成 diff）。经 25-agent 三维对抗审查（spec 准确性/测试严格性/CI 接线，21 条成立 finding 全部修复，含 `/stats` 后端差异、allOf 闭合陷阱、`/report` 平铺形态、Windows pwsh 吞错、lockfile 镜像源等） | 定案 3 的唯一新增债，越晚越贵 | 前端类型由 schema 生成，改一处两端同步 |
| 2 ✅ | **主体模型** —— **已完成（2026-10-11，v0.5.0）**：`Subject` 节点（`subj:{slug}`）+ `HAS_HANDLE: Subject→Account`（1:N）；`HAS_PERSONA`/`HAS_EPISODE`/`HAS_MEMORY` 起点全部移到 Subject；MemoryNote 节点只存 fact/category，主观状态（status/pinned/confidence/ts）在 HAS_MEMORY 边上（方案 B）；`partition = subject_id`（submit_result/consensus 归一写入）；记忆函数全部接受 subject-or-account（`store.subject_of()` 归一，API 签名不变）；`fulfill_promise` 必须显式给 owner（串号 bug 从构造上消失）；三后端（memory/sqlite/neo4j）同步；seed 增加 lily 的 tiktok 第二平台账号 | blocker；修 §3.3 三个后果；是第 7 步 trait 层的前提 | ✅ `demo/run_subject_demo.py`：**20 项全绿**——跨平台不失忆（承诺/召回/情景/摘要互通，Episode ID 用主体 slug）、A 兑现不影响 B（共享节点+边上状态分叉）、节点只存客观字段、分区=Subject |
| 3 | **claim 租约**（§4.2）：`claim(subject_id, action, period)` 原子占位 + 租约过期 | 小、团队版必需、内核原语 | 新 demo：**两个进程同时为同一 Subject 决策，只有一个执行** |
| 4 | **注册表化**：`NODE_TYPES`/`ID_PATTERNS`/`EDGE_TYPES`/`BLOCK_PRIORITY`/`RISK_BUDGETS`/`RULE_CHECKERS` 从模块常量改为可注入（§6.5 表） | 小时级成本；不做的话第 11 步（第二领域）无法进行 | 现有 demo 用「注入的注册表」而非模块常量，仍全绿 |
| 5 | **写入/检索路径一次重设计**：双时态（`valid_from`/`valid_to`/`recorded_at`）+ tombstone 与 provenance 抑制 + `origin`/`trust_tier` + `DERIVED_FROM` 边 + 指纹 40→64 bit + 矛盾检测（写入前相似检索，冲突产生 Contradiction 而非静默覆盖） | 五条同路径，合并成一步（见上方引言） | 新 demo：**删掉一条事实后重跑固化，它不复活**；**互斥事实同时入库 → 产生 Contradiction**；**低信任来源不能授权高副作用动作** |
| 6 | **信念晋升管线**（§8）：共识机制从 Rule-only 泛化到全域 Belief，quorum 改「N 个独立来源」，补齐降级路径（矛盾→降回 candidate / 预测失败→衰减 / 用户否决→墓碑）、`pinned` 只由用户或管线终态授予、`AFFECTED_BY` 反向索引 + `forget --source X`、派生深度标记与自产比例上限 | 依赖第 5 步的 origin 与 DERIVED_FROM | 新 demo：**一条网页来源的假事实永远升不到 belief**；**`forget --source` 能级联切除派生物** |
| 7 | **三层人格模型**（§3.8）：trait 抽取器 + 晋升 + `persona_card` 块纳入已晋升 trait + 演化曲线数据 | 依赖第 5、6 步 | §3.8 的三条验收标准（90 天合成流水 / 反证注入 / 人设护栏） |
| 8 | **承诺状态机**（§5 缺口 6）：`due_at` 或 `trigger_condition` 二选一强制、补 conditional/violated/expired/cancelled、`standing` 承诺不占置顶槽位 | 独立、小、且现在承诺机制正在自我污染 | 新 demo：**半年前的过期承诺不再占据决策上下文最贵槽位** |
| 9 | **embedding + 语义检索**：`sqlite-vec`（Python binding）替换/补充 `kb/search.py` 的 bigram 倒排；检索入口支持自然语言问句（不只 `action + topic label`） | 第 5 步的矛盾检测已依赖语义相似度，此处补齐召回侧 | 中文同义改写可召回（「防晒」↔「SPF50 隔离霜」） |
| 10 | **记忆评测集**：LongMemEval 五轴（单会话用户信息 / 多会话 / 时序推理 / 知识更新 / 该拒答时拒答）+ 矛盾注入 + 投毒注入 + 遗忘无误伤，做成可回归 fixture | 没有它，「自动迭代形成正反馈」不可证伪 —— 闭环可能是负反馈而你永远不会知道 | CI 里跑；precision@k 有基线数字 |
| 11 | **第二领域（软著申请）硬编码接入**，执行判据一 | 见 §10.2 第 5 条 | **0 行内核签名改动**跑通；否则按 §6.5 第 7 条判定接缝画错 |
| 12 | **GUI 记忆审查面**：每周「我学到的 N 件事」diff 队列、单条记忆一键「这是错的」→ 墓碑 + 级联切除、「为什么这么做」用 `DECIDED_VIA` 渲染成白话 | 依赖第 5、6 步的墓碑与反向索引。**这是自建 App 相对寄生宿主的唯一不可替代价值** | 用户能纠正 agent，且纠正不会被下次固化复活 |
