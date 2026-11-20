# 合成输入数据

用于跟踪转型期间岗位变化、课程名额、培训参与和实际就业结果的匿名合成记录。

所有记录均为虚构，仅供本地开发使用。日期与数值均为示例。

| 文件 | 用途 | 关键字段 |
| --- | --- | --- |
| `workforce.csv` | 劳动者经历与所在群体 | `person_token`、`employer_id`、`role_code`、`employment_status`、`group_code` |
| `courses_commitments.csv` | 课程名额、费用与企业招聘计划 | `seats`、`fee_per_seat`、`planned_openings`、`commitment_status` |
| `outcomes.csv` | 课程参与与后续就业事件 | `event_type`、`event_date`、`employer_id`、`retained_90d` |
| `production_lines.csv` | 生产线调整 | `facility_id`、`product`、`valid_from`、`valid_to` |
| `meters_output.csv` | 能耗与产量（就业域外，暂未使用） | — |
| `retrofits.csv` | 改造措施（就业域外，暂未使用） | — |

## 就业事件类型

`completed`（结业）、`offer`（录用通知）、`started`（实际到岗）、
`retained_90d`（持续任职）、`withdrawn` / `declined` / `not_started`（未继续）。

## 状态日志

运行时写入 `data/state/*.csv`（可用 `--state` 改名）。这些是追加式记录：
`allocations.csv`、`clashes.csv`、`corrections.csv`、`fulfillments.csv`、
`supports.csv`、`job_aliases.csv`、`outcome_events.csv`。原始输入 CSV 只读。
