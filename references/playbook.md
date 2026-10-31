# 拆分与调度手册

## 怎么读编排表

`hl plan` 输出的是脚本生成的执行方案，不会派工，也不会修改 `plan.json`。
表格的“批次”按 DAG 波次排列；同一批任务没有未完成的依赖。
每批宽度受 route 配置限制，且不会超过 3 个 worker。
“模型”列按 alias、runner、model 展示本次选择。
“选择理由”会区分显式指定 alias 与自动选择。
自动选择理由列出 tier、tag、rank 和额度检查结果。
额度为 critical 或 exhausted 的候选会列在“额度跳过”中。
如果没有可用模型，任务仍留在表中，并列明原因供主控处理。
“写入范围”和“依赖”用于发现冲突与不可并行的工作。
估时同时显示串行总分钟和并行估算；口径是 `max(关键路径, 总工作量 / N) + N × 交接成本 + 整合成本`。
这里的 N 是实际并行宽度，交接默认按策略值，整合默认 5 分钟。
确认提示在至少 3 个可运行 worker 或使用 strong 档时为“是”。
`hl run` 因确认门槛退出码为 3 时，也会先打印同一张表再提示 `--yes`。
`hl plan --md` 只额外写 `.herdr-run/plan.md`；它不写回计划状态。
对用户的方案直接附这张输出，不手工另造一张表；无需确认的“交给 A”也附表。
`hl digest` 在开头放最终编排表，模型列按实际提交记录中的 alias 显示。

## 先判断是否拆分

先写清最终交付、完成条件和禁止修改的范围。
把有依赖的工作排成 DAG，不把前置结果不确定的任务同时启动。
把写入目标相交的任务串行化。
每个子任务指定唯一 owner 和独立写入范围。
主控始终负责整合、检查和交付。
单文件小改、短任务、沟通成本高时主控直接完成。
任务估时低于两倍交接开销时优先不拆。
默认每次交接估计 4 分钟，按实际情况调整。
并行只适用于独立写集或只读调查。
并行前确定共享接口、输入和输出。
不要为了增加 agent 数量而拆任务。

## 并发数量

估算串行时间 S = 各任务执行时间之和。
估算并行时间 P = 最长依赖链时间 + 各轮交接与整合成本。
预期收益为 S - P；只有收益为正且足以覆盖风险时并行。
更细的估算：P 约为 max(关键路径, 总工作量 / N) + N × 交接成本 + 整合成本。
N 为同时运行的 worker 数量。
例如三个各 20 分钟的独立任务，交接每人 4 分钟、整合 5 分钟。
串行约 60 分钟，三人并行约 37 分钟，可以考虑拆分。
估算不是实际耗时保证，任务大小不均会增加等待。
默认并发上限为 3，策略可设置更低的值。
资源或工作区限制更严格时遵守更严格限制。
一条关键路径上的任务必须按依赖顺序运行。
避免提前为尚未满足依赖的任务占用窗口。

## 首次配置

从 GitHub 安装 herdr-lead 后，首次使用须生成模型登记文件 `~/.config/herdr-dispatch/models.conf`。
配置入口为 `hl models setup`：
- 在交互终端直接运行 `hl models setup`，启动交互向导（scan-clis 发现可用 runner → 选 runner → scan-models 扫描模型并推荐 tier → 用户逐项确认/改 tier → apply 写入配置并提示 `hl models test <alias>`）。
- 在非交互终端未带参数调用时，打印子命令用法并退出（退出码 2）。
- 脚本化或自动化配置可分步调用以下三条子命令：
  1. `hl models setup scan-clis`：扫描 PATH（`claude`、`codex`、`agy`、`pi`）及 `runners.local.sh` 中定义的 `run_*` 函数，输出可用 runner 的 JSON 列表。
  2. `hl models setup scan-models <runner>`：复用 `scan_runner` 列出指定 runner 的可用候选模型，按模型名关键字推断建议 tier（mini/flash/haiku/lite/low → cheap；pro/opus/sonnet/terra/high → strong；其他 → mid），输出 JSON 建议列表，不写文件。
  3. `hl models setup apply --entry "alias|runner|model|thinking|tier" [--entry ...] [--force]`：按给定顺序写入 `models.conf`（rank 从 1 开始，enabled=yes，when_to_use 按 tier 自动填充），文件权限 600。若文件已存在且未加 `--force` 则报错拒绝；加 `--force` 会先自动备份为 `models.conf.bak.<时间戳>` 再替换。写入完成后自动列出当前登记模型。

### models.conf 格式说明与示例

文件位置：`~/.config/herdr-dispatch/models.conf`（权限 600）。
每行以竖线 `|` 分隔 8 个字段：
`alias|runner|model|default_thinking|enabled|when_to_use|tier|rank`

