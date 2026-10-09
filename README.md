# callfans 知识库 MVP

云手机社交矩阵的知识中枢：本体 Schema + 图存储 + 分区写入队列 +
两段式决策工具 + 回写摄取漏斗 + 滚动压缩。

零第三方依赖，Python 3.9+。

## 快速开始

```bash
# 离线闭环（不调 LLM）
python3 demo/run_demo.py

# 执行层适配验证（决策→xhs.py 命令→回写，不依赖 sma_autoui/真机/LLM）
python3 demo/run_executor_adapter_demo.py

# 长期记忆系统验证（层级 rollup/固化/承诺/共识/审计，不依赖 LLM）
python3 demo/run_memory_demo.py

# 记忆链路 LLM 实机验证（周期叙事/固化抽取/承诺兑现）
export CALLFANS_LLM_BASE_URL=https://api.proma.cool
export CALLFANS_LLM_API_KEY=sk-...
export CALLFANS_LLM_MODEL=glm-5.3-flash
python3 demo/run_memory_llm_demo.py

# Neo4j 持久化验证（需 docker compose up -d + pip install neo4j）
export CALLFANS_NEO4J_URI=bolt://localhost:7687
export CALLFANS_NEO4J_USER=neo4j
export CALLFANS_NEO4J_PASSWORD=callfans-dev
python3 demo/run_neo4j_demo.py

# SQLite 嵌入式后端验证（桌面单机形态，零外部服务）
python3 demo/run_sqlite_demo.py

# 维护任务（cron 入口，存储后端由 CALLFANS_STORE 决定）
CALLFANS_STORE=neo4j python3 -m kb.maintenance daily

# 统一 CLI 与本地 HTTP API（一个核心三种壳）
CALLFANS_STORE=sqlite python3 -m kb status
CALLFANS_STORE=sqlite python3 -m kb memory acc:xiaohongshu:lily_beauty
CALLFANS_STORE=sqlite python3 -m kb serve --port 8765
# ↑ serve 启动后浏览器打开 http://127.0.0.1:8765/ 即三栏 Web 控制台

# 核心 LLM 实机验证（任一 OpenAI Chat Completions 兼容服务）
export CALLFANS_LLM_BASE_URL=https://api.proma.cool
export CALLFANS_LLM_API_KEY=sk-...
export CALLFANS_LLM_MODEL=glm-5.3-flash
python3 demo/run_llm_demo.py
```

配置说明见 `.env.example`。

## 下载与安装（Release 应用包）

GitHub Release 提供四平台自包含包（macOS arm64/x64、Windows x64、
Linux x64 无桌面版）：解压后 `./callfans` 无参数启动即打开 Web 控制台；
无头环境 `./callfans serve --no-browser`；CLI 用法同 `python3 -m kb`。
自己构建：`git tag v0.1.0 && git push origin v0.1.0` 触发 Actions。

## 项目结构

```text
├── docs/
│   ├── 01-knowledge-schema-v1.md         # Schema 设计（节点/边/事件/仲裁/冷热分层）
│   ├── 02-query-and-write-protocol-v1.md # 查询与回写协议（工具/决策/预算/并发/LLM 层）
│   ├── 03-memory-and-consensus-v1.md     # 长期记忆与共识（分层记忆/承诺/检索/不失真）
│   └── 04-deployment-and-maintenance-v1.md # 部署与维护（存储后端/定时任务/备份）
├── kb/
│   ├── schema.py    # 节点/边/事件定义 + 校验（Schema 即过滤器）
│   ├── store.py     # 内存图存储 + 分区写队列（幂等/仲裁/属性历史）
│   ├── store_sqlite.py # SQLite 嵌入式后端（桌面单机，events 幂等 + 物化表）
│   ├── store_neo4j.py # Neo4j 持久化后端（同接口，事件唯一约束跨进程去重）
│   ├── factory.py   # 存储工厂（CALLFANS_STORE=memory|sqlite|neo4j）
│   ├── ingest.py    # 摄取漏斗：执行结果 → Schema 约束事件
│   ├── tools.py     # 查询工具 + 规则闸门 + 两段式决策 + 回写 + 审计 + 承诺扫描
│   ├── rollup.py    # 层级 rollup（日/周/月/年）+ 记忆固化 + 承诺追踪
│   ├── search.py    # bigram 倒排检索 + 分层下钻
│   ├── consensus.py # 公共记忆上卷（多账号确证）+ 灰度生效
│   ├── llm.py       # LLM 接入层：决策/抽取/摘要/周期叙事/记忆固化/承诺扫描六钩子（OpenAI 兼容）
│   ├── executor.py  # 执行层适配：decide()→xhs.py 命令行；结果→幂等回写+发布后承诺扫描
│   ├── maintenance.py # 维护任务（daily/weekly/monthly/yearly，cron 入口）
│   ├── cli.py       # 统一 CLI（python3 -m kb：status/query/memory/plan/serve）
│   ├── serve.py     # 本地 HTTP API + 静态页（GUI / 远程客户端的统一入口）
│   └── static/index.html  # 三栏 Web 控制台（单文件，零构建）
└── demo/
    ├── seed.py        # 种子数据（小红书 6 账号 / 3 人设 / 8 话题 / 4 规则）
    ├── run_demo.py    # 离线端到端验证（不调 LLM）
    ├── run_llm_demo.py # LLM 实机验证（决策/抽取/摘要/故障降级）
    ├── run_executor_adapter_demo.py  # 执行层适配全链路验证（FakeRunner）
    ├── run_memory_demo.py            # 长期记忆系统全链路验证（无 LLM）
    ├── run_memory_llm_demo.py        # 记忆链路 LLM 实机验证（叙事/固化/承诺）
    ├── run_sqlite_demo.py             # SQLite 嵌入式后端全链路（并发/恢复）
    ├── run_neo4j_demo.py             # Neo4j 持久化全链路（并发去重/状态恢复）
    └── xhs.py         # 云机执行器实例（小红书发布视频，依赖外部 sma_autoui）
```

