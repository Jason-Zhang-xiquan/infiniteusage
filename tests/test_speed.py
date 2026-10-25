"""iu/speed.py：测速记录、按健康和快慢排序、过期才补测。全部在临时文件里，不联网"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu import delegate, offload, speed  # noqa: E402


class TestSpeed(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "speed.json")
        self.env = mock.patch.dict(os.environ, {"IU_SPEED_FILE": self.file, "IU_NO_SPEED_PROBE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_record_moving_average_and_reset_fails(self):
        speed.record("a", True, 10)
        speed.record("a", True, 20)
        self.assertEqual(speed.load()["a"]["real"], 14.0)  # 0.4 * 20 + 0.6 * 10
        speed.record("a", False)
        speed.record("a", False)
        self.assertEqual(speed.load()["a"]["fails"], 2)
        speed.record("a", True, 14)
        self.assertEqual(speed.load()["a"]["fails"], 0)

    def test_no_data_keeps_original_order(self):
        self.assertEqual(speed.rank(["strong", "mid", "weak"]), ["strong", "mid", "weak"])

    def test_faster_first_and_failed_last(self):
        speed.record("slow", True, 30)
        speed.record("fast", True, 5)
        speed.record("broken", True, 1)
        speed.record("broken", False)
        self.assertEqual(speed.rank(["broken", "slow", "fast"]), ["fast", "slow", "broken"])

    def test_unknown_counts_as_median(self):
        speed.record("a", True, 5)
        speed.record("b", True, 50)
        speed.record("c", True, 20)
        self.assertEqual(speed.rank(["x", "b", "a", "c"]), ["a", "x", "c", "b"])  # x 按中位数 20 算，同分时原顺序在前

    def test_probe_only_is_scaled(self):
        speed.record("p", True, 2, probe=True)  # 估 8 秒
        speed.record("r", True, 20)
        self.assertEqual(speed.rank(["r", "p"]), ["p", "r"])

    def test_close_speeds_keep_strong_first(self):
        speed.record("strong", True, 2.1, probe=True)
        speed.record("weak", True, 1.3, probe=True)  # 快不到 1.5 倍：算一样快
        self.assertEqual(speed.rank(["strong", "weak"]), ["strong", "weak"])

    def test_cooldown_expires(self):
        speed.record("a", False)
        d = speed.load()
        d["a"]["fail_at"] = time.time() - speed.COOLDOWN - 1
        speed.save(d)
        self.assertFalse(speed.cooling(speed.load()["a"]))

    def test_kick_only_when_stale(self):
        with mock.patch.object(speed.subprocess, "Popen") as popen:
            speed.kick_if_stale()
            speed.kick_if_stale()  # 刚起过，不再起
            self.assertEqual(popen.call_count, 1)
            self.assertIn("--probe", popen.call_args[0][0])
            d = speed.load()
            d["_meta"] = {"probed_at": time.time()}
            speed.save(d)
            speed.kick_if_stale()
            self.assertEqual(popen.call_count, 1)

    def test_kick_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"IU_NO_SPEED_PROBE": "1"}), \
                mock.patch.object(speed.subprocess, "Popen") as popen:
            speed.kick_if_stale()
        popen.assert_not_called()

    def test_routing_uses_speed_order_and_records(self):
        speed.record("freellmapi:m1", True, 40)
        speed.record("freellmapi:m2", True, 4)
        calls = []

        def fake_chat(system, user, model, **kw):
            calls.append(model)
            return {"choices": [{"message": {"content": "ok"}}], "_seconds": 3.0}

        with mock.patch.object(offload, "chain_for", return_value=["m1", "m2"]), \
                mock.patch.object(offload, "setting", return_value="k"), \
                mock.patch("iu.providers.ready", return_value=[]), \
                mock.patch.object(offload, "chat", side_effect=fake_chat), \
                mock.patch.object(speed, "kick_if_stale"):
            offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)
        self.assertEqual(calls, ["m2"])  # 快的先试，成功就不再试别的
        self.assertEqual(speed.load()["freellmapi:m2"]["real"], 3.6)  # 0.4 * 3 + 0.6 * 4


    def test_delegate_caps_first_attempt_and_records(self):
        root = tempfile.mkdtemp()
        for a in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
        speed.record("agent:opencode/slow", False)  # 刚卡住过：排到后面
        timeouts = []

        def fake_run(cmd, r, timeout, extra_env=None):
            timeouts.append((cmd[0], timeout))
            if cmd[0] == "hang":
                return None, "timed out"
            open(os.path.join(r, "fix.py"), "w").write("x = 1\n")
            return "done", None

        plan = [("opencode/slow", ["slow"]), ("opencode/hang", ["hang"]), ("opencode/ok", ["ok"])]
        args = argparse.Namespace(brief="fix it", brief_file=None, verify="none", verify_timeout=60, budget=420,
                                  model=None, via=None, max_models=3)
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with mock.patch.object(delegate, "attempts", return_value=plan),                     mock.patch.object(delegate, "run_agent", side_effect=fake_run),                     mock.patch.object(delegate, "log"), mock.patch("sys.stdout"):
                delegate.cmd_delegate(args)
        finally:
            os.chdir(cwd)
        self.assertEqual([c for c, _ in timeouts], ["hang", "ok"])  # 刚失败过的 slow 排到最后，ok 成功就停
        self.assertLessEqual(timeouts[0][1], 252)  # 后面还有别的：最多用 60%，不吃光 420 秒
        d = speed.load()
        self.assertEqual(d["agent:opencode/hang"]["fails"], 1)
        self.assertIn("real", d["agent:opencode/ok"])


    def test_run_agent_stops_at_quota_message(self):
        fake = [sys.executable, "-c", "import sys,time;print('ERROR stream error=AI_APICallError: Free usage limit "
                                      "reached. Subscribe to OpenCode Go');sys.stdout.flush();time.sleep(60)"]
        t0 = time.time()
        reply, err = delegate.run_agent(fake, tempfile.mkdtemp(), 120)
        self.assertEqual(err, delegate.QUOTA_ERR)
        self.assertLess(time.time() - t0, 15)  # 不等到 120 秒超时

    def test_run_agent_drops_log_lines_from_reply(self):
        fake = [sys.executable, "-c", "print('timestamp=2026 level=INFO x');print('Fixed the guard in a.py')"]
        reply, err = delegate.run_agent(fake, tempfile.mkdtemp(), 60)
        self.assertIsNone(err)
        self.assertEqual(reply, "Fixed the guard in a.py")

    def test_quota_skips_other_models_of_same_agent(self):
        root = tempfile.mkdtemp()
        for a in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
        ran = []

        def fake_run(cmd, r, timeout, extra_env=None):
            ran.append(cmd[0])
            if cmd[0].startswith("oc"):
                return None, delegate.QUOTA_ERR
            open(os.path.join(r, "fix.py"), "w").write("x = 1\n")
            return "done", None

        plan = [("opencode/a", ["oc-a"]), ("opencode/b", ["oc-b"]), ("kilo", ["kilo"])]
        args = argparse.Namespace(brief="fix it", brief_file=None, verify="none", verify_timeout=60, budget=420,
                                  model=None, via=None, max_models=3)
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with mock.patch.object(delegate, "attempts", return_value=plan),                     mock.patch.object(delegate, "run_agent", side_effect=fake_run),                     mock.patch.object(delegate, "log"), mock.patch("sys.stdout"):
                delegate.cmd_delegate(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(ran, ["oc-a", "kilo"])  # opencode 的额度用完了：b 也不试，直接换 kilo

    GEMINI = {"id": "gemini", "base_url": "https://g.example/v1", "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash"}

    def test_opencode_api_env_references_key_by_name(self):
        with mock.patch("iu.offload.setting", return_value="secret-123"):
            env = delegate.opencode_api_env([self.GEMINI])
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        p = cfg["provider"]["iu-gemini"]
        self.assertEqual(p["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(p["options"], {"baseURL": "https://g.example/v1", "apiKey": "{env:GEMINI_API_KEY}"})
        self.assertIn("gemini-2.5-flash", p["models"])
        self.assertNotIn("secret-123", env["OPENCODE_CONFIG_CONTENT"])  # 配置里只有变量名
        self.assertEqual(env["GEMINI_API_KEY"], "secret-123")  # 值只进子进程环境变量

    def test_attempts_append_api_backends_after_zen(self):
        with mock.patch("iu.agents.chain", return_value=["opencode"]), \
                mock.patch.object(delegate, "opencode_bin", return_value="opencode"), \
                mock.patch.object(delegate, "coder_models", return_value=["zen-a"]), \
                mock.patch.object(delegate, "api_backends", return_value=[self.GEMINI]):
            plan = delegate.attempts(None, None, "fix it", "/repo")
        self.assertEqual([n for n, _ in plan], ["opencode/zen-a", "opencode+gemini/gemini-2.5-flash"])
        self.assertIn("iu-gemini/gemini-2.5-flash", plan[1][1])
        with mock.patch.dict(os.environ, {"IU_NO_TRAINING_MODELS": "1"}), \
                mock.patch("iu.providers.ready", return_value=[self.GEMINI]):
            self.assertEqual(delegate.api_backends(), [])  # 免费层会拿数据训练

    def test_zen_quota_falls_back_to_own_key(self):
        root = tempfile.mkdtemp()
        for a in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
        ran = []

        def fake_run(cmd, r, timeout, extra_env=None):
            ran.append((cmd[0], extra_env))
            if cmd[0].startswith("zen"):
                return None, delegate.QUOTA_ERR
            open(os.path.join(r, "fix.py"), "w").write("x = 1\n")
            return "done", None

        plan = [("opencode/a", ["zen-a"]), ("opencode/b", ["zen-b"]), ("opencode/c", ["zen-c"]),
                ("opencode+gemini/gemini-2.5-flash", ["gem"])]
        args = argparse.Namespace(brief="fix it", brief_file=None, verify="none", verify_timeout=60, budget=420,
                                  model=None, via=None, max_models=3)
        cwd = os.getcwd()
        os.chdir(root)
        try:
            with mock.patch.object(delegate, "attempts", return_value=plan), \
                    mock.patch.object(delegate, "run_agent", side_effect=fake_run), \
                    mock.patch.object(delegate, "opencode_api_env", return_value={"OPENCODE_CONFIG_CONTENT": "{}"}), \
                    mock.patch.object(delegate, "api_backends", return_value=[self.GEMINI]), \
                    mock.patch.object(delegate, "log"), mock.patch("sys.stdout"):
                delegate.cmd_delegate(args)
        finally:
            os.chdir(cwd)
        # Zen 额度用完：b、c 不试、也不占 --max-models 的名额，直接用自己的 Gemini key；只有它拿到临时配置
        self.assertEqual(ran, [("zen-a", None), ("gem", {"OPENCODE_CONFIG_CONTENT": "{}"})])
        d = speed.load()
        self.assertTrue(all(speed.cooling(d["agent:opencode/" + x]) for x in "abc"))  # 下次 Zen 直接排到后面

    def test_opencode_does_not_get_other_secrets(self):
        env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "a", "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "s",
               "OPENCODE_CONFIG": "/c.json", "HOME": "/h"}
        with mock.patch.dict(os.environ, env, clear=True):
            oc = delegate.agent_env(["/x/opencode", "run"], {"GEMINI_API_KEY": "k", "OPENCODE_CONFIG_CONTENT": "{}"})
            other = delegate.agent_env(["/x/kilo", "run"])
        self.assertEqual({k for k in oc if k in env or k.endswith("KEY") or k.startswith("OPENCODE")},
                         {"PATH", "HOME", "OPENCODE_CONFIG", "GEMINI_API_KEY", "OPENCODE_CONFIG_CONTENT"})
        self.assertEqual(other["ANTHROPIC_API_KEY"], "a")  # 别的 agent 可能靠环境变量登录：不动


if __name__ == "__main__":
    unittest.main()