字段说明：
1. `alias`：模型别名，全局唯一，派工和任务指定时使用。
2. `runner`：执行器名称（如 `codex`、`claude`、`agy`、`pi` 或本地自定义 runner）。
3. `model`：执行器对应的内部模型标识（如 `gpt-4o-mini`、`claude-3-5-sonnet-20241022`）。
4. `default_thinking`：默认思考/推理预算（`off`、`low`、`medium`、`high`）。
5. `enabled`：是否启用（`yes` 或 `no`）。
6. `when_to_use`：用途说明（cheap 默认填「简单任务」，mid 默认填「普通任务」，strong 默认填「复杂推理」；内容用于标签推断，如包含代码/code推断 code 标签）。
7. `tier`：模型分档（`cheap`、`mid`、`strong`）。
8. `rank`：选用优先级，从 1 开始的正整数（同 tier 内数字越小越优先选用）。

示例：
```conf
# alias|runner|model|default_thinking|enabled|when_to_use|tier|rank
codex-mini|codex|gpt-4o-mini|low|yes|简单任务|cheap|1
claude-sonnet|claude|claude-3-5-sonnet-20241022|high|yes|复杂推理|strong|2
pi-chat|pi|anthropic/claude-3.5-haiku|off|yes|普通任务|mid|3
```

## 模型自动选择

模型唯一登记来源是 `~/.config/herdr-dispatch/models.conf`。
每行记录 alias、runner、model、thinking、enabled、用途、tier 和 rank。
可选 tags 列用逗号分隔。
缺少 tags 时根据用途描述推断已识别标签。
`hl models list` 查看登记。
`hl models pick --tier cheap --tag code` 查找 cheap 代码模型。
筛选顺序为 enabled、指定 tier、指定 tag，再排除额度 critical 和 exhausted。
满足条件者按 rank 和 alias 顺序选择。
标签不匹配或没有可用模型时应调整任务或由主控处理。
不能把 unknown 额度直接当成充足。
运行时看到额度报错应停止向该模型派新任务。
需要不同 runner 复核时用 `hl review ID`。
自动选择不是对模型能力的保证；由主控检查输出。
明确指定的 alias 仍须启用且未处于 critical 或 exhausted。
模型登记由 `hl models setup` 生成与维护；派工运行期间保持只读，不手动修改本地 runner 文件。

## 上下文传递

先在 `.herdr-run/shared/contract.md` 写共享接口和边界。
每个 brief 写目标、只读材料、唯一写集、约束和验收。
brief 写入 `.herdr-run/brief/ID.md`。
worker 只向主控交付 report，不直连其他 worker。
report 写入 `.herdr-run/out/ID/report.md`。
report 必须给出状态、摘要、产物、证据、遗留问题和本次哨兵。
回报文件和哨兵是主通道；`herdr agent prompt` 只用于提醒。
leader 恢复时先读 `~/.agents/teamwork/inbox/`，再核对回报文件。
摘要不超过十行，产物给实际路径。
主控核对哨兵、回报新鲜度和产物是否存在。
`hl digest` 汇总每项摘要及回报路径。
主控优先读 digest，疑点再读对应 report 和产物。
需要新一轮任务时由主控把必要结论放进新的 brief。
不要转发无关日志、完整上下文或其他 worker 的整段对话。
共享目录并发改动无法独立归因，需主控结合证据复核。

## 长任务与恢复

计划和状态以项目内 `.herdr-run/plan.json` 为准。
每个任务记录依赖、状态、owner、重试数和回报路径。
中断后先读 plan 和 status，确认哪些任务已提交。
已提交或状态不确定的任务不要再次发送。
仍在运行的 worker 需先确认状态，再等待或收集回报。
超时不代表 worker 已停止。
接管和重试前确认旧 owner 已停止写入。
使用 `hl takeover ID --stopped` 或 `hl retry ID --stopped`。
压缩或重启后只需恢复项目路径、plan、contract 和 digest。
再按状态决定继续等待、收集、重试或处理下游依赖。
不要重新初始化并覆盖已有状态。
不同项目各有自己的 `.herdr-run/`。
不要把模型凭据或隐私信息写进共享状态。

## 提速与省 token

先确认依赖和写集，再启动互不冲突的任务。
确定性检查交给脚本；主控聚焦异常和验收。
简报只传完成任务所需的路径、规则和验收条件。
报告只传结论、证据位置和必要风险。
通过 wait 等待结果，不循环读取完整屏幕。
超时或不确定时检查一次，再按安全流程处理。
按收益选择并发数，不因等待而无条件增加 worker。
重复失败先诊断，再考虑升档或接管。
不要同时启动互相等待的下游任务。
不要把大量命令输出塞进共享黑板。
完成后整理 digest，再回报任务来源。

## 本机 runner 实测结论

本机 Claude 命令是 zsh 函数包装；启动前会先打印额度面板。
Claude 在 agent 模式下，Herdr 会丢失前台进程并返回 `agent_not_ready`。
因此本机 Claude 只使用 oneshot，不用 agent 模式。
Codex 的 agent 模式已在 `/private/tmp` 下实际启动并通过测试。
Agy 账号别名不支持 agent 模式；遇到账号专用别名时不要尝试启动 agent。
这些结论针对本机当前 runner 配置；runner 包装或 Herdr 行为变化后需重新实测。
