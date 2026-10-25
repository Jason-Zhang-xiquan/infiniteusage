"""任务型能力画像：每个免费模型 × 每类任务 → 准确率、编造、延迟、失败率。在 Mac 上跑（FreeLLMAPI 在本机）。

1. 探测：候选模型各发一个小请求，记下能用的、实际路由到哪家
2. 按语料逐条跑，每个模型 N 次；同一模型两次调用至少隔 31 秒（OVH 匿名每模型每分钟 2 次），
   所有 Kilo 模型合计每小时不超过 150 次（Kilo 匿名每 IP 每小时 200 次）
3. 评分沿用 run_bench.score；testgen 类：把生成的测试放进副本，在干净代码上要通过、注入 bug 后要失败
用法（Mac）: python3 bench/profile_models.py /tmp/iu_profile.jsonl [--reps 2] [--models a,b,...]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from iu import offload  # noqa: E402
from run_bench import numbered_file, score  # noqa: E402

CORPORA = ["/tmp/iu_corpus", "/tmp/iu_corpus2"]
TYPE_OF = {"vitest_fail": "build", "vitest_pass": "build", "tsc_errors": "build", "vite_build_ok": "build",
           "npm_404": "build", "node_crash": "build", "rg_keyless": "search", "git_log": "git",
           "ask_index_kilo": "file_qa", "ask_index_ovh": "file_qa"}
CANDIDATES = ["auto", "qwen3.8-27b", "qwen3.6-27b", "qwen3.5-397b", "gpt-oss-120b", "kilo-auto",
              "nemotron-3-super-120b", "nemotron-3-ultra-550b", "laguna-s-2.1", "laguna-xs-2.1", "dots3-note-preview",
              "qwen3.5-9b", "glm-4.7-flash", "deepseek-v4-flash"]
PROF_REPO = "/tmp/flbench_prof"
ENV = {**os.environ, "PATH": "/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:" + os.environ["PATH"], "NO_COLOR": "1",
       "CI": "1"}
last_call, kilo_calls = {}, deque()


def pace(model, platform):
    wait = 31 - (time.time() - last_call.get(model, 0)) if model != "auto" else 0
    if platform == "kilo" or model in ("auto", "kilo-auto"):
        while kilo_calls and time.time() - kilo_calls[0] > 3600:
            kilo_calls.popleft()
        if len(kilo_calls) >= 150:
            wait = max(wait, 3600 - (time.time() - kilo_calls[0]) + 1)
        kilo_calls.append(time.time() + max(wait, 0))
    if wait > 0:
        time.sleep(wait)
    last_call[model] = time.time()


def probe(models):
    ok = {}
    for m in models:
        try:
            res = offload.chat("Reply with OK.", "ping", model=m, max_tokens=10, timeout=60, attempts=1)
            routed = res.get("_routed") or ""
            ok[m] = routed.split("/")[0] if routed else "?"
            print(f"probe {m}: ✓ {routed}", flush=True)
        except SystemExit as e:
            print(f"probe {m}: ✗ {str(e)[:120]}", flush=True)
        time.sleep(3)
    return ok


def grade_testgen(gold, text):
    m = re.search(r"```(?:ts|typescript)?\s*\n(.*?)```", text, re.S)
    if not m:
        return 0.0, ["(没有代码块)"]
    path = os.path.join(PROF_REPO, "server/src/__tests__/lib/_gen.test.ts")
    open(path, "w", encoding="utf-8").write(m.group(1))
    src = os.path.join(PROF_REPO, "server", gold["rel"])
    original = open(src, encoding="utf-8").read()

    def vitest():
        p = subprocess.run(["npx", "vitest", "run", "src/__tests__/lib/_gen.test.ts"], cwd=os.path.join(PROF_REPO, "server"),
                           capture_output=True, env=ENV, timeout=300)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        n = re.search(r"Tests\s+(?:\d+ failed \| )?(\d+) passed", out)
        return p.returncode, out, int(n.group(1)) if n else 0

    try:
        code_clean, _, passed = vitest()
        lines = original.split("\n")
        mut = gold["mutant"]
        assert lines[mut["line"] - 1] == mut["old"]
        lines[mut["line"] - 1] = mut["new"]
        open(src, "w", encoding="utf-8").write("\n".join(lines))
        code_mut, _, _ = vitest()
    finally:
        open(src, "w", encoding="utf-8").write(original)
        if os.path.exists(path):
            os.remove(path)
    if code_clean == 0 and passed >= 2 and code_mut != 0:
        return 1.0, []
    if code_clean == 0 and passed >= 2:
        return 0.5, ["(测试没发现注入的 bug)"]
    return 0.0, ["(测试在正确代码上就不通过)"]


def main():
    out_path = sys.argv[1]
    reps = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 2
    models = sys.argv[sys.argv.index("--models") + 1].split(",") if "--models" in sys.argv else CANDIDATES
    only_types = sys.argv[sys.argv.index("--types") + 1].split(",") if "--types" in sys.argv else None
    items = []
    for c in CORPORA:
        for i in sorted(os.listdir(c)):
            g = json.load(open(os.path.join(c, i, "gold.json"), encoding="utf-8"))
            g["task_type"] = g.get("task_type") or TYPE_OF.get(i, "other")
            if only_types and g["task_type"] not in only_types:
                continue
            items.append((g, open(os.path.join(c, i, "raw.txt"), encoding="utf-8").read()))
    if not os.path.exists(PROF_REPO):
        subprocess.run(["cp", "-Rc", "/tmp/flbench_clean", PROF_REPO], check=True)
    usable = probe(models)
    print(f"可用模型 {len(usable)}/{len(models)}：{usable}；语料 {len(items)} 条", flush=True)
    for rep in range(1, reps + 1):
        for g, raw in items:
            for m, platform in usable.items():
                pace(m, platform)
                t0 = time.time()
                rec = {"item": g["id"], "task_type": g["task_type"], "kind": g["kind"], "model": m, "rep": rep,
                       "raw_chars": len(raw)}
                try:
                    if g["kind"] == "sum":
                        text, res, _ = offload.summarize_text(g["command"], g["exit"], raw, goal=g.get("goal", ""),
                                                              model=m, timeout=120, attempts=1)
                        r, h, miss = score(g, text, raw)
                    else:
                        text, res, _ = offload.ask_text(g["question"], numbered_file(g, raw), model=m,
                                                        max_tokens=2000 if g["kind"] == "testgen" else 1200,
                                                        timeout=120, attempts=1)
                        if g["kind"] == "testgen":
                            r, miss = grade_testgen(g, text)
                            h = 0
                        else:
                            r, h, miss = score(g, text, raw)
                    rec.update(score=r, halluc=h, missing=miss, chars=len(text), seconds=round(time.time() - t0, 1),
                               routed=res.get("_routed"), output=text)
                except SystemExit as e:
                    rec.update(score=0.0, halluc=0, missing=["(调用失败)"], chars=0,
                               seconds=round(time.time() - t0, 1), error=str(e)[:200])
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"r{rep} {g['id']:<24} {m:<24} score {rec['score']:.2f} halluc {rec['halluc']} "
                      f"{rec['seconds']}s {rec.get('routed') or rec.get('error', '')[:60]}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
