"""iu.hook 的单元测试：python -m unittest discover -s tests"""

import argparse
import io
import json
import os
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu import hook  # noqa: E402

PILOT = os.path.realpath(os.path.abspath("/work/pilot-repo"))
OTHER = os.path.realpath(os.path.abspath("/work/other-repo"))


def payload(cmd, cwd=PILOT, **extra):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": cwd,
            "tool_input": {"command": cmd, "description": "Run the tests", **extra}}


def decide(p, private=False):
    return hook.decide(p, [os.path.normcase(PILOT)], is_private=lambda cwd: private, python="/usr/bin/python3")


class TestDecide(unittest.TestCase):
    def test_rewrites_whitelisted(self):
        for cmd in ["npm test", "pytest -x tests", "npx tsc --noEmit -p .", "git log --stat -40", "rg -n keyless src",
                    "cd server && npx vitest run", "cargo test", "grep -rn TODO src"]:
            with self.subTest(cmd=cmd):
                new = decide(payload(cmd))
                self.assertIsNotNone(new)
                argv = shlex.split(new["command"])
                self.assertEqual(argv[2], "sum")
                self.assertEqual(argv[-1], cmd)  # 原命令完整保留，作为一个参数
                self.assertIn("--goal", argv)
                self.assertEqual(new["description"], "Run the tests")  # 其他字段原样带上

    def test_trailing_tail_head(self):
        for cmd, base, word in [("cd server && npx vitest run src/__tests__/lib 2>&1 | tail -100",
                                 "cd server && npx vitest run src/__tests__/lib 2>&1", "last 100"),
                                ("npm test | tail -n 30", "npm test", "last 30"),
                                ("git log --stat | head -50", "git log --stat", "first 50")]:
            with self.subTest(cmd=cmd):
                new = decide(payload(cmd))
                self.assertIsNotNone(new)
                argv = shlex.split(new["command"])
                self.assertEqual(argv[-1], base)  # 去掉 | tail，摘要完整输出
                self.assertIn(word, argv[argv.index("--goal") + 1])

    def test_leaves_others_alone(self):
        for cmd in ["ls -la", "npm install left-pad", "pip install x", "git push origin main", "git status",
                    "npm test | grep FAIL", "npm test | tail -20 | grep x", "ls | tail -5", "pytest -q",
                    "echo hi", "python script.py", "rm -rf build"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(decide(payload(cmd)))

    def test_not_in_pilot_or_private(self):
        self.assertIsNone(decide(payload("npm test", cwd=OTHER)))
        self.assertIsNone(decide(payload("npm test"), private=True))
        self.assertIsNotNone(decide(payload("npm test", cwd=os.path.join(PILOT, "server"))))  # 子目录也算

    def test_background_and_already_wrapped(self):
        self.assertIsNone(decide(payload("npm test", run_in_background=True)))
        self.assertIsNone(decide(payload("python D:/x/run_iu.py sum -- npm test")))
        self.assertIsNone(decide(payload("iu sum -- npm test")))

    def test_other_tools(self):
        p = payload("npm test")
        p["tool_name"] = "Read"
        self.assertIsNone(decide(p))

    def test_timeout_extended(self):
        new = decide(payload("npm test", timeout=120000))
        self.assertEqual(new["timeout"], 165000)
        self.assertEqual(decide(payload("npm test", timeout=590000))["timeout"], 600000)

    def test_quotes_survive(self):
        cmd = "git log --format='%h %s' -20 -- 'path with space/a.ts'"
        new = decide(payload(cmd))
        self.assertEqual(shlex.split(new["command"])[-1], cmd)


class TestExploreGate(unittest.TestCase):
    def gate(self, tool, cwd=PILOT, denials=0, **inp):
        p = {"tool_name": tool, "cwd": cwd, "tool_input": inp}
        return hook.gate_decide(p, [os.path.normcase(PILOT)], is_private=lambda c: False, python="/usr/bin/python3",
                                denials=denials)

    def test_denies_search(self):
        for tool, inp in [("Grep", {"pattern": "parseBudget"}), ("Glob", {"pattern": "**/*.ts"}),
                          ("Bash", {"command": "rg -n parseBudget src"}), ("Bash", {"command": "grep -rn foo ."}),
                          ("Bash", {"command": "find src -name '*.ts'"}), ("Bash", {"command": "git grep foo"})]:
            with self.subTest(tool=tool, inp=inp):
                out = self.gate(tool, **inp)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("explore", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_others(self):
        self.assertIsNone(self.gate("Read", file_path="/x/a.ts"))
        self.assertIsNone(self.gate("Bash", command="npx vitest run"))
        self.assertIsNone(self.gate("Bash", command="python3 /x/run_iu.py explore \"where is grep used\""))
        self.assertIsNone(self.gate("Grep", cwd=OTHER, pattern="x"))  # 不在试点里
        self.assertIsNone(self.gate("Grep", denials=hook.GATE_ESCAPE, pattern="x"))  # 被拒够多次后放行


class TestEntryPoint(unittest.TestCase):
    def run_hook(self, stdin_bytes, dirs, args=None):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_bytes))), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(hook, "pilot_dirs", return_value=dirs), \
                mock.patch.object(hook, "LOG", os.devnull):
            hook.cmd_hook_pre(args)
        return out.getvalue()

    def test_malformed_input_passes_through(self):
        self.assertEqual(self.run_hook(b"not json", [PILOT]), "")
        self.assertEqual(self.run_hook(b"", [PILOT]), "")

    def test_output_shape(self):
        with mock.patch.object(hook, "private_marker", return_value=None):
            out = self.run_hook(json.dumps(payload("npm test")).encode(), [os.path.normcase(PILOT)])
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hso)  # 不替用户放行
        self.assertIn("sum", hso["updatedInput"]["command"])

    def test_codex_output_shape(self):
        """Codex 的 PreToolUse 输入（codex-rs/hooks/schema/generated/pre-tool-use.command.input.schema.json）：
        tool_input 只有 command；updatedInput 必须配 permissionDecision allow，且只带 command"""
        p = {"session_id": "s", "transcript_path": None, "cwd": PILOT, "hook_event_name": "PreToolUse",
             "model": "gpt-6-astra", "permission_mode": "default", "turn_id": "t", "tool_name": "Bash",
             "tool_use_id": "call_1", "tool_input": {"command": "npm test"}}
        with mock.patch.object(hook, "private_marker", return_value=None):
            out = self.run_hook(json.dumps(p).encode(), [os.path.normcase(PILOT)], argparse.Namespace(codex=True))
            plain = self.run_hook(b'{"tool_name": "Bash", "cwd": "/nowhere", "tool_input": {"command": "npm test"}}',
                                  [os.path.normcase(PILOT)], argparse.Namespace(codex=True))
        hso = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertEqual(list(hso["updatedInput"]), ["command"])
        argv = shlex.split(hso["updatedInput"]["command"])
        self.assertEqual((argv[2], argv[-1]), ("sum", "npm test"))
        self.assertEqual(plain, "")  # 不改写时什么都不输出（Codex 照原命令执行）


class TestSaveRaw(unittest.TestCase):
    def test_falls_back_when_data_dir_unwritable(self):
        import tempfile
        from iu import offload
        blocker = tempfile.NamedTemporaryFile(delete=False)  # DATA_DIR 指向一个文件：在它下面建目录必然失败
        blocker.close()
        try:
            with mock.patch.object(offload, "DATA_DIR", blocker.name):
                path = offload.save_raw("hello")
                self.assertTrue(path and path.startswith(tempfile.gettempdir()))
                self.assertEqual(open(path, encoding="utf-8").read(), "hello")
                os.remove(path)
                offload.log_call("sum", {}, 1)  # 写不了账本也不抛错
        finally:
            os.remove(blocker.name)


if __name__ == "__main__":
    unittest.main()
