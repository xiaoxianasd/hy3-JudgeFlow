# 过程错误检出专项报告

## 范围与口径

本报告专门验证“最终答案正确但过程不成立”。检出、漏检、误报和弃判互斥：

- 检出：金标过程错误，评估器判定过程错误；
- 漏检：金标过程错误，评估器判定过程成立；
- 误报：金标过程成立，评估器判定过程错误；
- 弃判：存在过程金标，但评估器因证据不足、服务失败或结论缺失返回未知。

受控构造与自然生成样本分别统计，不计算合并准确率。

## Hy4 preview 受控构造结果

新增两组各6条配对反事实轨迹。每条只替换一个标准步骤，保留参考代码和其余标准过程，并附能直接推翻错误陈述的反例。另加入6条标准正确过程作为误报对照。18条代码均通过固定测试与属性测试。

| 类型 | 样本 | 检出 | 漏检 | 误报 | 真阴性 | 弃判 | 首错精确命中 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 正确代码＋错误证明 | 6 | 3 | 0 | 0 | 0 | 3 | 3/3 |
| 错误条件推广、代码仍正确 | 6 | 2 | 0 | 0 | 0 | 4 | 2/2 |
| 标准正确过程 | 6 | 0 | 0 | 0 | 4 | 2 | 不适用 |
| 合计 | 18 | 5 | 0 | 0 | 4 | 9 | 5/5 |

本轮使用 `hy4-preview` 的Single审查。错误过程检出覆盖率为5/12（41.67%）；在有明确过程结论的错误样本中为5/5，但不能脱离覆盖率单独报告。正确过程对照的明确评审覆盖率为4/6，误报0/4。9条弃判由HTTP 429、读超时或结构化输出失败造成，其中错误过程7条、正确过程2条。

合并机器结果：`hy4_preview_process_validation_20260908.json/jsonl/csv`。其中保留每条样本的所有尝试摘要、最终选中尝试、金标、反例、执行证据与评审证据。首次Hy3额度耗尽运行保存在 `controlled_process_20260908.*`，未与Hy4结果合并。

## 自然生成结果

使用2026-09-07真实Hy3分层试运行的6条自然生成答案及完整独立人工复核：

| 范围 | 样本 | 检出 | 漏检 | 误报 | 弃判 |
|---|---:|---:|---:|---:|---:|
| 全部自然生成样本 | 6 | 1 | 0 | 1 | 0 |
| 最终答案正确 | 5 | 0 | 0 | 1 | 0 |

唯一检出是实现步骤的语法错误；唯一误报来自历史沙盒导入契约。自然样本没有出现“答案正确、过程错误”，所以该能力的自然样本证据仍为空。

机器汇总：`natural_process_20260908.json`。人工复核原始记录继续保存在 `hy3_seed_stratified_20260907_audit_completed.jsonl`。

## 复现

```bash
set HY3_MODEL=hy4-preview
set HY3_REVIEW_REASONING_EFFORT=low
tracejudge benchmark --source fixtures --hy3-review --review-mode single --hypothesis-examples 20 --fixture-profile correct_code_wrong_proof --output reports/hy4_wrong_proof.json
tracejudge benchmark --source fixtures --hy3-review --review-mode single --hypothesis-examples 20 --fixture-profile condition_overgeneralization --output reports/hy4_condition.json
tracejudge benchmark --source fixtures --hy3-review --review-mode single --hypothesis-examples 20 --fixture-profile gold --fixture-delay 30 --output reports/hy4_gold.json
tracejudge audit-summary --benchmark reports/hy3_seed_stratified_20260907.json --annotations reports/hy3_seed_stratified_20260907_audit_completed.jsonl --output reports/natural_process_20260908.json
```

高负载时使用 `--fixture-sample` 精确重试弃判样本，再由 `scripts/merge_fixture_reports.py` 去重合并。多次明确结论冲突时脚本会拒绝合并，要求人工裁决。
