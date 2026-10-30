#!/usr/bin/env bash
# 每个 CLI 两个函数：
#   list_<cli>   输出该 CLI 当前账号可用的模型 ID，每行一个
#   run_<cli>    非交互执行一次任务。输入环境变量：
#                HD_MODEL  HD_THINK(off|low|medium|high)  HD_EFFORT(同 THINK，off 映射为 low)  HD_PROMPT_FILE
# 派工窗口无人值守，默认跳过权限确认；想收紧就在 ~/.config/herdr-dispatch/runners.local.sh 里重定义 run_<cli>。

# --- Claude Code ---
# 没有列模型命令；别名由 Claude Code 自动指向账号可用的最新版本。其他 ID 用 hd-setup add claude <id> 补。
list_claude(){ printf '%s\n' sonnet opus haiku; }
run_claude(){ claude -p --model "$HD_MODEL" --effort "$HD_EFFORT" --dangerously-skip-permissions "$(cat "$HD_PROMPT_FILE")"; }

# --- pi ---
list_pi(){ pi --list-models 2>/dev/null | awk 'NF>=2 && tolower($1)!="provider" && $1 ~ /^[A-Za-z0-9._-]+$/ {print $1"/"$2}'; }
run_pi(){ pi -p --no-session --model "$HD_MODEL" --thinking "$HD_THINK" "@$HD_PROMPT_FILE" "按附件任务执行，并按汇报要求写 report.md"; }

# --- Codex CLI ---
# codex 没有列模型子命令，读它自己的模型缓存（登录并启动过一次 codex 后才有）
list_codex(){
  local f="${CODEX_HOME:-$HOME/.codex}/models_cache.json"; [ -f "$f" ] || return 0
  python3 - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for m in (d.get("models", []) if isinstance(d, dict) else d):
    if m.get("visibility", "list") == "list" and m.get("slug"):
        print(m["slug"])
PY
}
run_codex(){ codex exec --skip-git-repo-check -m "$HD_MODEL" -c "model_reasoning_effort=$HD_EFFORT" --dangerously-bypass-approvals-and-sandbox "$(cat "$HD_PROMPT_FILE")"; }

# --- Agy (Antigravity CLI) ---
list_agy(){ agy models 2>/dev/null | awk -F'\t' 'NF>=2 && $1 !~ /[[:space:]]/ {print $1}'; }
run_agy(){ agy -p "$(cat "$HD_PROMPT_FILE")" --model "$HD_MODEL" --effort "$HD_EFFORT" --dangerously-skip-permissions; }
