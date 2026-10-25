import unittest
from unittest import mock

from iu import agents, delegate


class AgentsTest(unittest.TestCase):
    def test_default_chain_free_first_no_subscription(self):
        found = [{"id": "codex", "cost": "sub", "status": "untested"},
                 {"id": "qwen", "cost": "byo", "status": "untested"},
                 {"id": "kilo", "cost": "free", "status": "untested"},
                 {"id": "opencode", "cost": "free", "status": "tested"}]
        self.assertEqual(agents.default_chain(found), ["opencode", "kilo", "qwen"])

    def test_build_cmd_keeps_prompt_as_one_argument(self):
        a = agents.agent_by_id("codex")
        cmd = agents.build_cmd(a, "/bin/codex", 'fix "it"\nnow {dir}', "/repo")
        self.assertEqual(cmd[:2], ["/bin/codex", "exec"])
        self.assertIn("/repo", cmd)
        self.assertEqual(cmd[-1], 'fix "it"\nnow {dir}')  # 任务说明原样，不做替换

    def test_attempts_expand_opencode_models_then_other_agents(self):
        with mock.patch.object(agents, "chain", return_value=["opencode", "kilo", "codex"]), \
             mock.patch.object(delegate, "opencode_bin", return_value="oc"), \
             mock.patch.object(agents, "find_bin", side_effect=lambda a: {"kilo": "kilo"}.get(a["id"])):
            names = [n for n, _ in delegate.attempts(None, None, "task", "/r")]
        self.assertEqual(names[:len(delegate.CODER_MODELS)], [f"opencode/{m}" for m in delegate.CODER_MODELS])
        self.assertEqual(names[-1], "kilo")  # codex 没装就跳过
        self.assertNotIn("codex", names)

    def test_skips_agent_that_is_the_host(self):
        with mock.patch.dict("os.environ", {"OPENCODE": "1"}),              mock.patch.object(agents, "chain", return_value=["opencode", "kilo"]),              mock.patch.object(delegate, "opencode_bin", return_value="oc"),              mock.patch.object(agents, "find_bin", side_effect=lambda a: {"kilo": "kilo"}.get(a["id"])):
            names = [n for n, _ in delegate.attempts(None, None, "task", "/r")]
        self.assertEqual(names, ["kilo"])

    def test_via_forces_one_agent(self):
        with mock.patch.object(agents, "find_bin", return_value="codex"):
            names = [n for n, _ in delegate.attempts("codex", None, "task", "/r")]
        self.assertEqual(names, ["codex"])


if __name__ == "__main__":
    unittest.main()
