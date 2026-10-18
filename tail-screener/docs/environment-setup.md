# 运行环境与首次配置

技能指令遵循Agent Skills结构；筛选、计算与账户逻辑在Python程序中执行。更换agent或模型不改变程序参数、数据状态和账户规则。`agents/openai.yaml`仅提供Codex的可选界面信息，其他宿主无需加载它。

## 最低条件

Windows、macOS或Linux上的Python 3.10+，具备venv和pip；能安装`requirements.txt`要求的依赖。默认日线模式不要求Qlib、LightGBM、付费行情接口或模型API密钥。agent需能访问项目文件、运行命令并联网。没有相应工具的聊天环境只能解释文件，不能完成采集。

先检查可用解释器：Unix通常为`python3 --version`，Windows可用`py -3 --version`或`python --version`。实际运行时使用核验过的解释器路径，路径有空格时按宿主shell正确引用。Windows PowerShell执行带引号的路径需前置调用运算符`&`。

## 优先使用自动准备

在项目根目录用已核验的解释器执行：

```bash
python scripts/screen.py --mode post-close --top 6
```

入口会复用合格依赖，或创建本机专用环境，读取宿主已有包并将缺少或不兼容的依赖安装到专用环境，自检后继续扫描。宿主提供venv但缺少ensurepip时，若宿主pip达到22.3，入口自动改用该pip向不含pip的专用环境安装。运行目录不可写时，程序尝试使用系统用户缓存目录保存运行数据。保留输出中实际使用的存储路径，以便后续找到报告与账户。

排查已有环境时使用`python scripts/screen.py --doctor-offline`，它不联网也不安装。首次诊断缺少依赖时仍应调用正常入口，让自动准备执行一次；不能把诊断的非零退出码当成安装已失败。依赖名称以`requirements.txt`为准：HTTP库是`requests`，不是`request`；不要凭名称另写安装清单。

准备失败时读取终端打印的阶段、退出码、依赖版本及`preparation.jsonl`路径。记录只包含错误类别，不保存可能携带凭据的安装器原始输出。`no_matching_distribution`与网络/证书错误同时存在时，先检查源访问，不能直接判断版本不存在。未分类的失败需结合宿主提供的原始错误继续定位。网络禁止下载且宿主缺少依赖时，仍无法启动；不能通过忽略日历依赖或用工作日代替交易日解决。

## 手工建立隔离环境

仅在自动准备失败或需要自行管理环境时使用。不要复制另一台机器的`.venv`或`.runtime`。

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/screen.py --doctor-offline
.venv/bin/python scripts/screen.py --mode post-close --top 6
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
& ./.venv/Scripts/python.exe scripts/screen.py --doctor-offline
& ./.venv/Scripts/python.exe scripts/screen.py --mode post-close --top 6
```

无需激活环境。Windows辅助启动器`screen.cmd`支持通过`TAIL_SCREENER_PYTHON`指定解释器绝对路径；未指定时尝试已有项目环境和系统Python。所有系统均可以直接运行Python入口，不依赖Windows启动器。

## 失败处理

| 失败阶段 | 处理 |
|---|---|
| 没有合格Python | 安装适合系统的Python 3.10+，重新核验；不猜测agent私有运行时位置 |
| venv或pip缺失 | 使用操作系统或Python发行版支持的方式补齐组件；所需系统权限由宿主决定 |
| 依赖下载失败 | 检查联网、证书、代理及pip源；遵从现场配置，保留原错误，不禁用TLS验证 |
| 依赖无兼容发行包或原生库被系统拒绝 | 使用受支持的Python版本、架构和依赖发行包；保留具体阶段，不绕过系统策略 |
| 离线诊断通过但扫描失败 | 读取退出码与本次报告；环境健康不能证明行情源可用 |
| 输出目录或旧账户冲突 | 选择新的空输出目录；账户规则不匹配时保留独立账户，不覆盖旧结果 |

首次配置只建立本机运行条件。数据完整性、公告证据和盈利有效性仍分别由每次结果及相应验证判断。

## 安装后的发布差异

正常请求直接运行`python scripts/screen.py`。正常启动不比较发布清单；不要先以发布清单的逐字节一致性作为能否启动的前提。仅在需要检查发布来源时运行：

```bash
python scripts/restore_support.py --verify-package
```

此辅助命令只比较发布记录，不联网、不安装。清单缺失、JSON格式被改写、元数据变化或程序与发布摘要不同，均返回`status=warning`、`blocks_execution=false`及退出码0，列出差异以便追溯。提示不是环境、行情或账户检查通过的证明，也不是筛选失败；不要据此停止正常入口、循环重装或按当前安装文件重写清单。

能否运行由实际程序加载、必要规则、依赖和参数决定。行情新鲜度、源数据冲突、快照、公告及账户哈希链的核验仍独立执行；不得将损坏或未知的交易证据当作有效输入。代码身份按实际安装文件计算，旧账户不强行接续到不同实现。

发布者使用`--verify-release`及打包程序检查原始文件；它们属于开发发布流程，不用于拦截安装后的用户运行。

## 轻量技能与可选历史资料

日常技能直接携带必要规则和程序，不包含support-files.tar.xz、历史报告、测试集、行情缓存、账户或Python环境。启动不需要解压数百份历史附件。当前运行所需行情仍按实际请求采集，运行资料包run-bundle.zip仍是每次运行的产物。

另行提供完整历史资料时，`--restore-only`仍按原始归档、长度与文件哈希严格还原，不覆盖不同证据。轻量包未附带这些资料时返回`historical_materials=not_included`，不会伪称已还原或核验历史业绩。
