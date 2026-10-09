# 部署与维护协议 v1（callfans-agent）

对应 2026-10-10 讨论：持久化选型（Neo4j 直上）+ 定时任务编排。

## 1. 存储后端

| 模式 | 实现 | 适用 |
| --- | --- | --- |
| `memory`（默认） | `kb/store.py` | 离线 Demo / CI / 测试：零依赖、零运维 |
| `sqlite` | `kb/store_sqlite.py` | **桌面单机版**：嵌入式单文件，WAL 多进程读写，随 App 分发 |
| `neo4j` | `kb/store_neo4j.py` | 服务器 / 团队共享：跨进程共享的物化层 |

架构原则：**图是缓存，事件是真相**。两种后端的写入路径都是
Event（Schema 校验）→ 去重 → apply。Neo4j 模式下 `apply()` 在同一事务里
写 KBEvent 节点（`event_id` 唯一约束承担跨进程幂等去重）+ 更新图状态，
与内存版语义完全对齐（仲裁 / 属性历史 / duplicate 返回值）。

数据形态：图结构原生（KBNode 标签 + 关系类型 + ID）；属性载荷以 JSON
存储（Neo4j 属性不支持嵌套 dict，如 Rule.params）。需要 Cypher 直接过滤
热属性时，v2 可把该字段提升为原生属性。

### 环境变量

```bash
CALLFANS_STORE=neo4j                 # memory | neo4j
CALLFANS_NEO4J_URI=bolt://localhost:7687
CALLFANS_NEO4J_USER=neo4j
CALLFANS_NEO4J_PASSWORD=...
```

切换用 `kb.factory.open_store() / open_kb()`；`demo.seed.build_seed(store,
queue)` 支持注入任意后端。SQLite 后端额外环境变量：`CALLFANS_SQLITE_PATH`
（默认 `~/.callfans/callfans.db`）；events 表 `INSERT OR IGNORE` 承担跨进程
幂等去重，nodes/edges 物化表加速读路径（无需重放）。

## 1.1 统一 CLI（python3 -m kb）

命令与 GUI 动作一一对应（Cmd+K 面板复用同一套词汇）：

```bash
python3 -m kb status                    # 图谱/后端/账号概览
python3 -m kb query "防晒"               # 知识检索
python3 -m kb memory acc:xiaohongshu:lily_beauty   # 人可读记忆摘要
python3 -m kb plan acc:... post topic:... --job-id X  # 决策→执行命令（dry run）
python3 -m kb maintenance daily         # 维护任务（cron 同款）
python3 -m kb serve --port 8765         # 本地 HTTP API
```

## 1.2 HTTP API（kb serve）

零依赖实现（http.server）。本机/内网 API，公网暴露需加反向代理。

| 端点 | 说明 |
| --- | --- |
| `GET /health /stats /accounts` | 存活 / 图谱统计 / 账号状态 |
| `GET /account?account= /memory?account=` | 单账号状态 / 人可读记忆摘要 |
| `GET /search?q=` | 知识检索 |
| `POST /decide` | 两段式决策（body: account_id/action_type/topic_id） |
| `POST /plan` | 决策→执行命令（body 可含 job_id/task_params/device_env） |
| `POST /report` | 执行结果回写（自动承诺扫描） |
| `POST /maintenance/{level}` | 维护任务 |

桌面 GUI 与远程客户端都是这套 API 的消费者：**一个核心（kb core），三种壳
（CLI / serve / GUI）**。

## 1.3 Web 控制台（GUI）

`python3 -m kb serve` 后浏览器打开 `http://127.0.0.1:8765/` 即用——
`kb/static/index.html`（单文件、零构建依赖）：

- 三栏布局：左·账号墙（风险状态色）/ 中·工作区（Episode 时间线 + 记忆摘要）/
  右·检查器（未兑现承诺 + 三因子召回 + 情景索引）
- 顶部全局命令框（⌘K 聚焦）：自然语言检索知识库；`/stats /daily /weekly` 命令
- 底部状态栏：后端/图谱统计 + 一键维护按钮
- 决策可解释性：记忆摘要渲染承诺（☐ 待兑现）、里程碑（📌）、过期记忆（划线）

