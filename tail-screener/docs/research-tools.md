# 恢复任务与离线账户实验操作

日常入口为`python scripts/screen.py`，账户配置示例见[account-config.json](../configs/account-config.json)。轻量包不含旧历史报告和验证附件，涉及这些材料的操作须先由用户提供，不能把缺失材料当成已核验证据。以下工具用于补充研究工作，使用入口已经准备好的Python环境执行。它们不会定时运行或连接券商。

## 查看与登记恢复任务

近期采集完成后会将所需输入按证券和目标交易日加入`.cache/recovery`，保存原始字节及内容哈希。源修订新增证据版本，旧版本继续保留。采集失败、冲突和任务耗尽分别记录；保存索引失败会阻止本次新入场计划。

```bash
python scripts/manage_recovery.py --queue .cache/recovery --list
python scripts/manage_recovery.py --queue .cache/recovery --import-report reports/某次运行/report.json
python scripts/manage_recovery.py --queue .cache/recovery --enqueue recovery-tasks.json
```

任务文件是JSON列表，例如：

```json
[{"code":"SZ000001","start":"2026-01-01","end":"2026-10-16","kind":"historical_daily","priority":20}]
```

可选类型为`daily_window`、`historical_daily`和`corporate_actions`。相同证券、日期范围和类型只登记一项任务；数值越小优先级越高。存储限额默认20亿字节，达到限额会报错并保留旧证据。任务租约超时可重新认领，迟到的原写入者不能提交。默认最多尝试3次，无法解决则保留耗尽状态，不无限重试。

`RecoveryQueue.run_pending(fetch, budget_seconds=60, max_tasks=20)`提供有时间和数量上限的调度接口。来源适配器接收任务与总截止时刻，必须限制自身网络超时。历史工作器按类型、当前计划和复权口径认领任务，不接管其他计划或公司行动任务。索引成功不证明历史证券范围或公司行动完整。

## 恢复免费历史行情

```bash
python scripts/recover_history.py --scope examples/history-scope.json --start 2018-01-01 --end 2026-10-16 --adjustment-basis sina_hfq_factor --budget 180 --max-tasks 100 --output reports/history-run-1
```

范围文件的`securities`声明证券代码和上市日期，可附`listing_evidence`说明来源；缺少独立核验时仍标为未审核。示例日期不替代历史风险简称、退市、代码变更和公司行动证据。每个计划最多256只证券、25万个证券交易日，较大范围分批运行；日历由已安装的交易所日历核验，不按工作日猜测。

每只证券可附`required_sessions`有序交易日数组，指定缺口表中的实际需求；不提供时取整个起止范围。数组须非空、唯一、位于请求范围内且不早于声明的上市日期。稀疏日期按日历连续区间再分片，不跨长空档拼成限条数请求；只对声明日期计覆盖，不把两个窗口之间的空白视为已取得。

默认每40个交易日拆片，保留3个重叠交易日。下次用相同范围、日期、口径和队列，改用新的输出目录即可续跑；成功片段不重复下载。`--plan-only`只规划并导出当前覆盖；`--retry-incomplete`显式重新尝试未完成任务；`--refresh`也会重取已完成任务并保存新版本。网络预算与最多任务数均有上限。

支持两种独立口径：`tencent_hfq`要求明确的腾讯原始和后复权字段；`sina_hfq_factor`使用腾讯原始量价和新浪带日期的后复权因子，因子仅对生效日期之后使用。命令示例使用后者，能够处理腾讯缺少明确后复权字段的证券。程序不会把普通`day`字段当作后复权，不平移负前复权价格，不把两种口径拼在同一任务或快照。价格一致性仍不属于独立双源核验。

每个输出目录包含：

| 文件 | 内容 |
|---|---|
| `acquisition.json` | 实际运行阶段、处理数量、请求诊断、计划与实现身份 |
| `coverage.csv` | 按证券、交易日和字段区分已取得、缺失、冲突及有证据的停牌 |
| `coverage.json` | 覆盖计数、未解决项、重叠冲突、源证据和停牌检查 |
| `snapshot.json` | 可由`RecentDailyStore`读取的固定快照，保留口径和未审计状态 |

重叠日期有价格、成交量或成交额冲突时保持未解决；后复权边界冲突或无法核对时，不导出该证券为一条可信的连续调整序列。数据源返回非交易日日线也拒绝成功。恢复期间代码变化时保留证据并要求重新导出。

仅对已经请求且整根原始日线缺失的日期查询历史停牌记录，每次最多检查60个不同交易日。明确覆盖全天的停牌可以解释没有日线，不能产生价格、成交量、成交或持仓估值。已有原始成交记录的日期不会被停牌说明豁免。

`acquisition_complete`表示所声明的采集需求均已取得或有明确停牌解释；`price_data_complete`仍要求所有必要价格字段存在。`valuation_ready`保持假，历史资格与全部公司行动仍未审计。恢复命令退出码0表示采集需求已解决，1表示仍有未解决缺项，参数错误为2；`--plan-only`的0仅表示规划完成。不要把恢复命令的0当作完整回测或投资有效性。

来源接口及计算口径参考[腾讯适配源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py)和[新浪后复权因子适配源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_zh_a_sina.py)。本实现使用严格JSON解码，不执行接口返回的脚本。

## 重建旧事后研究名单

