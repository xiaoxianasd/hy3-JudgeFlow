# TraceJudge

> Process-Level Evaluation and Error Localization with Hy3

面向“标准答案可自动验证”的算法题，调用 **Hy3 模型**生成完整解题过程，并结合代码沙盒、固定测试、Hypothesis 属性测试、规则量表和 Hy3 多 Agent 复核，判断过程是否成立、定位首个错误步骤、归类错误，以及识别“答案正确但过程不支持结论”的样本。

> 这是个人参赛原型，不是腾讯或 Hy3 官方项目。仓库内的构造集结果用于评估器验收，不能冒充 Hy3 模型能力结果；真实模型结果必须在连接 Hy3 服务后重新生成。

## 核心能力

- **完整解题**：Hy3 输出题意、算法、证明、复杂度、边界与可执行代码，而非只有最终答案。
- **双层结果验证**：固定/隐藏测试提供稳定回归，Hypothesis 自动生成输入并搜索、缩减反例。
- **过程级判断**：题目量表与五个专业 Agent 分别检查题意、算法、证明、复杂度和边界。
- **首错定位**：区分根因与下游传播错误，输出最早错误步骤及标准化错误类型。
- **结果—过程解耦**：单独识别 `final_correct=true`、`process_correct=false` 的“猜对或过程不支持结论”样本。
- **可复现实验**：提供离线受控错误集、分层指标、人工抽检模板以及 JSON/JSONL/CSV 报告。

## 项目状态

| 模块 | 当前状态 |
|---|---|
| Hy3 OpenAI-compatible / TokenHub 接入 | 已实现 |
| 生产 Web/API、鉴权、限流与异步任务队列 | 已实现 |
| MySQL 持久化队列、租约恢复与 Alembic 迁移 | 已实现 |
| 固定测试、隐藏测试与 Hypothesis 差分验证 | 已实现 |
| Single / Supervisor / Bounded Swarm | 已实现 |
| 6 道 easy / medium / hard 种子题 | 已实现 |
| 24 条受控轨迹的评估器验收 | 已实现 |
| 外部题库适配器与大规模真实 Hy3 评测 | 扩展项 |

详细技术方案见 [方案设计文档](docs/DESIGN.md)，评估原理见 [方法文档](docs/METHOD.md)，当前离线结果见 [结果报告](docs/RESULTS.md)。

## 快速开始：Windows 一键运行

双击项目根目录的 `Hy3_TraceJudge.bat`，即可通过菜单完成环境安装、Hy3 检查、Web 前后端启动、离线验收、真实批量评测和项目测试。

也可以在 CMD 中直接调用子命令：

```cmd
Hy3_TraceJudge.bat start
Hy3_TraceJudge.bat doctor
Hy3_TraceJudge.bat fixtures
Hy3_TraceJudge.bat benchmark
Hy3_TraceJudge.bat test
Hy3_TraceJudge.bat database
```

启动 Web 后会自动打开 `http://127.0.0.1:8765`；承载后端日志的 CMD 窗口必须保持打开。

![Demo](docs/demo.gif)

## 一次评估会发生什么

```text
题目
  └─ Hy3 /v1/chat/completions（reasoning_effort=high）
       ├─ 题意 / 算法 / 证明 / 复杂度 / 边界 / 代码
       ├─ 固定公开 + 隐藏测试
       ├─ Hypothesis 生成、差分验证并缩减反例
       ├─ 可解释规则量表逐步核验
       └─ Supervisor 并行分派 5 个 Hy3 专业 Agent
            ├─ 题意 / 算法 / 证明 / 复杂度 / 边界专家
            ├─ 确定性 Supervisor 汇总最早根因
            └─ Swarm 模式：仅在冲突时追加一次 Hy3 仲裁
                 └─ 最终正确性、过程正确性、首错、错误类型
```

Hypothesis 不是只用于项目单测：它位于产品评估主链中。候选代码即使通过预置用例，仍会与参考实现进行属性化差分；发现失败后保留缩减反例，从而检测“恰好过测试但实现逻辑有缺陷”。

网页中的“Hypothesis 自动测试输入上限”表示**每道题最多尝试生成多少组候选输入**。它不是题目数、训练样本数或 Agent 数。数值越大，发现隐蔽实现错误的机会通常越高，但执行时间也会增加；交互演示推荐 `60`，正式评测可提高到 `200–500`。

## 1. 安装

