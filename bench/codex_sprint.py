"""Codex 版修 bug 冲刺 A/B：同一个冲刺（bench/ab_sprint.py 的 S1~S3：4 个注入的 bug + 查代码的问题），用 codex exec 一次做完。

组：
  D     默认（不改压缩点；272K 窗口的模型约在 232K 压缩）
  K150  -c model_auto_compact_token_limit=150000（iu install 给 Codex 装的值）
  K<N>  -c model_auto_compact_token_limit=<N>000（比如 K60：冲刺上下文到不了 150K 时，用来看「压缩真的发生」时的代价）
评分同 ab_sprint.grade：lib 单元测试全过 + 没改测试文件 + ANSWERS.md 有答案关键字。
用量从 Codex 自己的会话记录（~/.codex/sessions/**/rollout-*<thread_id>.jsonl）按每次请求累加：
等效 token = 未缓存输入 1 + 缓存读 0.1 + 输出 8（和 iu/report.py 一致）；同时记最大上下文、压缩次数。
两组都在提示里加一句「不要调用 iu」：Mac 的全局 AGENTS.md 有 iu 常驻规则，免费 agent 在沙箱里连不上网，只会加噪声。

用法（Mac，一次只跑一个会话）:
  python3 bench/codex_sprint.py OUT.jsonl --sprints S1 --arms D,K150 --reps 2 [--model gpt-6-astra] [--effort medium]
"""

from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_sprint as ab  # noqa: E402

CODEX = os.path.expanduser("~/.local/bin/codex")
SESSIONS = os.path.expanduser("~/.codex/sessions")
PROMPT = ab.ONE_SHOT + " Work directly with your own tools; do not call iu or run_iu.py for this task."
TIMEOUT = 2400


def arm_limit(arm: str):
    return None if arm == "D" else int(arm[1:]) * 1000


def rollout_stats(thread_id: str) -> dict:
    files = glob.glob(os.path.join(SESSIONS, "**", f"rollout-*{thread_id}*.jsonl"), recursive=True)
    st = {"requests": 0, "in": 0, "cached": 0, "out": 0, "reasoning": 0, "max_ctx": 0, "compactions": 0, "eq": 0.0,
          "rollout": files[0] if files else None}
    seen = set()
    for path in files:
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"token_usage_record"' not in line and '"compacted"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "compacted":
                st["compactions"] += 1
                continue
            p = d.get("payload") or {}
            if d.get("type") != "token_usage_record" or p.get("response_id") in seen:
                continue
            seen.add(p.get("response_id"))
            u = p.get("usage") or {}
            inp, cached, out = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0, u.get("output_tokens") or 0
            st["requests"] += 1
            st["in"] += inp
            st["cached"] += cached
            st["out"] += out
            st["reasoning"] += u.get("reasoning_output_tokens") or 0
            st["max_ctx"] = max(st["max_ctx"], inp)
            st["eq"] += (inp - cached) + 0.1 * cached + 8 * out
    st["eq"] = round(st["eq"])
    return st


def run_codex(d: str, arm: str, log: str, model: str, effort: str) -> tuple:
    opts = ["--json", "--skip-git-repo-check", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
            "-s", "workspace-write"]
    lim = arm_limit(arm)
    if lim:
        opts += ["-c", f"model_auto_compact_token_limit={lim}"]
    cmd = [CODEX, "exec", *opts, "-C", d, PROMPT]
    timed_out = False
    with open(log, "wb") as f:
        p = subprocess.Popen(cmd, cwd=d, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, env=ab.ENV,
                             start_new_session=True)
        try:
            p.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
        try:  # 整个进程组都杀掉（codex 起的 vitest / node 子进程）
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    thread, usage = None, {}
    for line in open(log, "rb"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "thread.started":
            thread = e.get("thread_id")
        if e.get("type") == "turn.completed":
            for k, v in (e.get("usage") or {}).items():
                usage[k] = usage.get(k, 0) + (v or 0)
    return thread, usage, timed_out


def run_one(sprint, arm, rep, tasks, out_path, model, effort):
    if not ab.wait_for_memory():
        print(f"!! 跳过 {sprint} r{rep} {arm}：可用内存不够", flush=True)
        return None
    ab.ROOT.setdefault(arm, f"/tmp/iu_cdx{arm}")
    d = ab.prepare(sprint, arm, rep, tasks)
    os.makedirs(ab.TRANSCRIPTS, exist_ok=True)
    log = os.path.join(ab.TRANSCRIPTS, f"codex_{sprint}_r{rep}_{arm}.jsonl")
    t0 = time.time()
    thread, usage, timed_out = run_codex(d, arm, log, model, effort)
    wall = round(time.time() - t0, 1)
    ab.kill_orphans(d)
    g = ab.grade(sprint, d)
    st = rollout_stats(thread) if thread else {}
    rec = {"host": "codex", "sprint": sprint, "arm": arm, "rep": rep, "model": model, "effort": effort,
           "compact_limit": arm_limit(arm), "wall_s": wall, "timed_out": timed_out, **g, "thread": thread,
           "exec_usage": usage, **{k: v for k, v in st.items() if k != "rollout"}, "rollout": st.get("rollout")}
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{sprint} r{rep} {arm}: success={rec['success']} tests={g['tests_pass']} answers={g['answers_ok']} "
          f"eq={st.get('eq', 0):,} requests={st.get('requests')} max_ctx={st.get('max_ctx', 0):,} "
          f"compactions={st.get('compactions')} wall={wall}s timeout={timed_out}", flush=True)
    ab.kill_orphans(d)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return rec


def main():
    tasks = {t["id"]: t for t in json.load(open(ab.TASKS, encoding="utf-8"))}
    out_path = sys.argv[1]

    def opt(name, default):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

    sprints = opt("--sprints", "S1").split(",")
    arms = opt("--arms", "D,K150").split(",")
    reps = int(opt("--reps", "2"))
    rep_from = int(opt("--rep-from", "1"))
    model, effort = opt("--model", "gpt-6-astra"), opt("--effort", "medium")
    for rep in range(rep_from, reps + 1):  # 交替跑：D、K、D、K……，时间上的波动两组平摊
        for sprint in sprints:
            for arm in arms:
                try:
                    run_one(sprint, arm, rep, tasks, out_path, model, effort)
                except Exception as e:  # noqa: BLE001
                    print("run failed:", repr(e), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
