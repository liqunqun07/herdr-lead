# herdr-dispatch

让一个"指挥"模型（Claude Code、pi 上的 GPT 等，你自己选）在做复杂任务计划时，自动给出**派工方案**：开几个 herdr 窗口、每个窗口用哪个便宜的小模型、什么 thinking/effort。你确认后，它在旁边开窗口把子任务派给小模型，小模型做完写汇报并主动通知指挥，指挥验收。目的是省 token：贵的模型只做规划和验收。

支持的执行端 CLI：**Claude Code (`claude`)、pi、Codex CLI (`codex`)、Agy (`agy`)**。不需要 API key，直接用各 CLI 已登录的账号。

## 安装

```sh
git clone https://github.com/liqunqun07/herdr-dispatch.git && cd herdr-dispatch && ./install.sh
```

- skill 装到 `~/.agents/skills/herdr-dispatch`，并链接到已安装 agent 的 skills 目录（`~/.claude/skills`、`~/.codex/skills`、`~/.pi/agent/skills`、`~/.gemini/config/skills`）。
- 命令 `hd-*` 装到 `~/.local/bin`。
- 安装时可选把一行"计划+复杂任务时用 herdr-dispatch"的规则写进 `CLAUDE.md` / `AGENTS.md`（默认不写，skill 的描述本身就能触发）。
- 卸载：`./install.sh --uninstall`（保留你的配置）。

依赖：bash、perl、python3、herdr。

## 首次配置（只做一次）

每个人装的 CLI 和能用的模型都不同，所以第一次由你确认偏好：

```sh
hd-setup          # 向导：扫描 CLI → 选 CLI → 扫描模型 → 按偏好顺序选 CLI×模型 → 改级别 → 实测 → 保存
```

也可以不开终端：第一次在 agent 里触发派工时，它会运行 `hd-setup scan-clis` / `scan-models` 把候选给你看，按你的选择执行 `hd-setup apply`。

- **级别**：`cheap` 机械活 / `mid` 普通实现 / `strong` 复杂推理。按模型名猜一个，你可以改。
- **偏好顺序**：同一级别里越靠前越优先；某个不可用时自动用下一个。
- **实测**：保存前每个模型都真实调用一次，调不通的自动停用。

配置存在 `~/.config/herdr-dispatch/models.conf`，升级 skill 不会覆盖。之后派工**不再询问**，想改随时：

```sh
hd-models                                   # 看当前偏好
hd-setup set pi-deepseek-v4-flash rank=1    # 调到第一
hd-setup set codex-gpt-5-6-luna enabled=no  # 停用
hd-setup set agy-gemini-3-8-flash-low tier=mid thinking=low
hd-setup add pi deepseek/deepseek-v4-flash && hd-setup apply --append <编号>   # 加一个扫不到的
hd-setup remove <alias> / hd-setup reset    # 删除 / 全部重来
```

## 日常使用

在指挥 agent 里正常提需求、让它先做计划。任务够复杂时，计划里会出现：

```
派工方案：共 3 个窗口并行
#    TASK   MODEL                      CLI    THINK   STATUS  NOTE
1    t1     agy-gemini-3-8-flash-low   agy    low     ok      批量改 import
2    t2     pi-gpt-5-6-luna            pi     medium  ok      补单元测试
3    t3     claude-sonnet              claude high    ok      重构解析器
```

你回复"通过"（或"改成 2 个窗口""t3 换强一点"）。窗口数、模型、thinking 由指挥按你的偏好自动决定。之后：

`hd-plan run` 开窗口派发 → 小模型完成后写 `report.md` 并给指挥 pane 发通知 → 指挥 `hd-collect` 收汇报、`git diff` 验收 → `hd-clean --all` 关窗口。

## 权限

派工窗口无人值守，默认跳过各 CLI 的权限确认（`hd-setup show-runner <cli>` 看实际命令）。想收紧，在 `~/.config/herdr-dispatch/runners.local.sh` 里重定义对应函数，例如：

```sh
run_claude(){ claude -p --model "$HD_MODEL" --effort "$HD_EFFORT" --permission-mode acceptEdits "$(cat "$HD_PROMPT_FILE")"; }
```

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `HD_CONF_DIR` | `~/.config/herdr-dispatch` | 用户配置 |
| `HD_DIR` | `$TMPDIR/herdr-dispatch-$USER` | 运行时工作文件 |
| `HD_MAX_PANES` | 4 | 一次最多开几个窗口 |
| `HD_VERIFY_TIMEOUT` | 240 | 实测单个模型的超时秒数 |
| `HD_COLLECT_TIMEOUT` | 540 | `hd-collect` 单次最长等待 |
| `HD_NOTIFY` | 1 | 0 = 小模型完成后不主动通知指挥 |

## 测试

```sh
bash tests/test.sh   # 用假的 CLI 和 herdr 离线跑完整流程，不调用真实模型
```
