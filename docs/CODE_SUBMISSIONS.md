# 用户代码提交与过程评估

网页顶部提供两个独立工作区：**Hy3 自动解题**保留原有生成与多 Agent 评估流程；**提交我的代码**直接验证用户代码，不调用 Solver，不替用户编写推理步骤。

## 操作方式

1. 点击“提交我的代码”，从左侧选择题库中对应的题目。题面、输入格式、约束与公开示例仍可查看。
2. 粘贴 Python 代码，必须实现 `solve_case(case)`，参数为题目规定字段组成的字典，使用返回值输出结果，不读取 stdin。
3. 可选填写解题说明：每行一个完整步骤，最多 20 步，每步不超过 2,000 字符。代码最多 20,000 字符；总请求还受 `WEB_MAX_REQUEST_BYTES` 限制。
4. 点击“提交代码并评估”。服务端进入现有有界队列，依次运行固定/隐藏测试、已配置的 Hypothesis 策略和一次 Hy3 代码与过程审查。
5. 查看测试结果、实现逻辑判定、推理过程判定、错误分类、问题位置及修正建议。代码行与用户步骤使用不同编号，均从 1 开始。

草稿按题目分别保存在当前页面内存中，切换工作区不会丢失；刷新页面会清空。服务端保存提交内容和结果，使用现有任务保留策略清理。代码及步骤会发送到配置的 Hy3 服务，勿提交密码、密钥或个人敏感信息。

## 判定边界

- 只提交代码：`process_correct=null`、`process_status=not_provided`。可以发现实现逻辑错误，但不能宣称识别了作者未提交的思考过程。
- 提交代码与步骤：只审查实际提供的步骤是否成立且足以支撑结论，不套用要求固定措辞的关键词量表。
- `final_correct` 表示通过本次固定测试且没有发现差分反例，不是对所有输入的数学证明。Hy3 的逻辑审查另列为 `code_correct`。
- `unsupported_correct=true` 仅在测试通过但已提交的步骤被判为有问题时成立；没有步骤或审查未知时为 `null`。
- 模型置信度低于 0.65、无证据的否定、越界的行号/步骤号会降级为未确定。报告保留可复核意见，不将其作为已确认定位。
- Hy3 审查不可用时保留测试结果，逻辑与过程不能自动视为正确。验证基础设施故障也不能作为算法反例。
- 代码行定位为模型审查意见，应结合代码与反例复核，不声称具备完备的自动证明能力。
- 6 道原创题使用专用 Hypothesis 策略，60 道外部题使用逐题审核的显式策略；报告记录实际策略版本、输入域和执行状态。

## 安全环境与数据库升级

**用户代码任务在开发与生产环境中都必须使用 Docker。** 未配置 Docker、镜像不存在或服务不可用时，接口拒绝入队；Worker 执行时再次检查。不会退回本机执行。

安装并启动 Docker Desktop（Windows 使用 Linux containers）后，在项目目录构建镜像：

```cmd
docker build -f sandbox/Dockerfile -t hy3-process-sandbox:py3.12 .
```

在 `.env` 配置：

```dotenv
SANDBOX_BACKEND=docker
SANDBOX_DOCKER_IMAGE=hy3-process-sandbox:py3.12
ALLOW_UNSAFE_LOCAL_EXECUTION=false
```

Docker 后端沿用无网络、非 root、只读根文件系统、无宿主目录挂载、资源和超时限制。生产鉴权、限流、请求大小及有界队列同样适用于此入口。代码/注释被视为待评数据，不作为评审器指令。评审请求不包含参考实现或隐藏测试详情；状态响应不回传排队中的原始提交内容。

使用 MySQL 时先停止 Web/Worker，执行启动器菜单 **7** 或以下 CMD 命令，然后重新启动 Web：

```cmd
Hy3_TraceJudge.bat database
Hy3_TraceJudge.bat start
```

新增 Alembic `0003` 迁移仅添加可空的 `submission_json` 字段，旧任务保留且仍按 Hy3 自动生成流程运行。提交代码及步骤随任务持久化，重启与租约恢复后仍能继续执行。请在部署前备份数据库；不要对含用户提交的库随意执行降级迁移，降级会删除该字段。只使用内存队列的开发环境不需要数据库迁移。

## API

`POST /api/v1/code-submissions`，使用与原评估接口相同的 `X-API-Key` 或 Bearer 鉴权：

```json
{
  "problem_id": "two_sum_exists",
  "hypothesis_examples": 60,
  "code": "def solve_case(case):\n    seen = set()\n    for value in case['nums']:\n        if case['target'] - value in seen:\n            return True\n        seen.add(value)\n    return False\n",
  "reasoning_steps": [
    "使用集合保存已经访问过的值。",
    "先查找补数再加入当前值，因此不会重复使用同一个下标。"
  ]
}
```

返回 `202` 和 `job.id`、`status_url`，继续通过 `GET /api/v1/evaluations/{job_id}` 查询。`reasoning_steps` 可省略或为空数组。请求不接受自带标准答案、替换题面或任意执行入口。新工作区固定使用一次专用 Hy3 审查，不提供原生成模式的 Supervisor/Swarm 选择。

成功结果以 `source=user_submission` 标记。新增字段包含 `code_correct`、`process_status`、`first_error_line`、`submission_review` 和 `assessment_note`。用户提交不自动混入 Hy3 生成能力基准统计。

## 回归测试

```cmd
".\.venv\Scripts\python.exe" -m pytest tests/test_submissions.py tests/test_web_api.py tests/test_database_queue.py -q
```

可选浏览器测试需要 Node.js 与 Playwright：`node --test tests/web_ui.test.cjs`；设置 `JUDGEFLOW_BROWSER_CHANNEL=msedge` 可使用已安装的 Edge。浏览器用模拟响应测试界面，不消耗真实 Hy3 额度；不能替代 Docker 与真实 Hy3 的部署验收。