```bash
python scripts/reconstruct_legacy_account.py --snapshots 历史导出1/snapshot.json 历史导出2/snapshot.json --forecasts 旧区间-forecasts.json --action-cache 研究事件缓存 --output 新重建目录 --budget 300
```

输入必须是已保存的连续交易日名单和带哈希的行情快照。只合并相同复权口径，拒绝重复日期的数值冲突、上市日期冲突和快照损坏。源快照、名单及实现身份在计算前记录，结束时再次核对。保留历史恢复队列中的原始证据字节；快照本身不能替代上游原响应审查。

此入口采用旧名单的5席位、10万元、无固定持有期研究场景，成本倍率默认分别计算1倍和2倍；它不重新运行全市场排名，也不代入当前5日持有和追价参数。所用历史名单是事后研究输出，登记时间是本次真实运行时间，不能称为过去的事前登记。修复输入后成交路径可能变化，原账户与原名单不改写。

`--seed-actions`可指定已有完整事件包目录；保存的正文按当前解析器重新核对金额、日期及正文哈希，不直接沿用旧支持标记。事件缓存和本次`action-evidence`保留正文及来源。`--offline`禁止补抓事件，缺少整个事件包时直接失败；已有包的正文缺失或不支持则维持待核验，不自行推算。

每个区间与成本生成独立JSON，包含条件化净值、实际尝试的事件、阻断原因和`additional_history_demand`，另有逐字段缺口CSV。新持仓路径可能暴露原缺口表没有列入的日期，按新需求继续补抓并重建到新目录。`input_complete`与`metrics.valuation_complete`分别表示输入与条件化估值；任一不完整时退出码1。原历史缺口减少不等于当前策略通过验证，`current_strategy_validated`与`recommendation_eligible`保持假。

## 登记默认账户实验

先准备独立的实验定义、行情快照和覆盖证据，再登记，最后计算。不要使用旧尾盘验证脚本或旧周策略成绩为默认MA账户授予资格。

```bash
python scripts/validate_account.py --register experiment.json --registry experiments/registry
python scripts/validate_account.py --registration experiments/registry/登记哈希.json --snapshot snapshot.json --coverage coverage.json --output experiments/result
```

登记文件使用独占写入，包含实际登记时间、验证器哈希及定义哈希。计算前核对生产规则、验证器、行情和覆盖证据身份；任何变化都需要新登记。输出`validation.json`，重复执行使用新输出目录。

实验定义必须包含：

| 字段 | 要求 |
|---|---|
| `schema_version` | 整数1 |
| `strategy_id` | `akquant-ma5-20-account` |
| `rule_sha256` | 当前生产规则身份 |
| `snapshot_sha256` | 快照中`data`的规范化JSON哈希，与快照封装一致 |
| `coverage_sha256` | 完整覆盖证据的规范化JSON哈希 |
| `config` | 生产`account_config`规范化结构，可附`data_source` |
| `start`和`end` | 快照内实际交易日，按时间顺序计算 |
| `cost_multiplier` | 整数1或2，分别登记 |
| `data_class` | `synthetic_fixture`或`retrospective_development` |
| `criteria` | `min_sessions`、`min_closed_trades`和`max_drawdown_pct`，结果计算前固定 |

规范化哈希使用UTF-8、键排序、无多余空白、不转义中文且拒绝非有限数值；实现位于`recent_daily._digest`。

覆盖文件使用`schema_version: 1`，`sessions`按交易日保存以下字段：

- `screen_inputs_complete`表示本轮筛选输入完整；缺失或非真会阻止形成后续名单。
- `excluded`为当日资格排除原因，不能用今天的简称或行业替代历史资格。
- `historical_eligibility_verified`、`corporate_action_coverage_verified`及`official_price_limits_verified`记录对应证据是否核对。填写布尔值不能替代上游真实来源。
- `actions`为引擎支持且有来源依据的公司行动；`unresolved_actions`列出事件尚未解决的证券。事件覆盖未知时阻断相关入场，并保留已有持仓的未知状态。
- 可选`benchmark`位于文件顶层，包含`kind`及`prices`。价格收益与含分红收益必须明确区分，缺失时保持未知。

快照复用`RecentDailyStore`封装。先按已冻结的上一交易日名单尝试成交，再由本日收盘信号产生下一交易日名单；输出保留信号日期、执行日期和事后重建标记。当前入口不证明历史上实际留下过这些名单。

必要输入或审计覆盖缺失时，全期收益和最大回撤保持未知。合成样例可用于独立手算核对，不能作为股票历史业绩。样本数和回撤检查只是已登记研究条件的一部分；统计晋升、完整时间切分及多次试验校正仍待后续实现，输出始终保留`recommendation_eligible: false`。

## 理解观察记录与版本

观察记录保存在当前独立账户的`observations/observations.sqlite3`，每次报告另存`observations.json`。记录带哈希链，派生状态与记录不一致时停止写入；SQLite事务保证重复运行和并发写入的一致性。

首次完整日观察保持冻结，同日后续输入变动列为修订。因数据不足而尚未建立的观察，可在同日取得完整数据后建立。移出展示榜后继续核对原对象；未知或漏跑会中断连续确认，原信号未失效时不会因再次上榜而变成新样本。观察到期后持续有效的同一信号也不会反复创建新周期。

`identity_manifest`保存声明的策略定义、完整实现哈希、快照和账户系列。声明的策略相同不能证明实现行为等价；当前仍按完整规则与配置严格隔离账户，没有自动迁移或拼接旧成绩。
