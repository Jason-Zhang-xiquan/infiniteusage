"""iu api（用户自己加的免费 API）和摘要时的回退顺序：全部在临时目录里，不联网"""

import argparse
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
from iu import install, offload, providers  # noqa: E402


class TestProviders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(providers, "PROVIDERS", os.path.join(self.tmp, "providers.json")),
                        mock.patch.object(providers, "KEYS", os.path.join(self.tmp, "keys.env")),
                        mock.patch.object(providers, "DATA_DIR", self.tmp),
                        mock.patch.object(offload, "DATA_DIR", self.tmp),
                        mock.patch.dict(os.environ, {"FREELLMAPI_KEY": "", "GEMINI_API_KEY": "", "GROQ_API_KEY": ""})]
        for p in self.patches:
            p.start()
        if os.path.exists(os.environ["IU_SPEED_FILE"]):  # 每个测试从没有测速记录开始
            os.remove(os.environ["IU_SPEED_FILE"])

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def args(self, action, name=None, **kw):
        return argparse.Namespace(action=action, name=name, base_url=kw.get("base_url"), model=kw.get("model"),
                                  key_env=kw.get("key_env"), no_qualify=kw.get("no_qualify", False))

    def test_write_key_keeps_other_lines_and_replaces_same_name(self):
        open(providers.KEYS, "w").write("FREELLMAPI_KEY=abc\nGEMINI_API_KEY=old\n")
        providers.write_key("GEMINI_API_KEY", "new")
        self.assertEqual(open(providers.KEYS).read(), "FREELLMAPI_KEY=abc\nGEMINI_API_KEY=new\n")

    def test_pick_model_prefers_pattern_and_newest(self):
        ids = ["gemini-2.0-flash-lite", "gemini-2.5-flash", "gemini-3.0-flash", "gemini-2.5-pro", "embedding-001"]
        self.assertEqual(providers.pick_model(ids, providers.PRESETS["gemini"]["prefer"]), "gemini-3.0-flash")
        self.assertEqual(providers.pick_model(["a", "b"], [r"zzz"]), "a")
        self.assertIsNone(providers.pick_model([], [r"x"]))

    def test_add_refuses_without_terminal(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(SystemExit) as e:
                providers.cmd_api(self.args("add", "gemini"))
        self.assertIn("aistudio.google.com", str(e.exception))
        self.assertFalse(os.path.exists(providers.PROVIDERS))

    def test_add_with_existing_key_picks_model_from_list(self):
        open(providers.KEYS, "w").write("GEMINI_API_KEY=k\n")
        with mock.patch.object(providers, "list_models", return_value=["gemini-2.5-flash", "gemini-2.5-pro"]),                 mock.patch("iu.qualify.qualify_and_save") as qual:
            providers.cmd_api(self.args("add", "gemini"))
        qual.assert_called_once_with("gemini")  # 加完马上跑资格测试
        item = providers.load()[0]
        self.assertEqual((item["id"], item["model"], item["key_env"]), ("gemini", "gemini-2.5-flash", "GEMINI_API_KEY"))
        self.assertNotIn("k", json.dumps(item).replace("key_env", "").replace("GEMINI_API_KEY", ""))  # 不存 key 的值
        self.assertTrue(install.free_backend_ready())

    def test_custom_needs_base_url(self):
        with self.assertRaises(SystemExit):
            providers.cmd_api(self.args("add", "myapi"))

    def test_plain_http_only_for_localhost(self):
        with self.assertRaises(SystemExit) as e:
            providers.cmd_api(self.args("add", "myapi", base_url="http://example.com/v1"))
        self.assertIn("https", str(e.exception))
        open(providers.KEYS, "w").write("MYAPI_API_KEY=k\n")
        providers.cmd_api(self.args("add", "myapi", base_url="http://127.0.0.1:3001/v1", model="m", no_qualify=True))
        self.assertEqual(providers.load()[0]["base_url"], "http://127.0.0.1:3001/v1")

    def test_route_falls_back_to_user_providers_in_order(self):
        open(providers.KEYS, "w").write("GROQ_API_KEY=g\nGEMINI_API_KEY=k\n")
        providers.save([{"id": "gemini", "base_url": "https://x/v1", "key_env": "GEMINI_API_KEY", "model": "gm"},
                        {"id": "groq", "base_url": "https://y/v1", "key_env": "GROQ_API_KEY", "model": "gq"}])
        calls = []

        def fake_chat(system, user, model="auto", backend=None, **kw):
            calls.append((backend or {}).get("id"))
            if backend and backend["id"] == "gemini":
                raise SystemExit("429")
            return {"model": model, "choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(offload, "chat", side_effect=fake_chat):
            res = offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)
        self.assertEqual(calls, ["gemini", "groq"])  # 没有 FreeLLMAPI：直接按加的顺序
        self.assertEqual(res["model"], "gq")

    def test_route_without_any_backend_says_how_to_add(self):
        with self.assertRaises(SystemExit) as e:
            offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)
        self.assertIn("iu api add", str(e.exception))


if __name__ == "__main__":
    unittest.main()