## 六个问题 → 模块映射

| 问题 | 方案 | 代码 |
| --- | --- | --- |
| 1. 精简重要信息 | Schema 即过滤器 + 决策相关性准入 | `kb/schema.py` |
| 2. 压缩存储 | 原子事实 + 聚合边计数 + digest + 冷热分层 | `kb/store.py` `kb/rollup.py` |
| 3. 联结 | 统一 ID + 本体边/聚合边/provenance 边 | `kb/schema.py` EDGE_TYPES |
| 4. 查询与思考 | 工具化查询 + 规则闸门 + 风险分级预算 LLM 决策 + 记忆召回 | `kb/tools.py` `kb/llm.py` `kb/search.py` |
| 5. 结果回写 | 摄取漏斗 + 幂等事件 + 冲突仲裁 + 层级 rollup | `kb/ingest.py` `kb/rollup.py` |
| 6. 隔离与并发 | 分区键=账号 + 幂等去重 + 共识确证公共记忆 | `kb/store.py` `kb/consensus.py` |

长期记忆（情景/语义/承诺/公共记忆与不失真设计）见 `docs/03`。

## 最小使用示例

```python
from demo.seed import build_seed
from kb.llm import LLMClient, make_decision_llm
from kb.tools import KBTools

store, queue = build_seed()
tools = KBTools(store, queue)

# 两段式决策（不传 llm 走确定性桩；接真模型如下）
client = LLMClient()   # 读 CALLFANS_LLM_* 环境变量
d = tools.decide("acc:xiaohongshu:lily_beauty", "post", "topic:beauty.skincare",
                 llm=make_decision_llm(client))

# 执行后回写（幂等，重试安全；raw_text 可选，交给 LLM 抽取候选事实）
tools.submit_result({
    "action_id": "act:xxx", "account_id": "acc:xiaohongshu:lily_beauty",
    "action": "post", "topic_id": "topic:beauty.skincare",
    "new_post_id": "post:xiaohongshu:p010", "digest": "……",
    "decision_id": d["decision_id"], "knowledge_refs": d["knowledge_refs"],
})
```

## 执行层对接（kb/executor.py）

适配 `demo/xhs.py` 这类独立 CLI 执行器（调度平台拉起、argv 传参、标题/正文 base64）：

```python
from kb.executor import ExecutorAdapter

adapter = ExecutorAdapter(tools)          # 测试可注入 FakeRunner

# 方向一：决策 → xhs.py 命令（闸门拦截时 status=blocked，不生成命令）
plan = adapter.plan("acc:xiaohongshu:lily_beauty", "post",
                    "topic:beauty.skincare", job_id="job-001",
                    task_params={"folder_name": "Movies", "images_nums": 1})
run = adapter.run(plan["spec"], device_env)   # subprocess 拉起云机脚本

# 方向二：结果 → 幂等回写（job_id 派生 action_id，重试自动去重）
adapter.report(plan["spec"], outcome={
    "ok": True, "stats": {"likes": 28},
    "raw_text": "评论区反馈：求防晒用量说明",
})
```

## 下一步

1. ~~对接现有云手机执行层~~（`kb/executor.py` 已完成，接真机时把 FakeRunner 换成真实调度即可）
2. 数据量超过进程内存后：`KnowledgeStore` → Neo4j，`WriteQueue` → Kafka 分区 worker
   （接口不变，见 docs/02 第 6 节）
3. v2 扩展：向量检索、边时间衰减、人设演化版本（见 docs/01 第 8 节）
