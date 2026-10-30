#!/usr/bin/env bash
# 离线自测：用假的 claude/pi/codex/agy/herdr 跑通 扫描 → 确认偏好 → 修改 → 方案 → 派发 → 回报 → 清理。
# 不调用任何真实模型，不碰真实配置。用法: bash tests/test.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HD_CONF_DIR="$T/conf" HD_DIR="$T/run" CODEX_HOME="$T/codex" HD_VERIFY_TIMEOUT=10
FAKE="$T/fake"; mkdir -p "$FAKE" "$CODEX_HOME"
export PATH="$FAKE:$ROOT/bin:/usr/bin:/bin:/usr/sbin:/sbin"
pass=0; fail=0
ok(){ if eval "$2"; then pass=$((pass+1)); echo "  PASS $1"; else fail=$((fail+1)); echo "  FAIL $1"; fi; }

# 假 CLI：记录参数；模型名含 bad 时失败；提示里要求写 report.md 就写一份
cat > "$FAKE/fakecli" <<'EOF'
#!/usr/bin/env bash
me="$(basename "$0")"; echo "$me $*" >> "$FAKE_LOG"
[ "${1:-}" = --version ] && { echo "$me 9.9"; exit 0; }
case "$me $*" in
  "pi --list-models") printf 'provider  model  context\nacme  tiny-flash  8K\nacme  big-pro  1M\n'; exit 0;;
  "agy models") printf 'Fetching available models...\nglm-5.3-flash\tGLM Flash\nopus-x\tOpus X\n'; exit 0;;
esac
case "$*" in *bad*) echo "model not found" >&2; exit 1;; esac
text=""; for a in "$@"; do case "$a" in @*) text="$text$(cat "${a#@}")";; *) text="$text $a";; esac; done
rp="$(printf '%s' "$text" | grep -o '[^ ]*/report\.md' | head -1)"
[ -n "$rp" ] && printf -- '- 状态: done\n- 改动文件列表: 无\n- 做了什么: fake by %s\n- 需要指挥决定的问题: 无\n' "$me" > "$rp"
echo OK
EOF
chmod +x "$FAKE/fakecli"; for c in claude pi codex agy; do ln -s fakecli "$FAKE/$c"; done
export FAKE_LOG="$T/cli.log"
echo '{"models":[{"slug":"gpt-luna","visibility":"list"},{"slug":"gpt-hidden","visibility":"hide"}]}' > "$CODEX_HOME/models_cache.json"
cat > "$FAKE/herdr" <<'EOF'
#!/usr/bin/env bash
echo "herdr $*" >> "$HERDR_LOG"
case "$1 $2" in
  "pane split") n=$(( $(grep -c 'pane split' "$HERDR_LOG") )); echo "{\"result\":{\"pane\":{\"pane_id\":\"p$n\"}}}";;
  "pane run") bash -c "$4" >/dev/null 2>&1 & ;;
  "pane read") echo "(screen)";;
esac
exit 0
EOF
chmod +x "$FAKE/herdr"; export HERDR_LOG="$T/herdr.log" HERDR_PANE_ID=p0

echo "== 未配置时拦截"
ok "hd-models 拒绝" '! hd-models >/dev/null 2>&1'
ok "hd-setup status 非0" '! hd-setup status >/dev/null 2>&1'

echo "== 扫描"
out="$(hd-setup scan-clis)"
ok "四个 CLI 都识别" '[ "$(grep -c "✓" <<<"$out")" -eq 5 ]'
out="$(hd-setup scan-models)"; echo "$out" | sed 's/^/    /'
ok "claude 别名" 'grep -q "claude  *sonnet" <<<"$out"'
ok "pi 两列解析为 provider/model" 'grep -q "acme/tiny-flash" <<<"$out"'
ok "codex 读缓存且排除隐藏" 'grep -q gpt-luna <<<"$out" && ! grep -q gpt-hidden <<<"$out"'
ok "agy 跳过提示行" 'grep -q glm-5.3-flash <<<"$out" && ! grep -q Fetching <<<"$out"'
ok "级别猜测 flash=cheap" 'grep "acme/tiny-flash" <<<"$out" | grep -q cheap'
num(){ grep -F "$1" "$HD_CONF_DIR/.candidates" | head -1 | cut -d'|' -f1; }
hd-setup add pi acme/bad-model >/dev/null

