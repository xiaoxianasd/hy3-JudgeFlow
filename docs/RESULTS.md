# 结果与能力边界

## 2026-09-07：修复后的离线构造集

本报告基于当前代码重新执行 `benchmark --source fixtures --hypothesis-examples 20`，对应 `reports/fixtures_benchmark.json/jsonl/csv`。这是执行与弃判行为验收，不是 Hy3 模型能力结果。旧版依赖关键词规则得到的“100% 首错定位、0% 误报”已撤下，不能作为修复后评审器的验证数据。

| 指标 | 当前结果 |
|---|---:|
| 去重并加入对抗变换后的样本 | 28 |
| 测试通过 | 22 / 28 |
| 检出实现失败 | 6 / 28 |
| 未启用语义评审，过程证据不足 | 22 |
| 已确认过程/实现有问题 | 6 |
| 过程评估覆盖率 | 21.43% |
| 错误答案上的问题检出率 | 100.00% |
| 精确匹配构造金标的首错定位率 | 0.00% |
| 正确过程上的误报率 | 未评估：没有明确语义结论 |

离线执行能证明 6 个候选实现有问题，但不能证明其最早的文字推理错误在哪里。当前实现证据指向隐式代码步骤，因此未匹配构造金标中的更早推理错误。22 个测试通过样本保留过程未知，其中既包含正确推导，也包含关键词堆砌、错误推导配正确代码等样本；不能把未知记作正确或错误。

统计中的 `process_accuracy=0` 仅表示已明确判定的 6 个样本均被判有问题，不能解释为全部 28 个过程都错了。覆盖率及未知数必须一同展示。

## 覆盖与回归案例

- 6 道种子题；原 24 条记录去重后为 16 条，再增加 6 条关键词堆砌/循环论证、6 条正确否定错误示例，共 28 条。
- 正确硬币兑换解答中否定“反复选择当前最大硬币”，不再被词汇规则直接判为步骤 2 错误。明确完整的语义正面复核可确认其成立。
- 用关键词代替推导，离线模式不再确认过程成立；其语义错误检出能力需启用 Hy3 后测量。
- 参考程序抛异常、候选返回 `None` 时，返回 `ReferenceOracleError`；合法参考程序返回 `None` 仍可验证。
- 人工复核导出及汇总已跑通：28 条全部保留待复核，人工覆盖率为 0，相关比率为 `null`。没有代填人工结论。

## 真实结果生成

具体步骤见 [评估与人工复核流程](EVALUATION_WORKFLOW.md)。

```bash
tracejudge benchmark --source fixtures --hy3-review --output reports/fixtures_hy3_review.json
tracejudge benchmark --source hy3 --tier seed --per-difficulty 2 --output reports/hy3_seed.json
tracejudge audit-export --benchmark reports/hy3_seed.json --output reports/hy3_seed_audit.jsonl
# 由实际复核者填写后再汇总
tracejudge audit-summary --benchmark reports/hy3_seed.json --annotations reports/hy3_seed_audit.jsonl --output reports/hy3_seed_validity.json
```

上述真实调用命令会使用配置的 Hy3 服务。本次功能修复未执行真实模型实验。外部题 AST 难度尚未人工校准；尚不能从当前构造集推断 Hy3 的能力边界或临界点。
