#!/usr/bin/env bash
# 公共函数。HD_HOME=代码目录（skill 目录）；HD_CONF_DIR=用户配置（升级不覆盖）；HD_DIR=运行时工作目录
HD_HOME="${HD_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HD_CONF_DIR="${HD_CONF_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-dispatch}"
HD_DIR="${HD_DIR:-${TMPDIR:-/tmp}/herdr-dispatch-$(id -un)}"
HD_DIR="${HD_DIR%/}"
MODELS_CONF="$HD_CONF_DIR/models.conf"
HD_CLIS="claude pi codex agy"   # 支持的 CLI；每个都在 hd-runners.sh 里有 list_/run_ 函数
die(){ echo "hd: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "找不到命令: $1"; }

# 可移植超时（macOS 默认没有 timeout）：tmo <秒> <命令...>
tmo(){ local s="$1"; shift; perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$s" "$@"; }

# 载入各 CLI 的列模型/执行函数；用户可在 $HD_CONF_DIR/runners.local.sh 里重定义 run_<cli> 覆盖参数
load_runners(){ . "$HD_HOME/bin/hd-runners.sh"; [ -f "$HD_CONF_DIR/runners.local.sh" ] && . "$HD_CONF_DIR/runners.local.sh"; return 0; }

# 查模型：输出 runner|model|default_thinking|enabled
lookup_model(){
  [ -f "$MODELS_CONF" ] || return 0
  awk -F'|' -v a="$1" '!/^[[:space:]]*#/ && NF>=5 && $1==a {print $2"|"$3"|"$4"|"$5; exit}' "$MODELS_CONF"
}
conf_rows(){ grep -v '^[[:space:]]*#' "$MODELS_CONF" 2>/dev/null | grep -v '^[[:space:]]*$'; }

# 是否已完成首次配置（至少有一个 enabled=yes）
configured(){ conf_rows | awk -F'|' '$5=="yes"{f=1} END{exit !f}'; }
require_configured(){ configured || die "尚未配置（首次使用）。请让用户确认 CLI 与模型偏好：终端运行 hd-setup；agent 按 SKILL.md 第 0 步执行 hd-setup scan-clis / scan-models / apply"; }

# 别名也可写成级别 cheap|mid|strong：取该级别里 rank 最靠前、启用且 CLI 在 PATH 的模型；
# 该级别没有可用模型时依次退到更强、再到更弱的级别
resolve_alias(){
  case "$1" in cheap) order="cheap mid strong";; mid) order="mid strong cheap";; strong) order="strong mid cheap";; *) echo "$1"; return;; esac
  local t a
  for t in $order; do
    while IFS='|' read -r a r _ _ e _ tier _; do
      [ "$tier" = "$t" ] && [ "$e" = yes ] && command -v "$r" >/dev/null 2>&1 && { echo "$a"; return; }
    done < <(conf_rows | sort -t'|' -k8,8n)
  done
  echo "$1"
}
