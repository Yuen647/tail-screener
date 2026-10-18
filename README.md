# Tail Screener

沪深 A 股短线研究与模拟账户工具，包含实验性 HGB33 候选排序。项目采集近期行情和公告，检查数据完整性与报价约束，生成观察名单、研究报告和模拟账户记录。

> **使用边界：** 本项目用于研究和模拟，不连接券商、不自动下单。策略盈利有效性尚未得到验证；筛选结果和流动性排名不代表上涨概率或买入建议。使用时请核对每次报告的数据日期、完整性和警告。

## 功能

- 汇总沪深 A 股日线、报价、公告等研究证据，并标出缺失或冲突的数据。
- 按配置筛选候选，计算趋势、K 线结构、量能和追价条件。
- 分别管理研究预选与模拟持仓，生成 HTML 和 JSON 报告。
- 支持盘后筛选、盘中报价复核、环境诊断和历史账户接续。
- 为 Codex、Claude Code 等支持 Agent Skills 的宿主提供 `tail-screener` 技能入口。

## 快速开始

需要 Python 3.10+、网络连接和可写磁盘空间。进入项目目录：

```powershell
cd tail-screener
.\screen.cmd --mode post-close --top 6
```

Windows 启动器会优先使用项目已有的 Python 环境；首次运行时可准备所需依赖。其他系统可直接运行：

```bash
cd tail-screener
python3 scripts/screen.py --mode post-close --top 6
```

常用命令：

```powershell
# 按自定义资金、持仓数和持有期限运行模拟研究
.\screen.cmd --capital 50000 --max-positions 3 --max-holding-sessions 3 --mode post-close --top 6

# 盘中复核最新报价和追价约束
.\screen.cmd --mode live --top 6

# 查看可用参数
.\screen.cmd --help

# 离线检查运行环境，不联网、不安装依赖
.\screen.cmd --doctor-offline
```

候选数量、研究跟踪数量和同时持仓上限是独立设置。程序生成报告和本地模拟账户资料；首次运行可能需要数分钟。免费行情源可能暂时不可用，遇到不完整结果时按报告中的日期和警告判断，不要把空名单当作市场没有信号。

## 安装为 Agent Skill

从 `tail-screener` 项目目录运行安装器：

```bash
python install_skill.py --agent codex
```

Claude Code：

```bash
python install_skill.py --agent claude-code
```

安装后在支持的宿主中调用 `$tail-screener` 或 `/tail-screener`，并说明筛选日期、预算、席位和持有期限等要求。安装器只需 Python 标准库；筛选运行所需依赖见 [`requirements.txt`](tail-screener/requirements.txt)。

## 配置和文档

- [技能规则与研究边界](tail-screener/SKILL.md)
- [完整使用指南](tail-screener/docs/usage.md)
- [环境准备与故障处理](tail-screener/docs/environment-setup.md)
- [策略验证规范](tail-screener/docs/strategy-validation.md)
- [K 线分析说明](tail-screener/docs/kline-analysis.md)
- [账户配置示例](tail-screener/configs/account-config.json)
- [2026-10-18 改进审阅报告](tail-screener-improvement-review-20261018.html)

命令行参数优先用于本次运行。账户参数变化可能建立独立的模拟账户系列；不要把个人账户数据或运行产生的缓存、报告、模拟记录提交到公开仓库。
