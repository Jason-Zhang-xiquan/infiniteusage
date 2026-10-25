"""资格测试：iu api add 加完一个模型，按能力画像的同一套语料和门槛（docs/profiles.md）测一遍，
没过的那类任务以后不交给它。

- 摘要 / 问答：iu/qualify_cases 里 7 条真实输出（从 bench/corpus 挑的），评分规则同 bench/run_bench.score：
  标准答案里的必需内容和「文件:行」命中多少；摘要里出现原始输出中没有的「文件:行」算编造
- 门槛同能力画像：回答质量 ≥ 85%、编造 0、失败率 ≤ 30%
- 改代码：有 OpenCode 时，让它用这家的 key 修一个小 bug（临时目录里的 git 仓库），单元测试通过才算过
- 结果存在 providers.json 里这一家的 "qualify" 字段；没测过的不拦（iu api qualify <名字> 补测）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .i18n import t

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "qualify_cases")
TYPES = {"build": ["npm_404", "node_crash", "tsc_errors", "vite_build_ok"], "git": ["git_log"],
         "search": ["rg_keyless"], "file_qa": ["ask_index_kilo"]}
MIN_SCORE, MAX_FAIL = 0.85, 0.30
BUDGET = 300  # 摘要 / 问答这部分最多测几秒：慢的或一直限流的模型，剩下的就不测了（那几类记「没测成」，不拦）
GAP = 3  # 两次调用之间等几秒：免费层大多按每分钟次数限流
LABEL = {"build": ("测试 / 构建输出摘要", "test / build output summaries"),
         "git": ("git 输出摘要", "git output summaries"),
         "search": ("搜索结果摘要", "search result summaries"),
         "file_qa": ("读文件回答问题", "answering questions about files"),
         "delegate": ("改代码（OpenCode 用这个 key）", "code changes (OpenCode with this key)")}


def echo(s: str):
    print(s, flush=True)  # 测一次要一两分钟：边测边显示


def label(task: str) -> str:
    zh, en = LABEL.get(task, (task, task))
    return t(zh, en)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def loc_hit(loc: str, out: str, kind: str) -> bool:
    m = re.match(r"(.+?)[:(](\d+)", loc)
    name, num = m.group(1), m.group(2)
    if kind == "ask":  # 读单个文件回答问题：只看行号
        return re.search(r"\b" + num + r"\b", out) is not None
    return re.search(re.escape(name) + r"[^\n]{0,40}?\b" + num + r"\b", out) is not None


def alt_hit(alt: str, n_out: str) -> bool:
    m = re.fullmatch(r"(\d+) failed", alt.strip())
    if m:  # 「N failed」：数字和 fail 靠在一起就算
        n = m.group(1)
        return re.search(rf"\b{n}\b[^\n]{{0,25}}fail|fail[^\n]{{0,25}}\b{n}\b", n_out) is not None
    return norm(alt) in n_out


def score(gold: dict, out: str, raw: str):
    """返回 (质量 0~1, 编造的位置数)"""
    n_out = norm(out)
    hits = [any(alt_hit(a, n_out) for a in g) for g in gold["required"]]
    hits += [loc_hit(loc, out, gold["kind"]) for loc in gold["locations"]]
    fake = 0
    if gold["kind"] == "sum":
        for name, num in re.findall(r"([\w\-.]+\.(?:ts|tsx|js|mjs|py|json))\s*(?::|\(|,?\s*line\s+)(\d+)", out):
            if not re.search(re.escape(name) + r"\s*[:(]\s*" + num + r"\b", raw):
                fake += 1
    return (sum(hits) / len(hits) if hits else 1.0), fake


def load_case(cid: str):
    d = os.path.join(CASES, cid)
    with open(os.path.join(d, "gold.json"), encoding="utf-8") as f:
        gold = json.load(f)
    with open(os.path.join(d, "raw.txt"), encoding="utf-8") as f:
        return gold, f.read()


def run_case(b: dict, gold: dict, raw: str) -> str:
    from . import offload
    if gold["kind"] == "ask":
        name = gold["files"][0] if gold["files"] else "file"
        files = f"=== {name} ===\n" + "\n".join(f"{i}: {line}" for i, line in enumerate(raw.splitlines(), 1))
        out, _, _ = offload.ask_text(gold["question"], files, timeout=120, attempts=1, backend=b)
    else:
        out, _, _ = offload.summarize_text(gold["command"], gold["exit"], raw, gold.get("goal", ""), timeout=120,
                                           attempts=1, backend=b)
    return out


def call_with_retry(fn):
    """限流（429 / rate limit）就等 30 秒再试一次。返回 (结果, 错误)"""
    for i in range(2):
        try:
            return fn(), None
        except SystemExit as e:
            err = str(e)
            if i == 0 and re.search(r"429|rate.?limit|too many requests", err, re.I):
                time.sleep(30)
                continue
            return None, err
    return None, "rate limited"


def judge(scores: list, fakes: int, fails: int, n: int) -> dict:
    """ok：True 过、False 没过、None 没测成（一次都没拿到回答：限流、网络、超时预算，说明不了能力，不拦）"""
    ok_runs = len(scores)
    q = sum(scores) / ok_runs if ok_runs else 0.0
    passed = None if not ok_runs else (q >= MIN_SCORE and fakes == 0 and fails / n <= MAX_FAIL)
    return {"ok": passed, "score": round(q, 3), "fabricated": fakes, "failed": fails, "runs": n}


DELEGATE_FILES = {
    "stats.py": "def median(xs):\n    xs = sorted(xs)\n    return xs[len(xs) // 2]\n",
    "test_stats.py": ("import unittest\nfrom stats import median\n\n\nclass T(unittest.TestCase):\n"
                      "    def test_odd(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n\n"
                      "    def test_even(self):\n        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n\n"
                      "if __name__ == \"__main__\":\n    unittest.main()\n"),
}
DELEGATE_BRIEF = ("median() in stats.py returns the wrong value for lists with an even number of items. Fix it so that "
                  "`python -m unittest` passes. Only change stats.py.")


def qualify_delegate(b: dict, timeout: int = 240, tries: int = 2):
    """有 OpenCode 才测，没有返回 None（不测，不拦）。最多试 tries 次，有一次单元测试通过就算过：
    同一个模型偶尔会一次没改好，真委托时有验证命令兜底、失败自动还原"""
    from . import delegate
    try:
        ob = delegate.opencode_bin()
    except SystemExit:
        return None
    runs = []
    for _ in range(tries):
        r = delegate_once(delegate, ob, b, timeout)
        runs.append(r)
        if r["ok"] or r["seconds"] >= timeout * 0.9:  # 超时说明太慢，不再花一轮
            break
    last = runs[-1]
    return {"ok": last["ok"], "seconds": last["seconds"], "error": last["error"],
            "passed": sum(r["ok"] for r in runs), "runs": len(runs)}


def delegate_once(delegate, ob: str, b: dict, timeout: int) -> dict:
    root = tempfile.mkdtemp(prefix="iu_qualify_")
    try:
        for name, text in DELEGATE_FILES.items():
            with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        for a in (["init", "-q"], ["add", "."], ["-c", "user.email=iu@local", "-c", "user.name=iu", "commit", "-qm", "init"]):
            subprocess.run(["git", *a], cwd=root, capture_output=True)
        t0 = time.time()
        reply, err = delegate.run_agent(delegate.api_cmd(ob, b, root, DELEGATE_BRIEF + delegate.GUIDE), root, timeout,
                                        delegate.opencode_api_env([b]))
        secs = round(time.time() - t0, 1)
        test = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=root, capture_output=True, timeout=60)
        ok = test.returncode == 0
        why = err or t("单元测试没通过", "unit tests failed") + (f"：{' '.join((reply or '').split())[-120:]}" if reply else "")
        return {"ok": ok, "seconds": secs, "error": "" if ok else why[:200]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(b: dict, delegate_too: bool = True, say=echo) -> dict:
    from . import speed
    from .offload import speed_key
    types, first, deadline = {}, True, time.time() + BUDGET
    for task, cids in TYPES.items():
        scores, fakes, fails, n = [], 0, 0, 0
        for rnd in range(2):
            for cid in cids:
                if time.time() > deadline:
                    say(t(f"  {cid}: 没测（超过 {BUDGET} 秒预算）", f"  {cid}: skipped (over the {BUDGET} s budget)"))
                    continue
                n += 1
                if not first:
                    time.sleep(GAP)
                first = False
                gold, raw = load_case(cid)
                t0 = time.time()
                out, err = call_with_retry(lambda: run_case(b, gold, raw))
                if err:
                    fails += 1
                    speed.record(speed_key((b["model"], b)), False)
                    say(t(f"  {cid}: 失败（{err[:100]}）", f"  {cid}: failed ({err[:100]})"))
                    continue
                speed.record(speed_key((b["model"], b)), True, time.time() - t0)
                q, fake = score(gold, out, raw)
                scores.append(q)
                fakes += fake
                say(t(f"  {cid}: 质量 {q:.0%}" + (f"，编造 {fake} 处" if fake else ""),
                      f"  {cid}: quality {q:.0%}" + (f", {fake} fabricated" if fake else "")))
            r = judge(scores, fakes, fails, max(n, 1))
            if rnd or r["ok"] is not False or fakes or time.time() > deadline:
                break
            # 没过、也没编造：再测一轮，两轮合起来判（能力画像也是每个模型测 2 次）
            say(t(f"  {label(task)}没过门槛，再测一轮", f"  {label(task)}: below the bar, one more round"))
        types[task] = r
    if delegate_too:
        say(t("  改代码：OpenCode 用这个 key 修一个小 bug……", "  code change: OpenCode fixes a small bug with this key..."))
        r = qualify_delegate(b)
        if r is None:
            say(t("  没有 OpenCode，跳过改代码这项（不拦）", "  OpenCode not found; code-change test skipped (not blocked)"))
        else:
            types["delegate"] = r
    return {"at": time.time(), "types": types}


def show(q: dict, say=echo):
    for task, r in q.get("types", {}).items():
        mark = mark_of(r)
        if r["ok"] is None:
            say(f"  {mark} {label(task)}" + t("：没测成（限流、网络或太慢），先照常用；稍后可以重测",
                                               ": inconclusive (rate limits, network or too slow); used as usual, re-test later"))
            continue
        if task == "delegate":
            detail = t(f"{r.get('passed', int(r['ok']))}/{r.get('runs', 1)} 次通过，{r['seconds']} 秒",
                       f"passed {r.get('passed', int(r['ok']))}/{r.get('runs', 1)}, {r['seconds']} s") + \
                (t("，", ", ") + r["error"] if r.get("error") else "")
        else:
            detail = t(f"质量 {r['score']:.0%}，编造 {r['fabricated']}，失败 {r['failed']}/{r['runs']}",
                       f"quality {r['score']:.0%}, fabricated {r['fabricated']}, failed {r['failed']}/{r['runs']}")
        say(f"  {mark} {label(task)}" + t("：", ": ") + detail)


def mark_of(r: dict) -> str:
    return "?" if r.get("ok") is None else ("✓" if r["ok"] else "✗")


def qualify_and_save(name: str, delegate_too: bool = True, say=echo) -> dict:
    from . import providers
    items = providers.load()
    b = next((p for p in items if p["id"] == name), None)
    if not b:
        raise SystemExit(t(f"没有叫 {name} 的 API，先 iu api add {name}", f"No API named {name}; run iu api add {name} first"))
    say(t(f"资格测试 {name}（{b['model']}）：7 条真实输出摘要 / 问答" + ("，加 1 个改代码任务" if delegate_too else "")
          + "，通常 1~3 分钟（最长约 10 分钟），用的是这家的免费额度",
          f"Qualifying {name} ({b['model']}): 7 real outputs to summarize / answer"
          + (", plus 1 code change" if delegate_too else "") + "; usually 1-3 minutes (at most about 10), on this provider's free quota"))
    q = run(b, delegate_too, say)
    items = providers.load()  # 测的时候可能被改过：重新读再写
    for p in items:
        if p["id"] == name:
            p["qualify"] = q
    providers.save(items)
    say(t("结果（没过的那类任务以后不交给它）：", "Result (task types it failed will not be sent to it):"))
    show(q, say)
    oks = [r["ok"] for r in q["types"].values()]
    if None in oks:
        say(t(f"有没测成的：稍后运行 iu api qualify {name} 重测", f"Some tests were inconclusive: re-run iu api qualify {name} later"))
    if oks and all(x is False for x in oks):
        say(t(f"一项都没过：iu 不会用它。限流或网络问题导致的，稍后可以 iu api qualify {name} 重测",
              f"Passed nothing, so iu will not use it. If it was rate limiting or the network, re-run iu api qualify {name} later"))
    return q
