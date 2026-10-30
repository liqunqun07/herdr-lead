---
name: herdr-dispatch
description: 用于计划阶段的复杂或长期任务，生成方案并在用户确认后将一次性独立子任务派给小模型，收集回报；团队分工、接管和额度管理由 herdr-team-roles 负责。
---

# herdr-dispatch（指挥派工）

你是指挥：规划、派工、验收；大量实现交给小模型窗口。用户只做两件事：首次确认 CLI×模型偏好；每次确认派工方案。
窗口数、拆分、每个窗口的模型和 thinking 由你决定，不要让用户选。所有操作用 `hd-*` 脚本，不要手写 herdr / CLI 命令。

团队分工、指挥、多 AI、接管或额度管理请使用 `herdr-team-roles`（如已安装）。

## 何时触发
- 在做计划，并且任务复杂/长：能拆成 ≥3 个独立子任务，或要改很多文件、跑很久。
- 不派工的情况：任务小、强依赖顺序、子任务都要改同一批文件。此时正常做，计划里不提派工。

## 0. 首次配置（仅当脚本提示"尚未配置"时；配置过就跳过，不要再问用户）
`hd-setup status` 退出码非 0 = 未配置。偏好必须由用户决定，你不能替用户选、不能默认全选。
1. `hd-setup scan-clis`：把结果告诉用户，问启用哪些 CLI（支持 claude / pi / codex / agy）。
2. `hd-setup scan-models <用户选的 CLI...>`：把带编号的候选表发给用户，问三件事：
   ①启用哪些（CLI×模型组合）②偏好顺序（越靠前越优先）③要不要改级别（cheap=机械活 / mid=普通实现 / strong=复杂推理）。
   扫不到的模型：`hd-setup add <cli> <模型ID>` 后再展示一次。
3. 告诉用户派工窗口会跳过权限确认（`hd-setup show-runner <cli>` 可看实际命令），用户不同意就请他编辑 `~/.config/herdr-dispatch/runners.local.sh`。
4. 按用户的顺序执行 `hd-setup apply 编号[:级别] ...`（会实测每个模型能否调用，失败的自动停用），把结果表给用户看。
5. 继续第 1 步。之后用户要改偏好（"把 X 调到第一""停用 Y""加一个模型"）时用：
   `hd-setup set <alias> rank=1|enabled=no|tier=cheap|thinking=low`、`hd-setup add` + `hd-setup apply --append <编号>`、`hd-setup remove <alias>`；全部重来用 `hd-setup reset` 后重做第 0 步。

## 1. 写计划时：生成派工方案（不执行）
1. 拆分：子任务之间不改同一文件；有依赖的合并成一个任务顺序做；窗口数 = 独立任务数，上限 4（`HD_MAX_PANES`）。
2. 每个任务写一个任务文件（放 `/tmp/herdr-dispatch-tasks/<id>.md`）：目标、允许改动的文件范围、验收标准、禁止事项。汇报格式脚本会自动附上，不用写。
3. 按难度选级别和 thinking（thinking 会自动映射成各 CLI 的 effort）：
   - 机械改动/批量替换/样板 → `cheap`，off 或 low
   - 普通实现/写测试/读代码总结 → `mid`，low 或 medium
   - 需要推理的 bug/重构 → `strong`，medium 或 high
   模型列直接写级别（`cheap|mid|strong`），脚本按用户偏好自动选该级别里最靠前的可用模型；只有确实需要某个特定模型时才写 `hd-models` 里的别名。
4. 写 plan 文件 `/tmp/herdr-dispatch-plan.txt`，每行：
   `task_id|级别或别名|thinking|工作目录(绝对路径)|任务文件|一句话说明`
5. 运行 `hd-plan show /tmp/herdr-dispatch-plan.txt`。报错就自己修正后重跑。**把输出表格原样放进计划**，再补两句：为什么这样拆、省在哪里。
6. 等用户确认。用户说"通过/改成 2 个窗口/这个换强一点"，按要求改 plan 后重新 show。

## 2. 用户确认后：执行
**没有用户明确确认，绝不运行 `hd-plan run` / `hd-dispatch`。**
1. `hd-plan run /tmp/herdr-dispatch-plan.txt`（在当前 pane 旁边开窗口，并自动给窗口命名）。
2. 小模型结束时会主动给你的 pane 发一条 `[herdr-dispatch] 任务 X 已结束` 消息；你也可以直接 `hd-collect <task_id...>` 阻塞等待并打印全部汇报（最多等 540s，没结束就再运行一次；超时的会显示 pane 现场）。
3. 验收：对照每份 report 的改动文件，用 `git diff` 抽查是否越界、是否满足验收标准。
   - blocked/failed：补充信息重派，或换 `strong`；同一模型不要反复重试。
   - 小问题你自己修，不再派工。
4. `hd-clean --all` 关闭窗口、清理工作文件。
5. 向用户汇报：开了几个窗口、各用什么模型/thinking、结果、遗留问题。

## 命令速查
| 命令 | 作用 |
|---|---|
| `hd-setup status` | 是否已配置 + 当前偏好 |
| `hd-models` | 已配置的 CLI×模型、级别、偏好顺序、可用状态 |
| `hd-pick cheap\|mid\|strong` | 该级别当前会选中的模型 |
| `hd-plan show\|run <plan>` | 校验展示 / 派发方案 |
| `hd-dispatch <id> <级别或别名> <任务文件> [-t thinking] [--cwd DIR]` | 单独派一个 |
| `hd-collect <id...>` / `hd-clean --all` | 收汇报 / 关窗口 |

## 注意
- 不要编造模型 ID；只用级别或 `hd-models` 里的别名。
- 用户配置在 `~/.config/herdr-dispatch/`（models.conf、runners.local.sh），升级 skill 不会覆盖；不要手改脚本。
