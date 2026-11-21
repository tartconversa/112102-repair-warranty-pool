# 低空服务部件的停用风险与保修备件调配

零部件在役、检查、保修条款及备用件交接的合成资料。 所有标识、金额、地名和观察值均为合成数据。

## 来源资料

- `sources/service_intervals.csv`
- `sources/checks.csv`
- `sources/lot_corrections.csv`
- `sources/warranty_terms.csv`
- `sources/interchange.csv`
- `sources/replacement_events.csv`

service_intervals 每行是一段观察窗，usage_hours 是窗内累计使用小时，observed_to 是该站观察结束。ending 为结束原因；planned 表示计划拆换，transfer 表示换站使连续观察中断，in_service 表示仍在役。checks 的 inspection_day 早于 known_day 时，检查结论是补交的。

lot_corrections 为供应方后补的实物归属凭证。warranty_terms 的 return_days 从发出替换件起算，advance_limit 是条款下尚未闭环的替换额度，unit_cost 为合成货币单位。

interchange 中同一 spare 可在多个站使用，但表示同一个实物。replacement_events 记录申请、预留、调出、待复验及原件退回的回执。

## 本地检查

使用 Python 3.14，仅需标准库。在项目目录执行以下命令检查输入表结构、事件引用和基础记录覆盖。

```sh
python -m unittest discover -s tests -v
```
