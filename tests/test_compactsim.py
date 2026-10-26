"""iu/compactsim.py 的单元测试：日志全是临时目录里造的，不读真实的 ~/.claude"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu import compactsim as cs  # noqa: E402

T0 = time.time() - 3 * 86400


def ts(i: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(T0 + i * 30))


def req(i: int, C: int, mid: str, new: int = 5_000) -> dict:
    return {"type": "assistant", "timestamp": ts(i), "cwd": "/work/demo",
            "message": {"id": mid, "model": "claude-x",
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": new,
                                  "cache_creation": {"ephemeral_1h_input_tokens": new},
                                  "cache_read_input_tokens": max(C - new - 10, 0), "output_tokens": 500}}}


def boundary(i: int, pre: int, post: int = 15_000, trigger: str = "auto") -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts(i),
            "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": post}}


def growing(prefix: str, n: int, start: int = 20_000, step: int = 5_000, i0: int = 0) -> list:
    return [req(i0 + k, start + k * step, f"{prefix}{k}") for k in range(n)]


class TestCompactSim(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def write(self, name: str, rows: list):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return p

    def load(self):
        return cs.load_sessions(T0 - 86400, self.root)

    def test_load_dedupes_and_skips_subagents(self):
        rows = growing("a", 5)
        self.write("proj/s1.jsonl", rows + [boundary(6, 400_000)])
        self.write("proj/s2.jsonl", rows + [boundary(6, 400_000)] + growing("b", 2, i0=7))  # 接着 s1 继续，抄了旧记录
        self.write("proj/s1/subagents/agent-1.jsonl", growing("c", 3))
        side = req(9, 50_000, "d0")
        side["isSidechain"] = True
        self.write("proj/s3.jsonl", [side])
        ss = self.load()
        reqs = [e for s in ss for e in s["events"] if e[0] == "req"]
        resets = [e for s in ss for e in s["events"] if e[0] == "reset"]
        self.assertEqual(len(reqs), 7)
        self.assertEqual(len(resets), 1)
        self.assertEqual(ss[0]["cwd"], "/work/demo")

    def test_simulate_noop_matches_actual(self):
        self.write("p/s.jsonl", growing("a", 100))
        r = cs.simulate(self.load(), 0)
        self.assertAlmostEqual(r["actual"], r["sim"], delta=1)

    def test_long_session_saves_short_does_not(self):
        self.write("p/long.jsonl", growing("a", 200))  # 长到 1015K
        self.write("p/short.jsonl", growing("b", 30))  # 最长 165K
        ss = self.load()
        r = cs.simulate(ss, 300_000)
        self.assertGreater(r["compactions"], 0)
        self.assertGreater(r["saving"], 0.2)
        short = [s for s in ss if s["path"].endswith("short.jsonl")]
        r2 = cs.simulate(short, 300_000)
        self.assertEqual(r2["compactions"], 0)
        self.assertAlmostEqual(r2["saving"], 0, places=6)

    def test_choose_reasons(self):
        self.write("p/s.jsonl", growing("a", 50))
        r = cs.choose(self.load(), cs.iso(T0 - 86400))
        self.assertEqual((r["reason"], r["compact_at"]), ("few", cs.CANDIDATES[0]))
        for k in range(12):  # 请求够多，但都没长到 300K
            self.write(f"p/short{k}.jsonl", growing(f"s{k}-", 30, i0=100 + k * 40))
        r = cs.choose(self.load(), cs.iso(T0 - 86400))
        self.assertEqual((r["reason"], r["compact_at"]), ("short", 0))
        for k in range(3):
            self.write(f"p/long{k}.jsonl", growing(f"l{k}-", 190, i0=1000 + k * 200))
        r = cs.choose(self.load(), cs.iso(T0 - 86400))
        self.assertEqual(r["reason"], "sim")
        self.assertIn(r["compact_at"], cs.CANDIDATES)
        self.assertEqual(len(r["rows"]), len(cs.CANDIDATES))

    def test_choose_until_ignores_later_logs(self):
        for k in range(3):
            self.write(f"p/long{k}.jsonl", growing(f"l{k}-", 190, i0=k * 200))
        cut = cs.iso(T0 - 3600)  # 所有请求都在截止时间之后
        r = cs.choose(self.load(), cs.iso(T0 - 86400), cut)
        self.assertEqual(r["requests"], 0)

    def test_audit_counts_paid_off_and_lost(self):
        long_after = growing("a", 60, start=300_000, step=2_000) + [boundary(60, 420_000)] \
            + growing("b", 80, start=20_000, step=2_000, i0=61)
        self.write("p/won.jsonl", long_after)
        self.write("p/lost.jsonl", growing("c", 60, start=300_000, step=2_000, i0=300)
                   + [boundary(360, 420_000)] + [req(361, 25_000, "d0")])
        self.write("p/manual.jsonl", growing("e", 5, i0=500) + [boundary(505, 400_000, trigger="manual")])
        self.write("p/small.jsonl", growing("f", 5, i0=600) + [boundary(605, 160_000)])  # 小窗口模型自己压缩
        self.write("p/short.jsonl", growing("g", 10, i0=700))
        a = cs.audit(self.load(), T0 - 60, time.time(), 300_000)
        self.assertEqual(len(a["items"]), 2)
        won = next(x for x in a["items"] if x["path"].endswith("won.jsonl"))
        lost = next(x for x in a["items"] if x["path"].endswith("lost.jsonl"))
        self.assertGreater(won["net"], 0)
        self.assertLess(lost["net"], 0)
        self.assertEqual(won["after_requests"], 80)
        self.assertEqual(a["other_compactions"], 2)
        self.assertEqual(a["short_sessions"], 1)
        self.assertAlmostEqual(a["net"], won["net"] + lost["net"])

    def test_audit_stops_at_default_limit(self):
        rows = growing("a", 5, start=300_000) + [boundary(5, 900_000, post=15_000)] \
            + growing("b", 50, start=20_000, step=10_000, i0=6)
        self.write("p/s.jsonl", rows)
        a = cs.audit(self.load(), T0 - 60, time.time(), 300_000)
        # 拿掉约 885K：之后第 6 次请求起不压缩也超过 950K（Claude Code 自己会压缩），只算前 5 次，每次约省 88K
        self.assertTrue(4 * 80_000 < a["items"][0]["saved"] < 6 * 90_000, a["items"][0]["saved"])


if __name__ == "__main__":
    unittest.main()
