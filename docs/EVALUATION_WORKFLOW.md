# 评估与人工复核操作流程

2026-09-07 更新。使用项目虚拟环境中的 `tracejudge`，或以 `python -m hy3_tracejudge` 替代。

## 判定修复

- 题目量表中的关键词、禁用词都只作语义审查提示。`criteria.passed=null`，`signal` 表示词汇匹配情况，不代表过程对错。否定、引用错误方法不会被词汇规则直接判错。
- 未调用语义评审时，测试通过不能证明推理成立，过程保持 `null`；真实反例仍能确认实现有问题。
- 专业评审必须覆盖全部目标步骤。未分配给专家的额外步骤会阻止正面结论。
- Swarm 的正面仲裁须明确覆盖全部步骤、没有缺失检查，而且没有确定性反例，才可撤回专家负面意见。执行失败不能被仲裁抹去。
- 差分验证检查参考程序的逐用例异常和结果完整性；参考程序异常不会变成期望值 `null`。合法返回 `None` 仍可比较。
- 服务连接检查要求模型列表包含所配置的模型，其他模型可用不会再使 Hy3 状态显示成功。
- 生成提示与沙盒共用同一份安全导入契约。候选代码可导入白名单标准库及白名单成员，其他模块、星号导入、相对导入和直接调用 `__import__` 仍会被拒绝。
- 在线结果的 `failure_owner_status=provisional` 只表示自动暂定归属；人工复核后的 `adjudicated_records` 才标记为 `adjudicated`，并能把模型失败、评估器误报和基础设施失败分开。

上述调整不增加依赖、Agent 数量或数据库迁移。运行中的 Web 与独立 Worker 需重启以加载 Python 代码；历史任务不会自动改判。

## 1. 离线检查与构造集

```bash
python -m pytest -q
tracejudge benchmark --source fixtures --hypothesis-examples 20 --output reports/fixtures_benchmark.json
```

构造集去除相同题目下重复答案，并加入“关键词堆砌和循环论证”及“正确过程明确否定错误示例”两类对抗样本。构造金标是程序可追溯的受控变换，不是独立人工抽检。离线模式只验证执行和弃判行为，不再凭关键词报告语义定位能力。

要在这些已知错误位置上检验真实 Hy3 评审，显式启用模型调用：

```bash
tracejudge benchmark --source fixtures --hy3-review --review-mode supervisor --output reports/fixtures_hy3_review.json
```

这评估的是评审器，不是 Hy3 解题能力。默认不启用模型调用。

## 2. 真实模型分层评测

```bash
tracejudge doctor
tracejudge benchmark --source hy3 --tier seed --per-difficulty 2 --review-mode supervisor --output reports/hy3_seed.json
```

每层选 2 题，共 6 题；按题库固定顺序选择并记录实际题目 ID。题数不足会在生成前报错。`--limit` 与 `--per-difficulty` 互斥；`--tier all` 包含外部题。外部题仍是未人工校准的 AST 难度，不能直接据其分层断言模型临界点。

输出包含题面快照、实际答案、评估、调用元数据、JSON/JSONL/CSV，以及按难度统计的准确率、覆盖率和 Wilson 95% 区间。CSV 保留 `run_error`、`failure_owner` 和归属状态；生成内容不合协议记为模型失败，评审输出不合协议记为评估器失败，网络、上游服务或验证环境不可用记为基础设施失败。区间依赖抽样与样本独立性，应与覆盖率一起解释；本程序不会自动宣布模型临界点。

真实评测会在每道题的答案生成和验证阶段后原子更新主 JSON。进程中断后，使用完全相同的题目范围与参数续跑：

```bash
tracejudge benchmark --source hy3 --tier seed --per-difficulty 2 --review-mode supervisor --output reports/hy3_seed.json --resume
```

已完成题会跳过；已生成但尚未验证的答案会直接复用。需要重新尝试失败项时增加 `--retry-failed`，并可用 `--max-attempts` 和 `--retry-delay` 限制尝试次数与间隔。续跑会校验题目快照、评测配置及实现指纹，任何不一致都会停止，以免把不同实验合并成一份报告。

全部 6 道种子题和 65 道外部题均有显式 Hypothesis 策略。外部策略保存在独立注册表中，并记录策略版本与输入域；固定测试仍是稳定回归基础，属性测试用于继续搜索输入空间中的差分反例。

## 3. 导出人工复核任务

```bash
tracejudge audit-export --benchmark reports/hy3_seed.json --output reports/hy3_seed_audit.jsonl
```

导出所有已完成评估的样本，含题面和答案，不展示评估器预测及构造金标，以降低标注偏差。真实调用失败的样本不进入复核队列。

每行初始为 `status: "pending"`。复核者填写：

- `annotator`：实际复核者标识。
- `independent_human_audit: true`：实际完成独立人工复核后的声明；程序不能代替身份核验。
- `final_answer_correct`、`human_process_valid`：JSON 布尔值。
- `first_error_step`：过程正确时为 `null`；过程错误时为实际首错编号，代码实现步骤为文字步骤数加 1。尚不能定位时保持 `pending`。
- `evidence`：具体依据。
- `status: "completed"`：完成后再设置。

保留 `sample_id` 与 `record_sha256`。指纹绑定题目、答案和评估，防止把旧运行的人工记录套到新答案上。若存在多人分歧，先人工裁决，每个样本保留一条最终记录，并在依据中保留裁决说明。导出命令拒绝覆盖已存在的文件，避免丢失人工工作。

## 4. 汇总人工结论

```bash
tracejudge audit-summary --benchmark reports/hy3_seed.json --annotations reports/hy3_seed_audit.jsonl --output reports/hy3_seed_validity.json
```

汇总报告给出首错定位准确率、过程问题检出率、正确过程上的误报率、被标记正确答案中的真实问题/误报比例，并同时报告总体复核覆盖率和重点样本覆盖率。`automated_*` 字段只描述自动预测，`human_confirmed_*` 字段只描述人工金标；`failure_owner_distribution` 与 `adjudicated_records` 给出人工裁决后的失败归属，避免混用两个口径。

未完成、未提交的记录不进入指标分母；没有有效样本时显示 `null`。重复 ID、错误指纹、非布尔判定、无效步骤和缺少人工声明/依据的完成记录会被拒绝。汇总只描述已复核样本，不能自动代表全体模型输出。

## 当前仍需实验与人工完成的事项

真实 Hy3 生成与复核实验、难度人工校准、独立人工标注，以及真实流程演示录像。新增功能提供执行入口，不会代填这些实验结论。