要求 Python 3.10+：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e .
```

复制环境配置：

```bash
# Windows PowerShell
Copy-Item .env.example .env
# Linux/macOS
cp .env.example .env
```

默认连接本机 Hy3 OpenAI 兼容服务：

```dotenv
HY3_BASE_URL=http://127.0.0.1:8000/v1
HY3_API_KEY=EMPTY
HY3_MODEL=hy3
HY3_TIMEOUT_SECONDS=180
HY3_MAX_TOKENS=8192
```

腾讯云 TokenHub（广州站）使用：

```dotenv
HY3_BASE_URL=https://tokenhub.tencentmaas.com/v1
HY3_API_KEY=控制台中新轮换的密钥
HY3_MODEL=hy3
```

客户端会自动识别 TokenHub，并使用其 Chat API 的顶层 `reasoning_effort` 参数；自托管 Hy3 则使用 vLLM/SGLang 的 `chat_template_kwargs` 扩展。

## 2. 启动 Hy3 模型 API

按 [Hy3 官方仓库](https://github.com/Tencent-Hunyuan/Hy3) 部署。官方 vLLM 示例的核心启动参数如下（Hy3 是 295B MoE，官方建议 8 张大显存 GPU）：

```bash
vllm serve tencent/Hy3 \
  --tensor-parallel-size 8 \
  --speculative-config.method mtp \
  --speculative-config.num_speculative_tokens 2 \
  --tool-call-parser hy_v3 \
  --reasoning-parser hy_v3 \
  --enable-auto-tool-choice \
  --port 8000 \
  --served-model-name hy3
```

也可把 `.env` 指向任意提供该 OpenAI 兼容接口、且实际加载 Hy3 的远端服务。先验连接：

```bash
tracejudge doctor
```

## 3. 运行应用

Web/API（开发模式）：

```bash
tracejudge serve --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，选择题目和编排模式后运行：

Web 端使用版本化 `/api/v1` 协议：提交评估后立即得到任务编号，再轮询任务状态，避免 Hy3 长耗时请求持续占用 HTTP 连接。MySQL 保存任务、租约、重试状态和最终结果，服务重启后仍可查询；MySQL 8 的 `SKIP LOCKED` 支持多 Worker/多实例安全抢占。生产环境还会强制 API 密钥、有限队列、提交限流、请求大小限制、安全响应头与隐藏测试脱敏。完整部署方式见 [生产 Web/API 文档](docs/PRODUCTION.md)，队列细节见 [MySQL 任务队列文档](docs/DATABASE.md)。

- `supervisor`（默认）：生成 1 次，5 个专业 Agent 并行审查，共 6 次 Hy3 调用；
- `swarm`：在 Supervisor 基础上，发现冲突或错误时最多追加 1 次仲裁；
- `single`：兼容原有基线，生成 1 次、通用审查 1 次。

专业 Agent 并行执行可以降低等待时间，但不会减少 TokenHub 计费调用数。固定测试与 Hypothesis 的执行结果是硬证据，语言 Agent 不能抹去已发现的反例。

CLI 完整流程：

```bash
tracejudge list
tracejudge solve --problem coin_change --review-mode supervisor --hypothesis-examples 80 --output reports/coin_change.json
```

真实 Hy3 分层基准：

```bash
tracejudge benchmark --source hy3 --review-mode supervisor --hypothesis-examples 60 --output reports/hy3_benchmark.json
```

### 扩展外部题集（MBPP+ / HumanEval+）

种子集之外，可以从 EvalPlus 导入函数级外部题扩大真实模型评测的分层样本：

```bash
python scripts/import_evalplus.py            # 自动下载（需网络）并沙盒验证后导入
python scripts/import_evalplus.py --source mbpp --max 120
```

导入题写入 `data/problems_external.json`，标记 `tier: external`：

- 每题的参考实现与测试都会在项目沙盒中实际执行验证，失败即拒绝导入；
- `rubric/gold_steps` 为空，过程判定依赖固定测试 + Hy3 多 Agent 证据（Hypothesis 差分对外部题自动关闭并在报告中标注）；
- 难度为 AST 复杂度启发式，需人工复核；
- fixtures 构造集验收仍然只用 6 道原创种子题，不受外部题影响；
- 真实基准可用 `--tier seed|external|all` 控制范围，例如 `tracejudge benchmark --source hy3 --tier external --limit 30`。

不依赖模型服务的评估器验收：

```bash
tracejudge benchmark --source fixtures --hypothesis-examples 20
```

后者使用人工指定的单点错误和金标准首错，仅验证定位准确率/误报率；输出会明确标为 `evaluator_validation_fixtures`。

## 4. 题集与数据栈

当前可离线运行的种子集包含 6 道原创参数化题，按 easy / medium / hard 分层；每题具有公开测试、隐藏测试、参考实现、标准过程、规则量表和一个人工审查的受控错误。运行：

```bash
python scripts/prepare_data.py
```

会生成带 SHA-256 指纹与固定 split ID 的 `data/processed/*.jsonl`。完整扩展数据栈记录在 [sources.json](data/manifests/sources.json)：