Tauri 桌面壳（macOS/Windows/Linux）= sidecar 拉起 `kb serve`（sqlite 后端）
+ WebView 加载同一页面，前端零重写。

### 启动 Neo4j（Docker）

```bash
docker compose up -d      # 见 docker-compose.yml（含内存参数与数据卷）
python3 demo/run_neo4j_demo.py   # 实机验证：全链路 + 双进程去重 + 状态恢复
```

注意：旧版 Docker Desktop（如 20.x）可能与新版 JVM 冲突（线程创建 EPERM），
需 `--security-opt seccomp=unconfined`（docker-compose.yml 已包含则忽略）。

## 2. 维护任务（kb/maintenance.py）

全部幂等：到点就跑、跑挂了下次再跑、漏跑了补跑即可，无需调度语义。
单账号失败不影响其他账号（错误记录在返回值里）。LLM 钩子可选
（`--llm`，需 CALLFANS_LLM_* 环境变量），失败自动回退确定性兜底。

| 命令 | 建议时刻 | 内容 |
| --- | --- | --- |
| `python3 -m kb.maintenance daily` | 每天 03:00–04:00（错峰） | 未归档流水 → 日摘要 |
| `python3 -m kb.maintenance weekly` | 每周一 03:30 | 周卷积（Episode）+ 记忆固化 |
| `python3 -m kb.maintenance monthly` | 每月 1 日 03:30 | 月卷积 |
| `python3 -m kb.maintenance yearly` | 每年 1 月 1 日 | 年度叙事 |

事件驱动的任务（不需要 cron，已内嵌在执行流程）：
信号上报（`consensus.report_signal`，执行器遇异常时）、承诺扫描
（`executor.report` 发布成功后）、承诺兑现（`fulfill_promise`）。

### crontab 示例（每实例错峰 5 分钟）

```cron
# 数据库实例 A（业务进程所在机器）
30 3 * * *  cd /path/to/callfans && CALLFANS_STORE=neo4j python3 -m kb.maintenance daily
30 3 * * 1  cd /path/to/callfans && CALLFANS_STORE=neo4j python3 -m kb.maintenance weekly --llm
30 3 1 * *  cd /path/to/callfans && CALLFANS_STORE=neo4j python3 -m kb.maintenance monthly

# 实例 B 错峰：35 3 * * * ...（多机部署时各实例错开 5-10 分钟）
```

LLM 成本控制：日摘要 + 周叙事 + 固化 ≈ 每账号每周 9 次调用。灰度期
可只对头部账号启用（维护函数按账号粒度调用即可），其余走 stub 兜底。

## 3. 多进程并发模型（当前边界）

- **读**：任意进程连 Neo4j 直接读（无锁）
- **写**：每进程独立 WriteQueue → Neo4j 事务；`event_id` 唯一约束保证
  跨进程幂等（实机验证：双进程并发提交同事件，胜者 applied、败者
  duplicated，互动计数精确 +1）
- **同分区串行**：目前靠"同账号任务由同一调度实例执行"的部署约定；
  严格跨进程分区串行是 Kafka 分区 worker 的课题（接口不变）
- **注意**：属性历史 / 仲裁采用读-改-写，同一节点的并发写依赖 Neo4j
  行锁串行化——语义正确，但高频同节点写入会有锁竞争（当前量级无影响）

## 4. 备份与恢复

- 备份：`docker exec callfans-neo4j neo4j-admin database dump neo4j --to-path=/data/backup`
  （或直接停容器拷 volume）
- 恢复：事件即真相——KBEvent 节点保留全部历史，极端情况下可
  `MATCH (e:KBEvent) RETURN e` 导出事件流重放重建
- 时间旅行：按 `e.ts` 过滤事件流重放，可重建任意时点的图状态

## 5. 验证

```bash
python3 demo/run_demo.py                    # 内存模式离线闭环
python3 demo/run_memory_demo.py             # 记忆系统（内存模式）
python3 demo/run_neo4j_demo.py              # Neo4j 全链路 + 并发 + 恢复
CALLFANS_STORE=neo4j python3 -m kb.maintenance daily   # 维护 CLI
```
