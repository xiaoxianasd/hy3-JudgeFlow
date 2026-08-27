# Data stack

```text
problems.json                 可直接运行的 6 道种子题（金标准、测试、量表、受控错误）
manifests/sources.json        外部题集/过程数据、角色、抽样计划、许可证风险
raw/                          不入库的上游原始数据落点
processed/                    固定 split、统一 JSONL 与内容指纹
traces/                       Hy3 原始结构化解答（真实运行后写入）
execution/                    固定测试与 Hypothesis 执行证据
mutations/                    单点受控错误及注入记录
annotations/                  人工抽检与仲裁
meta_eval/                    ProcessBench / PRMBench 固定子集
```

统一问题记录的最小字段是：`id/source/source_record_id/title/statement/difficulty/function_name/tests/reference_solution/gold_steps/rubric/content_sha256`。`scripts/prepare_data.py` 可重建当前 seed split；大型上游数据应另写适配器并在 manifest 中固定 revision 和样本 ID。
