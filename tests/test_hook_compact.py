"""iu hook-compact（压缩后把 NOTES.md 塞回上下文）的单元测试：全部在临时目录里"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测速记录写到临时文件、不起后台补测进程（iu/speed.py）
os.environ.setdefault("IU_SPEED_FILE", os.path.join(tempfile.mkdtemp(), "speed.json"))
os.environ["IU_NO_SPEED_PROBE"] = "1"
from iu import hook  # noqa: E402


def payload(cwd, source="compact", **extra):
    return {"hook_event_name": "SessionStart", "source": source, "cwd": cwd, "session_id": "s1", **extra}


class TestCompactContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        self.sub = os.path.join(self.repo, "src", "pkg")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(self.sub)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_injects_notes_after_compact(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- sign reads IU-46\n- ladybug #D7263D on the sign\n")
        ctx = hook.compact_context(payload(self.repo))
        self.assertIn("IU-46", ctx)
        self.assertIn("ladybug #D7263D", ctx)
        self.assertIn("compaction", ctx)

    def test_only_on_compact_or_startup(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "x")
        for src in ("resume", "clear", "fork", None):
            self.assertIsNone(hook.compact_context(payload(self.repo, source=src)), src)
        self.assertIsNone(hook.compact_context({**payload(self.repo), "hook_event_name": "PreToolUse"}))
        self.assertIsNone(hook.compact_context({**payload(self.repo, source="startup"), "hook_event_name": "PreToolUse"}))

    def test_injects_notes_at_startup(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- sign reads IU-46\n- 7 birds\n")
        ctx = hook.compact_context(payload(self.sub, source="startup"))
        self.assertIn("IU-46", ctx)
        self.assertIn("7 birds", ctx)
        self.assertIn("earlier sessions", ctx)
        self.assertIn("latest words win", ctx)
        self.assertNotIn("compaction", ctx)

    def test_startup_ignores_transcript(self):
        notes = os.path.join(self.repo, "NOTES.md")
        self.write(notes, "- sign reads IU-46\n")
        os.utime(notes, (1, 1))
        tr = os.path.join(self.tmp, "t.jsonl")
        self.write(tr, json.dumps({"type": "user", "timestamp": "2026-10-24T10:00:00Z",
                                   "message": {"content": "sign should read IU-99"}}) + "\n")
        ctx = hook.compact_context(payload(self.repo, source="startup", transcript_path=tr))
        self.assertIn("IU-46", ctx)
        self.assertNotIn("IU-99", ctx)

    def test_startup_truncates_long_notes(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- item\n" * 5000)
        ctx = hook.compact_context(payload(self.repo, source="startup"))
        self.assertLessEqual(len(ctx), hook.CONTEXT_MAX)
        self.assertIn("truncated here", ctx)

    def test_finds_notes_at_project_root_from_subdir(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "root notes")
        self.assertIn("root notes", hook.compact_context(payload(self.sub)))

    def test_cwd_notes_win_over_root(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "root notes")
        self.write(os.path.join(self.sub, "NOTES.md"), "sub notes")
        ctx = hook.compact_context(payload(self.sub))
        self.assertIn("sub notes", ctx)
        self.assertNotIn("root notes", ctx)

    def test_no_notes_or_empty_notes(self):
        self.assertIsNone(hook.compact_context(payload(self.repo)))
        self.write(os.path.join(self.repo, "NOTES.md"), "  \n\n")
        self.assertIsNone(hook.compact_context(payload(self.repo)))

    def test_no_git_uses_cwd_only(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.write(os.path.join(self.tmp, "NOTES.md"), "parent notes")  # 不在项目里，不该被捡到
        self.assertIsNone(hook.compact_context(payload(plain)))

    def test_long_notes_truncated_under_claude_cap(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "a" * 50_000)
        ctx = hook.compact_context(payload(self.repo))
        self.assertLess(len(ctx), 10_000)  # Claude Code 超过 10000 字就只给预览
        self.assertIn("Read", ctx)

    def transcript(self, entries):
        path = os.path.join(self.tmp, "session.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def test_user_messages_after_last_notes_save_are_restored(self):
        notes = os.path.join(self.repo, "NOTES.md")
        self.write(notes, "- sign reads IU-45\n")
        os.utime(notes, (1_800_000_000, 1_800_000_000))  # 2027-01-15T08:00:00Z
        t = self.transcript([
            {"type": "user", "timestamp": "2027-01-15T07:59:00Z", "message": {"content": "old: sign reads IU-45"}},
            {"type": "user", "timestamp": "2027-01-15T08:01:00Z",
             "message": {"content": "Last changes: the sign now reads IU-46, fish #00B4D8"}},
            {"type": "assistant", "timestamp": "2027-01-15T08:01:10Z", "message": {"content": [{"type": "text", "text": "noted"}]}},
            {"type": "user", "timestamp": "2027-01-15T08:01:20Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "file body"}]}},
            {"type": "user", "timestamp": "2027-01-15T08:02:00Z", "isCompactSummary": True,
             "message": {"content": "This session is being continued from a previous conversation"}},
            {"type": "user", "timestamp": "2027-01-15T08:02:30Z", "message": {"content": "<command-name>/compact</command-name>"}},
            {"type": "user", "timestamp": "2027-01-15T08:03:00Z",
             "message": {"content": [{"type": "text", "text": "Also: a ladybug #D7263D on the sign"}]}},
            "not json",
        ])
        ctx = hook.compact_context(payload(self.repo, transcript_path=t))
        self.assertIn("IU-46, fish #00B4D8", ctx)
        self.assertIn("ladybug #D7263D", ctx)
        self.assertLess(ctx.index("IU-46, fish"), ctx.index("ladybug"))  # 按时间顺序
        for absent in ("old: sign", "file body", "being continued", "/compact"):
            self.assertNotIn(absent, ctx)
        self.assertIn(t, ctx)

    def test_no_recent_messages_no_extra_section(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- 7 birds\n")
        t = self.transcript([{"type": "user", "timestamp": "2000-01-01T00:00:00Z", "message": {"content": "ancient"}}])
        ctx = hook.compact_context(payload(self.repo, transcript_path=t))
        self.assertNotIn("ancient", ctx)
        self.assertNotIn("after NOTES.md was last saved", ctx)
        self.assertIn("7 birds", hook.compact_context(payload(self.repo, transcript_path="/no/such/file.jsonl")))

    def test_long_notes_and_messages_stay_under_cap(self):
        notes = os.path.join(self.repo, "NOTES.md")
        self.write(notes, "n" * 50_000)
        os.utime(notes, (1_800_000_000, 1_800_000_000))
        t = self.transcript([{"type": "user", "timestamp": f"2027-01-15T09:0{i}:00Z", "message": {"content": f"msg{i} " + "m" * 2000}}
                             for i in range(8)])
        ctx = hook.compact_context(payload(self.repo, transcript_path=t))
        self.assertLess(len(ctx), 10_000)
        self.assertIn("msg7", ctx)  # 最新的那条一定在
        self.assertNotIn("msg0", ctx)

    def run_cmd(self, data, env=None):
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            hook.cmd_hook_compact(None)
        return out.getvalue()

    def test_cmd_outputs_session_start_json(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- 7 birds\n")
        out = json.loads(self.run_cmd(payload(self.repo)))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("7 birds", hso["additionalContext"])

    def test_cmd_silent_on_bad_input_and_when_disabled(self):
        self.write(os.path.join(self.repo, "NOTES.md"), "- 7 birds\n")
        self.assertEqual(self.run_cmd(b"not json"), "")
        self.assertEqual(self.run_cmd(payload(self.repo), env={"IU_NO_NOTES": "1"}), "")


if __name__ == "__main__":
    unittest.main()
