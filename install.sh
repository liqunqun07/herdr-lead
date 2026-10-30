#!/usr/bin/env bash
# 安装 herdr-dispatch：skill 放到 ~/.agents/skills/herdr-dispatch，链接到各 agent 的 skills 目录，命令放进 ~/.local/bin。
#   ./install.sh              安装/升级（用户配置在 ~/.config/herdr-dispatch，不会被覆盖）
#   ./install.sh --uninstall  卸载（保留用户配置）
# 环境变量: HD_INSTALL_DIR 安装位置；HD_BIN_DIR 命令目录；HD_ADD_RULE=1 非交互时也写自动派工规则
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${HD_INSTALL_DIR:-$HOME/.agents/skills/herdr-dispatch}"
BIN_DIR="${HD_BIN_DIR:-$HOME/.local/bin}"
CMDS="hd-setup hd-models hd-pick hd-plan hd-dispatch hd-collect hd-clean"
# 各 agent 的用户级 skills 目录：只在该 agent 已安装（父目录存在）时链接；Claude Code 总是链接
SKILL_DIRS="$HOME/.claude/skills $HOME/.codex/skills $HOME/.pi/agent/skills $HOME/.gemini/config/skills $HOME/.agents/skills"
RULE_FILES="$HOME/.claude/CLAUDE.md $HOME/.codex/AGENTS.md $HOME/.pi/agent/AGENTS.md"
MARK="<!-- herdr-dispatch -->"

if [ "${1:-}" = "--uninstall" ]; then
  for c in $CMDS; do f="$BIN_DIR/$c"; grep -qs "herdr-dispatch wrapper" "$f" && rm -f "$f"; done
  for d in $SKILL_DIRS; do [ -L "$d/herdr-dispatch" ] && rm -f "$d/herdr-dispatch"; done
  [ -d "$DEST/.git" ] || rm -rf "$DEST"   # 不删 git 仓库（HD_INSTALL_DIR 指向克隆目录时）
  echo "已卸载。规则文件里的 $MARK 段落与 ~/.config/herdr-dispatch 未删除，需要请手动删。"; exit 0
fi

for c in bash perl python3; do command -v "$c" >/dev/null || { echo "缺少依赖: $c" >&2; exit 1; }; done
command -v herdr >/dev/null || echo "!! 未找到 herdr：可以先装好再用，派工需要它"

# 1) 代码
if [ "$DEST" != "$SRC" ]; then
  mkdir -p "$DEST"
  rsync -a --delete --exclude .git "$SRC/" "$DEST/"
fi
chmod +x "$DEST"/bin/hd-* "$DEST/install.sh"

# 2) 命令（用包装脚本而非软链接，脚本里就不需要 readlink -f）
mkdir -p "$BIN_DIR"
for c in $CMDS; do
  printf '#!/usr/bin/env bash\n# herdr-dispatch wrapper\nexec %q "$@"\n' "$DEST/bin/$c" > "$BIN_DIR/$c"; chmod +x "$BIN_DIR/$c"
done
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "!! $BIN_DIR 不在 PATH，请把它加进 shell 配置";; esac

# 3) skill 链接
for d in $SKILL_DIRS; do
  [ "$d" = "$(dirname "$DEST")" ] && continue
  case "$d" in "$HOME/.claude/skills") ;; *) [ -d "$(dirname "$d")" ] || continue;; esac
  if [ -e "$d/herdr-dispatch" ] && [ ! -L "$d/herdr-dispatch" ]; then echo "跳过 $d/herdr-dispatch（已存在且不是链接）"; continue; fi
  mkdir -p "$d"; ln -sfn "$DEST" "$d/herdr-dispatch"; echo "skill -> $d/herdr-dispatch"
done

# 4) 自动派工规则（可选）：让指挥在"做计划 + 复杂任务"时一定想起派工
RULE="$MARK
做计划且任务复杂/较长（可拆 3 个以上独立子任务）时，使用 herdr-dispatch skill：自己决定窗口数、小模型和 thinking，在计划中给出派工方案，等用户确认后再派发。简单任务不要派工。
$MARK"
want_rule=0
if [ -t 0 ]; then read -r -p "把一行自动派工规则写进 agent 的全局规则文件（CLAUDE.md / AGENTS.md）？[y/N] " yn; case "$yn" in [Yy]*) want_rule=1;; esac
elif [ "${HD_ADD_RULE:-0}" = 1 ]; then want_rule=1; fi
if [ $want_rule = 1 ]; then
  for f in $RULE_FILES; do [ -d "$(dirname "$f")" ] || continue
    grep -qsF "$MARK" "$f" || { printf '\n%s\n' "$RULE" >> "$f"; echo "rule -> $f"; }; done
fi

echo; echo "安装完成: $DEST"
if "$DEST/bin/hd-setup" status >/dev/null 2>&1; then echo "已有配置，沿用（hd-models 查看，hd-setup -h 修改）"
elif [ -t 0 ]; then
  read -r -p "现在扫描本机 CLI 和模型并确认偏好？[Y/n] " yn
  case "${yn:-Y}" in [Yy]*) "$DEST/bin/hd-setup" wizard;; *) echo "稍后运行 hd-setup（未配置前无法派工；agent 首次使用时也会引导你确认）";; esac
else echo "首次使用请运行 hd-setup（扫描本机 CLI 和模型，由你确认偏好）"; fi
