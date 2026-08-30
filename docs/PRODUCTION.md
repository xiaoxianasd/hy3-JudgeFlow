# 生产 Web/API 部署

## 架构与边界

生产请求不再把一次 Hy3 评估保持为长 HTTP 请求。客户端先提交任务，服务端返回 `202` 和随机任务编号；有界工作队列负责调用 Hy3、运行沙盒和多 Agent 复核，客户端轮询任务状态。

```text
Browser / API client
        │ HTTPS
        ▼
  Caddy / Nginx
        │ 127.0.0.1:8765
        ▼
 FastAPI + Uvicorn（可多实例）
        ├─ MySQL 持久化租约队列 → Worker → Hy3 TokenHub
        └─ SandboxExecutor → 一次性 Docker 容器
```

任务、状态、重试和结果保存在 MySQL。多个 API 实例可通过 `FOR UPDATE SKIP LOCKED` 安全抢占任务，Worker 使用租约和心跳恢复异常中断的运行。`WEB_JOB_WORKERS` 表示每个 API 实例的评估 Worker 数；所有实例必须使用一致的队列容量与租约配置。完整语义见 [MySQL 任务队列文档](DATABASE.md)。

## 安全控制

- 生产环境没有 `WEB_API_KEY` 或密钥短于 24 字符时拒绝启动；该密钥不得复用 TokenHub 密钥。
- `POST /api/v1/evaluations` 和任务查询需要 `X-API-Key` 或 Bearer Token。
- 提交接口按访问密钥与客户端地址限流；队列满时返回 `503` 和 `Retry-After`。
- 请求体被限制为 64 KiB，字段白名单验证，Hypothesis 上限固定为 500。
- API 错误不返回堆栈、Hy3 地址或 TokenHub 响应正文；用 `request_id` 关联服务端日志。
- 默认同源访问；只有 `WEB_ALLOWED_ORIGINS` 中的精确来源允许跨域，生产禁止 `*`。
- 响应包含 CSP、禁止嵌入、禁止 MIME 猜测、无引用来源等安全头。
- 候选代码仍必须使用 Docker 沙盒；生产模式禁止退回本地执行器。

API 密钥会由网页保存在 `sessionStorage`，关闭标签页后消失。生产站点必须使用 HTTPS，否则浏览器到反向代理之间的密钥可能被窃取。

## 部署步骤

1. 准备 Python 3.10+、MySQL 8.0+、Docker 和专用低权限系统账号。推荐使用 rootless Docker；不要开放未经认证的 Docker TCP API 或公网 MySQL 端口。

2. 安装应用并构建沙盒镜像：

```bash
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
docker build -f sandbox/Dockerfile -t hy3-process-sandbox:py3.12 .
```

正式发布应把 `sandbox/Dockerfile` 的 Python 基础镜像固定到审核过的 digest，并定期重建补丁版本。

3. 复制 `.env.production.example` 为 `.env`，替换站点域名、TokenHub 密钥和随机 Web API 密钥。可生成独立密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

4. 创建并升级 MySQL 数据库：

```bash
.venv/bin/tracejudge database upgrade
.venv/bin/tracejudge database status
```

数据库迁移必须在启动新版本 API 前单独执行，应用进程不会自动修改生产表结构。

5. 启动 API：

```bash
.venv/bin/tracejudge serve
```

服务默认只监听 `127.0.0.1:8765`。不要直接把 Uvicorn 端口暴露到公网，也不要使用 `--reload`。

6. 使用 Caddy 或 Nginx 提供 TLS。仓库中的 `deploy/Caddyfile.example` 可作为最小起点。反向代理和 API 必须位于同一受控主机时，才启用：

```dotenv
WEB_TRUST_PROXY_HEADERS=true
WEB_FORWARDED_ALLOW_IPS=127.0.0.1
```

如果 API 端口可能被绕过代理直接访问，应关闭代理头信任，避免攻击者伪造限流来源地址。

## API 协议

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---:|---|
| `GET` | `/api/v1/health/live` | 否 | 进程存活检查 |
| `GET` | `/api/v1/health/ready` | 否 | 队列和生产沙盒配置检查 |
| `GET` | `/api/v1/health/hy3` | 是 | Hy3 上游连接检查 |
| `GET` | `/api/v1/problems` | 否 | 可公开题目列表，不含答案和隐藏测试 |
| `POST` | `/api/v1/evaluations` | 是 | 提交评估，返回 `202` |
| `GET` | `/api/v1/evaluations/{id}` | 是 | 查询任务和结果 |
| `GET` | `/api/v1/queue/stats` | 是 | 查询队列深度、活动数和最老等待时间 |

提交示例：

```bash
curl -X POST https://tracejudge.example.com/api/v1/evaluations \
  -H "X-API-Key: $TRACEJUDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"problem_id":"two_sum_exists","hypothesis_examples":60,"review_mode":"supervisor"}'
```

## 上线验收

```bash
curl -fsS http://127.0.0.1:8765/api/v1/health/live
curl -fsS http://127.0.0.1:8765/api/v1/health/ready
tracejudge database status
python -m unittest tests.test_database_queue -v
python -m unittest tests.test_web_api -v
python -m unittest discover -s tests -v
```

另外必须从另一台机器验证 HTTPS、无密钥返回 `401`、超限返回 `429/503`、任务在服务重启后的恢复、MySQL 备份恢复和 Docker 恶意代码样本。任务队列是 at-least-once 语义；极端网络分区可能产生重复 Hy3 调用，但只有持有有效租约的 Worker 能提交最终状态。
