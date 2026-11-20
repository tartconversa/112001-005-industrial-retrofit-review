# 产业就业支持分析

把产业转型期间的岗位变化转成可执行的培训和安置支持。输入是脱敏的劳动者经历、
企业岗位变动与招聘计划、培训课程名额与费用、课程参与及后续就业记录。

需要 Python 3.11 或更新版本。仅使用 Python 标准库。

## 快速开始

```bash
python -m unittest discover -s tests -v          # 运行测试
python -m transitionsvc summary --data data       # 汇总视图（人读格式）
python -m transitionsvc funnel --data data --json  # 计划/录用/到岗/留任漏斗
python -m transitionsvc groups --data data        # 按群体
python -m transitionsvc skills --data data        # 按技能的名额与岗位缺口
python -m transitionsvc lines --data data         # 生产线调整与受影响人数
python -m transitionsvc equivalence --data data   # 岗位名称对应关系（含未决）
python -m transitionsvc regional 2026-Q2 --data data
```

默认把状态日志写到 `data/state/`，不会改动输入 CSV；可用 `--state DIR` 指定别处。

## 用 Python 调用

```python
from transitionsvc import Service

svc = Service("data")
svc.allocate_seats("C10", [("P-17", "G1"), ("P-42", "G2")])
svc.release_seat("C10", "P-17", reason="参与人转入其他课程")
svc.record_employer_fulfillment(
    course_id="C10", employer_id="F-A", job_code="process_tech",
    fulfilled_openings=8, report_date="2026-08-01",
)
print(svc.employment_funnel())
print(svc.explain_allocation("C10"))
print(svc.impact_preview("C10"))
```

## 关键规则

**计划 ≠ 录用 ≠ 到岗 ≠ 留任。** `planned_openings` 来自企业计划招聘；
`offers`、`started`、`retained_90d` 分别来自真实事件。没有真实到岗记录时，
`started` 不会因为企业“确认招聘”而增加；`record_outcome(..., as_actual_from_commitment=True)`
会被拒绝。已撤回/取消的课程不计入 `active_planned_openings`。

**同一劳动者只算一次安置。** 漏斗顶部按去重后的人头计数；逐课程数字按
（人，课程）去重。同一来源内的重复事件不会把安置人数翻倍。

**课程名额不超过机构能力。** 每次分配都对照 `seats` 校验，超额人员进入候补；
分配与释放都带原因，`explain_allocation` 可逐条追溯依据（提交顺序或群体优先级）。

**培训支出不可抹去。** 一旦占用名额即计入 `committed`；释放名额只减少
`outstanding`，不减少 `committed`。同一（课程，人）不会重复计费。

**更正保留原值。** `correct_outcome` 追加更正记录，原始 `event_type` 保存在
`original_event_type` 中。更正时需明确日期意图：给出 `new_event_date`、
声明 `date_unknown=True`，或保持不变；旧日期不会在后续更正中被重新当成已知。

**无法对应就保持未决。** 岗位名称通过 `register_job_alias` 登记；未确认的别名
不会合并进技能缺口汇总，`unmapped_jobs()` 会列出课程技能与企业岗位代码之间
尚未对应的链接。劳动者代号冲突用 `register_identity_clash` 登记为 `unresolved`；
在 `resolve_identity` 确认前，该人员的统计不会合并，也不能分配名额。确认后，
同一人在课程、劳务派遣和企业资料中的不同代号只计一次。

**支持状态分开记录。** `informed`（已告知）和 `approved`（已批准）都不等于
`executed`（已执行）；`impact_preview` 只把未执行的支持列为暴露项。

## 目录

- `transitionsvc/app.py` — 领域规则
- `transitionsvc/store.py` — CSV 读取与追加式日志
- `transitionsvc/cli.py` — 命令行入口
- `data/` — 合成输入与我说明
