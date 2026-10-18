# 日K线结构

默认扫描自动计算 `kline_review`，使用截止信号交易日的20根完整复权OHLC与成交额。候选名单、研究池和研究预选保留相同字段；展示数量、仓位数、资金和持有期各自独立。

## 计算与解释

- 实体为收盘减开盘，上影为最高减开收较大值，下影为开收较小值减最低；比例分母为当天振幅。平盘零振幅的比例记0，不命中小实体形态。
- 内包/外包比较相邻两天高低区间，使用严格不等式；实体覆盖还要求相邻K线方向相反。
- 前5日、前19日参考高低均不包含信号当天。盘中越过前5日高点、收盘回到其下，称“上破后收回”；低点恢复相反。
- 成交额倍数=信号日成交额/此前5日平均成交额；区间压缩比=最近5日平均振幅/再之前15日平均振幅。量能只是证据，不将任何倍数称为已验证买点。
- 上/下影占振幅至少一半以及实体占振幅不超过一成是固定描述阈值；不借此修改筛选门槛。三个最近交易日的几何、包络和区间信息组成40项研究特征。
- 图中历史OHLC以信号日复权因子统一换算到该日股价尺度，便于比较；不是各历史日期的原始成交价格。程序不生成图形预测线或未来目标。

逐股复核按“位置→形态→量能→均线与公告→对应期限下仍缺什么”组织。出现阴实体覆盖、上破后收回、长上影或收盘跌破前5日低点时，直接列作已发生的反对证据。反对证据不等于必跌，形态改善也不等于已证明上涨。

## 离线分析用户提供的完整日线

```bash
python scripts/analyze_kline.py --input bars.json --output 新的空目录
```

`bars.json` 为 `{"price_basis":"adjusted","amount_unit":"thousand_yuan","fields":{"open":[],"high":[],"low":[],"close":[],"amount":[],"factor":[]},"sessions":[]}`。各数组按时间递增且长度相同，至少20根。必须显式指定价格口径和成交额单位：price_basis为adjusted（已统一复权）或raw（原始OHLC，程序逐日乘factor后比较）；amount_unit为yuan或thousand_yuan。factor为逐日原始价到统一复权价的乘数，无复权时明确提供全1因子。sessions可省略，提供时须与数组对齐、严格递增且不重复的ISO日期。不要将原始OHLC标成adjusted。

入口只读输入，生成 `kline.json` 和带悬停数值的 `index.html`，不联网、不操作账户；输出目录必须为空。状态未知退出码1，有完整计算退出码0。正常筛选使用自动入口，无需用户手工准备JSON。

## 本版验证记录

见 `references/kline-validation.json`。该记录分别注明计算测试与收益研究结论，不能混用。几何因素的通行表达可参看Microsoft Qlib官方源码 https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py ；采用公式不意味着策略盈利已获证明。

## 查看六只展示名单之外的候选

```bash
python scripts/query_kline.py --report 报告目录/report.json --group failed_breakout --limit 20
python scripts/query_kline.py --report 报告目录/report.json --code 000001 --limit 20
```

支持 breakout、breakdown、failed_breakout、low_recovery、bull_body_engulf、bear_body_engulf、inside、outside、upper_shadow、lower_shadow、range。结果来自本次扫描的全部严格合格候选，不受展示数量截断；同一股票可属于多个组，组内仍按流动性排序。目录只证明对应量价及形态条件，不表示已逐股完成公告正文复核。原扫描不完整时查询为partial，不把局部目录称全市场。旧报告无目录时返回unavailable，不悄悄重扫。
