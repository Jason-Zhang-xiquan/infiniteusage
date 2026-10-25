import os
import subprocess
import tempfile
import unittest

from iu import delegate, hook


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def tree(root):
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = open(p, "rb").read()
    return out


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        git(self.root, "init", "-q")
        open(os.path.join(self.root, "a.py"), "w").write("a = 1\n")
        open(os.path.join(self.root, "b.py"), "w").write("b = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        # 调用前就有的未提交改动和未跟踪文件，必须原样保住
        open(os.path.join(self.root, "a.py"), "w").write("a = 2  # 用户自己改的\n")
        open(os.path.join(self.root, "notes.txt"), "w").write("用户的草稿\n")

    def test_restore_brings_back_exact_state(self):
        before = tree(self.root)
        snap = delegate.snapshot(self.root)
        # 免费 agent 乱改一通
        open(os.path.join(self.root, "a.py"), "w").write("a = 3\n")
        os.remove(os.path.join(self.root, "b.py"))
        open(os.path.join(self.root, "notes.txt"), "w").write("被改了\n")
        os.makedirs(os.path.join(self.root, "new"))
        open(os.path.join(self.root, "new", "c.py"), "w").write("c = 1\n")
        open(os.path.join(self.root, "staged.py"), "w").write("s = 1\n")
        git(self.root, "add", "staged.py")
        tracked, new, edited = delegate.changes(self.root, snap)
        self.assertIn("a.py", tracked)
        self.assertIn("b.py", tracked)
        self.assertIn("staged.py", tracked)
        self.assertEqual(new, ["new/c.py"])
        self.assertEqual(edited, ["notes.txt"])
        delegate.restore(self.root, snap)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(delegate.changes(self.root, snap), ([], [], []))

    def test_no_changes(self):
        snap = delegate.snapshot(self.root)
        self.assertEqual(delegate.changes(self.root, snap), ([], [], []))


class AgentHookTest(unittest.TestCase):
    def payload(self, **inp):
        return {"tool_name": "Agent", "cwd": "/x", "tool_input": {"prompt": "fix the bug", **inp}}

    def test_general_purpose_gets_prefix(self):
        new = hook.agent_decide(self.payload(subagent_type="general-purpose"), ["*"], lambda c: False, "py")
        self.assertTrue(new["prompt"].startswith(hook.AGENT_MARK))
        self.assertIn("delegate --verify", new["prompt"])
        self.assertTrue(new["prompt"].endswith("fix the bug"))

    def test_skips_explore_private_and_already_marked(self):
        self.assertIsNone(hook.agent_decide(self.payload(subagent_type="Explore"), ["*"], lambda c: False, "py"))
        self.assertIsNone(hook.agent_decide(self.payload(), ["*"], lambda c: True, "py"))
        once = hook.agent_decide(self.payload(), ["*"], lambda c: False, "py")
        self.assertIsNone(hook.agent_decide({"tool_name": "Agent", "cwd": "/x", "tool_input": once}, ["*"],
                                            lambda c: False, "py"))
        self.assertIsNone(hook.agent_decide({**self.payload(), "tool_name": "Bash"}, ["*"], lambda c: False, "py"))


if __name__ == "__main__":
    unittest.main()
