# callfans-agent 项目地图

callfans 知识库 MVP：云手机社交矩阵的知识中枢（本体 Schema + 图存储 + 分区写入队列 + 两段式决策工具 + 回写摄取漏斗 + 滚动压缩）。Python 3.9+，零第三方依赖。

## 目录结构

- `kb/` — 核心代码：`schema.py`（节点/边/事件定义）、`store.py`（内存图存储+分区写队列，默认后端）、`store_sqlite.py`（SQLite 嵌入式后端，桌面单机形态）、`store_neo4j.py`（Neo4j 持久化后端，同接口）、`factory.py`（CALLFANS_STORE 切换 memory|sqlite|neo4j）、`ingest.py`（摄取漏斗+MemoryNote 候选归一化）、`tools.py`（查询工具+两段式决策+审计+承诺扫描）、`rollup.py`（层级 rollup 日/周/月/年+记忆固化+承诺追踪）、`search.py`（bigram 倒排检索+分层下钻）、`consensus.py`（公共记忆上卷+灰度生效）、`llm.py`（LLM 接入层：决策/抽取/摘要/周期叙事/记忆固化/承诺扫描六钩子）、`executor.py`（执行层适配：decide()→xhs.py 命令行，结果→幂等回写+发布后承诺扫描）、`maintenance.py`（定时维护任务，cron 入口，含语义记忆时效淘汰）、`cli.py`（统一 CLI：python3 -m kb，status/query/memory/plan/serve）、`serve.py`（本地 HTTP API + 静态页）、`static/index.html`（三栏 Web 控制台，serve 启动后浏览器访问 http://127.0.0.1:8765/）
- `docs/` — 设计文档：`01-knowledge-schema-v1.md`（Schema）、`02-query-and-write-protocol-v1.md`（查询/回写协议）、`03-memory-and-consensus-v1.md`（长期记忆与共识）、`04-deployment-and-maintenance-v1.md`（部署/存储后端/定时任务/备份）
- `demo/` — `seed.py`（种子数据，可注入任意存储后端）、`run_demo.py`（离线端到端，不调 LLM）、`run_llm_demo.py`（LLM 实机验证）、`run_executor_adapter_demo.py`（执行层适配全链路验证，FakeRunner）、`run_memory_demo.py`（长期记忆系统全链路验证，无 LLM）、`run_memory_llm_demo.py`（记忆链路 LLM 实机验证）、`run_sqlite_demo.py`（SQLite 嵌入式后端全链路）、`run_neo4j_demo.py`（Neo4j 持久化全链路：并发去重/状态恢复）、`xhs.py`（云机执行器实例：小红书发布视频，依赖外部 sma_autoui，不在本仓验证范围内）
- `docker-compose.yml` — Neo4j 持久化后端容器（CALLFANS_NEO4J_* 环境变量）
- `README.md` — 快速开始与结构说明；`deepseek_markdown_20261009_7e1431.md` — 本体模型与 AI 调度的设计讨论笔记（2026-10-09）
- `.agents/skills/` — 飞书官方 Agent Skills（lark-* 系列，由 skills CLI 安装并 symlink 到 `.claude/`、`.qwen/`、`.trae/` 等目录；这些点目录不是项目代码）
- `.env.example` — LLM 接入配置模板

## 常用命令

```bash
python3 demo/run_demo.py                  # 离线闭环验证（无 LLM、无网络）
python3 demo/run_executor_adapter_demo.py # 执行层适配全链路（无真机/无 LLM）
python3 demo/run_memory_demo.py           # 长期记忆系统全链路（无 LLM）
python3 demo/run_memory_llm_demo.py       # 记忆链路 LLM 实机（需 CALLFANS_LLM_* 环境变量）
python3 demo/run_llm_demo.py              # LLM 实机验证（需 CALLFANS_LLM_* 环境变量）
python3 demo/run_sqlite_demo.py             # SQLite 嵌入式后端全链路（零外部服务）
python3 demo/run_neo4j_demo.py            # Neo4j 持久化全链路（需 docker compose up -d + pip install neo4j）
```

统一 CLI（后端由 CALLFANS_STORE 决定）：`python3 -m kb status|query|memory|plan|maintenance|serve`
Web 控制台：`python3 -m kb serve` 后浏览器打开 http://127.0.0.1:8765/（三栏 GUI，kb/static/index.html）

## 飞书 CLI（lark-cli）

- 已全局安装 `@larksuite/cli`（v1.0.97，位于 `~/.nvm/versions/node/v24.12.0/bin/lark-cli`），以用户「雷强」身份完成 OAuth 授权（2026-10-09 核验，`lark-cli auth status` 显示 user + bot 身份均 ready）。
- 授权范围覆盖：消息/群组、云文档、云空间、多维表格、电子表格、日历、邮箱、任务、知识库、妙记、视频会议、OKR、审批、通讯录等；`vc:meeting.realtime:read` 被企业管理员禁止申请。
- 凭证由 CLI 自身安全存储；不要把 token、cookie、OAuth code 写入本仓库任何文件或 mcp.json。
- 常用命令：`lark-cli auth status`（状态）、`lark-cli auth check --scope <scope>`（权限检查）、`lark-cli help`（总览）、`lark-cli schema <service.resource.method>`（接口详情）。
- 高危写操作（high-risk-write）需 `--yes` 且先经用户确认。

## 验证方式

- 离线：`python3 demo/run_demo.py` 全绿（内存模式，零依赖）
- 持久化：`python3 demo/run_neo4j_demo.py` 全绿（Neo4j 模式）
- 飞书连通性：`lark-cli auth status` + 任一只读调用（如 `lark-cli calendar calendars list`）

## 协作知识演进（Proma 维护）

- 保持本文件中的项目地图与已验证项目事实同步；命令、架构、边界和入口变化时做最小更新，不复制到协作记忆。
- Proma 工作区的 `memory/` 是可扩展的长期协作知识库：`MEMORY.md` 只做主题索引和路由，按证据创建用户画像、协作偏好、纠错与经验、决策理由等主题文件；不要把临时过程或长篇证据写入其中。
- 用户画像按具体领域渐进修订，不以“新手/专家”等全局标签定性。只有稳定、会改变未来协作判断的信息才值得维护。若记忆时间敏感、状态会更新，或记录具有后续判断价值的阶段性进展，须在对应正文相邻标注事实/状态的发生、生效或截至时间（至少日期；日内顺序、截止点或时区会影响判断时写明时间和时区），不能用文件修改时间替代；稳定事实无需额外添加时间戳。
- 基于明确、稳定证据的 Memory 最小增量可直接写入并在完成后说明；仅在删除或大段覆盖、与既有记录冲突、存在不确定推断，或可能涉及敏感个人信息时，先提出候选并取得确认。项目地图的已验证事实可直接更新。历史会话仅在用户授权后作为分批、限量的补充证据，不得全量扫描。

<!-- proma:knowledge-maintenance:start -->
本文件由 Proma 维护，仅记录已核验的项目事实（架构、目录、命令、验证、边界）。
更新原则：最小增量、保留用户手写内容、先核验后写入；不要把会话流水账或未经验证的推断写进来。
<!-- proma:knowledge-maintenance:end -->
