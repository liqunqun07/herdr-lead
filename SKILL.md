---
name: herdr-lead
description: 在 herdr 环境里，用户要把长任务或复杂任务交给你分工、派给其他模型、多窗口并行或接管时使用。普通的计划、方案、并行、额度不触发；简单任务、单文件改动、不在 herdr 里时不用。
---

# Herdr Lead

## 何时使用

用户明确要求在 Herdr 中分工、并行派工或接管长任务时使用。
先确认 Herdr 可连接、任务范围清楚、并行收益大于沟通成本。
普通方案讨论不触发。
简单任务、单文件修改或不在 Herdr 内时不使用。
主控负责拆分、派工、验收、故障处理和向任务来源回报。

## A：五步工作流

1. 明确目标、约束、验收、可写范围和是否值得并行。
2. 用 `hl init` 建项目状态，用 `hl load` 导入任务，用 `hl check` 检查依赖与冲突。
3. 运行 `hl plan` 生成编排表；给用户的方案必须附这张脚本生成的表，不手写重排。再检查 `hl models list`、`hl models pick` 和额度，为任务生成 brief。
4. 查看 `hl run --dry-run` 的命令；按需要取得确认和 runner 授权，再运行。
5. 查看状态和回报，复核结果；阻塞时确认停写后接管，汇总 digest 并回报来源。

真实派工由验收席验证；启用前先检查任务计划与 runner 授权。
项目内状态文件为 `.herdr-run/plan.json`，恢复时先读它。
任务之间有依赖时，前置任务验收为 done 后才运行下一轮。
计划存在错误时先修正，不强行派工。
回报无证据、哨兵不符或产物过期都不能视作完成。

## 命令速查

入口：`python3 ~/.agents/skills/herdr-lead/scripts/hl.py`，下文简称 hl。
所有子命令均支持 `--help`；项目位置通过全局参数 `--root PATH` 指定。
`hl init --goal GOAL` 创建状态。
`hl load FILE` 导入任务 JSON。`acceptance` 必须是列表，不能写成单个字符串。

完整任务 JSON 示例：
```json
[
  {
    "id": "T1",
    "title": "整理输入数据",
    "goal": "清理源数据并生成可复核的结果文件",
    "mode": "oneshot",
    "tier": "cheap",
    "reads": ["data/source.csv"],
    "writes": ["results/clean.csv"],
    "deps": [],
    "minutes": 15,
    "acceptance": ["输出文件存在", "列名与 contract 一致"]
  }
]
```
`hl check` 检查 DAG、冲突、估时和并发上限。
`hl plan` 只读生成批次、选模理由、额度跳过和耗时估算；`hl plan --md` 另写 `.herdr-run/plan.md`，不修改 `plan.json`。
给用户的方案（包括只说“交给 A”且无需确认的情形）先附 `hl plan` 表，再放 digest；表格的批次与选模理由必须来自脚本。`hl digest` 开头包含最终编排表，并使用实际运行的 alias。
`hl brief ID` 为单个任务生成简报。
首次使用须先运行 `hl models setup` 配置模型登记（交互终端自动走向导，非交互终端输出用法）。
`hl models setup scan-clis` 列出 PATH 与本地 runner 定义中可用的 runner（JSON）。
`hl models setup scan-models RUNNER` 列出 runner 候选模型并按模型名推断建议 tier（cheap/mid/strong）。
`hl models setup apply --entry "alias|runner|model|thinking|tier" [--entry ...] [--force]` 按指定顺序写入 models.conf（rank 从 1 开始，权限 600；已存在需 `--force` 且自动备份）。
`hl models list` 显示登记模型。
`hl models scan` 通过 runner 的列举函数扫描。
`hl models test ALIAS` 测试已启用的 cheap 模型。
`hl models pick --tier cheap --tag code` 选择模型，tag 可省略。
`hl consent RUNNER` 登记已确认的 runner 授权。
`hl run --dry-run` 查看计划及命令。
`hl run --yes` 在满足条件后执行。
`hl status` 查看任务状态；`--json` 输出 JSON。
`hl collect ID` 收集指定任务回报。
`hl review ID` 增加不同 runner 的独立复核任务。
`hl retry ID --stopped` 在确认旧 worker 停止后重试。
`hl takeover ID --stopped` 在确认停写后选择接管者。
`hl digest` 生成主控摘要。
`hl doctor` 检查本机依赖。
一次性 runner 调用需要先核对并确认实际命令。
强模型或三名及以上 worker 需要 `--yes`；一次性模式另需 runner 授权。
runner 预览使用 macOS 沙箱；无法安全展开时停止，不派工。
授权文件和策略文件仅由明确授权的用户操作维护。
`models.conf` 由 `hl models setup` 生成与维护，派工期间只读。

## 调度与安全规则

每项任务只有一个 owner。
并行任务写入范围不得相交。
contract 先于 worker 写入；worker 之间不直接发送任务。
每个 brief 写明目标、输入、写集、验收和回报格式。
brief 指向 `.herdr-run/brief/ID.md`。
worker 把回报写入 `.herdr-run/out/ID/report.md`。
回报要含本次运行的哨兵、证据、产物和不超过十行摘要。
回报文件和哨兵是主通道；`herdr agent prompt` 只用于提醒。
leader 恢复时先读 `~/.agents/teamwork/inbox/`，再核对回报文件。
主控通过 digest 汇总，不把大段原文转发到其他会话。
只用 `herdr agent prompt` 提交 agent 消息。
只用 `herdr pane run` 提交 shell 命令。
不得使用 `pane send-text`、`agent send` 或 `agent start --split`。
先分屏，再用 `agent start --pane`，最后提交 prompt。
pane 和 tab 均命名为「任务·模型」。
提交前核对身份、模型、状态及空输入框。
额度 critical 或 exhausted 时不派新活。
屏幕实际额度报错优先于缓存。
信任或权限确认对话框会阻塞任务并保留 pane；人工确认后运行 `hl reset ID --stopped`，禁止自动点击。
`agent_not_ready` 也会阻塞且不重发；worker 回报文件和哨兵为主通道，prompt 仅提醒。
一次提交结果不确定时不得重复发送。
等待使用 agent wait；超时只读一次以辅助判断。
接管前确认原 worker 已停写，不能仅凭超时推定。
完成且回报经检查后才关闭 worker pane。
受阻和完成均主动回报派工方。

## 状态与参考

`.herdr-run/shared/contract.md` 保存共享约定。
`.herdr-run/digest.md` 保存摘要。
重试超过两次时按 cheap、mid、strong 升档。
不同 runner 执行独立复核。
共享目录并发修改无法完全归因，主控需复核。
拆分、模型和上下文传递见 [playbook](references/playbook.md)。
额度、失败和接管见 [takeover](references/takeover.md)。
