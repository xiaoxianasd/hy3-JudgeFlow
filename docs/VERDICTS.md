# 三态判定与验证覆盖

## 设计目标

测试通过、过程成立、错误可定位是三个不同问题。自动解题的 Single / Supervisor / Swarm 和离线规则共用 `multi_agent._build_decision`，`verdicts.py` 提供执行证据及三态语义；网页只展示结果，不自行调整判定阈值。代码提交保持独立的作者步骤/代码行语义，但复用执行证据和结果—过程组合规则。

本次没有增加依赖或新的 Agent。默认负面/正面模型意见的置信度门槛仍为 0.65；该分数是模型自报值，不是经过校准的错误概率，后续仍需要人工标注的留出集验证。

## 判定规则

| 证据情况 | 过程结论 | 定位 |
|---|---|---|
| 有可靠反例或足够置信度的实质错误意见 | `false` | 仅接受真实步骤编号；无法确定位置时为 `null` |
| 没有错误证据，且所有所需评审完整、意见明确、覆盖目标步骤 | `true` | 不适用 |
| 评审未完成、低置信度、返回未知、未覆盖目标步骤、存在待复核意见 | `null` | 不确定 |
| 标准答案或验证服务故障，且没有其他可靠错误证据 | `null` | 不确定 |

- 已确认的问题优先于缺失证据：一个明确反例不会因为其他 Agent 掉线而消失。
- 错误存在与定位能力分开：高置信度负面意见缺少位置，不能反向变成正确。布尔值、负数、字符串、越界编号都不能冒充步骤编号。
- 过滤接口适配误判只能撤销该意见，不能证明对应阶段正确；无独立完整评审时保留未知。
- Swarm 不可抹去确定性反例；完整覆盖全部步骤且没有缺失检查时，可撤回专家误报。对未能可靠定位的结论，不能仅凭未知位置相等而记为定位成功。
- 所有词汇规则均为提示（`criteria.passed=null`），不参与直接定罪或证明。离线模式即使匹配全部关键词也不能确认过程成立；存在可执行反例时仍可确认实现错误。
- `unsupported_correct` 只有在测试和过程结论均明确时计算。任一项未知，结果就是 `null`。

评估结果中的 `reasoning_evidence` 只保留支持最终首错位置的语义评审证据，包括步骤、来源、置信度、原因和反例说明。网页将固定测试与 Hypothesis 单列为“代码验证证据”，将该字段单列为“推理反例证据”，并放在结论下方、完整过程与 Agent 详情之前。评审反例会显示其模型来源和置信度，不能冒充可执行测试；若反例可以写成具体输入，评审提示要求同时给出该步骤声称的结果和正确结果。

## 失败归属

评估结果用 `failure_owner` 区分失败来源：`model` 表示模型答案、格式或实现本身失败，`evaluator` 表示人工确认答案与过程成立但自动评估给出否定结论，`infrastructure` 表示沙盒、参考实现、属性测试或所需评审未可靠完成。没有失败时为 `null`。

在线自动结果可给出 `model`、`evaluator`、`infrastructure` 或 `null`，并标记 `failure_owner_status=provisional`。模型答案或实现的确定性失败归入 `model`；评审器返回不合协议的结构归入 `evaluator`；网络、上游服务和验证环境不可用归入 `infrastructure`。自动误报仍须经人工确认：完成独立人工复核后，`audit-summary` 在 `adjudicated_records` 中写入 `failure_owner_status=adjudicated`。网页明确显示这两种状态，避免把自动归属当作人工事实。

## Hypothesis 状态

| `hypothesis.status` | 含义 |
|---|---|
| `unsupported` | 未配置此题的输入生成策略，检查次数为 0 |
| `passed` | 在当前预算内未找到反例，不是正确性证明 |
| `failed` | 找到反例 |
| `error` | 验证未可靠完成，不能作为反例或通过依据 |

未运行时 `hypothesis=null`。`examples_checked` 是实际进入差分检查的次数，含收缩和重放，可能超过 `max_examples` 生成预算。目前 6 道核心题和 60 道外部题均有显式策略；结果记录策略版本和输入域。验证服务异常、参考实现异常及不稳定的属性测试结果不作为算法错误。

## 指标口径

- `process_accuracy = process_correct_count / process_evaluated_samples`；测试准确率同理。仅在明确结论子集上计算，分母为 0 时是 `null`。
- 同时报告 `*_uncertain_count`、`*_evaluated_samples`、`*_coverage`，必须与准确率一同解读。未知样本不计作正确或错误。
- 检错率、精确首错定位率仍以全部金标准错误答案为分母：弃判不算检出或定位成功。`null == null` 永远不是定位成功。
- 正确过程误报率以给出明确判定的人工确认正确过程为分母，同时给出 `sound_process_review_coverage`；被标红样本中的真实问题/误报比例仍独立报告。
- 难度分层使用相同口径。不能只展示已判定子集的高准确率、隐去大量未知样本。

## 2026-09-07 更新

修复否定关键词误报、关键词堆砌误通过、逐用例参考程序异常漏检和隐藏沙盒导入契约导致的误报；增加失败归属、人工复核导出与指纹校验汇总、分层等量选题及 Wilson 区间。详见 [评估与人工复核流程](EVALUATION_WORKFLOW.md)。

## 兼容与发布

本次保留原布尔字段，并加入可空状态、覆盖统计及说明，不改数据库表结构。旧 JSON 任务不会自动改判；若旧任务保存了 Supervisor 选中的专家意见，网页会从中恢复首错反例用于展示，缺少这些数据时仍标为历史信息。需要按新规则重新判定时请重新发起评估。重启 Web 和独立 Worker 后刷新页面。此前代码提交功能要求的 `0003` 迁移仍需完成，但本次无需新增迁移。

## 参考依据

- [Z3 官方：Basic Commands](https://microsoft.github.io/z3guide/docs/logic/basiccommands/)：不能确定时返回独立的 `unknown`。此处只借鉴区分证据不足的设计原则，没有集成 Z3，也不宣称模型审查是形式化证明。
- [Pydantic 官方：严格布尔类型](https://docs.pydantic.dev/latest/api/standard_library_types/#booleans)：严格区分布尔值与可转换为布尔的其他输入。模型协议显式接受布尔或 null，不接受字符串 `"false"` 冒充布尔值。
- [Hypothesis 官方：max_examples](https://hypothesis.readthedocs.io/en/latest/reference/api.html#hypothesis.settings.max_examples)：生成预算不等于包括收缩和重放在内的总执行次数，页面单独展示两者。

## 回归验证

```bash
python -m pytest -q
node --test tests/web_verdicts.test.cjs
```

新增测试包含三态组合的 Hypothesis 属性检查、评审故障、置信度异常、遗漏覆盖、无法定位的错误、验证服务故障、统计分母、CSV 状态及网页显示。网页状态测试使用 Node 内置测试器和 DOM stub，无额外前端依赖；不替代真实浏览器布局检查或 Docker/Hy3/MySQL 端到端验收。

2026-09-03 本轮结果：Python 96 项通过、2 项真实 MySQL 集成测试跳过；Node 页面状态测试 8 项通过。另通过 HTTP JSON 空值传递检查、JavaScript 语法检查及 `git diff --check`。保留了原有的 Starlette/httpx 弃用提示和测试辅助函数返回值警告，未为本次判定修复扩大依赖升级范围。
