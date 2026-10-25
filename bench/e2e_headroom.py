"""端到端 A/B：headroom 代理（headroomlabs-ai/headroom 0.41.0，--mode cache）在真实 Claude Code 修 bug 冲刺上省不省。
  base    ：照常
  headroom：ANTHROPIC_BASE_URL 指向本机 headroom 代理（不用 headroom wrap：它会顺带装 Serena，混进别的变量）
两组都关掉我们自己的 hook（IU_NO_OFFLOAD / IU_NO_DELEGATE），提示词里也让它别用 iu，只看 headroom 的效果。
花费按会话记录里 API 返回的 usage 算等效 token（代理压缩后真正计费的量）。会花付费额度：每次一个 Sonnet 冲刺。

用法（Mac，先起代理）：
  HEADROOM_TELEMETRY=off HEADROOM_BEACON=off DO_NOT_TRACK=1 ~/iu_tools/headroom/bin/headroom proxy --port 8787 --mode cache
  python3 bench/e2e_headroom.py [--sprints S1,S2,S3] [--arms base,headroom] [--model sonnet]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_run import ENV, PROMPT, sh  # noqa: E402
from ab_sprint import prepare  # noqa: E402
from e2e_delegate import session_cost  # noqa: E402
from free_coder import kill_group, mem_free  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "e2e_headroom.jsonl")
PROXY = "http://127.0.0.1:8787"
PROXY_LOG = "/tmp/headroom_proxy.log"
HR_PROMPT = PROMPT + " Do the work yourself in this session: do not delegate, and do not use the infiniteusage skill or iu commands."


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def proxy_stats(offset):
    """这次运行期间代理日志里的 PERF 行：压缩前后 token、它自己估的节省、用了哪些变换"""
    if not os.path.exists(PROXY_LOG):
        return {}
    with open(PROXY_LOG, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        perf = [x for x in f if " PERF " in x]
    num = lambda k: sum(int(m.group(1)) for x in perf for m in [re.search(rf"\b{k}=(\d+)", x)] if m)
    transforms = sorted({":".join(t.split(":")[:2]) for x in perf for m in [re.search(r"transforms=(.*?) client=", x)] if m
                         for t in m.group(1).split()})
    return {"requests": len(perf), "tok_before": num("tok_before"), "tok_after": num("tok_after"),
            "total_saved_claimed": num("total_saved"), "transforms": transforms[:15]}


def run(sprint, arm, tasks, rep, model):
    d = prepare(sprint, "C0", rep, tasks)
    env = {**ENV, "IU_NO_OFFLOAD": "1", "IU_NO_DELEGATE": "1"}
    if arm == "headroom":
        env["ANTHROPIC_BASE_URL"] = PROXY
    off = os.path.getsize(PROXY_LOG) if os.path.exists(PROXY_LOG) else 0
    t0 = time.time()
    try:
        p = subprocess.Popen(["claude", "-p", HR_PROMPT, "--model", model, "--output-format", "json",
                              "--allowedTools=Bash,Read,Edit,Write,Glob,Grep", "--max-turns", "80"],
                             cwd=d, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=env, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=2400)
        finally:
            kill_group(p.pid)
        res = json.loads(out.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        res = {"error": str(e)[:200]}
    wall = round(time.time() - t0, 1)
    code, _ = sh("cd server && npx vitest run src/__tests__/lib", d, timeout=600)
    _, touched = sh("git status --porcelain -- server/src/__tests__", d)
    sid = res.get("session_id")
    main, _sub, _n, _used = session_cost(sid) if sid else (0, 0, 0, False)
    r = {"ts": time.time(), "sprint": sprint, "arm": arm, "model": model, "rep": rep, "wall_s": wall,
         "tests_pass": code == 0, "tests_touched": bool(touched.strip()), "eq_total": round(main),
         "cost_usd": res.get("total_cost_usd"), "turns": res.get("num_turns"), "session": sid,
         "is_error": res.get("is_error"), "proxy": proxy_stats(off) if arm == "headroom" else None,
         "result": (res.get("result") or res.get("error") or "")[-300:]}
    open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{sprint} {arm}: 测试{'全过' if r['tests_pass'] else '没过'} 等效 {r['eq_total']:,} ${r['cost_usd']} "
          f"{r['turns']} 轮 {wall}s {r['proxy'] or ''}", flush=True)


def main():
    tasks = {t["id"]: t for t in json.load(open("/tmp/iu_tasks.json"))}
    model = arg("--model", "sonnet")
    rep = int(arg("--rep0", "500"))
    for s in arg("--sprints", "S1,S2,S3").split(","):
        for a in arg("--arms", "base,headroom").split(","):
            while mem_free() < 50:
                time.sleep(60)
            rep += 1
            run(s, a, tasks, rep, model)


if __name__ == "__main__":
    main()
