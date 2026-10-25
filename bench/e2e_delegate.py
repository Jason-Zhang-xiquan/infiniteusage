"""端到端 A/B：真实 Opus 会话里，主控把修 bug 交给一个通用子 agent。
  base ：照常（关掉 hook-agent）
  deleg：hook-agent 打开，子 agent 先试 iu delegate（免费 agent），不过再自己做
两组都关掉长输出外包（IU_NO_OFFLOAD），只看 hook-agent 的效果。花费按主会话 + 子 agent 全部请求算等效 token。
会花付费额度：每次约一个 Opus 冲刺。Mac 上串行。

用法（Mac）: python3 bench/e2e_delegate.py [--sprints S1,S2] [--arms base,deleg]
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_run import ENV, PROMPT, sh  # noqa: E402
from ab_sprint import prepare  # noqa: E402
from free_coder import kill_group, kill_strays, mem_free  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "e2e_delegate.jsonl")
LOG = os.path.expanduser("~/.infiniteusage/offload.jsonl")
E2E_PROMPT = PROMPT + (" Delegate the investigation and the fixing to ONE general-purpose subagent (Agent tool) with "
                       "a complete, self-contained brief that includes the test command. When it returns, run the "
                       "tests once yourself to confirm before you reply.")
W = {"in": 1.0, "cw1h": 2.0, "cw5m": 1.25, "cr": 0.1, "out": 5.0}


def eq(u):
    cc = u.get("cache_creation") or {}
    c1h, c5 = cc.get("ephemeral_1h_input_tokens"), cc.get("ephemeral_5m_input_tokens")
    cw = (c1h or 0) * W["cw1h"] + (c5 or 0) * W["cw5m"] if (c1h is not None or c5 is not None) \
        else (u.get("cache_creation_input_tokens") or 0) * W["cw1h"]
    return ((u.get("input_tokens") or 0) * W["in"] + cw + (u.get("cache_read_input_tokens") or 0) * W["cr"]
            + (u.get("output_tokens") or 0) * W["out"])


def session_cost(sid):
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")) + \
        glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}/subagents/*.jsonl"))
    main = sub = 0.0
    nsub, used_delegate = 0, False
    for p in files:
        us = {}
        is_sub = "subagents" in p
        nsub += is_sub
        for line in open(p, encoding="utf-8", errors="replace"):
            if "delegate --verify" in line:
                used_delegate = True
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            if d.get("type") == "assistant" and m.get("usage") and m.get("id"):
                us[m["id"]] = m["usage"]
        c = sum(eq(u) for u in us.values())
        if is_sub:
            sub += c
        else:
            main += c
    return main, sub, nsub, used_delegate


def run(sprint, arm, tasks, rep):
    d = prepare(sprint, "C0", rep, tasks)
    env = {**ENV, "IU_NO_OFFLOAD": "1"}
    if arm == "base":
        env["IU_NO_DELEGATE"] = "1"
    t0, log_size = time.time(), os.path.getsize(LOG) if os.path.exists(LOG) else 0
    try:
        p = subprocess.Popen(["claude", "-p", E2E_PROMPT, "--model", "opus", "--output-format", "json",
                              "--allowedTools=Bash,Read,Edit,Write,Glob,Grep,Agent,Task", "--max-turns", "80"],
                             cwd=d, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=env, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=3600)
        finally:
            kill_group(p.pid)
        res = json.loads(out.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        res = {"error": str(e)[:200]}
    wall = round(time.time() - t0, 1)
    kill_strays()
    code, _ = sh("cd server && npx vitest run src/__tests__/lib", d, timeout=600)
    kill_strays()
    _, touched = sh("git status --porcelain -- server/src/__tests__", d)
    sid = res.get("session_id")
    main, sub, nsub, used = session_cost(sid) if sid else (0, 0, 0, False)
    dl = []
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            f.seek(log_size)
            dl = [json.loads(x) for x in f if '"delegate"' in x]
    r = {"ts": time.time(), "sprint": sprint, "arm": arm, "rep": rep, "wall_s": wall, "tests_pass": code == 0,
         "tests_touched": bool(touched.strip()), "eq_main": round(main), "eq_sub": round(sub),
         "eq_total": round(main + sub), "cost_usd": res.get("total_cost_usd"), "subagents": nsub,
         "used_delegate": used, "delegate_calls": [{"ok": x.get("ok"), "s": x.get("seconds"), "model": x.get("routed")}
                                                   for x in dl], "session": sid, "is_error": res.get("is_error"),
         "result": (res.get("result") or res.get("error") or "")[-300:]}
    open(OUT, "a").write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{sprint} {arm}: 测试{'全过' if r['tests_pass'] else '没过'} 等效 {r['eq_total']:,}（主 {r['eq_main']:,} / "
          f"子 {r['eq_sub']:,}）${r['cost_usd']} {wall}s 子agent {nsub} 用了delegate {used} {r['delegate_calls']}",
          flush=True)


def main():
    tasks = {t["id"]: t for t in json.load(open("/tmp/iu_tasks.json"))}
    sprints = (sys.argv[sys.argv.index("--sprints") + 1] if "--sprints" in sys.argv else "S1,S2").split(",")
    arms = (sys.argv[sys.argv.index("--arms") + 1] if "--arms" in sys.argv else "base,deleg").split(",")
    rep = 100
    for s in sprints:
        for a in arms:
            while mem_free() < 50:
                time.sleep(60)
            rep += 1
            run(s, a, tasks, rep)


if __name__ == "__main__":
    main()
