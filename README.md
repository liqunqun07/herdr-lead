# herdr-lead

把一个长任务交给 A 模型，由 A 在 [herdr](https://herdr.dev) 里自己分工、派给 B、C 等其他模型，完成后由 A 交付给你。A 和 B、A 和 C 之间的通信由 A 自己负责。本项目由原 `herdr-dispatch` 合并重写而来：之前的一次性派工、团队分工、额度接管合成一个 skill，并配一个脚本入口 `scripts/hl.py`。

支持的执行端：Claude Code（`claude`）、Codex CLI（`codex`）、Agy（`agy`）、pi，以及你在 `runners.local.sh` 里自定义的 runner。不需要 API key，直接用各 CLI 已经登录的账号。

## 安装

```sh
git clone https://github.com/liqunqun07/herdr-lead.git ~/.agents/skills/herdr-lead
# 链接到你在用的 agent 的 skills 目录，例如：
ln -s ~/.agents/skills/herdr-lead ~/.claude/skills/herdr-lead
ln -s ~/.agents/skills/herdr-lead ~/.codex/skills/herdr-lead
```

依赖：python3（只用标准库）、herdr、git（可选，用于越界检测）。

## 首次配置：生成模型登记

```sh
alias hl='python3 ~/.agents/skills/herdr-lead/scripts/hl.py'
hl models setup                      # 交互向导：扫描 CLI → 扫描模型 → 确认档位与顺序 → 写入
# 非交互方式（agent 可以代你执行）：
hl models setup scan-clis
hl models setup scan-models codex
hl models setup apply --entry "codex-cheap|codex|gpt-6-luna|low|cheap" \
                      --entry "codex-strong|codex|gpt-6.1-terra|medium|strong"
hl models test codex-cheap           # 实测一次
```

登记文件是 `~/.config/herdr-dispatch/models.conf`，格式为 `alias|runner|model|default_thinking|enabled|when_to_use|tier|rank[|tags]`。tier 取 cheap / mid / strong；rank 越小越优先；tags 可选（如 `code,visual,zh`）。文件已存在时，需要加 `--force` 才会覆盖，覆盖前自动备份。设置环境变量 `HL_CONFIG_DIR` 可以换配置目录。

## 用法

```sh
hl init --goal "目标"
hl load tasks.json        # 任务列表，格式见 SKILL.md 里的示例
hl check                  # 检查依赖环、写入范围是否重叠、单一负责人；任务太小会提示不必拆
hl plan                   # 编排表：分几批、每个任务用哪个模型及原因、预计耗时、是否需要你确认
hl run                    # 派工、等待、核对证据、失败重试或升档
hl status / hl digest     # A 只读 digest：每个任务 ≤10 行 + 产物路径
```

编排表示例：

```
| 批次 | 任务 | 模式 | 模型(alias/runner/model) | 选择理由 | 写入范围 | 依赖 | 预计分钟 |
| 1 | T1 写alpha | oneshot | agy-cheap / agy / gemini-3.8-flash-low | tier=cheap rank=1；额度 ok | a.txt | - | 10 |
| 2 | T3 合并 | agent | codex-strong / codex / gpt-6.1-terra | 指定 alias | c.txt | T1,T2 | 10 |
- 预计耗时：串行 30 分钟，并行约 24 分钟 …
- 需要用户确认：是（原因：用到 strong）
```

## 设计要点

- **两种模式**：`oneshot` 用于机械的一次性任务（`claude -p`、`codex exec` 等），省而且结果确定；`agent` 用于需要交互的长任务，固定按 `pane split --no-focus` → `agent start --pane` → `agent prompt` 三步启动。
- **自动选模型**：按 tier、tags、rank 从登记里选，额度不足的跳过；同一任务重试 2 次仍失败，就按 cheap → mid → strong 升档；复核交给与实现者不同的 runner。
- **何时确认**：worker ≥3 个，或用到 strong 档，`hl run` 会先打印编排表，退出码 3，需要加 `--yes` 才执行；其余情况直接执行。oneshot runner 如果带绕过权限的参数，第一次使用前要先用 `hl consent <runner>` 记录同意；agent 模式从不添加这类参数。
- **上下文传递**：只传文件，不传对话。worker 拿到 `brief/ID.md`，把结果写到 `out/ID/report.md`，回报里带哨兵、证据和最多 10 行摘要；并行前先写 `shared/contract.md` 定好接口；worker 之间不直接通信。
- **长任务恢复**：所有状态都在项目内的 `.herdr-run/plan.json`，A 的上下文被压缩或进程重启后，读它就能接着做；中断时停在一半的提交会标成 stale，必须人工确认对方已停止写入后才能重试，不会自动重发。
- **防错**：检查写入范围是否重叠，检测越界写入（git diff 或文件快照），核对目标 pane 的身份和模型，遇到信任或批准对话框就停下、交给人确认（不会自动点信任），额度耗尽的 runner 不再派新活。
- **额度钩子（可选）**：`~/.config/herdr-lead/policy.json` 里的 `quota_cmd` 可以接任意脚本，只要输出 JSON；自带的 `scripts/quota_local.py` 是作者本机专用的。不配置时额度视为 unknown，不会阻断派工。

什么时候并行、开几个 agent、怎么提速、怎么省 token，见 [references/playbook.md](references/playbook.md)；接管与额度见 [references/takeover.md](references/takeover.md)。

## 测试

```sh
python3 -B -m unittest discover -s tests -q
```

测试用假的 herdr 和假的 CLI 离线运行，不会派出真实任务。

## 从 herdr-dispatch 迁移

原来的 `models.conf` 和 `runners.local.sh` 可以直接沿用（路径没变）；`hd-*` 命令已全部移除，对应关系如下：`hd-setup` → `hl models setup`，`hd-plan` → `hl plan` / `hl run`，`hd-collect` → `hl digest`。

## License

MIT