echo "== 用户确认偏好（含一个会失败的模型）"
out="$(hd-setup apply "$(num gpt-luna)" "$(num acme/tiny-flash)" "$(num glm-5.3-flash)":cheap "$(num opus)" "$(num acme/bad-model)" 2>&1)"; echo "$out" | sed 's/^/    /'
ok "已配置" 'hd-setup status >/dev/null'
ok "失败模型自动停用" 'grep -q "^pi-bad-model|.*|no|" "$HD_CONF_DIR/models.conf"'
ok "rank 按用户顺序" '[ "$(awk -F"|" "\$1==\"codex-gpt-luna\"{print \$8}" "$HD_CONF_DIR/models.conf")" = 1 ]'
ok "用户改的级别生效" 'grep -q "^agy-glm-5-3-flash|.*|cheap|3$" "$HD_CONF_DIR/models.conf"'
ok "hd-pick cheap 取最靠前（codex luna 猜 cheap）" '[ "$(hd-pick cheap)" = codex-gpt-luna ]'

echo "== 后续修改偏好"
hd-setup set agy-glm-5-3-flash rank=1 >/dev/null
ok "rank=1 前移并重排" '[ "$(hd-pick cheap)" = agy-glm-5-3-flash ] && [ "$(cut -d"|" -f8 <(grep -v "^#" "$HD_CONF_DIR/models.conf") | tr "\n" " ")" = "1 2 3 4 5 " ]'
hd-setup set agy-glm-5-3-flash enabled=no >/dev/null
ok "停用后自动换下一个" '[ "$(hd-pick cheap)" = codex-gpt-luna ]'
hd-setup set agy-glm-5-3-flash enabled=yes >/dev/null
ok "非法值被拒" '! hd-setup set agy-glm-5-3-flash tier=huge >/dev/null 2>&1'
ok "strong 级别解析" '[ "$(hd-pick strong)" = claude-opus ]'
hd-setup remove pi-bad-model >/dev/null
ok "删除" '! grep -q pi-bad-model "$HD_CONF_DIR/models.conf"'
n_cand=$(num gpt-luna); ok "append 不重复添加" 'hd-setup apply --append --no-verify "$n_cand" 2>&1 | grep -q 已在配置里'

echo "== 派工方案"
mkdir -p "$T/tasks" "$T/proj"
for i in a b c; do echo "任务 ${i}：什么也不改" > "$T/tasks/$i.md"; done
printf 't1|cheap|off|%s|%s|机械\nt2|pi-tiny-flash|medium|%s|%s|实现\nt3|claude-opus|high|%s|%s|推理\n' \
  "$T/proj" "$T/tasks/a.md" "$T/proj" "$T/tasks/b.md" "$T/proj" "$T/tasks/c.md" > "$T/plan.txt"
out="$(hd-plan show "$T/plan.txt")"; rc=$?; echo "$out" | sed 's/^/    /'
ok "show 通过" '[ $rc -eq 0 ]'
ok "级别被解析成别名" 'grep -q agy-glm-5-3-flash <<<"$out"'
printf 'x1|nope|low|%s|%s|坏\n' "$T/proj" "$T/missing.md" > "$T/bad.txt"
ok "坏方案被拦截且 run 拒绝" '! hd-plan show "$T/bad.txt" >/dev/null && ! hd-plan run "$T/bad.txt" >/dev/null 2>&1'

echo "== 派发 → 回报 → 清理"
hd-plan run "$T/plan.txt" | sed 's/^/    /'
out="$(hd-collect -T 20 t1 t2 t3)"; echo "$out" | sed 's/^/    /'
ok "三份汇报都收到" '[ "$(grep -c "状态: done" <<<"$out")" -eq 3 ]'
ok "agy 收到 effort=low（thinking off 映射）" 'grep -q "^agy -p .* --model glm-5.3-flash --effort low" "$FAKE_LOG"'
ok "claude 带 effort high" 'grep -q "^claude -p --model opus --effort high" "$FAKE_LOG"'
ok "pi 用附件传提示" 'grep -q "^pi -p --no-session --model acme/tiny-flash --thinking medium @" "$FAKE_LOG"'
ok "主动回报指挥 pane" '[ "$(grep -c "agent prompt p0" "$HERDR_LOG")" -eq 3 ]'
ok "窗口已命名" 'grep -q "pane rename p1 t1·agy-glm-5.3-flash" "$HERDR_LOG"'
hd-clean --all >/dev/null
ok "清理关闭 pane" '[ "$(grep -c "pane close" "$HERDR_LOG")" -eq 3 ] && [ -z "$(ls "$HD_DIR")" ]'

echo; echo "结果: $pass 通过, $fail 失败"; [ $fail -eq 0 ]
