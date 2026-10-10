# callfans-agent

云手机社交矩阵的**知识中枢**，正在改造为**拥有长期记忆的通用 agent 内核**：
本体 Schema + 图存储 + 分区写入队列 + 两段式决策 + 回写摄取漏斗 + 滚动压缩
+ 长期记忆（情景/语义/承诺/共识）。

零第三方依赖，Python 3.9+。

> 当前状态：**改造工作区**。本仓库是 Python 内核的改造主线（详见
> [`docs/05-requirement-review-v1.md`](docs/05-requirement-review-v1.md)：
> 六视角需求评审 + 对抗反驳 + 主体模型/人格层/技术栈定案 + 12 步改造顺序）。
> 桌面壳（Tauri）等前端资产暂在 Proma 工作区，待内核改造稳定后迁回
>（见 `AGENTS.md` 工程现状）。TUI 不做，操作面 = CLI + Web 控制台。

## 快速开始

```bash
# 离线闭环验证（不调 LLM、零外部服务）
python3 demo/run_demo.py

# 长期记忆系统全链路（层级 rollup/固化/承诺/共识/审计，无 LLM）
python3 demo/run_memory_demo.py

# 执行层适配全链路（决策→执行器命令→幂等回写，FakeRunner）
python3 demo/run_executor_adapter_demo.py

# SQLite 嵌入式后端（桌面单机形态：双进程并发去重/状态恢复）
python3 demo/run_sqlite_demo.py

# 统一 CLI（后端由 CALLFANS_STORE 决定：memory|sqlite|neo4j）
python3 -m kb status
python3 -m kb query "防晒"
python3 -m kb memory acc:xiaohongshu:lily_beauty

# Web 控制台（本地 HTTP API + 三栏 GUI）
python3 -m kb serve --port 8765
# ↑ 浏览器打开 http://127.0.0.1:8765/
```

需要 LLM / Neo4j 的实机验证：

```bash
# LLM 实机（任一 OpenAI Chat Completions 兼容服务）
export CALLFANS_LLM_BASE_URL=https://api.proma.cool
export CALLFANS_LLM_API_KEY=sk-...
export CALLFANS_LLM_MODEL=glm-5.3-flash
python3 demo/run_llm_demo.py
python3 demo/run_memory_llm_demo.py

# Neo4j 持久化（需 docker compose up -d + pip install neo4j）
export CALLFANS_NEO4J_URI=bolt://localhost:7687
export CALLFANS_NEO4J_USER=neo4j
export CALLFANS_NEO4J_PASSWORD=callfans-dev
python3 demo/run_neo4j_demo.py
```

配置说明见 `.env.example`。

## 下载与安装（Release 应用包）

GitHub Release 提供四平台自包含 CLI/serve 包（macOS arm64/x64、
Windows x64、Linux x64）：解压后 `./callfans` 无参数启动即打开 Web
控制台；无头环境 `./callfans serve --no-browser`（systemd 单元见
`deploy/callfans.service`）；CLI 用法同 `python3 -m kb`。

自己构建：`git tag v0.x.0 && git push origin v0.x.0` 触发 Actions
（四平台 PyInstaller 构建 + 自动创建 Release）。

## 架构一图

```text
        ┌──────────────────────── 知识图谱（主）────────────────────────┐
        │  8 节点/13 边本体 + 冷热分层（热区流水→温区摘要→冷区原文）      │
        └──────────────────────────┬────────────────────────────────────┘
                                   │ 只读 / 分区写
   任务触发 ──► 阶段一 check_rules（纯代码，零 LLM，毫秒级）
                │ 规则来源：Rule 节点 × RULE_CHECKERS 注册表
                ▼
               阶段二 decide（LLM 按需介入：上下文预算化组装 → 结构化决策）
                │
                ▼
               执行器（Appium/uiautomator2/adb，经 kb/executor.py 适配）
                │
                ▼
               回写：摄取漏斗 → Schema 校验事件 → WriteQueue（event_id 幂等，
                     partition=account 同账号串行）→ 层级 rollup 压缩
                     → 记忆固化 → 三因子召回（喂回决策上下文）
```

一个核心（kb core），三种壳（CLI / serve / GUI）：`python3 -m kb` 是 CLI，
`kb serve` 是本地 HTTP API + Web 控制台（`kb/static/index.html`，单文件零构建），
桌面与远程客户端都是这套 API 的消费者。

## 项目结构

