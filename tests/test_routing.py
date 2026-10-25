"""iu.offload 路由的单元测试：按任务类型取链、失败依次换下一个、全失败时报错（调用方再走规则过滤）"""

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
from iu import offload  # noqa: E402


class TestTaskType(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(offload.task_type_of("git log --stat -40"), "git")
        self.assertEqual(offload.task_type_of("cd server && git show abc"), "git")
        self.assertEqual(offload.task_type_of("rg -n keyless src"), "search")
        self.assertEqual(offload.task_type_of("grep -rn TODO src"), "search")
        self.assertEqual(offload.task_type_of("cd server && npx vitest run"), "build")
        self.assertEqual(offload.task_type_of("pytest -x"), "build")


class TestChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"routes": {"build": ["m1", "m2"], "testgen": []}}, self.tmp)
        self.tmp.close()
        self.p = mock.patch.object(offload, "ROUTES", self.tmp.name)
        self.p.start()
        if os.path.exists(os.environ["IU_SPEED_FILE"]):  # 每个测试从没有测速记录开始
            os.remove(os.environ["IU_SPEED_FILE"])

    def tearDown(self):
        self.p.stop()
        os.unlink(self.tmp.name)

    def test_chain_lookup(self):
        self.assertEqual(offload.chain_for("build"), ["m1", "m2"])
        self.assertEqual(offload.chain_for("unknown"), ["auto"])
        with mock.patch.object(offload, "ROUTES", "/nonexistent/routes.json"):
            self.assertEqual(offload.chain_for("build"), ["auto"])

    def test_falls_through_to_next_model(self):
        calls = []

        def fake_chat(system, user, model, **kw):
            calls.append(model)
            if model == "m1":
                raise SystemExit("HTTP 429")
            return {"choices": [{"message": {"content": "ok"}}], "_routed": "x/" + model}

        with mock.patch.object(offload, "chat", side_effect=fake_chat):
            res = offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)
        self.assertEqual(calls, ["m1", "m2"])
        self.assertEqual(res["_routed"], "x/m2")

    def test_all_fail_raises(self):
        with mock.patch.object(offload, "chat", side_effect=SystemExit("HTTP 429")):
            with self.assertRaises(SystemExit):
                offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)

    def test_empty_chain_raises(self):
        with self.assertRaises(SystemExit):
            offload.chat_routed("s", "u", "route", "testgen", 100, False, 30, 1)

    def test_explicit_model_bypasses_routes(self):
        with mock.patch.object(offload, "chat", return_value={"ok": 1}) as c:
            offload.chat_routed("s", "u", "auto", "build", 100, False, 30, 2)
        self.assertEqual(c.call_args.kwargs["model"], "auto")


if __name__ == "__main__":
    unittest.main()
