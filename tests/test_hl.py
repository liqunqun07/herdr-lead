import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hl", BASE / "scripts" / "hl.py")
hl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hl)


def task(tid, **kw):
    value = dict(id=tid, title=tid, goal=tid, tier="cheap", mode="agent", minutes=20)
    value.update(kw)
    return hl.normalize(value)


def model(alias, runner="codex", tier="cheap", rank=1):
    return dict(alias=alias, runner=runner, model="model-" + alias, tier=tier,
                enabled=True, rank=rank, tags=[], thinking="low")


class OfflineChecks(unittest.TestCase):
    def test_nested_runner_preview_expands_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            runners = Path(tmp) / "runners.sh"
            runners.write_text('run_outer(){ run_inner account; }\n'
                               'run_inner(){ python3 -c "print(1)" "$1" '
                               '--model "$HD_MODEL" "$(cat "$HD_PROMPT_FILE")"; }\n')
            out = io.StringIO()
            with mock.patch.object(hl, "LOCAL_RUNNERS", runners), contextlib.redirect_stdout(out):
                hl.print_runner(model("a", "outer"), "/dev/stdin", "hello world\n")
            self.assertIn("最终执行命令: python3 -c", out.getvalue())
            self.assertIn("account --model model-a hello\\ world", out.getvalue())

    def test_overlapping_writes(self):
        plan = {"tasks": [task("a", writes=["src/a.py"]), task("b", writes=["src/"])]}
        self.assertTrue(any("重叠" in e for e in hl.analyze(plan, hl.load_routes(None))[0]))

    def test_cycle(self):
        plan = {"tasks": [task("a", deps=["b"]), task("b", deps=["a"])]}
        self.assertTrue(any("成环" in e for e in hl.analyze(plan, hl.load_routes(None))[0]))

    def test_short_task_warning(self):
        t = task("a")
        t["minutes"] = 7
        self.assertTrue(any("不拆" in w for w in hl.analyze({"tasks": [t]}, hl.load_routes(None))[1]))

    def test_pick_skips_exhausted(self):
        with mock.patch.object(hl, "models", return_value=[model("a", "codex"), model("b", "claude")]):
            self.assertEqual("b", hl.pick("cheap", snapshot={"codex": {"level": "exhausted"}})["alias"])

    def test_scan_uses_runner_listing_and_reports_failure(self):
        with mock.patch.object(hl, "sh", side_effect=[(0, "b\na\na\n", ""), (127, "", "missing")]) as run:
            self.assertEqual((0, ["a", "b"], ""), hl.scan_runner("claude"))
            self.assertEqual((127, [], "missing"), hl.scan_runner("absent"))
            self.assertIn("list_claude", run.call_args_list[0].args[0][-1])
        with self.assertRaises(ValueError):
            hl.scan_runner("codex;exit")
        with tempfile.TemporaryDirectory() as tmp:
            runners = Path(tmp) / "runners.sh"
            runners.write_text("list_custom-runner(){ printf '%s\n' discovered-model; }\n")
            with mock.patch.object(hl, "LOCAL_RUNNERS", runners):
                self.assertEqual((0, ["discovered-model"], ""), hl.scan_runner("custom-runner"))

    def test_agent_sequence(self):
        cmds = hl.task_commands(Path("/project"), task("a"), model("a"))
        self.assertEqual(["pane", "split"], cmds[0][1:3])
        self.assertEqual(["agent", "start"], cmds[1][1:3])
        self.assertIn("--pane", cmds[1])
        self.assertEqual(["agent", "prompt"], cmds[2][1:3])
        self.assertNotIn("--split", sum(cmds, []))
        self.assertNotIn("send-text", sum(cmds, []))

    def test_verify_target_fails_closed(self):
        m = model("a")
        m["model"] = "gpt-6.1-terra"
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "codex", "agent_status": "idle"}]}})
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "models", return_value=[dict(model="gpt-6.1-terra", enabled=True)]), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, "model: GPT-6.1 Terra xhigh\n❯ ", "")]):
            self.assertEqual("", hl.verify_target("p1", m))
        for screen in ("mentioned unrelated-model-b\n❯ ", "model: GPT-6.1 Terra xhigh\n❯ existing draft",
                       "model: GPT-6.1 Terra xhigh\nquota reached\n❯ ", "model: GPT-6.1 Terra xhigh"):
            with self.subTest(screen=screen), \
                 mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
                 mock.patch.object(hl, "models", return_value=[dict(model="gpt-6.1-terra", enabled=True)]), \
                 mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
                self.assertTrue(hl.verify_target("p1", m))

    def test_claude_framed_blank_prompt_ignores_try_placeholder_and_trailing_blanks(self):
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "claude", "agent_status": "idle"}]}})
        screen = 'Opus 5.5\n│ ❯ Try "..."\n\n\n'
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
            self.assertEqual("", hl.verify_target("p1", dict(model("x", runner="claude"), model="claude-opus-5-5")))

    def test_new_agent_without_detectable_prompt_is_allowed_but_existing_is_blocked(self):
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "claude", "agent_status": "idle"}]}})
        screen = "Claude Code started · 🧠 claude-opus-5-5\n\n"
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
            self.assertEqual("", hl.verify_target("p1", dict(model("x", runner="claude"), model="claude-opus-5-5"), started=True))
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
            self.assertEqual("无法确认输入框", hl.verify_target("p1", dict(model("x", runner="claude"), model="claude-opus-5-5")))

    def test_reset_requires_stopped_and_clears_retry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="reset"))
            failed = task("a")
            failed.update(status="failed", retries=2, owner="old-agent", submission="stopped",
                          reason="previous failure", last_failure="boom")
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [failed]
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_reset(SimpleNamespace(root=str(root), id="a", stopped=True))
            reset = hl.read_plan(root)["tasks"][0]
            self.assertEqual(("todo", 0, "", "stopped", ""),
                             (reset["status"], reset["retries"], reset["owner"], reset["submission"], reset["reason"]))
            self.assertNotIn("last_failure", reset)

    def test_confirmation(self):
        tasks = [task(str(i)) for i in range(3)]
        args = SimpleNamespace(root="/project", dry_run=True, yes=False)
        output = io.StringIO()
        with mock.patch.object(hl, "plan_tx", return_value=contextlib.nullcontext({"tasks": tasks})), \
             mock.patch.object(hl, "read_plan", return_value={"tasks": tasks}), \
             mock.patch.object(hl, "quota", return_value={}), \
             mock.patch.object(hl, "models", return_value=[model("a")]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as e:
                hl.cmd_run(args)
        self.assertEqual(3, e.exception.code)
        self.assertIn("| 批次 | 任务 | 模式 |", output.getvalue())

    def test_plan_table_reasons_and_columns(self):
        manual = task("manual", alias="manual", tier="mid")
        auto = task("auto")
        catalog = [model("skip", "codex", rank=1), model("manual", "claude", tier="mid", rank=4),
                   model("chosen", "claude", rank=2)]
        routes = hl.load_routes(None)
        routes["max_width"] = 3
        with mock.patch.object(hl, "models", return_value=catalog):
            table = hl.render_plan({"goal": "g", "tasks": [manual, auto]},
                                   routes, {"codex": {"level": "exhausted"}})
        for column in ("批次", "任务", "模式", "模型(alias/runner/model)", "选择理由", "写入范围", "依赖", "预计分钟"):
            self.assertIn(column, table)
        self.assertIn("指定 alias", table)
        self.assertIn("tier=cheap rank=2", table)
        self.assertIn("额度跳过 skip", table)

    def test_plan_md_changes_only_plan_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="g"))
            t = task("a")
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [t]
            before = __import__("hashlib").sha256(hl.P(root, "plan.json").read_bytes()).hexdigest()
            files_before = {p.name for p in hl.P(root).iterdir() if p.is_file()}
            with mock.patch.object(hl, "models", return_value=[model("a")]), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_plan(SimpleNamespace(root=str(root), md=True))
            after = __import__("hashlib").sha256(hl.P(root, "plan.json").read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertTrue(hl.P(root, "plan.md").is_file())
            files_after = {p.name for p in hl.P(root).iterdir() if p.is_file()}
            self.assertEqual(files_before | {"plan.md"}, files_after)

    def test_strong_requires_confirmation(self):
        t = task("a")
        t["tier"] = "strong"
        args = SimpleNamespace(root="/project", dry_run=True, yes=False)
        with mock.patch.object(hl, "plan_tx", return_value=contextlib.nullcontext({"tasks": [t]})), \
             mock.patch.object(hl, "read_plan", return_value={"tasks": [t]}), \
             mock.patch.object(hl, "quota", return_value={}), \
             mock.patch.object(hl, "models", return_value=[model("a", tier="strong")]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                hl.cmd_run(args)
        self.assertEqual(3, e.exception.code)

    def test_missing_consent(self):
        t = task("a")
        t["mode"] = "oneshot"
        args = SimpleNamespace(root="/project", dry_run=True, yes=True)
        with mock.patch.object(hl, "read_plan", return_value={"run_id": "test", "tasks": [t]}), \
             mock.patch.object(hl, "quota", return_value={}), \
             mock.patch.object(hl, "models", return_value=[model("a")]), \
             mock.patch.object(hl, "consented", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                hl.cmd_run(args)

    def test_oneshot_consent_does_not_require_yes(self):
        t = task("a")
        t["mode"] = "oneshot"
        args = SimpleNamespace(root="/project", dry_run=True, yes=False)
        with mock.patch.object(hl, "plan_tx", return_value=contextlib.nullcontext({"run_id": "test", "tasks": [t]})), \
             mock.patch.object(hl, "read_plan", return_value={"run_id": "test", "tasks": [t]}), \
             mock.patch.object(hl, "quota", return_value={}), \
             mock.patch.object(hl, "models", return_value=[model("a")]), \
             mock.patch.object(hl, "consented", return_value=True), \
             mock.patch.object(hl, "print_runner"), \
             contextlib.redirect_stdout(io.StringIO()):
            hl.cmd_run(args)

    def test_retry_escalation(self):
        t = task("a")
        for _ in range(3):
            self.assertTrue(hl.do_retry(Path("/project"), {}, t))
        self.assertEqual("mid", t["tier"])
        self.assertEqual(0, t["retries"])

    def test_scope_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "before.txt").write_text("old")
            before = hl.snapshot_files(root)
            (root / "other.txt").write_text("new")
            changes = hl.changes_since(root, before)
            self.assertEqual(["other.txt"], changes)
            self.assertFalse(hl.in_scope(changes[0], ["allowed/"]))

    def test_git_scope_ignores_preexisting_dirty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-q")
            (root / "old.txt").write_text("base")
            (root / "new.txt").write_text("base")
            git("add", "old.txt", "new.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-qm", "initial")
            (root / "old.txt").write_text("already dirty")
            before = hl.snapshot_files(root)
            (root / "new.txt").write_text("new change")
            self.assertEqual(["new.txt"], hl.changes_since(root, before))

    def test_review_requires_known_original_runner(self):
        t = task("a")
        t["status"] = "done"
        @contextlib.contextmanager
        def transaction(_root):
            yield {"tasks": [t]}
        with mock.patch.object(hl, "plan_tx", side_effect=transaction), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                hl.cmd_review(SimpleNamespace(root="/project", id="a"))

    def test_takeover_order(self):
        t = task("a")
        t.update(alias="first", runner="codex")
        catalog = [model("first"), model("second"), model("third", "claude")]
        self.assertEqual("second", hl.takeover_choice(t, catalog, {})["alias"])
        self.assertEqual("third", hl.takeover_choice(t, catalog, {"codex": {"level": "exhausted"}})["alias"])
        self.assertIsNone(hl.takeover_choice(t, catalog, {"codex": {"level": "exhausted"}, "claude": {"level": "critical"}}))

    def test_digest_summary_limit(self):
        t = task("a")
        t["summary"] = "\n".join(str(i) for i in range(20))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".herdr-run").mkdir()
            args = SimpleNamespace(root=str(root))
            with mock.patch.object(hl, "read_plan", return_value={"goal": "", "tasks": [t]}), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_digest(args)
            text = (root / ".herdr-run" / "digest.md").read_text()
            self.assertNotIn("\n8\n", text)
            self.assertTrue(text.startswith("## 编排方案："))

    def test_multiround_and_oneshot_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
            a, b = task("a"), task("b", deps=["a"])
            a["mode"] = "oneshot"
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [a, b]
                run_id = plan["run_id"]
            calls = []
            def fake_sh(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"}, "tab": {"tab_id": "t1"}}}), ""
                if argv[:3] == ["herdr", "pane", "run"]:
                    current = hl.read_plan(root)["tasks"][0]
                    out = hl.report_path(root, current)
                    out.write_text("STATUS: done\nSUMMARY: ok\nARTIFACTS: none\nEVIDENCE: ok\nOPEN_QUESTIONS: none\n" + f"SENTINEL: HERDR-DONE:{run_id}:a\n")
                    (out.parent / ("exit-" + str(current["marker_ns"]))).write_text("0\n")
                if argv[:3] == ["herdr", "agent", "wait"]:
                    hl.report_path(root, {"id": "b"}).write_text("STATUS: done\nSUMMARY: ok\nARTIFACTS: none\nEVIDENCE: ok\nOPEN_QUESTIONS: none\n" + f"SENTINEL: HERDR-DONE:{run_id}:b\n")
                    return 0, "done", ""
                return 0, "", ""
            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "verify_target", return_value=""), \
                 mock.patch.object(hl, "consented", return_value=True), \
                 mock.patch.object(hl, "print_runner"), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[model("a")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)
            self.assertEqual(["done", "done"], [t["status"] for t in hl.read_plan(root)["tasks"]])
            ops = [c[1:3] for c in calls if c[0] == "herdr"]
            self.assertLess(ops.index(["pane", "run"]), ops.index(["agent", "prompt"]))

    def test_wait_failure_and_timeout_are_not_resent(self):
        for mode, exit_code, expected in (("oneshot", "1", "failed"),
                                          ("oneshot", None, "blocked"),
                                          ("agent", None, "blocked")):
            with self.subTest(mode=mode, exit_code=exit_code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
                t = task("a")
                t.update(mode=mode, status="running", submission="submitted", owner="p1", marker_ns=123)
                with hl.plan_tx(root) as plan:
                    plan["tasks"] = [t]
                if exit_code is not None:
                    marker = hl.report_path(root, t).parent / "exit-123"
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(exit_code)
                with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                     mock.patch.object(hl, "quota", return_value={}), \
                     mock.patch.object(hl, "sh", return_value=(1, "", "timeout")) as shell, \
                     contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_run(SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=0))
                saved = hl.read_plan(root)["tasks"][0]
                self.assertEqual(expected, saved["status"])
                self.assertFalse(any(c.args[0][:3] == ["herdr", "agent", "prompt"] for c in shell.call_args_list))

    def test_review_takeover_and_retry_transactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
            first = task("a", writes=["a.txt"])
            first.update(status="done", runner="codex", alias="a")
            second = task("b")
            second.update(status="blocked", runner="codex", alias="a", submission="stopped")
            catalog = [model("a"), model("b"), model("c", "claude")]
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [first, second]
            with mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=catalog), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_review(SimpleNamespace(root=str(root), id="a"))
                hl.cmd_takeover(SimpleNamespace(root=str(root), id="b", stopped=True, to=None, leader=False))
                for _ in range(3):
                    with hl.plan_tx(root) as plan:
                        hl.find(plan, "b").update(status="failed", submission="stopped")
                    hl.cmd_retry(SimpleNamespace(root=str(root), id="b", stopped=True))
            plan = hl.read_plan(root)
            self.assertEqual("c", hl.find(plan, "R-a")["alias"])
            self.assertEqual(["a"], hl.find(plan, "R-a")["deps"])
            self.assertEqual("mid", hl.find(plan, "b")["tier"])

    def test_takeover_accepts_stale_and_needs_review(self):
        catalog = [model("a"), model("b", "claude")]
        for status in ("stale", "needs_review"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_init(SimpleNamespace(root=str(root), goal="takeover"))
                item = task("a")
                item.update(status=status, runner="codex", alias="a", submission="stopped")
                with hl.plan_tx(root) as plan:
                    plan["tasks"] = [item]
                with mock.patch.object(hl, "quota", return_value={}), \
                     mock.patch.object(hl, "models", return_value=catalog), \
                     contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_takeover(SimpleNamespace(root=str(root), id="a", stopped=True, to=None, leader=False))
                saved = hl.read_plan(root)["tasks"][0]
                self.assertEqual(("todo", "b"), (saved["status"], saved["alias"]))

    def test_agent_start_retries_busy_twice_then_succeeds(self):
        calls = []
        def fake(argv, **kwargs):
            calls.append(argv)
            return (1, "", '{"error":{"code":"agent_pane_busy"}}') if len(calls) < 3 else (0, "", "")
        with mock.patch.object(hl, "sh", side_effect=fake), \
             mock.patch.object(hl.time, "sleep") as sleep:
            self.assertEqual((0, "", ""), hl.start_agent(["herdr", "agent", "start", "x"]))
        self.assertEqual(3, len(calls))
        self.assertEqual(2, sleep.call_count)

    def test_simulated_agent_lifecycle_and_no_resend(self):
        # Exercise the implementation with all external commands mocked.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task("a", alias="strong-alias")]
                run_id = plan["run_id"]
            commands = []
            start_attempts = 0

            def fake_sh(argv, **kwargs):
                nonlocal start_attempts
                commands.append(argv)
                if argv[:3] == ["herdr", "agent", "start"]:
                    start_attempts += 1
                    if start_attempts < 3:
                        return 1, "", '{"error":{"code":"agent_pane_busy"}}'
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"},
                                                     "tab": {"tab_id": "t1"}}}), ""
                if argv[:3] == ["herdr", "pane", "list"]:
                    return 0, json.dumps({"result": {"panes": [{"pane_id": "p1",
                         "agent": "codex", "agent_status": "idle"}]}}), ""
                if argv[:3] == ["herdr", "pane", "read"]:
                    return 0, "model: GPT-6.1 Terra xhigh\n❯ ", ""
                if argv[:3] == ["herdr", "agent", "wait"]:
                    hl.report_path(root, {"id": "a"}).write_text(
                        "STATUS: done\nSUMMARY: 完成\nARTIFACTS: none\n"
                        "EVIDENCE: 模拟通过\nOPEN_QUESTIONS: none\n"
                        f"SENTINEL: HERDR-DONE:{run_id}:a\n")
                    return 0, "done", ""
                if argv[0] == "git":
                    return 1, "", "not a repository"
                return 0, "", ""

            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1"}), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[dict(model("strong-alias", tier="strong"), model="gpt-6.1-terra")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)
                hl.cmd_run(args)
            seq = [cmd[1:3] for cmd in commands if cmd[0] == "herdr"]
            self.assertLess(seq.index(["pane", "split"]), seq.index(["agent", "start"]))
            self.assertLess(seq.index(["agent", "start"]), seq.index(["agent", "prompt"]))
            self.assertEqual(1, seq.count(["agent", "prompt"]))
            self.assertEqual("done", hl.read_plan(root)["tasks"][0]["status"])
            self.assertEqual("strong", hl.read_plan(root)["tasks"][0]["tier"])
            self.assertEqual(3, start_attempts)
            self.assertIn(["pane", "close"], seq)

    def test_fake_herdr_executable_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            binary = temp / "herdr"
            log = temp / "calls"
            binary.write_text('#!/usr/bin/env python3\nimport json, os\nwith open(os.environ["FAKE_LOG"], "a") as f: f.write(json.dumps(__import__("sys").argv[1:]) + "\\n")\nprint("status: running")\n')
            binary.chmod(0o755)
            env = {"PATH": str(temp) + os.pathsep + os.environ["PATH"], "FAKE_LOG": str(log)}
            with mock.patch.dict(os.environ, env):
                rc, out, err = hl.sh(["herdr", "status"])
            self.assertEqual((0, "status: running\n", ""), (rc, out, err))
            self.assertEqual(["status"], json.loads(log.read_text().splitlines()[0]))

    def test_trust_dialog_blocks_and_keeps_started_pane(self):
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "claude", "agent_status": "blocked"}]}})
        screens = [
            "Opus 5.5\nQuick safety check\nYes, I trust this folder",
            "Opus 5.5\nDo you want to allow access to this workspace?",
            "Opus 5.5\nApprove this action to continue",
        ]
        for screen in screens:
            with self.subTest(screen=screen), mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
                 mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
                self.assertEqual("目标目录需人工确认信任",
                                 hl.verify_target("p1", dict(model("x", runner="claude"), model="claude-opus-5-5"), started=True))

    def test_trust_dialog_blocks_without_closing_or_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="trust"))
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task("trust-a")]
            calls = []
            def fake_sh(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"}, "tab": {"tab_id": "t1"}}}), ""
                if argv[:3] == ["herdr", "agent", "start"]:
                    return 0, "started", ""
                if argv[:3] == ["herdr", "agent", "prompt"]:
                    return 0, "sent", ""
                return 0, "", ""
            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "verify_target", return_value="目标目录需人工确认信任"), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[model("a")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)
            operations = [call[1:3] for call in calls if call[0] == "herdr"]
            self.assertNotIn(["pane", "close"], operations)
            self.assertNotIn(["agent", "prompt"], operations)
            saved = hl.read_plan(root)["tasks"][0]
            self.assertEqual("blocked", saved["status"])
            self.assertEqual("目标目录需人工确认信任；确认后 hl reset trust-a --stopped", saved["reason"])

    def test_startup_blocked_trust_dialog_keeps_pane_but_other_failure_closes(self):
        for screen, should_close in (("Quick safety check\nYes, I trust this folder", False),
                                     ("agent startup failed", True)):
            with self.subTest(screen=screen), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_init(SimpleNamespace(root=str(root), goal="startup trust"))
                with hl.plan_tx(root) as plan:
                    plan["tasks"] = [task("startup-trust")]
                calls = []
                def fake_sh(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["herdr", "pane", "split"]:
                        return 0, json.dumps({"result": {"pane": {"pane_id": "p1"}, "tab": {"tab_id": "t1"}}}), ""
                    if argv[:3] == ["herdr", "agent", "start"]:
                        return 1, '{"code":"agent_not_ready","message":"blocked during startup"}', ""
                    if argv[:3] == ["herdr", "pane", "read"]:
                        return 0, screen, ""
                    return 0, "", ""
                args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
                with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                     mock.patch.object(hl, "sh", side_effect=fake_sh), \
                     mock.patch.object(hl, "quota", return_value={}), \
                     mock.patch.object(hl, "models", return_value=[model("a")]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    hl.cmd_run(args)
                closed = any(call[:3] == ["herdr", "pane", "close"] for call in calls)
                self.assertEqual(should_close, closed)
                reads = [call for call in calls if call[:3] == ["herdr", "pane", "read"]]
                self.assertEqual(1, len(reads))
                saved = hl.read_plan(root)["tasks"][0]
                self.assertEqual("blocked", saved["status"])
                if not should_close:
                    self.assertEqual("目标目录需人工确认信任；确认后 hl reset startup-trust --stopped", saved["reason"])

    def test_agent_not_ready_is_blocked_without_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="not ready"))
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task("not-ready-a")]
            calls = []
            def fake_sh(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"}, "tab": {"tab_id": "t1"}}}), ""
                if argv[:3] == ["herdr", "agent", "prompt"]:
                    return 1, '{"code":"agent_not_ready","message":"no longer the pane foreground process"}', ""
                return 0, "", ""
            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "verify_target", return_value=""), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[model("a")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)
                hl.cmd_run(args)
            prompts = [call for call in calls if call[:3] == ["herdr", "agent", "prompt"]]
            self.assertEqual(1, len(prompts))
            saved = hl.read_plan(root)["tasks"][0]
            self.assertEqual("blocked", saved["status"])
            self.assertIn("agent_not_ready", saved["reason"])
            self.assertIn("未重发", saved["reason"])
            self.assertIn("hl reset not-ready-a --stopped", saved["reason"])

    def test_pre_submission_blocked_closes_split_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task("a")]
            calls = []

            def fake_sh(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"},
                                                     "tab": {"tab_id": "t1"}}}), ""
                return 0, "", ""

            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "verify_target", return_value="目标核验失败"), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[model("a")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)

            operations = [c[1:3] for c in calls if c[0] == "herdr"]
            self.assertLess(operations.index(["pane", "split"]), operations.index(["pane", "close"]))
            self.assertNotIn(["agent", "prompt"], operations)
            saved = hl.read_plan(root)["tasks"][0]
            self.assertEqual(("blocked", "uncertain"), (saved["status"], saved["submission"]))

    def test_uncertain_submission_is_not_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="test"))
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task("a")]
            calls = []
            def fake_sh(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ["herdr", "pane", "split"]:
                    return 0, json.dumps({"result": {"pane": {"pane_id": "p1"},
                                                     "tab": {"tab_id": "t1"}}}), ""
                if argv[:3] == ["herdr", "agent", "prompt"]:
                    return 1, "", "connection lost"
                return 0, "", ""
            args = SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "verify_target", return_value=""), \
                 mock.patch.object(hl, "sh", side_effect=fake_sh), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "models", return_value=[model("a")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(args)
                hl.cmd_run(args)
            prompts = [c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]
            self.assertEqual(1, len(prompts))
            saved = hl.read_plan(root)["tasks"][0]
            self.assertEqual(("blocked", "uncertain"), (saved["status"], saved["submission"]))

    def test_verify_target_matches_real_model_labels(self):
        cases = [
            ("claude-opus-5-5", "claude", "🧠 claude-opus-5-5"),
            ("gpt-6.1-terra", "codex", "GPT-6.1 Terra xhigh"),
            ("gemini-3.8-flash-low", "agy", "auto · Gemini 3.8 Flash · high"),
        ]
        catalog = [dict(model="claude-opus-5-5", enabled=True),
                   dict(model="gpt-6.1-terra", enabled=True),
                   dict(model="gemini-3.8-flash-low", enabled=True),
                   dict(model="claude-sonnet-4-5", enabled=True)]
        for model_id, runner, label in cases:
            pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": runner, "agent_status": "idle"}]}})
            with self.subTest(model=model_id), mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
                 mock.patch.object(hl, "models", return_value=catalog), \
                 mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, label + "\n❯ ", "")]):
                self.assertEqual("", hl.verify_target("p1", dict(model("x", runner=runner), model=model_id)))

    def test_model_evidence_prefers_current_status_over_history(self):
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "codex", "agent_status": "idle"}]}})
        screen = "Earlier selected GPT-6.1 Terra; current model is Sonnet 4.5\n❯ "
        catalog = [dict(model="gpt-6.1-terra", enabled=True)]
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "models", return_value=catalog), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""), (0, screen, "")]):
            self.assertEqual("当前显示模型与目标模型不匹配",
                             hl.verify_target("p1", dict(model("x"), model="gpt-6.1-terra")))

    def test_approaching_rate_limits_is_not_exhausted(self):
        pane_list = json.dumps({"result": {"panes": [{"pane_id": "p1", "agent": "codex", "agent_status": "idle"}]}})
        with mock.patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "w1"}), \
             mock.patch.object(hl, "models", return_value=[dict(model="gpt-6.1-terra", enabled=True)]), \
             mock.patch.object(hl, "sh", side_effect=[(0, pane_list, ""),
                 (0, "GPT-6.1 Terra xhigh\nApproaching  rate limit\n❯ ", "")]):
            self.assertEqual("", hl.verify_target("p1", dict(model("x"), model="gpt-6.1-terra")))

    def test_review_fail_receipt_blocks_downstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="review"))
            review = task("R-a")
            review["marker_ns"] = 0
            out = hl.report_path(root, review)
            out.parent.mkdir(parents=True)
            out.write_text("STATUS: done\nSUMMARY: FAIL: rejected\nARTIFACTS: none\nEVIDENCE: checked\nOPEN_QUESTIONS: none\nSENTINEL: HERDR-DONE:test:R-a\n")
            with mock.patch.object(hl, "validate", return_value=([], [])):
                self.assertEqual("failed", hl.collect_task(root, {"tasks": [review]}, review))
            self.assertEqual("failed", review["status"])
            self.assertEqual("FAIL", review["verdict"])
            self.assertIn("FAIL", review["reason"])
            review["status"] = "failed"
            dependent = task("b", depends_on=["R-a"])
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [review, dependent]
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "quota", return_value={}), \
                 mock.patch.object(hl, "sh") as shell, contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_run(SimpleNamespace(root=str(root), dry_run=False, yes=True, timeout=1))
            self.assertEqual("todo", hl.read_plan(root)["tasks"][1]["status"])
            shell.assert_not_called()

    def test_out_of_task_write_requires_review_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="scope"))
            t = task("a", writes=["owned.txt"])
            t["marker_ns"] = 0
            t["before"] = {}
            out = hl.report_path(root, t)
            out.parent.mkdir(parents=True)
            out.write_text("STATUS: done\nSUMMARY: finished\nARTIFACTS: none\nEVIDENCE: checked\nOPEN_QUESTIONS: none\nSENTINEL: HERDR-DONE:test:a\n")
            with mock.patch.object(hl, "changes_since", return_value=["foreign.txt"]), \
                 mock.patch.object(hl, "validate", return_value=([], [])):
                self.assertEqual("needs_review", hl.collect_task(root, {"tasks": [t]}, t))
            self.assertIn("foreign.txt", t["reason"])
            shown = hl.status_text({"run_id": "x", "tasks": [t]})
            self.assertIn("foreign.txt", shown)
            with mock.patch.object(hl, "read_plan", return_value={"goal": "", "tasks": [t]}), \
                 contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_digest(SimpleNamespace(root=str(root)))
            self.assertIn("foreign.txt", (root / ".herdr-run" / "digest.md").read_text())

    def test_normalized_agent_name_collisions_rejected(self):
        tasks = [task("A_b"), task("a-b")]
        errors, _, _, _ = hl.analyze({"tasks": tasks}, hl.load_routes(None))
        self.assertTrue(any("agent 名冲突 hl-a-b" in error for error in errors))

    def test_agy_quota_is_selected_model_family(self):
        gemini = model("gemini", runner="agy")
        gemini["model"] = "gemini-3.8-flash-low"
        claude = model("claude", runner="agy")
        claude["model"] = "claude-gpt-5"
        snapshot = {"agy": {"level": "ok", "families": {
            "gemini": {"level": "exhausted"}, "claude_gpt": {"level": "ok"}}}}
        with mock.patch.object(hl, "models", return_value=[gemini, claude]):
            self.assertEqual("claude", hl.pick("cheap", snapshot=snapshot)["alias"])
            self.assertIsNone(hl.model_for(task("x", alias="gemini"), snapshot=snapshot))

    def test_agy_quota_snapshot_records_each_family_level(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("quota_local", BASE / "scripts" / "quota_local.py")
        quota_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(quota_module)
        payload = {"current": "current", "accounts": [{"email": "current", "groups": [
            {"label": "Gemini Flash", "remaining": 0},
            {"label": "Claude/GPT", "remaining": 70}]}]}
        with mock.patch.object(quota_module, "_run", return_value=payload):
            result = quota_module.agy()
        self.assertEqual("exhausted", result["families"]["gemini"]["level"])
        self.assertEqual("ok", result["families"]["claude_gpt"]["level"])

    def test_models_conf_fields_are_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "models.conf"
            config.write_text(" a | codex | model-a | low | yes | code | cheap | 1 | code, zh \n")
            with mock.patch.object(hl, "MODEL_FILE", config):
                parsed = hl.models()[0]
        self.assertEqual(("a", "codex", "model-a", "low", "code", "cheap", ["code", "zh"]),
                         (parsed["alias"], parsed["runner"], parsed["model"], parsed["thinking"],
                          parsed["use"], parsed["tier"], parsed["tags"]))

    def test_run_marks_uncertain_submissions_stale_without_resending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                hl.cmd_init(SimpleNamespace(root=str(root), goal="stale"))
            task_a = task("a")
            task_a.update(status="running", submission="uncertain", owner="p1")
            with hl.plan_tx(root) as plan:
                plan["tasks"] = [task_a]
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                 mock.patch.object(hl, "sh") as shell, contextlib.redirect_stdout(output):
                hl.cmd_run(SimpleNamespace(root=str(root), dry_run=False, yes=False, timeout=1))
            saved = hl.read_plan(root)["tasks"][0]
            self.assertEqual("stale", saved["status"])
            self.assertIn("需确认停写后 hl retry/takeover a --stopped", output.getvalue())
            self.assertIn("stale", hl.status_text({"run_id": "x", "tasks": [saved]}))
            shell.assert_not_called()


class ModelsSetupTests(unittest.TestCase):
    def test_tier_guess_rules(self):
        # cheap: mini, flash, haiku, lite, low
        self.assertEqual("cheap", hl.guess_tier("gpt-4o-mini"))
        self.assertEqual("cheap", hl.guess_tier("gemini-1.5-flash"))
        self.assertEqual("cheap", hl.guess_tier("claude-3-5-haiku-20241022"))
        self.assertEqual("cheap", hl.guess_tier("deepseek-lite"))
        self.assertEqual("cheap", hl.guess_tier("codex-low"))
        # strong: pro, opus, sonnet, terra, high
        self.assertEqual("strong", hl.guess_tier("gemini-1.5-pro"))
        self.assertEqual("strong", hl.guess_tier("claude-3-opus"))
        self.assertEqual("strong", hl.guess_tier("claude-3-5-sonnet-20241022"))
        self.assertEqual("strong", hl.guess_tier("gpt-6.1-terra"))
        self.assertEqual("strong", hl.guess_tier("codex-high"))
        # mid: other
        self.assertEqual("mid", hl.guess_tier("gpt-4o"))
        self.assertEqual("mid", hl.guess_tier("deepseek-chat"))
        self.assertEqual("mid", hl.guess_tier("unknown-runner-model"))

    def test_scan_clis_finds_fake_runner_in_path_and_local_runners(self):
        with tempfile.TemporaryDirectory() as tmp_bin, tempfile.TemporaryDirectory() as tmp_conf:
            # Fake CLI binary placed at front of PATH
            fake_cli = Path(tmp_bin) / "claude"
            fake_cli.write_text("#!/bin/sh\necho fake\n")
            fake_cli.chmod(0o755)
            # Custom runner defined in runners.local.sh
            runners_file = Path(tmp_conf) / "herdr-dispatch" / "runners.local.sh"
            runners_file.parent.mkdir(parents=True, exist_ok=True)
            runners_file.write_text("run_custom-runner(){ echo ok; }\n")

            env = {"PATH": f"{tmp_bin}:{os.environ.get('PATH', '')}", "HL_CONFIG_DIR": tmp_conf}
            with mock.patch.dict(os.environ, env):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hl.cmd_setup(SimpleNamespace(setup_action="scan-clis"))
                discovered = json.loads(out.getvalue())
                self.assertIn("claude", discovered)
                self.assertIn("custom-runner", discovered)

    def test_scan_models_suggestions(self):
        with mock.patch.object(hl, "scan_runner", return_value=(0, ["claude-3-5-sonnet", "claude-3-5-haiku", "gpt-4o"], "")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hl.cmd_setup(SimpleNamespace(setup_action="scan-models", runner="claude"))
            results = json.loads(out.getvalue())
            by_model = {item["model"]: item["tier"] for item in results}
            self.assertEqual("strong", by_model["claude-3-5-sonnet"])
            self.assertEqual("cheap", by_model["claude-3-5-haiku"])
            self.assertEqual("mid", by_model["gpt-4o"])

    def test_apply_creates_file_content_rank_and_600_permissions(self):
        with tempfile.TemporaryDirectory() as tmp_conf:
            entries = [
                "c-cheap|codex|gpt-4o-mini|low|cheap",
                "c-strong|claude|claude-3-5-sonnet|high|strong",
                "c-mid|pi|anthropic/claude-3.5|medium|mid",
            ]
            env = {"HL_CONFIG_DIR": tmp_conf}
            with mock.patch.dict(os.environ, env):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hl.cmd_setup_apply(SimpleNamespace(entries=entries, force=False))

                conf = Path(tmp_conf) / "herdr-dispatch" / "models.conf"
                self.assertTrue(conf.is_file())
                self.assertEqual(0o600, conf.stat().st_mode & 0o777)
                lines = conf.read_text().splitlines()
                self.assertEqual("# alias|runner|model|default_thinking|enabled|when_to_use|tier|rank", lines[0])
                self.assertEqual("c-cheap|codex|gpt-4o-mini|low|yes|简单任务|cheap|1", lines[1])
                self.assertEqual("c-strong|claude|claude-3-5-sonnet|high|yes|复杂推理|strong|2", lines[2])
                self.assertEqual("c-mid|pi|anthropic/claude-3.5|medium|yes|普通任务|mid|3", lines[3])

                # Verify models() parses in correct rank order
                parsed = hl.models()
                self.assertEqual(["c-cheap", "c-strong", "c-mid"], [m["alias"] for m in parsed])
                self.assertEqual([1, 2, 3], [m["rank"] for m in parsed])

    def test_apply_rejects_existing_file_without_force(self):
        with tempfile.TemporaryDirectory() as tmp_conf:
            conf = Path(tmp_conf) / "herdr-dispatch" / "models.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text("old-alias|codex|old|low|yes|code|cheap|1\n")
            conf.chmod(0o600)

            env = {"HL_CONFIG_DIR": tmp_conf}
            with mock.patch.dict(os.environ, env):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        hl.cmd_setup_apply(SimpleNamespace(
                            entries=["new-alias|claude|new|high|strong"], force=False))
                self.assertNotEqual(0, cm.exception.code)
                self.assertIn("已存在", err.getvalue())
                # Confirm content unchanged
                self.assertIn("old-alias", conf.read_text())

    def test_setup_wizard_flow(self):
        with tempfile.TemporaryDirectory() as tmp_conf:
            env = {"HL_CONFIG_DIR": tmp_conf}
            with mock.patch.dict(os.environ, env), \
                 mock.patch("sys.stdin.isatty", return_value=True), \
                 mock.patch.object(hl, "scan_clis", return_value=["claude"]), \
                 mock.patch.object(hl, "scan_runner", return_value=(0, ["haiku", "sonnet"], "")), \
                 mock.patch("builtins.input", side_effect=[
                     "1",      # choose runner 1 (claude)
                     "1 2",    # choose models 1 and 2
                     "",       # keep cheap tier for haiku
                     "strong", # set strong tier for sonnet
                 ]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hl.cmd_setup(SimpleNamespace(setup_action=None))

                conf = Path(tmp_conf) / "herdr-dispatch" / "models.conf"
                self.assertTrue(conf.is_file())
                self.assertIn("提示: setup 结束", out.getvalue())
                self.assertIn("hl models test <alias>", out.getvalue())

    def test_apply_with_force_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp_conf:
            conf = Path(tmp_conf) / "herdr-dispatch" / "models.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text("old-alias|codex|old|low|yes|code|cheap|1\n")
            conf.chmod(0o600)

            env = {"HL_CONFIG_DIR": tmp_conf}
            with mock.patch.dict(os.environ, env):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hl.cmd_setup_apply(SimpleNamespace(
                        entries=["new-alias|claude|new|high|strong"], force=True))
                # Confirm new content written
                self.assertIn("new-alias", conf.read_text())
                # Confirm backup created
                bak_files = list(conf.parent.glob("models.conf.bak.*"))
                self.assertEqual(1, len(bak_files))
                self.assertIn("old-alias", bak_files[0].read_text())

    def test_setup_non_interactive_exits_code_2(self):
        out = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                hl.cmd_setup(SimpleNamespace(setup_action=None))
            self.assertEqual(2, cm.exception.code)
            self.assertIn("用法:", out.getvalue())
            self.assertIn("scan-clis", out.getvalue())
            self.assertIn("scan-models", out.getvalue())
            self.assertIn("apply", out.getvalue())

    def test_plan_timing_parallel_greater_or_equal_serial(self):
        # Case 1: parallel >= serial -> "并行不省时间：建议串行，或由 leader 自己做"
        plan_small = {"goal": "small", "tasks": [task("t1", minutes=10)]}
        table_small = hl.render_plan(plan_small)
        self.assertIn("并行不省时间：建议串行，或由 leader 自己做", table_small)
        self.assertNotIn("节省约 0 分钟", table_small)

        # Case 2: parallel < serial -> "节省约 X 分钟"
        plan_large = {"goal": "large", "tasks": [
            task("t1", minutes=20),
            task("t2", minutes=20),
            task("t3", minutes=20),
        ]}
        table_large = hl.render_plan(plan_large)
        self.assertIn("节省约", table_large)
        self.assertNotIn("并行不省时间", table_large)


if __name__ == "__main__":
    unittest.main()