```text
├── kb/                      # 核心代码（18 模块，零第三方依赖）
│   ├── schema.py            #   节点/边/事件定义 + 校验（Schema 即过滤器）
│   ├── store.py             #   内存图存储 + 分区写队列（幂等/仲裁/属性历史）
│   ├── store_sqlite.py      #   SQLite 嵌入式后端（桌面单机：events 幂等 + 物化表）
│   ├── store_neo4j.py       #   Neo4j 持久化后端（同接口，跨进程事件唯一约束去重）
│   ├── factory.py           #   存储工厂（CALLFANS_STORE=memory|sqlite|neo4j）
│   ├── ingest.py            #   摄取漏斗：执行结果 → Schema 约束事件
│   ├── tools.py             #   查询工具 + 规则闸门 + 两段式决策 + 审计 + 承诺扫描
│   ├── rollup.py            #   层级 rollup（日/周/月/年）+ 记忆固化 + 承诺追踪
│   ├── search.py            #   bigram 倒排检索 + 分层下钻
│   ├── consensus.py         #   公共记忆上卷（多账号确证）+ 灰度生效
│   ├── llm.py               #   LLM 接入层（OpenAI 兼容，六个可注入钩子）
│   ├── executor.py          #   执行层适配：decide()→执行器命令；结果→幂等回写
│   ├── maintenance.py       #   定时维护任务（daily/weekly/monthly/yearly，cron 入口）
│   ├── cli.py               #   统一 CLI（python3 -m kb）
│   ├── serve.py             #   本地 HTTP API + 静态页
│   └── static/index.html    #   三栏 Web 控制台（单文件，零构建）
├── demo/                    # 验证套件（也是改造期回归基线）
│   ├── seed.py              #   种子数据（6 账号 / 3 人设 / 8 话题 / 4 规则）
│   ├── run_demo.py          #   离线端到端（无 LLM）
│   ├── run_memory_demo.py   #   长期记忆全链路（10 项，无 LLM）
│   ├── run_sqlite_demo.py   #   SQLite 后端全链路（并发/恢复）
│   ├── run_neo4j_demo.py    #   Neo4j 全链路（并发去重/状态恢复）
│   ├── run_llm_demo.py      #   LLM 实机（决策/抽取/摘要/故障降级）
│   ├── run_memory_llm_demo.py # 记忆链路 LLM 实机
│   ├── run_executor_adapter_demo.py # 执行层适配（FakeRunner）
│   └── xhs.py               #   云机执行器实例（依赖外部 sma_autoui）
├── docs/                    # 设计文档（按编号即演进顺序）
│   ├── 01-knowledge-schema-v1.md        # Schema
│   ├── 02-query-and-write-protocol-v1.md # 查询与回写协议
│   ├── 03-memory-and-consensus-v1.md    # 长期记忆与共识
│   ├── 04-deployment-and-maintenance-v1.md # 部署与维护
│   ├── 05-requirement-review-v1.md      # 需求评审与重定位（改造总纲）
│   └── deepseek_markdown_20261009_7e1431.md # 本体模型设计讨论（2026-10-09）
├── callfans.py              # 应用入口（PyInstaller 打包目标；无参数=Web 控制台）
├── deploy/callfans.service  # Linux 无头部署 systemd 单元
├── docker-compose.yml       # Neo4j 后端容器
└── .github/workflows/release.yml # CI：tag v* → 四平台构建 → Release
```

## 设计文档（推荐阅读顺序）

| 文档 | 回答什么 |
| --- | --- |
| [docs/01](docs/01-knowledge-schema-v1.md) | 记什么：节点/边/事件本体，Schema 即过滤器，冷热分层 |
| [docs/02](docs/02-query-and-write-protocol-v1.md) | 怎么用：两段式决策、上下文 token 预算、幂等回写、并发模型 |
| [docs/03](docs/03-memory-and-consensus-v1.md) | 怎么记住：四层记忆、层级卷积、承诺追踪、共识灰度、失真管理 |
| [docs/04](docs/04-deployment-and-maintenance-v1.md) | 怎么跑：三存储后端、三种壳、定时任务、备份与时间旅行 |
| [docs/05](docs/05-requirement-review-v1.md) | 往哪去：通用记忆内核的重定位、主体模型定案、12 步改造顺序 |

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

## 改造路线（摘要）

`docs/05 §11` 定义了 12 步改造顺序，每步之后四个离线 demo 必须仍全绿。
要点：主体模型（Account→Subject，数字生命拥有跨平台记忆）、claim 租约
（多 agent 防双发）、写入/检索路径一次重设计（双时态/删除语义/信任分级/
溯源）、信念晋升管线（共识机制泛化，正反馈配负反馈）、三层人格模型
（Persona 设定层 + Trait 涌现层 + Episode 经历层，让数字生命会长大）。
技术栈定案：**Python 内核 + TS 前端**（GUI/Web/MCP 门面）。

## License

GPL-3.0，见 [LICENSE](LICENSE)。
