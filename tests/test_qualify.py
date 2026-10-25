"""资格测试（iu/qualify.py）：评分、门槛、存结果、不合格的不用。全部在临时目录里，不联网"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("IU_SPEED_FILE", os.path.join(tempfile.mkdtemp(), "speed.json"))
os.environ["IU_NO_SPEED_PROBE"] = "1"
from iu import delegate, offload, providers, qualify  # noqa: E402

GEM = {"id": "gemini", "base_url": "https://g/v1", "key_env": "GEMINI_API_KEY", "model": "gm"}


def perfect(gold: dict) -> str:
    """照标准答案写一份满分回答"""
    return " ".join([g[0] for g in gold["required"]] + gold["locations"])


class TestQualify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(providers, "PROVIDERS", os.path.join(self.tmp, "providers.json")),
                        mock.patch.object(providers, "KEYS", os.path.join(self.tmp, "keys.env")),
                        mock.patch.object(providers, "DATA_DIR", self.tmp),
                        mock.patch.object(offload, "DATA_DIR", self.tmp),
                        mock.patch.object(qualify, "GAP", 0),
                        mock.patch.dict(os.environ, {"FREELLMAPI_KEY": "", "GEMINI_API_KEY": "k", "GROQ_API_KEY": "g"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_cases_ship_with_iu_and_perfect_answer_scores_full(self):
        for cids in qualify.TYPES.values():
            for cid in cids:
                gold, raw = qualify.load_case(cid)
                self.assertEqual(qualify.score(gold, perfect(gold), raw), (1.0, 0), cid)

    def test_fabricated_location_and_missing_facts(self):
        gold, raw = qualify.load_case("node_crash")
        q, fake = qualify.score(gold, "TypeError at iu_crash.js:2 and iu_crash.js:999", raw)
        self.assertEqual(fake, 1)
        self.assertLess(q, 0.85)

    def test_judge_thresholds(self):
        self.assertTrue(qualify.judge([1.0, 0.9, 0.9], 0, 1, 4)["ok"])  # 失败 25%：过
        self.assertFalse(qualify.judge([1.0, 1.0], 0, 2, 4)["ok"])  # 失败 50%
        self.assertFalse(qualify.judge([1.0, 1.0, 1.0, 1.0], 1, 0, 4)["ok"])  # 编造 1 处
        self.assertFalse(qualify.judge([0.8], 0, 0, 1)["ok"])  # 质量不够
        self.assertIsNone(qualify.judge([], 0, 1, 1)["ok"])  # 一次回答都没拿到（限流）：没测成，不算不合格

    def test_run_marks_only_the_failed_type(self):
        def fake_case(b, gold, raw):
            return "no idea" if gold["id"] == "git_log" else perfect(gold)

        with mock.patch.object(qualify, "run_case", side_effect=fake_case):
            q = qualify.run(GEM, delegate_too=False, say=lambda *_: None)
        self.assertEqual({k: r["ok"] for k, r in q["types"].items()},
                         {"build": True, "git": False, "search": True, "file_qa": True})

    def test_borderline_gets_second_round_fabrication_does_not(self):
        seen = []

        def fake_case(b, gold, raw):
            seen.append(gold["id"])
            if gold["id"] == "node_crash" and seen.count("node_crash") == 1:
                return "TypeError perMinute iu_crash.js:2"  # 第一轮 3/4
            if gold["id"] == "tsc_errors" and seen.count("tsc_errors") == 1:
                return "TS2322 TS2304 undefinedHelperZz"  # 3/5：build 第一轮 84% 没过
            if gold["id"] == "rg_keyless":
                return perfect(gold) + " parallel.ts:999"  # 编造：不给第二轮
            return perfect(gold)

        with mock.patch.object(qualify, "run_case", side_effect=fake_case):
            q = qualify.run(GEM, delegate_too=False, say=lambda *_: None)
        self.assertTrue(q["types"]["build"]["ok"])  # 第二轮满分，两轮合起来 92%
        self.assertEqual(seen.count("npm_404"), 2)
        self.assertFalse(q["types"]["search"]["ok"])
        self.assertEqual(seen.count("rg_keyless"), 1)
        self.assertEqual(seen.count("git_log"), 1)  # 一轮就过的不重测

    def test_budget_skips_rest_as_inconclusive(self):
        clock = iter(range(0, 10000, 200))
        with mock.patch.object(qualify, "run_case", side_effect=lambda b, g, r: perfect(g)),                 mock.patch.object(qualify.time, "time", side_effect=lambda: next(clock)),                 mock.patch.object(qualify, "BUDGET", 300):
            q = qualify.run(GEM, delegate_too=False, say=lambda *_: None)
        self.assertTrue(q["types"]["build"]["ok"])  # 测到的照常判
        self.assertIsNone(q["types"]["file_qa"]["ok"])  # 预算用完没测到：没测成

    def test_rate_limit_waits_and_retries_once(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit("HTTP 429 Too Many Requests")
            return "ok"

        with mock.patch.object(qualify.time, "sleep") as sl:
            self.assertEqual(qualify.call_with_retry(fn), ("ok", None))
        sl.assert_called_once_with(30)
        out, err = qualify.call_with_retry(lambda: (_ for _ in ()).throw(SystemExit("HTTP 401")))
        self.assertIsNone(out)
        self.assertIn("401", err)

    def test_qualify_and_save_stores_result(self):
        providers.save([dict(GEM)])
        fake = {"at": 1, "types": {"build": {"ok": True, "score": 1.0, "fabricated": 0, "failed": 0, "runs": 4}}}
        with mock.patch.object(qualify, "run", return_value=fake):
            qualify.qualify_and_save("gemini", say=lambda *_: None)
        self.assertEqual(providers.load()[0]["qualify"], fake)
        with self.assertRaises(SystemExit):
            qualify.qualify_and_save("nope", say=lambda *_: None)

    def test_allowed(self):
        self.assertTrue(providers.allowed(GEM, "git"))  # 没测过：不拦
        p = dict(GEM, qualify={"types": {"git": {"ok": False}, "build": {"ok": True}}})
        self.assertFalse(providers.allowed(p, "git"))
        self.assertTrue(providers.allowed(p, "build"))
        self.assertTrue(providers.allowed(p, "delegate"))  # 没测这项（没有 OpenCode）
        self.assertTrue(providers.allowed(dict(GEM, qualify={"types": {"git": {"ok": None}}}), "git"))  # 没测成：不拦

    def test_route_skips_backend_that_failed_this_task(self):
        providers.save([dict(GEM, qualify={"types": {"git": {"ok": False}}}),
                        {"id": "groq", "base_url": "https://y/v1", "key_env": "GROQ_API_KEY", "model": "gq"}])
        calls = []

        def fake_chat(system, user, model="auto", backend=None, **kw):
            calls.append(backend["id"])
            raise SystemExit("503")

        with mock.patch.object(offload, "chat", side_effect=fake_chat):
            with self.assertRaises(SystemExit):
                offload.chat_routed("s", "u", "route", "git", 100, False, 30, 1)
            self.assertEqual(calls, ["groq"])  # git：gemini 没过，groq 失败了也不拿它顶
            calls.clear()
            with self.assertRaises(SystemExit):
                offload.chat_routed("s", "u", "route", "build", 100, False, 30, 1)
        self.assertEqual(sorted(calls), ["gemini", "groq"])  # build：gemini 过了，照常用

    def test_delegate_backends_skip_failed_code_test(self):
        providers.save([dict(GEM, qualify={"types": {"delegate": {"ok": False}}}),
                        {"id": "groq", "base_url": "https://y/v1", "key_env": "GROQ_API_KEY", "model": "gq"}])
        self.assertEqual([b["id"] for b in delegate.api_backends()], ["groq"])

    def test_qualify_delegate_judged_by_unit_tests(self):
        def fixes(cmd, root, timeout, extra_env=None):
            self.assertIn("iu-gemini/gm", cmd)
            self.assertIn("OPENCODE_CONFIG_CONTENT", extra_env)
            with open(os.path.join(root, "stats.py"), "w") as f:
                f.write("def median(xs):\n    xs = sorted(xs)\n    n = len(xs)\n"
                        "    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2\n")
            return "done", None

        with mock.patch.object(delegate, "opencode_bin", return_value="opencode"), \
                mock.patch.object(delegate, "run_agent", side_effect=fixes):
            self.assertTrue(qualify.qualify_delegate(GEM)["ok"])
        with mock.patch.object(delegate, "opencode_bin", return_value="opencode"), \
                mock.patch.object(delegate, "run_agent", return_value=(None, "timed out")) as ra:
            r = qualify.qualify_delegate(GEM)
        self.assertEqual((r["ok"], r["passed"], r["runs"], r["error"]), (False, 0, 2, "timed out"))
        self.assertEqual(ra.call_count, 2)  # 偶尔一次没改好：再给一次机会
        with mock.patch.object(delegate, "opencode_bin", return_value="opencode"),                 mock.patch.object(delegate, "run_agent", return_value=(None, "timed out")) as ra,                 mock.patch.object(qualify.time, "time", side_effect=[0, 300] * 3):
            r = qualify.qualify_delegate(GEM, timeout=240)
        self.assertEqual(ra.call_count, 1)  # 第一次就超时：太慢，不再花一轮
        flaky = iter([("nothing", None), None])

        def second_time_lucky(cmd, root, timeout, extra_env=None):
            res = next(flaky)
            return res if res else fixes(cmd, root, timeout, extra_env)

        with mock.patch.object(delegate, "opencode_bin", return_value="opencode"), \
                mock.patch.object(delegate, "run_agent", side_effect=second_time_lucky):
            r = qualify.qualify_delegate(GEM)
        self.assertEqual((r["ok"], r["passed"], r["runs"]), (True, 1, 2))
        with mock.patch.object(delegate, "opencode_bin", side_effect=SystemExit("no")):
            self.assertIsNone(qualify.qualify_delegate(GEM))  # 没有 OpenCode：不测


if __name__ == "__main__":
    unittest.main()
