# MySQL 持久化任务队列

## 为什么直接使用 MySQL 队列

评估任务包含 Hy3 生成、沙盒测试、Hypothesis 搜索和多 Agent 复核，运行时间远长于普通 HTTP 请求。任务写入 MySQL 后，API 可立即返回任务编号；Worker 再异步抢占执行。服务重启、API 进程替换或短时数据库断线不会丢失任务状态和已完成结果。

当前实现要求 MySQL 8.0+，使用 `SELECT ... FOR UPDATE SKIP LOCKED` 让多个 Worker 或多个 API 实例并发抢占而不重复领取同一任务，不需要 Redis。

## 状态机

```text
queued ──claim──> running ──success──> succeeded
  ▲                 │
  │                 ├─ transient Hy3 failure ──> retry_queued ──> queued
  │                 ├─ permanent failure ──────> failed
  └─ expired lease──┘
```

- 每次抢占都会增加 `attempts` 并设置 `lease_owner`、`lease_expires_at`。
- 执行期间独立心跳线程续租；正常完成时只有仍持有租约的 Worker 能写入结果。
- Worker 崩溃后，过期租约会被恢复为排队状态；达到 `JOB_MAX_ATTEMPTS` 后转为失败。
- Hy3 上游错误按指数退避重试；确定性的评估器错误不自动重试。
- 已完成任务按 `JOB_RETENTION_SECONDS` 保存，后台分批清理。
- 全局活动任务数由 MySQL 命名锁保护，超过 Worker 与队列容量时 API 返回 `503`。

该队列提供 **at-least-once** 执行语义。极端网络分区可能让同一评估调用 Hy3 两次，因此下游不能依赖“模型调用绝对只发生一次”；最终状态更新由租约所有权保护。

## 表结构

`evaluation_jobs` 保存：

- 题目、Hypothesis 预算和 Agent 编排模式；
- 逐任务选择的模型名，以及是否依赖浏览器临时凭据；
- 排队、运行、阶段、尝试次数和最大重试次数；
- Worker 租约、心跳、开始和完成时间；
- JSON 类型的最终评估结果或公开错误；
- 用于抢占、租约恢复和过期清理的组合索引。

数据库结构由 Alembic 管理。不要手工修改表，也不要在生产启动时自动建表。

网页填写的 TokenHub API Key 不进入 `evaluation_jobs`。同一进程内的临时凭据保险箱按任务编号保存 Key，数据库只记录 `requires_transient_credentials`。若进程在任务结束前重启，Worker 会将该任务标为 `transient_credentials_lost`，要求用户重新提交；不会退回服务端 Key 后继续执行。未填写网页 Key 的任务仍可使用服务端 `HY3_API_KEY`，所选 `requested_model` 可在重启后恢复。

## 初始化与升级

配置使用 URL 编码后的 SQLAlchemy URL：

```dotenv
QUEUE_BACKEND=mysql
DATABASE_URL=mysql+pymysql://tracejudge:URL_ENCODED_PASSWORD@127.0.0.1:3306/tracejudge?charset=utf8mb4&connect_timeout=5
```

首次运行以及每次发布新迁移后执行：

```bash
tracejudge database upgrade
tracejudge database status
```

Windows 也可以运行 `Hy3_TraceJudge.bat database`，或在菜单选择数据库初始化项。

`database upgrade` 会在账号有权限时创建目标数据库，然后升级到最新 revision。应用启动本身不会静默改变数据库结构。

## 生产账号权限

不要让 Web 服务长期使用 MySQL `root`。建议创建仅用于 TraceJudge 的账号，迁移期间授予 DDL 权限，迁移完成后运行账号只保留目标库的 `SELECT`、`INSERT`、`UPDATE`、`DELETE`：

```sql
CREATE DATABASE IF NOT EXISTS tracejudge CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'tracejudge'@'127.0.0.1' IDENTIFIED BY '使用密码管理器生成的随机密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP
  ON tracejudge.* TO 'tracejudge'@'127.0.0.1';
```

完成迁移后可撤销 `CREATE`、`ALTER`、`INDEX`、`DROP`。如果 MySQL 位于另一台主机，应启用 TLS、限制防火墙来源，并把 CA 参数加入连接 URL；不能把 3306 直接暴露到公网。

## 运维检查

```bash
tracejudge database status
curl -fsS http://127.0.0.1:8765/api/v1/health/ready
curl -H "X-API-Key: $TRACEJUDGE_API_KEY" http://127.0.0.1:8765/api/v1/queue/stats
python -m unittest tests.test_database_queue -v
```

真实 MySQL 集成测试默认跳过，防止普通测试误操作共享数据库。只在专用测试库或确认没有生产任务时启用：

```powershell
$env:TRACEJUDGE_RUN_MYSQL_TESTS="1"
python -m unittest tests.test_mysql_integration -v
```

生产环境还应监控队列中 `queued` 数量、最老任务等待时间、失败率、重试率、数据库连接池和磁盘空间，并定期做 MySQL 备份恢复演练。
