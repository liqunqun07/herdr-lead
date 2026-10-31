#!/usr/bin/env python3
"""hl — herdr-lead 编排驱动(只用标准库)。

Lead agent 用它做确定性的部分:任务图校验、派发、等待、回报验证、重试/升级、复核、交付草稿。
判断性的部分(要不要拆、怎么拆、写什么简报)仍由 Lead 完成。

状态全部在 <root>/.herdr-run/ 下:
  plan.json  routes.json  brief/  out/  shared/  wt/  digest.md
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

RUN = ".herdr-run"
STATUSES = ("todo", "running", "done", "blocked", "failed", "stale", "needs_review")
KEYS = ["STATUS", "SUMMARY", "ARTIFACTS", "EVIDENCE", "OPEN_QUESTIONS", "CHANGE_REQUEST", "SENTINEL"]
REQUIRED = ["STATUS", "SUMMARY", "ARTIFACTS", "EVIDENCE", "OPEN_QUESTIONS", "SENTINEL"]
BYPASS = ("--dangerously-skip-permissions", "--yolo", "--dangerously-bypass-approvals-and-sandbox")
TASK_LIST_FIELDS = ("reads", "writes", "constraints", "acceptance", "depends_on")

BRIEF = """# 任务 {id}:{title}
(run {run_id})

## 目标
{goal}

## 你可以读
{reads}

## 共享约定(只读,不要修改)
{shared}

## 你只能写这些路径
{writes}
工作目录:{cwd}

## 约束
{constraints}

## 验收标准
{acceptance}
{prior}
## 完成后你必须做
把回报写入这个文件(绝对路径):{out}
每个字段单独一行,以 `KEY:` 开头,值可以多行:

STATUS: done | blocked | failed
SUMMARY: 不超过 10 行,只写结论和改动
ARTIFACTS: 产出文件路径,每行一个(没有就写 none)
EVIDENCE: 你怎么验证的:命令和结果(STATUS 为 done 时必填,不能写 none)
OPEN_QUESTIONS: 没解决的问题(没有就写 none)
CHANGE_REQUEST: 需要改共享约定或超出写范围时写在这里,不要自己改(没有就写 none)
SENTINEL: {sentinel}
{review_note}
写完回报后停止:不要继续做别的事,不要把过程贴到屏幕上,不要提交、推送或发布。
"""


# ---------------------------------------------------------------- helpers
def die(msg: str, code: int = 1):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def root_of(args) -> Path:
    return Path(args.root or os.environ.get("HL_ROOT") or ".").resolve()


def P(root: Path, *parts) -> Path:
    return root.joinpath(RUN, *parts)


def sh(argv, cwd=None, dry=False, timeout=120):
    if dry:
        print("DRY:", " ".join(shlex.quote(str(a)) for a in argv))
        return 0, "", ""
    try:
        env = os.environ.copy()
        if argv[0] == "herdr":
            env["HERDR_ENV"] = "1"
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", f"找不到命令 {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时"
    return r.returncode, r.stdout, r.stderr




@contextmanager
def plan_tx(root: Path):
    """加锁读-改-写 plan.json;块内抛异常则不保存。"""
    if not P(root).exists():
        die("没有 .herdr-run,先运行 init")
    with open(P(root, ".lock"), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        plan = json.loads(P(root, "plan.json").read_text())
        yield plan
        plan["version"] = plan.get("version", 0) + 1
        tmp = P(root, "plan.json.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        os.replace(tmp, P(root, "plan.json"))


def read_plan(root: Path) -> dict:
    if not P(root, "plan.json").exists():
        die("没有 plan.json,先运行 init")
    return json.loads(P(root, "plan.json").read_text())


def find(plan: dict, tid: str) -> dict:
    for t in plan["tasks"]:
        if t["id"] == tid:
            return t
    die(f"找不到任务 {tid}")


def expand(tpl, **kw):
    out = []
    cmd = kw.pop("cmd", [])
    for el in tpl:
        if el == "{cmd...}":
            out.extend(cmd)
        else:
            out.append(el.format(**kw))
    return out


def static_prefix(pat: str) -> str:
    idx = [i for i in (pat.find(c) for c in "*?[") if i >= 0]
    return pat[: min(idx)] if idx else pat


def overlap(a, b) -> bool:
    for pa in a:
        for pb in b:
            x, y = static_prefix(pa), static_prefix(pb)
            if x.startswith(y) or y.startswith(x):
                return True
    return False


def ai_name(runner: str) -> str:
    """全局命名规则「任务·AI」：agy-acc1→Agy，claude-ccc→Claudeccc，codex→Codex。"""
    head, _, rest = runner.partition("-")
    return head.capitalize() + ("" if not rest or rest.startswith("acc") else rest)


def in_scope(f: str, pats) -> bool:
    for p in pats:
        if p.endswith("/") and f.startswith(p):
            return True
        if f == p or fnmatch.fnmatch(f, p):
            return True
    return False


def bullets(items, empty="(无)") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def tier_sig(routes, tier):
    t = routes["tiers"].get(tier, {})
    return json.dumps(t.get("cmd", []))


# ---------------------------------------------------------------- receipts
def parse_receipt(text: str) -> dict:
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^([A-Z_]+):\s?(.*)$", line)
        if m and m.group(1) in KEYS:
            cur = m.group(1)
            out[cur] = [m.group(2)] if m.group(2) else []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def changed_files(cwd: Path):
    rc, out, _ = sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(cwd))
    if rc != 0:
        return None
    files = []
    for line in out.splitlines():
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ")[1]
        p = p.strip('"')
        if p.startswith(RUN + "/") or p == RUN:
            continue
        files.append(p)
    return files


def validate(root: Path, plan: dict, t: dict, rec: dict):
    """返回 (problems, warnings)。只验证形式、证据和写范围,不判断内容对错。"""
    problems, warns = [], []
    for k in REQUIRED:
        if not rec.get(k):
            problems.append(f"缺少字段 {k}")
    st = rec.get("STATUS", "").lower()
    if st not in ("done", "blocked", "failed"):
        problems.append(f"STATUS 非法: {rec.get('STATUS')!r}")
    expected = f"HERDR-DONE:{plan['run_id']}:{t['id']}"
    if rec.get("SENTINEL", "").strip() != expected:
        problems.append("SENTINEL 与本次 run/任务不符(可能是旧回报或抄错)")
    if len(rec.get("SUMMARY", "").splitlines()) > 10:
        problems.append("SUMMARY 超过 10 行")
    if st == "done":
        if rec.get("EVIDENCE", "").strip().lower() in ("", "none", "无"):
            problems.append("done 必须附 EVIDENCE")
        base = Path(t.get("cwd") or root)
        for line in rec.get("ARTIFACTS", "").splitlines():
            a = line.strip().lstrip("-").strip().strip("`")
            if not a or a.lower() == "none":
                continue
            p = Path(a)
            if not p.is_absolute() and not (base / p).exists() and not (root / p).exists():
                problems.append(f"产物不存在: {a}")
            elif p.is_absolute() and not p.exists():
                problems.append(f"产物不存在: {a}")
        if t["id"].startswith("R-"):
            first = rec.get("SUMMARY", "").strip().split()[:1]
            if not first or first[0].strip(":").upper() not in ("PASS", "FAIL"):
                problems.append("复核回报的 SUMMARY 必须以 PASS 或 FAIL 开头")
    # 写范围
    if t.get("cwd") and not t["id"].startswith("R-"):
        ch = changed_files(Path(t["cwd"]))
        if ch is not None:
            if t.get("isolation") == "worktree":
                bad = [f for f in ch if not in_scope(f, t.get("writes", []))]
                if bad:
                    problems.append("越出写范围: " + ", ".join(bad[:5]))
            else:
                allw = [w for x in plan["tasks"] for w in x.get("writes", [])]
                bad = [f for f in ch if not in_scope(f, allw)]
                if bad:
                    warns.append("共享目录里有不属于任何任务的改动: " + ", ".join(bad[:5]))
    return problems, warns


def collect_task(root: Path, plan: dict, t: dict) -> str:
    f = report_path(root, t)
    if not f.exists():
        return "pending"
    if f.stat().st_mtime_ns < t.get("marker_ns", 0):
        t["status"], t["reason"] = "failed", "回报早于本次派发"
        return "failed"
    rec = parse_receipt(f.read_text(errors="replace"))
    if "SENTINEL" not in rec:
        return "pending"  # 还在写
    problems, warns = validate(root, plan, t, rec)
    scope_bad = []
    if "before" in t:
        changed = changes_since(root, t["before"])
        allowed = t.get("writes", [])
        bad = [f for f in changed if not in_scope(f, allowed)]
        if bad:
            scope_bad = bad[:10]
        
    for item in rec.get("ARTIFACTS", "").splitlines():
        name = item.strip().lstrip("-").strip().strip("`")
        if name and name.lower() != "none":
            path = Path(name)
            path = path if path.is_absolute() else root / path
            if path.is_file() and path.stat().st_mtime_ns < t.get("marker_ns", 0):
                problems.append("产物早于本次派发: " + name)
    t["receipt"] = str(f.relative_to(root))
    t["finished_at"] = time.time()
    t["summary"] = rec.get("SUMMARY", "")[:600]
    t["open_questions"] = rec.get("OPEN_QUESTIONS", "")
    t["warnings"] = warns
    if problems:
        t["status"], t["reason"] = "failed", "; ".join(problems)
        return "failed"
    if scope_bad:
        t["status"] = "needs_review"
        t["reason"] = "写集外改动需复核: " + ", ".join(scope_bad)
        return "needs_review"
    st = rec["STATUS"].lower()
    if t["id"].startswith("R-"):
        t["verdict"] = rec["SUMMARY"].strip().split()[0].strip(":").upper()
        if t["verdict"] == "FAIL":
            t["status"], t["reason"] = "failed", "独立复核结论 FAIL: " + rec["SUMMARY"].strip()[:500]
            return "failed"
    if st == "done":
        t["status"] = "done"
        t.pop("reason", None)
        return "done"
    t["status"] = st
    t["reason"] = rec.get("OPEN_QUESTIONS") or rec.get("SUMMARY", "")
    return st


# ---------------------------------------------------------------- plan graph
def analyze(plan: dict, routes: dict):
    errs, warns = [], []
    tasks = plan["tasks"]
    ids = [t["id"] for t in tasks]
    names = {}
    for t in tasks:
        agent_name = "hl-" + t["id"].lower().replace("_", "-")
        names.setdefault(agent_name, []).append(t["id"])
    for agent_name, task_ids in names.items():
        if len(task_ids) > 1:
            errs.append("生成的 agent 名冲突 " + agent_name + ": " + ", ".join(task_ids))
    if len(set(ids)) != len(ids):
        errs.append("任务 id 重复")
    byid = {t["id"]: t for t in tasks}
    for t in tasks:
        for k in ("id", "title", "tier"):
            if not t.get(k):
                errs.append(f"{t.get('id', '?')}: 缺少 {k}")
        for k in TASK_LIST_FIELDS:
            if not isinstance(t.get(k, []), list):
                errs.append(f"{t['id']}: {k} 必须是列表")
        if t.get("status", "todo") not in STATUSES:
            errs.append(f"{t['id']}: status 非法")
        if t.get("tier") and t["tier"] not in routes["tiers"]:
            errs.append(f"{t['id']}: 未知 tier {t['tier']}")
        for d in t.get("depends_on", []):
            if d not in byid:
                errs.append(f"{t['id']}: 依赖不存在 {d}")
        if not t.get("acceptance"):
            warns.append(f"{t['id']}: 没有验收标准,回报很难被验证")
        if not t.get("reads") and not t["id"].startswith("R-"):
            warns.append(f"{t['id']}: reads 为空,worker 可能通读整个仓库(费 token)")
    if errs:
        return errs, warns, [], "INVALID"
    # 拓扑分层
    level, remaining = {}, set(ids)
    while remaining:
        ready = [i for i in remaining if all(d in level for d in byid[i].get("depends_on", []))]
        if not ready:
            errs.append("依赖成环: " + ", ".join(sorted(remaining)))
            return errs, warns, [], "INVALID"
        for i in ready:
            level[i] = 1 + max([level[d] for d in byid[i].get("depends_on", [])] or [0])
            remaining.discard(i)
    waves = {}
    for i, l in level.items():
        waves.setdefault(l, []).append(i)
    wave_list = [sorted(waves[k]) for k in sorted(waves)]
    # 祖先集,用于判断两个任务是否有先后顺序
    anc = {}

    def ancestors(i):
        if i in anc:
            return anc[i]
        s = set()
        for d in byid[i].get("depends_on", []):
            s |= {d} | ancestors(d)
        anc[i] = s
        return s

    for a in ids:
        for b in ids:
            if a < b and a not in ancestors(b) and b not in ancestors(a):
                if overlap(byid[a].get("writes", []), byid[b].get("writes", [])):
                    errs.append(f"{a} 与 {b} 可能并行但写范围重叠")
    width = max(len(w) for w in wave_list) if wave_list else 0
    if len(tasks) <= 1:
        verdict = "SINGLE 只有一个任务:自己做,或只开一个 worker"
    elif width == 1:
        verdict = "CHAIN 全是串行依赖:拆分只增加交接成本,建议合并任务或自己做"
    else:
        verdict = f"PARALLEL 建议并行宽度 {min(routes.get('max_width', 3), width)}"
    strong = [t["id"] for t in tasks if t["tier"] == "strong"]
    if len(strong) > 1:
        warns.append(f"多个任务用了 strong 档({', '.join(strong)}),确认是否都需要")
    return errs, warns, wave_list, verdict


# ---------------------------------------------------------------- brief / spawn
def render_brief(root: Path, plan: dict, t: dict) -> str:
    shared = sorted(p.name for p in P(root, "shared").glob("*") if p.is_file())
    shared_txt = bullets([str(P(root, "shared", s)) for s in shared], "(无)")
    prior = ""
    if t.get("last_failure"):
        prior = f"\n## 上一次为什么没通过(这次请专门避免)\n{t['last_failure']}\n"
    review_note = ""
    if t["id"].startswith("R-"):
        review_note = "\n你是独立复核者:只读,不要修改任何文件。SUMMARY 第一个词必须是 PASS 或 FAIL,后面写理由。\n"
    return BRIEF.format(
        id=t["id"], title=t["title"], run_id=plan["run_id"],
        goal=t.get("goal") or t["title"],
        reads=bullets(t.get("reads", [])),
        shared=shared_txt,
        writes=bullets(t.get("writes", []), "(只读任务,不要修改任何文件)"),
        cwd=t.get("cwd") or str(root),
        constraints=bullets(t.get("constraints", [])),
        acceptance=bullets(t.get("acceptance", [])),
        prior=prior,
        out=P(root, "out", f"{t['id']}.md"),
        sentinel=f"HERDR-DONE:{plan['run_id']}:{t['id']}",
        review_note=review_note,
    )








def counts(plan):
    c = {s: 0 for s in STATUSES}
    for t in plan["tasks"]:
        c[t["status"]] += 1
    return c


def stuck(plan):
    byid = {t["id"]: t for t in plan["tasks"]}
    out = []
    for t in plan["tasks"]:
        if t["status"] == "todo" and any(byid[d]["status"] in ("blocked", "failed", "stale", "needs_review") for d in t.get("depends_on", [])):
            out.append(t["id"])
    return out


def status_text(plan: dict) -> str:
    c = counts(plan)
    lines = [f"run {plan['run_id']}  v{plan.get('version', 0)}  {plan.get('goal', '')[:60]}"]
    for t in plan["tasks"]:
        extra = ""
        if t.get("verdict"):
            extra = f" {t['verdict']}"
        if t["status"] in ("blocked", "failed", "stale", "needs_review") and t.get("reason"):
            extra = f"  ← {t['reason'][:70]}"
        r = f" r{t['retries']}" if t.get("retries") else ""
        lines.append(f"{t['id']:<6}{t['tier']:<9}{t['status']:<8}{t.get('owner', '-'):<10}{r}{extra}")
    lines.append("done {done}/{n}  running {running}  todo {todo}  blocked {blocked}  failed {failed}  stale {stale}  needs_review {needs_review}".format(n=len(plan["tasks"]), **c))
    s = stuck(plan)
    if s:
        lines.append("被上游卡住: " + ", ".join(s))
    return "\n".join(lines)


# ---------------------------------------------------------------- commands




def cmd_check(a):
    root = root_of(a)
    errs, warns, waves, verdict = analyze(read_plan(root), load_routes(root))
    for e in errs:
        print("错误:", e)
    for w in warns:
        print("警告:", w)
    if waves:
        print("分层:", " → ".join("[" + ",".join(w) + "]" for w in waves))
    print("判断:", verdict)
    sys.exit(1 if errs else 0)






def cmd_collect(a):
    root = root_of(a)
    with plan_tx(root) as plan:
        t = find(plan, a.id)
        st = collect_task(root, plan, t)
        print(f"{a.id}: {st}" + (f"  {t.get('reason', '')}" if st in ("failed", "blocked") else ""))
        for w in t.get("warnings", []):
            print("警告:", w)


def is_finished(plan) -> bool:
    byid = {t["id"]: t for t in plan["tasks"]}
    if any(t["status"] == "running" for t in plan["tasks"]):
        return False
    for t in plan["tasks"]:
        if t["status"] == "todo" and all(byid[d]["status"] == "done" for d in t.get("depends_on", [])):
            return False
    return True




def cmd_status(a):
    plan = read_plan(root_of(a))
    print(json.dumps(plan, ensure_ascii=False, indent=2) if a.json else status_text(plan))














# Phase 1 adaptation. Models are read-only and project state stays in plan.json.
CONFIG = Path.home() / ".config"
MODEL_FILE = CONFIG / "herdr-dispatch" / "models.conf"
LOCAL_RUNNERS = CONFIG / "herdr-dispatch" / "runners.local.sh"
POLICY_FILE = CONFIG / "herdr-lead" / "policy.json"
CONSENT_FILE = CONFIG / "herdr-lead" / "consent.json"
TIERS = ("cheap", "mid", "strong")
CHEAP_KEYWORDS = ("mini", "flash", "haiku", "lite", "low")
STRONG_KEYWORDS = ("pro", "opus", "sonnet", "terra", "high")
TIER_WHEN = {
    "cheap": "简单任务",
    "mid": "普通任务",
    "strong": "复杂推理",
}
DEFAULT_THINKING = {
    "cheap": "low",
    "mid": "medium",
    "strong": "high",
}
BUILTIN_RUNNERS = ("claude", "codex", "agy", "pi")

def config_dir() -> Path:
    env = os.environ.get("HL_CONFIG_DIR")
    if env:
        return Path(env).resolve()
    return CONFIG

def get_model_file() -> Path:
    if MODEL_FILE != CONFIG / "herdr-dispatch" / "models.conf":
        return MODEL_FILE
    return config_dir() / "herdr-dispatch" / "models.conf"

def get_local_runners() -> Path:
    if LOCAL_RUNNERS != CONFIG / "herdr-dispatch" / "runners.local.sh":
        return LOCAL_RUNNERS
    return config_dir() / "herdr-dispatch" / "runners.local.sh"

def get_policy_file() -> Path:
    if POLICY_FILE != CONFIG / "herdr-lead" / "policy.json":
        return POLICY_FILE
    return config_dir() / "herdr-lead" / "policy.json"

def get_consent_file() -> Path:
    if CONSENT_FILE != CONFIG / "herdr-lead" / "consent.json":
        return CONSENT_FILE
    return config_dir() / "herdr-lead" / "consent.json"

def guess_tier(model_name: str) -> str:
    s = model_name.lower()
    s_clean = s.replace("gemini", "")
    if any(k in s_clean for k in CHEAP_KEYWORDS):
        return "cheap"
    if any(k in s for k in STRONG_KEYWORDS):
        return "strong"
    return "mid"

def find_local_runners() -> list[str]:
    local_runners = get_local_runners()
    if not local_runners.exists():
        return []
    runners = set()
    try:
        rc, out, _ = sh(["bash", "-c", f". {shlex.quote(str(local_runners))}; declare -F"], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 3 and parts[2].startswith("run_"):
                    r = parts[2][4:]
                    if re.fullmatch(r"[A-Za-z0-9_-]+", r):
                        runners.add(r)
    except Exception:
        pass
    try:
        content = local_runners.read_text()
        matches = re.findall(r"^\s*(?:function\s+)?run_([A-Za-z0-9_-]+)\s*(?:\(\s*\))?", content, re.MULTILINE)
        for m in matches:
            if re.fullmatch(r"[A-Za-z0-9_-]+", m):
                runners.add(m)
    except Exception:
        pass
    return sorted(runners)

def scan_clis() -> list[str]:
    found = set()
    for c in BUILTIN_RUNNERS:
        if shutil.which(c):
            found.add(c)
    for c in find_local_runners():
        found.add(c)
    return sorted(found)

def policy():
    defaults = {"max_width": 3, "overhead": 4,
                "quota_cmd": [sys.executable, str(Path(__file__).with_name("quota_local.py"))]}
    policy_f = get_policy_file()
    if policy_f.exists():
        defaults.update(json.loads(policy_f.read_text()))
    return defaults

def models():
    result = []
    model_f = get_model_file()
    if not model_f.exists():
        return result
    for line in model_f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 8:
            raise ValueError("models.conf 格式错误: " + line)
        alias, runner, model, thinking, enabled, use, tier, rank = fields[:8]
        tags = fields[8].split(",") if len(fields) > 8 else [
            tag for tag, words in {
                "code": ("代码", "code", "编程"),
                "visual": ("视觉", "图片", "visual"),
                "literature": ("文献", "literature"),
                "zh": ("中文", "zh")}.items()
            if any(word in use.lower() for word in words)]
        result.append(dict(alias=alias, runner=runner, model=model, thinking=thinking,
                           enabled=enabled.lower() == "yes", use=use, tier=tier,
                           rank=int(rank), tags=[x.strip() for x in tags if x.strip()]))
    return sorted(result, key=lambda m: (m["rank"], m["alias"]))

def quota():
    try:
        command = policy()["quota_cmd"]
        if isinstance(command, str):
            command = shlex.split(command)
        p = subprocess.run(command, capture_output=True, text=True, timeout=20)
        return json.loads(p.stdout).get("ai", {}) if p.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}

def quota_level(runner, snapshot, model=None):
    item = snapshot.get(runner, snapshot.get(runner.split("-")[0], {}))
    if runner.split("-")[0] == "agy" and isinstance(item, dict) and isinstance(item.get("families"), dict):
        family = "gemini" if str(model or "").lower().startswith("gemini") else "claude_gpt"
        data = item["families"].get(family, {})
        return data.get("level", "unknown") if isinstance(data, dict) else "unknown"
    return item.get("level", "unknown") if isinstance(item, dict) else "unknown"

def pick(tier, tag=None, exclude_runner=None, snapshot=None):
    snapshot = quota() if snapshot is None else snapshot
    return next((m for m in models() if m["enabled"] and m["tier"] == tier
                 and (not tag or tag in m["tags"])
                 and m["runner"] != exclude_runner
                 and quota_level(m["runner"], snapshot, m.get("model")) not in ("critical", "exhausted")), None)

def model_for(task, snapshot=None):
    snapshot = quota() if snapshot is None else snapshot
    if task.get("alias"):
        model = next((m for m in models() if m["alias"] == task["alias"] and m["enabled"]), None)
        if model and quota_level(model["runner"], snapshot, model.get("model")) in ("critical", "exhausted"):
            return None
        return model
    return pick(task.get("tier", "cheap"), task.get("tag"), snapshot=snapshot)

def load_routes(root):
    return {"tiers": {t: {} for t in TIERS}, "max_width": policy()["max_width"],
            "max_retries": 2, "escalate": list(TIERS)}

def normalize(t):
    t.setdefault("title", t.get("goal", t.get("id", "")))
    t.setdefault("goal", t["title"])
    t.setdefault("tier", "cheap")
    t.setdefault("mode", "agent")
    t.setdefault("depends_on", t.get("deps", []))
    t["deps"] = t["depends_on"]
    t.setdefault("status", "todo")
    t.setdefault("retries", 0)
    t.setdefault("owner", "")
    t.setdefault("writes", [])
    t.setdefault("reads", [])
    t.setdefault("minutes", 0)
    t.setdefault("pane", None)
    t.setdefault("out", f".herdr-run/out/{t['id']}/report.md")
    return t

_base_analyze = analyze
def analyze(plan, routes):
    errors, warnings, waves, verdict = _base_analyze(plan, routes)
    tasks = plan["tasks"]
    owners = {}
    for t in tasks:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", t.get("id", "")):
            errors.append("任务 id 只能包含字母、数字、下划线和连字符")
        if t.get("mode") not in ("oneshot", "agent"):
            errors.append(t["id"] + ": mode 必须为 oneshot 或 agent")
        if t.get("minutes", 0) and t["minutes"] < 2 * policy()["overhead"]:
            warnings.append(t["id"] + ": 低于两倍交接开销，建议不拆")
        owner = t.get("owner")
        if owner and t.get("status") == "running":
            if owner in owners:
                errors.append(f"owner {owner} 同时负责 {owners[owner]} 与 {t['id']}")
            owners[owner] = t["id"]
    if waves and not errors:
        costs = {}
        byid = {t["id"]: t for t in tasks}
        for wave in waves:
            for tid in wave:
                costs[tid] = byid[tid].get("minutes", 0) + max(
                    [costs[d] for d in byid[tid].get("depends_on", [])] or [0])
        warnings.append("关键路径预计 %s 分钟；并发上限 %s" %
                        (max(costs.values(), default=0), routes["max_width"]))
    return errors, warnings, waves, verdict

def report_path(root, t):
    return P(root, "out", t["id"], "report.md")

_base_render = render_brief
def render_brief(root, plan, t):
    old = str(P(root, "out", f"{t['id']}.md"))
    return _base_render(root, plan, t).replace(old, str(report_path(root, t))) + (
        "\n仅向 leader 汇报，不与其他 worker 直接通信。\n")

def cmd_init(a):
    root = root_of(a)
    if P(root, "plan.json").exists():
        die("已有 plan.json，请继续现有任务")
    for d in ("brief", "out", "shared"):
        P(root, d).mkdir(parents=True, exist_ok=True)
    P(root, "shared", "contract.md").write_text("# 共享约定\n")
    P(root, "plan.json").write_text(json.dumps(
        {"run_id": secrets.token_hex(3), "goal": a.goal, "version": 0,
         "created_at": time.time(), "tasks": []}, ensure_ascii=False, indent=2))
    excl = root / ".git" / "info" / "exclude"
    if excl.parent.exists() and RUN not in (excl.read_text() if excl.exists() else ""):
        with excl.open("a") as f:
            f.write("\n" + RUN + "/\n")
    print("已初始化", P(root))

def cmd_load(a):
    root = root_of(a)
    new = json.loads(Path(a.file).read_text())
    new = new["tasks"] if isinstance(new, dict) else new
    with plan_tx(root) as plan:
        plan["tasks"].extend(normalize(t) for t in new)
        errors, warnings, _, _ = analyze(plan, load_routes(root))
        if errors:
            die("计划无效: " + "; ".join(errors))
    print("已载入", len(new), "个任务")
    for warning in warnings:
        print("警告:", warning)

def cmd_brief(a):
    root = root_of(a)
    plan = read_plan(root)
    task = find(plan, a.id)
    path = P(root, "brief", task["id"] + ".md")
    path.write_text(render_brief(root, plan, task))
    print(path)

def scan_runner(runner):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", runner):
        raise ValueError("非法 runner")
    code = ('import json,sys; d=json.load(open(sys.argv[1])); '
            'ms=d.get("models",[]) if isinstance(d,dict) else d; '
            'print("\\n".join(m["slug"] for m in ms '
            'if m.get("visibility","list")=="list" and m.get("slug")))')
    defaults = (
        "list_claude(){ printf '%s\\n' sonnet opus haiku; }; "
        "list_pi(){ pi --list-models | awk 'NF>=2 && tolower($1)!=\"provider\" && $1 ~ /^[A-Za-z0-9._-]+$/ {print $1\"/\"$2}'; }; "
        "list_agy(){ agy models | awk -F '\\t' 'NF>=2 && $1 !~ /[[:space:]]/ {print $1}'; }; "
        'list_codex(){ local f="${CODEX_HOME:-$HOME/.codex}/models_cache.json"; '
        '[ -f "$f" ] || return 0; python3 -c ' + shlex.quote(code) + ' "$f"; }; ')
    local_runners = get_local_runners()
    script = defaults + ("source " + shlex.quote(str(local_runners)) + "; " if local_runners.exists() else "")
    script += "list_" + runner
    rc, out, err = sh(["bash", "-o", "pipefail", "-c", script], timeout=30)
    return rc, sorted(set(line.strip() for line in out.splitlines() if line.strip())), err.strip()

def print_setup_usage():
    print("""用法:
  hl models setup scan-clis
  hl models setup scan-models <runner>
  hl models setup apply --entry "alias|runner|model|thinking|tier" [--entry ...] [--force]""")

def cmd_setup_apply(a):
    entries = getattr(a, "entries", None) or []
    if not entries:
        die("未指定 --entry")
    dest = get_model_file()
    force = getattr(a, "force", False)
    if dest.exists() and not force:
        die(f"{dest} 已存在；若要覆盖请加 --force")

    parsed = []
    for entry in entries:
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) != 5:
            die(f"entry 格式错误: {entry}（应为 alias|runner|model|thinking|tier）")
        alias, runner, model, thinking, tier = parts
        if tier not in TIERS:
            die(f"tier 只能是 cheap/mid/strong: {tier}")
        if thinking not in ("off", "low", "medium", "high"):
            die(f"thinking 只能是 off/low/medium/high: {thinking}")
        parsed.append((alias, runner, model, thinking, tier))

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and force:
        ts = time.strftime("%Y%m%d%H%M%S")
        bak = dest.with_name(f"{dest.name}.bak.{ts}")
        if bak.exists():
            bak = dest.with_name(f"{dest.name}.bak.{ts}_{int(time.time() * 1000) % 1000:03d}")
        shutil.copy2(dest, bak)

    lines = ["# alias|runner|model|default_thinking|enabled|when_to_use|tier|rank"]
    for rank, (alias, runner, model, thinking, tier) in enumerate(parsed, 1):
        when_to_use = TIER_WHEN[tier]
        lines.append(f"{alias}|{runner}|{model}|{thinking}|yes|{when_to_use}|{tier}|{rank}")
    content = "\n".join(lines) + "\n"

    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=dest.parent, prefix="models.conf.tmp.")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.chmod(0o600)
        os.replace(tmp_path, dest)
        dest.chmod(0o600)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    print(json.dumps(models(), ensure_ascii=False, indent=2))

def run_setup_wizard():
    print("== herdr-lead 模型配置向导 ==")
    clis = scan_clis()
    if not clis:
        print("未在 PATH 或 runners.local.sh 中发现可用 runner。")
        return
    print("发现可用 runner:")
    for i, c in enumerate(clis, 1):
        print(f"  {i}. {c}")
    try:
        choice = input(f"启用哪些 runner？(空格分隔，直接回车=全部已安装 [{' '.join(clis)}]): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    selected_runners = []
    if not choice:
        selected_runners = clis
    else:
        for part in choice.split():
            if part.isdigit() and 1 <= int(part) <= len(clis):
                selected_runners.append(clis[int(part) - 1])
            elif part in clis:
                selected_runners.append(part)
            else:
                print(f"忽略未知 runner: {part}")
    if not selected_runners:
        print("未选择任何有效 runner。")
        return

    candidates = []
    for runner in selected_runners:
        rc, found, err = scan_runner(runner)
        if rc != 0 or not found:
            print(f"[{runner}] 无法扫描候选模型: {err or '无模型'}")
            continue
        for m in found:
            t = guess_tier(m)
            candidates.append({"runner": runner, "model": m, "tier": t})

    if not candidates:
        print("未找到任何候选模型。")
        return

    print(f"\n找到 {len(candidates)} 个候选模型:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. [{c['runner']}] {c['model']} (建议级别: {c['tier']})")

    try:
        picks = input("\n请按偏好顺序输入要启用的编号 (空格分隔，直接回车=全部启用): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    chosen = []
    if not picks:
        chosen = list(candidates)
    else:
        for p in picks.split():
            if p.isdigit() and 1 <= int(p) <= len(candidates):
                chosen.append(candidates[int(p) - 1])
            else:
                print(f"忽略无效编号: {p}")

    if not chosen:
        print("未选择任何模型。")
        return

    entries = []
    for item in chosen:
        r = item["runner"]
        m = item["model"]
        suggested_tier = item["tier"]
        clean_name = re.sub(r"[^a-z0-9]+", "-", m.split("/")[-1].lower()).strip("-")
        suggested_alias = f"{r}-{clean_name}"
        try:
            tier_in = input(f"[{suggested_alias}] 级别 [{suggested_tier}] (cheap/mid/strong，回车保持): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        tier = tier_in if tier_in in TIERS else suggested_tier
        thinking = DEFAULT_THINKING.get(tier, "off")
        entries.append(f"{suggested_alias}|{r}|{m}|{thinking}|{tier}")

    force = False
    dest = get_model_file()
    if dest.exists():
        try:
            f_in = input(f"\n{dest} 已存在，是否覆盖并备份？ (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if f_in in ("y", "yes"):
            force = True
        else:
            print("取消写入。")
            return

    args = SimpleNamespace(entries=entries, force=force)
    cmd_setup_apply(args)
    print("\n提示: setup 结束。可运行 hl models test <alias> 测试模型可用性。")

def cmd_setup(a):
    action = getattr(a, "setup_action", None)
    if action == "scan-clis":
        print(json.dumps(scan_clis(), ensure_ascii=False, indent=2))
    elif action == "scan-models":
        rc, found, err = scan_runner(a.runner)
        if rc != 0 and not found:
            die(f"扫描 {a.runner} 失败: {err or '未知错误'}")
        results = [{"model": m, "tier": guess_tier(m)} for m in found]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif action == "apply":
        cmd_setup_apply(a)
    else:
        if not sys.stdin.isatty():
            print_setup_usage()
            sys.exit(2)
        run_setup_wizard()

def cmd_models(a):
    if a.action == "setup":
        cmd_setup(a)
    elif a.action == "list":
        print(json.dumps(models(), ensure_ascii=False, indent=2))
    elif a.action == "pick":
        model = pick(a.tier, a.tag)
        if not model:
            die("没有可用模型")
        print(json.dumps(model, ensure_ascii=False))
    elif a.action == "scan":
        runners = sorted({m["runner"] for m in models()})
        if not runners:
            die("未找到登记的 runner")
        for runner in runners:
            rc, found, err = scan_runner(runner)
            print(json.dumps({"runner": runner, "models": found, "ok": rc == 0,
                              "error": err if rc else ""}, ensure_ascii=False))
    elif a.action == "test":
        m = next((m for m in models() if m["alias"] == a.alias and m["enabled"]), None)
        if not m or m["tier"] != "cheap":
            die("测试只允许已启用的 cheap 模型")
        if quota_level(m["runner"], quota(), m.get("model")) in ("critical", "exhausted"):
            die("模型额度不足")
        command = runner_command(m, "/dev/stdin")
        print_runner(m, "/dev/stdin", "Reply with OK only.\n")
        if not consented(m["runner"]):
            die("runner 未授权；确认实际命令后运行 hl consent " + m["runner"])
        try:
            result = subprocess.run(command, shell=True, input="Reply with OK only.\n",
                                    capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            die("模型调用超时（60 秒）")
        if result.returncode or not result.stdout.strip():
            die("模型调用失败: " + result.stderr[-500:])
        print(result.stdout.strip())

def cmd_consent(a):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.runner):
        die("runner 名称非法")
    consent_f = get_consent_file()
    data = json.loads(consent_f.read_text()) if consent_f.exists() else {}
    data[a.runner] = {"at": time.time(), "consent": True}
    consent_f.parent.mkdir(parents=True, exist_ok=True)
    consent_f.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    consent_f.chmod(0o600)
    print("已记录", a.runner, "的 runner 授权")

def git_changed(root):
    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                               capture_output=True, text=True)
        if probe.returncode:
            return None
        diff = subprocess.run(["git", "diff", "--name-only", "--relative", "-z", "HEAD", "--", "."],
                              cwd=root, capture_output=True)
        if diff.returncode:
            return None
    except OSError:
        return None
    return sorted(os.fsdecode(raw) for raw in diff.stdout.split(b"\0") if raw)

def snapshot_files(root):
    files = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", RUN)]
        for name in names:
            path = Path(base) / name
            try:
                st = path.lstat()
                files[str(path.relative_to(root))] = [st.st_mtime_ns, st.st_size]
            except FileNotFoundError:
                pass
    files["\0git_diff"] = git_changed(root)
    return files

def changes_since(root, before):
    after = snapshot_files(root)
    keys = (set(before) | set(after)) - {"\0git_diff"}
    changed = {p for p in keys if before.get(p) != after.get(p)}
    if before.get("\0git_diff") is not None and after.get("\0git_diff") is not None:
        changed.update(set(after["\0git_diff"]) - set(before["\0git_diff"]))
    return sorted(changed)

def save_plan(root, plan):
    tmp = P(root, "plan.json.tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    os.replace(tmp, P(root, "plan.json"))

def pane_id(result):
    obj = json.loads(result).get("result", {})
    pane = obj.get("pane", {})
    tab = obj.get("tab") or {}
    return pane.get("pane_id") or pane.get("id"), pane.get("tab_id") or tab.get("tab_id") or obj.get("tab_id")

def runner_command(model, brief, preview=False):
    runner = model["runner"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", runner):
        raise ValueError("非法 runner")
    think = model["thinking"]
    env = {"HD_MODEL": model["model"], "HD_THINK": think,
           "HD_EFFORT": "low" if think == "off" else think, "HD_PROMPT_FILE": str(brief)}
    assignments = " ".join(k + "=" + shlex.quote(v) for k, v in env.items())
    defaults = (
        'run_claude(){ claude -p --model "$HD_MODEL" --effort "$HD_EFFORT" --dangerously-skip-permissions "$(cat "$HD_PROMPT_FILE")"; }; '
        'run_codex(){ codex exec --skip-git-repo-check -m "$HD_MODEL" -c "model_reasoning_effort=$HD_EFFORT" --dangerously-bypass-approvals-and-sandbox "$(cat "$HD_PROMPT_FILE")"; }; '
        'run_agy(){ agy -p "$(cat "$HD_PROMPT_FILE")" --model "$HD_MODEL" --effort "$HD_EFFORT" --dangerously-skip-permissions; }; '
        'run_pi(){ pi -p --no-session --model "$HD_MODEL" --thinking "$HD_THINK" "@$HD_PROMPT_FILE"; }; ')
    local_runners = get_local_runners()
    script = defaults + ("source " + shlex.quote(str(local_runners)) + "; " if local_runners.exists() else "")
    if preview:
        script += ('show(){ printf "__HL_COMMAND__"; printf " %q" "$@"; printf "\\n"; }; '
                   'command(){ show "$@"; }; python3(){ show python3 "$@"; }; '
                   'claude(){ show claude "$@"; }; codex(){ show codex "$@"; }; '
                   'agy(){ show agy "$@"; }; pi(){ show pi "$@"; }; '
                   'cat(){ if [[ "$1" == "$HD_PROMPT_FILE" ]]; then /bin/cat /dev/stdin; '
                   'else /bin/cat "$@"; fi; }; ')
    script += assignments + " run_" + runner
    return "bash -lc " + shlex.quote(script)

def print_runner(model, brief, prompt=""):
    command = runner_command(model, brief)
    print("runner 调用:", command)
    # Preview is sandboxed: runner functions can expand but cannot start models or write files.
    profile = ('(version 1)(allow default)(deny network*)(deny file-write*)'
               '(deny signal)(deny process-exec)'
               '(allow process-exec (literal "/bin/bash") (literal "/bin/cat"))')
    try:
        result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile] +
                                shlex.split(runner_command(model, brief, preview=True)),
                                input=prompt, text=True, capture_output=True, timeout=10,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        die("无法安全展开 runner: " + str(exc))
    lines = [line for line in result.stdout.splitlines() if line.startswith("__HL_COMMAND__")]
    if result.returncode or len(lines) != 1:
        die("无法安全展开 runner: " + result.stderr[-500:])
    print("最终执行命令:", lines[0].removeprefix("__HL_COMMAND__").strip())

def start_agent(command):
    deadline = time.monotonic() + 30
    delay = 0.25
    while True:
        rc, out, err = sh(command, timeout=35)
        if rc == 0 or "agent_pane_busy" not in (out + err):
            return rc, out, err
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return rc, out, err
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 2)


def task_commands(root, task, model):
    brief = P(root, "brief", task["id"] + ".md")
    cwd = str(root)
    name = "hl-" + task["id"].lower().replace("_", "-")
    kind = model["runner"].split("-")[0]
    if task["mode"] == "agent":
        return [["herdr", "pane", "split", "--current", "--direction", "right", "--cwd", cwd, "--no-focus"],
                ["herdr", "agent", "start", name, "--kind", kind, "--pane", "<pane>", "--", "--model", model["model"]],
                ["herdr", "agent", "prompt", name, f"读取 {brief} 并严格执行；完成后按简报写回报。"]]
    marker = report_path(root, task).parent / ("exit-" + str(task.get("marker_ns", "<dispatch>")))
    run = runner_command(model, brief)
    command = "(" + run + "); hl_rc=$?; printf '%s\\n' \"$hl_rc\" > " + shlex.quote(str(marker))
    return [["herdr", "pane", "split", "--current", "--direction", "right", "--cwd", cwd, "--no-focus"],
            ["herdr", "pane", "run", "<pane>", command]]

def consented(runner):
    try:
        consent_f = get_consent_file()
        return json.loads(consent_f.read_text()).get(runner, {}).get("consent") is True
    except (OSError, ValueError, AttributeError):
        return False

def trust_dialog(screen):
    pattern = r"quick safety check|trust this (?:folder|workspace|directory)|(?:do you|would you) trust|i trust this (?:folder|workspace|directory)|approve (?:this|the) (?:folder|workspace|directory|request|action)|permission(?:s)? required|allow access to (?:this|the) (?:folder|workspace|directory)|do you want to allow|(?:trust|approve|permission|allow|authorize|authorise).{0,80}(?:folder|workspace|directory|access|action|request|continue)|(?:folder|workspace|directory).{0,80}(?:trust|approve|permission|allow|access)"
    return bool(re.search(pattern, screen, re.I | re.S))


def verify_target(pane, model, started=False):
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        return "缺少当前 workspace"
    rc, output, _ = sh(["herdr", "pane", "list", "--workspace", workspace])
    try:
        panes = json.loads(output)["result"]["panes"] if rc == 0 else []
        matches = [p for p in panes if p.get("pane_id") == pane]
    except (ValueError, KeyError, TypeError, AttributeError):
        matches = []
    if len(matches) != 1 or matches[0].get("agent") != model["runner"]:
        return "pane 身份无法核实"
    status = matches[0].get("agent_status")
    rc, screen, _ = sh(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "28"])
    if rc:
        return "无法读取目标屏幕"
    screen = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen)
    if trust_dialog(screen):
        return "目标目录需人工确认信任"
    if status not in ("idle", "done"):
        return "agent 未空闲"
    quota_pattern = re.compile(r"\b429\b|usage limit|rate limits?|out of quota|quota (?:reached|limit|exceeded|exhausted)|individual quota|limit reached|额度(?:不足|用完|耗尽)", re.I)
    if any("approaching" not in line.lower() and quota_pattern.search(line) for line in screen.splitlines()):
        return "屏幕显示额度耗尽或限制"
    lines = screen.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    recent = lines[-15:]
    def model_key(value):
        value = value.strip().lower()
        value = re.sub(r"^(?:🧠\s*)?claude-", "", value)
        value = re.sub(r"[\s·_-]+(?:low|medium|high|xhigh)\s*$", "", value)
        return re.sub(r"[^a-z0-9]", "", value)
    expected_model = model_key(model["model"])
    family = re.compile(r"gpt|claude|opus|sonnet|haiku|fable|gemini|glm|qwen|kimi|deepseek|grok|o[1-9]", re.I)
    if not expected_model:
        return "缺少明确模型字段"
    occurrences = [(line_index, match) for line_index, line in enumerate(recent)
                   for match in family.finditer(line)]
    if not occurrences:
        return "缺少明确模型字段"
    line_index, match = occurrences[-1]
    current_model = model_key(recent[line_index][match.start():])
    if current_model != expected_model:
        return "当前显示模型与目标模型不匹配"
    prompts = [(i, m.group(1).strip()) for i, line in enumerate(lines)
               if (m := re.match(r"^\s*[│┃|]?\s*[❯›>]\s?(.*)$", line))]
    if not prompts or prompts[-1][0] < len(lines) - 12:
        return "" if started else "无法确认输入框"
    draft = prompts[-1][1]
    placeholder = (draft in ("Ask Codex to do anything", "Type your message", "Use /skills")
                   or re.fullmatch(r'Try\s+["“].*["”]', draft))
    if draft and not placeholder:
        return "输入框有未提交内容"
    return ""

def render_plan(plan, routes=None, snapshot=None, actual=False):
    routes = routes or load_routes(None)
    snapshot = quota() if snapshot is None else snapshot
    errs, warnings, waves, _ = analyze(plan, routes)
    catalog = models()
    byid = {t["id"]: t for t in plan["tasks"]}
    width = max(1, min(int(routes.get("max_width", 3)), 3))
    batches = []
    for wave in waves:
        for start in range(0, len(wave), width):
            batches.append(wave[start:start + width])
    batch_of = {tid: i + 1 for i, batch in enumerate(batches) for tid in batch}
    if not batches:
        batches = [[t["id"]] for t in plan["tasks"]]
        batch_of = {tid: i + 1 for i, batch in enumerate(batches) for tid in batch}

    rows, missing = [], []
    for t in plan["tasks"]:
        chosen = None
        reason = ""
        if actual and t.get("alias"):
            chosen = next((m for m in catalog if m["alias"] == t["alias"]), None)
            reason = "实际运行 alias" if chosen else "实际运行 alias 未在登记表找到"
        elif t.get("alias"):
            chosen = next((m for m in catalog if m["alias"] == t["alias"] and m["enabled"]), None)
            reason = "指定 alias" if chosen else "指定 alias 不存在或未启用"
            if chosen and quota_level(chosen["runner"], snapshot, chosen.get("model")) in ("critical", "exhausted"):
                reason += "；额度 " + quota_level(chosen["runner"], snapshot, chosen.get("model"))
                chosen = None
        else:
            candidates = [m for m in catalog if m["enabled"] and m["tier"] == t.get("tier", "cheap")
                          and (not t.get("tag") or t["tag"] in m.get("tags", []))]
            skipped = [m["alias"] for m in candidates
                       if quota_level(m["runner"], snapshot, m.get("model")) in ("critical", "exhausted")]
            chosen = model_for(t, snapshot)
            if chosen:
                reason = "tier=%s%s rank=%s；额度 ok" % (
                    chosen["tier"], " tag=" + t["tag"] if t.get("tag") else "",
                    chosen.get("rank", "?"))
            else:
                reason = "tier=%s%s；无可用模型" % (
                    t.get("tier", "cheap"), " tag=" + t["tag"] if t.get("tag") else "")
            if skipped:
                reason += "；额度跳过 " + ", ".join(skipped)
        if not chosen:
            missing.append(t["id"] + "（" + reason + "）")
        model_text = (chosen["alias"] + " / " + chosen["runner"] + " / " + chosen["model"]) if chosen else "无可用模型"
        deps = ", ".join(t.get("deps", [])) or "-"
        writes = ", ".join(t.get("writes", [])) or "-"
        rows.append("| %s | %s %s | %s | %s | %s | %s | %s | %s |" % (
            batch_of.get(t["id"], "-"), t["id"], t.get("title", ""), t.get("mode", ""),
            model_text, reason, writes, deps, t.get("minutes", 0)))
    total = sum(int(t.get("minutes", 0) or 0) for t in plan["tasks"])
    costs = {}
    if waves:
        for wave in waves:
            for tid in wave:
                task = byid[tid]
                costs[tid] = int(task.get("minutes", 0) or 0) + max(
                    [costs.get(d, 0) for d in task.get("deps", [])] or [0])
    critical = max(costs.values(), default=0)
    active_width = max((len(batch) for batch in batches), default=1)
    workers = max(1, min(width, active_width))
    overhead = int(policy().get("overhead", 4))
    integration = 5
    parallel = max(critical, (total + workers - 1) // workers) + workers * overhead + integration
    ready = [t for t in plan["tasks"] if t.get("status") == "todo"
             and all(byid[d].get("status") == "done" for d in t.get("deps", []))]
    ready_models = [model_for(t, snapshot) for t in ready]
    needs_confirm = len(ready) >= 3 or any(m and m["tier"] == "strong" for m in ready_models)
    reason = "≥3 worker" if len(ready) >= 3 else ("strong 档" if any(m and m["tier"] == "strong" for m in ready_models) else "无")
    lines = ["## 编排方案：" + plan.get("goal", ""),
             "| 批次 | 任务 | 模式 | 模型(alias/runner/model) | 选择理由 | 写入范围 | 依赖 | 预计分钟 |",
             "|---|---|---|---|---|---|---|---|"] + rows
    savings = ("节省约 %s 分钟" % (total - parallel)) if parallel < total else "并行不省时间：建议串行，或由 leader 自己做"
    lines += ["", "- 并行：%s 批，最大宽度 %s，并发上限 %s" % (len(batches), active_width, width),
              "- 预计耗时：串行 %s 分钟，并行约 %s 分钟（max(关键路径 %s, 总工作量 / %s) + %s×交接 %s + 整合 %s），%s" %
              (total, parallel, critical, workers, workers, overhead, integration, savings),
              "- 需要用户确认：%s（原因：%s）" % ("是" if needs_confirm else "否", reason)]
    split_warnings = [w for w in warnings if "建议不拆" in w]
    lines.append("- 不建议拆的任务：" + ("；".join(split_warnings) if split_warnings else "无"))
    lines.append("- 无可用模型的任务：" + ("；".join(missing) if missing else "无"))
    if errs:
        lines.append("- 计划错误：" + "；".join(errs))
    return "\n".join(lines) + "\n"


def cmd_plan(a):
    root = root_of(a)
    plan = read_plan(root)
    table = render_plan(plan, load_routes(root))
    print(table, end="")
    if getattr(a, "md", False):
        P(root, "plan.md").write_text(table)


def run_wave(a):
    root = root_of(a)
    if os.environ.get("HERDR_ENV") != "1" and not a.dry_run:
        rc, out, _ = sh(["herdr", "status"], timeout=10)
        if rc or not re.search(r"status:\s*running", out):
            die("Herdr 连接不可用，禁止控制 pane")
    routes = load_routes(root)
    plan = read_plan(root)
    errs, warns, _, _ = analyze(plan, routes)
    if errs:
        die("计划无效: " + "; ".join(errs))
    snap = quota()
    selected = []
    byid = {t["id"]: t for t in plan["tasks"]}
    for t in plan["tasks"]:
        if t["status"] != "todo" or any(byid[d]["status"] != "done" for d in t["deps"]):
            continue
        model = model_for(t, snap)
        if not model:
            die(t["id"] + ": 无可用模型或额度")
        if t["mode"] == "agent" and model["runner"] not in ("claude", "codex", "agy"):
            die(t["id"] + ": agent 模式无法核实账号专用 runner；禁止分屏")
        selected.append((t, model))
        print(t["id"], model["alias"], model["runner"], model["model"])
        for command in task_commands(root, t, model):
            print("  ", shlex.join(command))
        if t["mode"] == "oneshot":
            print_runner(model, P(root, "brief", t["id"] + ".md"), render_brief(root, plan, t))
    if not selected and not any(t["status"] == "running" and t.get("submission") == "submitted" for t in plan["tasks"]):
        print("没有待派任务")
        return
    needs_confirm = len(selected) >= 3 or any(m["tier"] == "strong" for t, m in selected)
    if needs_confirm and not a.yes:
        print(render_plan(plan, routes, snap), end="")
        print("需要 --yes 确认")
        raise SystemExit(3)
    for t, m in selected:
        if t["mode"] == "oneshot" and not consented(m["runner"]):
            die(f"runner {m['runner']} 未授权；确认上列实际命令后运行 hl consent {m['runner']}")
    if a.dry_run:
        return
    # Lock covers state changes and the first submission, so a second leader cannot double-send.
    with plan_tx(root) as plan:
        byid = {t["id"]: t for t in plan["tasks"]}
        running = sum(t["status"] == "running" for t in plan["tasks"])
        for chosen, model in selected:
            task = byid[chosen["id"]]
            if running >= routes["max_width"]:
                break
            if task["status"] != "todo" or any(byid[d]["status"] != "done" for d in task["deps"]):
                continue
            task["before"] = snapshot_files(root)
            task["marker_ns"] = time.time_ns()
            task.update(status="running", submission="preparing", runner=model["runner"],
                        alias=model["alias"], tier=model["tier"], cwd=str(root))
            brief = P(root, "brief", task["id"] + ".md")
            report_path(root, task).parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(render_brief(root, plan, task))
            save_plan(root, plan)
            commands = task_commands(root, task, model)
            rc, out, err = sh(commands[0])
            if rc:
                task.update(status="blocked", reason="分屏失败: " + err, submission="uncertain")
                save_plan(root, plan)
                continue
            try:
                pane, tab = pane_id(out)
                if not pane or not tab:
                    raise ValueError("分屏结果缺少 pane_id 或 tab_id")
            except (ValueError, TypeError, AttributeError) as e:
                task.update(status="blocked", reason=str(e), submission="uncertain")
                save_plan(root, plan)
                continue
            task["pane"] = pane
            label = task["title"].strip() + "·" + ai_name(model["runner"])
            for rename in (["herdr", "pane", "rename", pane, label],
                           ["herdr", "tab", "rename", tab, label]):
                rc, _, err = sh(rename)
                if rc:
                    break
            if rc:
                sh(["herdr", "pane", "close", pane])
                task.update(status="blocked", reason="命名失败: " + err, submission="uncertain")
                save_plan(root, plan)
                continue
            if task["mode"] == "agent":
                # Agent mode can only identify the canonical executable, not a runner alias
                # with an account-specific wrapper.
                if model["runner"] != model["runner"].split("-")[0]:
                    sh(["herdr", "pane", "close", pane])
                    task.update(status="blocked", reason="账号 runner 无法在 agent 模式核实身份", submission="uncertain")
                    save_plan(root, plan)
                    continue
                start = [pane if x == "<pane>" else x for x in commands[1]]
                rc, start_out, err = start_agent(start)
                if rc:
                    start_error = str(start_out) + "\n" + str(err)
                    if (re.search(r"\bagent_not_ready\b", start_error, re.I)
                            and re.search(r"blocked during startup", start_error, re.I)):
                        _, startup_screen, _ = sh(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "28"])
                        startup_screen = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", startup_screen)
                        if trust_dialog(startup_screen):
                            task.update(status="blocked", reason=f"目标目录需人工确认信任；确认后 hl reset {task['id']} --stopped", submission="uncertain")
                            save_plan(root, plan)
                            continue
                    sh(["herdr", "pane", "close", pane])
                    task.update(status="blocked", reason="启动失败: " + start_error, submission="uncertain")
                    save_plan(root, plan)
                    continue
                name = commands[2][3]
                reason = verify_target(pane, model, started=True)
                if reason:
                    if reason == "目标目录需人工确认信任":
                        task.update(status="blocked", reason=f"目标目录需人工确认信任；确认后 hl reset {task['id']} --stopped", submission="uncertain")
                    else:
                        sh(["herdr", "pane", "close", pane])
                        task.update(status="blocked", reason=reason, submission="uncertain")
                    save_plan(root, plan)
                    continue
                task["owner"] = name
                task["submission"] = "uncertain"
                save_plan(root, plan)
                rc, prompt_out, err = sh(commands[2])
            else:
                task["owner"] = pane
                task["submission"] = "uncertain"
                save_plan(root, plan)
                command = [pane if x == "<pane>" else x for x in commands[1]]
                print("实际提交:", shlex.join(command), flush=True)
                rc, _, err = sh(command)
            if rc:
                prompt_error = str(prompt_out) + "\n" + str(err)
                if re.search(r"\bagent_not_ready\b", prompt_error, re.I):
                    task.update(status="blocked", reason=f"agent_not_ready：目标 agent 已不在 pane 前台，未重发；确认后 hl reset {task['id']} --stopped", submission="uncertain")
                else:
                    task.update(status="blocked", reason="提交结果不确定: " + str(err))
            else:
                task.update(submission="submitted", started_at=time.time())
                running += 1
            save_plan(root, plan)
        running_tasks = [t for t in plan["tasks"] if t["status"] == "running"]
        for t in running_tasks:
            t["parallel_writes"] = [w for other in running_tasks if other["id"] != t["id"] for w in other.get("writes", [])]
        save_plan(root, plan)
    # Wait once per worker. A timeout is uncertain, not permission to send again.
    for task in read_plan(root)["tasks"]:
        if task["status"] != "running" or task.get("submission") != "submitted":
            continue
        if task["mode"] == "agent":
            rc, output, err = sh(["herdr", "agent", "wait", task["owner"], "--until", "done",
                                  "--until", "blocked", "--timeout", str(a.timeout * 1000)],
                                 timeout=a.timeout + 30)
            if rc or "blocked" in output.lower():
                sh(["herdr", "agent", "read", task["owner"], "--lines", "20"])
                with plan_tx(root) as plan:
                    t = find(plan, task["id"])
                    t.update(status="blocked", reason="等待超时或状态不确定；不得重发", submission="uncertain")
                continue
        else:
            marker = report_path(root, task).parent / ("exit-" + str(task["marker_ns"]))
            deadline = time.monotonic() + a.timeout
            while not marker.is_file() and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if not marker.is_file():
                with plan_tx(root) as plan:
                    find(plan, task["id"]).update(status="blocked", reason="一次性任务等待超时；不得重发", submission="uncertain")
                continue
            if marker.read_text().strip() != "0":
                with plan_tx(root) as plan:
                    find(plan, task["id"]).update(status="failed", reason="一次性命令失败: " + marker.read_text().strip(), submission="stopped")
                continue
        with plan_tx(root) as plan:
            t = find(plan, task["id"])
            result = collect_task(root, plan, t)
            if result == "pending":
                t.update(status="blocked", reason="回报尚未完整；不得重发", submission="uncertain")
            elif result == "done" and t.get("pane"):
                rc, _, err = sh(["herdr", "pane", "close", t["pane"]])
                if rc:
                    t["warnings"].append("pane 关闭失败: " + err)

def cmd_run(a):
    root = root_of(a)
    stale = []
    with plan_tx(root) as plan:
        for t in plan["tasks"]:
            if t["status"] == "running" and t.get("submission") in ("preparing", "uncertain"):
                t["status"] = "stale"
                t["reason"] = "派工状态中断，需人工确认旧 worker 已停写"
                stale.append(t["id"])
    for tid in stale:
        print(f"{tid}: 需确认停写后 hl retry/takeover {tid} --stopped")
    while True:
        before = [(t["id"], t["status"]) for t in read_plan(root_of(a))["tasks"]]
        run_wave(a)
        if a.dry_run:
            return
        after = [(t["id"], t["status"]) for t in read_plan(root_of(a))["tasks"]]
        if before == after or not any(status == "todo" for _, status in after):
            return

def do_retry(root, routes, t):
    n = t.get("retries", 0) + 1
    if n > 2:
        index = TIERS.index(t["tier"])
        if index == len(TIERS) - 1:
            return False
        t["tier"] = TIERS[index + 1]
        t["retries"] = 0
        t.pop("alias", None)
        t["escalated"] = True
    else:
        t["retries"] = n
    t["last_failure"] = t.get("reason", "")
    t["status"] = "todo"
    t["owner"] = ""
    return True

def cmd_retry(a):
    with plan_tx(root_of(a)) as plan:
        t = find(plan, a.id)
        if t["status"] not in ("failed", "blocked", "stale"):
            die("只有 failed、blocked 或 stale 可以重试")
        if t.get("submission") in ("submitted", "uncertain") and not getattr(a, "stopped", False):
            die("旧 worker 状态不确定，先确认停写并使用 takeover --stopped")
        if not do_retry(root_of(a), load_routes(root_of(a)), t):
            die("最高档重试已用尽，请 leader 接管")
        print(t["id"], t["tier"], "重试", t["retries"])

def cmd_reset(a):
    if not a.stopped:
        die("重置前必须确认旧 worker 已停写并指定 --stopped")
    with plan_tx(root_of(a)) as plan:
        t = find(plan, a.id)
        if t["status"] == "running":
            die("运行中的任务不能重置")
        t.update(status="todo", retries=0, owner="", submission="stopped", reason="")
        t.pop("last_failure", None)
        print(t["id"], "已重置为 todo；retries=0")


def takeover_choice(task, catalog, snap):
    available = [m for m in catalog if m["enabled"] and m["alias"] != task.get("alias")
                 and quota_level(m["runner"], snap, m.get("model")) not in ("critical", "exhausted")]
    same = [m for m in available if m["runner"] == task.get("runner")]
    other = [m for m in available if m["tier"] == task["tier"] and m["runner"] != task.get("runner")]
    return (same or other or [None])[0]

def cmd_takeover(a):
    if not a.stopped:
        die("接管前必须确认旧 worker 停写并指定 --stopped")
    with plan_tx(root_of(a)) as plan:
        t = find(plan, a.id)
        if t["status"] not in ("running", "blocked", "failed", "stale", "needs_review"):
            die("只有运行中、受阻或失败的任务可以接管")
        choice = takeover_choice(t, models(), quota())
        if a.to:
            choice = next((m for m in models() if m["alias"] == a.to and m["enabled"]
                           and quota_level(m["runner"], quota(), m.get("model")) not in ("critical", "exhausted")), None)
            if not choice:
                die("指定模型不可用")
        if a.leader or not choice:
            t.update(status="blocked", owner="leader", submission="stopped", reason="leader 接管")
            print("leader 接管")
        else:
            t.update(status="todo", owner="", alias=choice["alias"], tier=choice["tier"], submission="stopped")
            print("接管模型:", choice["alias"])

def cmd_review(a):
    root = root_of(a)
    with plan_tx(root) as plan:
        base = find(plan, a.id)
        if base["status"] != "done":
            die("任务未完成")
        runner = base.get("runner")
        if not runner and base.get("alias"):
            original = next((m for m in models() if m["alias"] == base["alias"]), None)
            runner = original["runner"] if original else None
        if not runner:
            die("无法核实原执行 runner，禁止创建独立复核")
        reviewer = pick(base["tier"], exclude_runner=runner)
        if not reviewer:
            die("没有不同 runner 的可用复核模型")
        rid = "R-" + a.id
        if any(t["id"] == rid for t in plan["tasks"]):
            die("复核任务已存在")
        task = normalize({"id": rid, "goal": "独立复核 " + a.id, "alias": reviewer["alias"],
                          "tier": reviewer["tier"], "reads": base.get("writes", []),
                          "writes": [], "deps": [a.id], "acceptance": base.get("acceptance", [])})
        plan["tasks"].append(task)
    print("已加入独立复核", rid)

def cmd_digest(a):
    root = root_of(a)
    plan = read_plan(root)
    lines = [render_plan(plan, load_routes(root), actual=True).rstrip(),
             "", "# 交付摘要", "目标：" + plan.get("goal", "")]
    for t in plan["tasks"]:
        lines.append("\n## " + t["id"] + " " + t["status"])
        lines.extend(t.get("summary", "").splitlines()[:8])
        lines.append("回报：" + str(report_path(root, t)))
        if t.get("reason"):
            lines.append("原因：" + t["reason"])
    text = "\n".join(lines) + "\n"
    P(root, "digest.md").write_text(text)
    print(text)

def cmd_doctor(a):
    connected = False
    if shutil.which("herdr"):
        rc, out, _ = sh(["herdr", "status"], timeout=10)
        connected = rc == 0 and bool(re.search(r"status:\s*running", out))
    checks = {
        "Herdr 环境或连接": os.environ.get("HERDR_ENV") == "1" or connected,
        "herdr": bool(shutil.which("herdr")),
        "models.conf": get_model_file().is_file(),
        "quota_local.py": Path(__file__).with_name("quota_local.py").is_file(),
    }
    try:
        checks["models.conf 可解析"] = bool(models())
    except (ValueError, OSError):
        checks["models.conf 可解析"] = False
    for label, ok in checks.items():
        print(("OK " if ok else "FAIL ") + label)
    if not all(checks.values()):
        raise SystemExit(1)

def main():
    ap = argparse.ArgumentParser(prog="hl")
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--goal", required=True); s.set_defaults(f=cmd_init)
    s = sub.add_parser("load"); s.add_argument("file"); s.set_defaults(f=cmd_load)
    s = sub.add_parser("check"); s.set_defaults(f=cmd_check)
    s = sub.add_parser("plan"); s.add_argument("--md", action="store_true"); s.set_defaults(f=cmd_plan)
    s = sub.add_parser("brief"); s.add_argument("id"); s.set_defaults(f=cmd_brief)
    s = sub.add_parser("models"); ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list"); ss.add_parser("scan")
    x = ss.add_parser("test"); x.add_argument("alias")
    x = ss.add_parser("pick"); x.add_argument("--tier", required=True, choices=TIERS); x.add_argument("--tag")
    p_setup = ss.add_parser("setup")
    sss = p_setup.add_subparsers(dest="setup_action")
    sss.add_parser("scan-clis")
    x = sss.add_parser("scan-models"); x.add_argument("runner")
    x = sss.add_parser("apply")
    x.add_argument("--entry", action="append", required=True, dest="entries",
                   help="alias|runner|model|thinking|tier")
    x.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_models)
    s = sub.add_parser("consent"); s.add_argument("runner"); s.set_defaults(f=cmd_consent)
    s = sub.add_parser("run"); s.add_argument("--yes", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.add_argument("--timeout", type=int, default=1800); s.set_defaults(f=cmd_run)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("collect"); s.add_argument("id"); s.set_defaults(f=cmd_collect)
    s = sub.add_parser("review"); s.add_argument("id"); s.set_defaults(f=cmd_review)
    s = sub.add_parser("retry"); s.add_argument("id"); s.add_argument("--stopped", action="store_true"); s.set_defaults(f=cmd_retry)
    s = sub.add_parser("reset"); s.add_argument("id"); s.add_argument("--stopped", action="store_true"); s.set_defaults(f=cmd_reset)
    s = sub.add_parser("takeover"); s.add_argument("id"); s.add_argument("--stopped", action="store_true"); s.add_argument("--to"); s.add_argument("--leader", action="store_true"); s.set_defaults(f=cmd_takeover)
    s = sub.add_parser("digest"); s.set_defaults(f=cmd_digest)
    s = sub.add_parser("doctor"); s.set_defaults(f=cmd_doctor)
    a = ap.parse_args()
    a.f(a)

if __name__ == "__main__":
    main()