| 层 | 数据 | 用途 |
|---|---|---|
| 主题集 | TACO | 算法题、参考实现、测试、难度 |
| OOD | LiveCodeBench | 时间切分、降低污染 |
| 执行推理 | CRUXEval | 代码输入/输出推理 |
| 测试增强 | EvalPlus | 边界测试设计参考 |
| 过程元评测 | ProcessBench | 首错定位 |
| 过程元评测 | PRMBench | 错误步骤与类型 |
| 研究参考 | PRM800K、BIG-Bench Mistake、Verify-then-Generate | 步骤监督与验证方法 |

上游大数据不直接提交。清单同时记录许可证和需要逐题复核的来源；不能因为聚合仓库使用开源许可证就自动假定每一道抓取题目可重新分发。

## 5. 判定与指标

- **最终答案正确**：固定测试全过，并且当前 Hypothesis 预算内未找到差分反例。
- **过程正确**：量表步骤全部成立、实现证据不矛盾，且专业 Agent 没有给出置信度 ≥ 0.65 的具体实质错误。
- **首错定位**：Supervisor 取量表、执行/反例和专业 Agent 中证据支持的最早步骤；Swarm 仲裁只能把根因提前，不能抹去确定性证据或把首错后移。纯代码错误记在推理步骤之后的“实现步骤”。
- **答案正确但过程不成立**：`final_correct=true` 且 `process_correct=false`。
- **定位准确率**：错误答案样本中，预测首错步骤与人工金标准完全一致的比例。
- **误报率**：人工确认过程成立且答案正确的样本中，被误判为过程错误的比例。

错误分类包括题意误读、概念错误、算法错误、定理误用、条件遗漏、计算错误、跳步、循环论证、复杂度错误、幻觉、实现错误和格式错误。完整设计见 [方法报告](docs/METHOD.md)，当前构造集验收见 [结果报告](docs/RESULTS.md)。

核心输出字段：

| 字段 | 含义 |
|---|---|
| `final_correct` | 固定测试全部通过且 Hypothesis 未发现反例 |
| `process_correct` | 推理链在规则、执行证据和 Agent 复核下成立 |
| `unsupported_correct` | 最终结果正确，但过程不能支持该结论 |
| `first_error_step` | 最早出现实质错误的步骤；纯实现错误位于文字步骤之后 |
| `error_types` | 标准化错误类型列表 |
| `orchestration` | 专业 Agent 意见、冲突、仲裁和调用用量 |

## 6. 测试与安全边界

```bash
python -m unittest discover -s tests -v
```

候选代码统一通过 `SandboxExecutor` 执行。默认 `local` 后端供 Windows 开发和单元测试使用，包含 AST 策略、受限 builtins、输入/输出上限和强制超时，但它不是宿主机安全边界。生产环境必须使用一次性 Docker 后端：

```bash
docker build -f sandbox/Dockerfile -t hy3-process-sandbox:py3.12 .
```

```dotenv
APP_ENV=production
SANDBOX_BACKEND=docker
ALLOW_UNSAFE_LOCAL_EXECUTION=false
SANDBOX_DOCKER_IMAGE=hy3-process-sandbox:py3.12
```

Docker 后端不挂载宿主目录，并启用无网络、只读根文件系统、非 root、CPU/内存/PID、临时目录和输出限制。`APP_ENV=production` 时本地后端会被强制拒绝，不能静默降级。完整威胁模型、构建和验收方式见 [安全沙盒文档](docs/SANDBOX.md)。Web/API 默认只监听 `127.0.0.1`，上线时通过 TLS 反向代理发布；生产配置样例见 `.env.production.example`。

## 目录

```text
hy3_tracejudge/       Hy3 客户端、多Agent编排、沙盒、Hypothesis、评估器、生产 API
migrations/           MySQL/Alembic 数据库结构迁移
data/problems.json    可运行种子题集与金标准过程
data/manifests/       外部数据栈、许可和抽样计划
data/annotations/     人工抽检模板
scripts/              数据准备与 Demo 生成
tests/                单元测试与参考实现属性测试
reports/              机器可读结果（运行后生成）
docs/                 方法与案例报告
```

## 文档

- [方案设计：架构、数据流、Agent 编排与接口](docs/DESIGN.md)
- [安全沙盒设计、生产配置与验收](docs/SANDBOX.md)
- [生产 Web/API：鉴权、异步任务、限流与部署](docs/PRODUCTION.md)
- [MySQL 持久化任务队列：租约、重试、迁移与运维](docs/DATABASE.md)
- [过程评估方法与有效性验证](docs/METHOD.md)
- [当前结果与典型案例](docs/RESULTS.md)
- [题集与数据栈](data/README.md)

## License

本项目采用 [Apache License 2.0](LICENSE)。外部数据集仍须分别遵守各自许可证与再分发要求。
