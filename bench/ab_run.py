"""阶段 B：真实任务配对 A/B。A 组不开 hook，B 组开 iu 的 PreToolUse hook。同一任务同一次重复的 A、B 同时跑。

每次运行：
  1. APFS 克隆干净副本到独立目录，注入变异，提交进 git（agent 用 git diff 看不到改了哪）
  2. 覆盖仓库自带的 .claude/settings.local.json：A 组为空；B 组只有 iu hook（带 || true）
  3. claude -p（Sonnet，无头），记录 stream-json
  4. 评分：整个 lib 单元测试全过，且测试文件没被改
记录：付费 token（逐条回复去重相加）、API 等价花费、轮数、耗时、Bash 命令、重复命令次数、读原始输出文件次数、hook 改写次数

用法（Mac）: python3 ab_run.py /tmp/iu_tasks.json /tmp/iu_ab_results.jsonl [--repeats 3] [--pairs 2] [--only t01_x,...]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CLEAN = "/tmp/flbench_clean"
ROOT_A, ROOT_B = "/tmp/iu_abA", "/tmp/iu_abB"
TRANSCRIPTS = "/tmp/iu_ab_transcripts"
IU = os.path.expanduser("~/InfiniteUsage/run_iu.py")
PILOT = os.path.expanduser("~/.infiniteusage/pilot_projects.txt")
HOOK_LOG = os.path.expanduser("~/.infiniteusage/hook.jsonl")
ENV = {**os.environ, "PATH": os.path.expanduser("~/.local/bin") + ":/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:"
       + os.environ["PATH"], "NO_COLOR": "1", "CI": "1"}
PROMPT = ("Some unit tests in the server package of this repository are failing. Run the lib unit tests "
          "(cd server && npx vitest run src/__tests__/lib) to see what fails, find the root cause in the source code "
          "under server/src/lib, and fix it. Do not modify any test files. When you are done, reply with one sentence "
          "describing the fix.")
MODEL = "sonnet"
lock = threading.Lock()


def sh(cmd, cwd, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, env=ENV, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def prepare(task, arm, rep):
    d = os.path.join(ROOT_A if arm == "A" else ROOT_B, f"{task['id']}_r{rep}")
    shutil.rmtree(d, ignore_errors=True)
    subprocess.run(["cp", "-Rc", CLEAN, d], check=True)
    path = os.path.join(d, task["file"])
    lines = open(path, encoding="utf-8").read().split("\n")
    assert lines[task["line"] - 1] == task["old"], (task["id"], "源码和任务不一致")
    lines[task["line"] - 1] = task["new"]
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    sh("git add -A server/src/lib && git -c user.email=bench@local -c user.name=bench commit -qm 'chore: snapshot'", d)
    settings = {}
    if arm == "B":
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"/usr/bin/python3 {IU} hook-pre || true", "timeout": 10}]}]}}
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    json.dump(settings, open(os.path.join(d, ".claude", "settings.local.json"), "w"))
    return d


def parse(transcript):
    usage, seen, bash, reads_raw, result = {}, set(), [], 0, {}
    for line in open(transcript, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant":
            msg = d.get("message") or {}
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    if c.get("name") == "Bash":
                        bash.append(inp.get("command", ""))
                    if c.get("name") == "Read" and ".infiniteusage/raw" in str(inp.get("file_path", "")):
                        reads_raw += 1
            u, mid = msg.get("usage"), msg.get("id")
            if u and mid not in seen:
                seen.add(mid)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                    usage[k] = usage.get(k, 0) + (u.get(k) or 0)
        elif d.get("type") == "result":
            result = d
    eq = (usage.get("input_tokens", 0) + 2.0 * usage.get("cache_creation_input_tokens", 0)
          + 0.1 * usage.get("cache_read_input_tokens", 0) + 5.0 * usage.get("output_tokens", 0))
    repeats = len(bash) - len(set(bash))
    return {"usage": usage, "eq_tokens": round(eq), "cost_usd": result.get("total_cost_usd"),
            "turns": result.get("num_turns"), "duration_s": round((result.get("duration_ms") or 0) / 1000, 1),
            "is_error": result.get("is_error"), "subtype": result.get("subtype"), "bash_cmds": bash,
            "repeat_cmds": repeats, "reads_raw": reads_raw, "final": (result.get("result") or "")[:400]}


def run_one(task, arm, rep, out_path):
    d = prepare(task, arm, rep)
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    tr = os.path.join(TRANSCRIPTS, f"{task['id']}_r{rep}_{arm}.jsonl")
    t0 = time.time()
    with open(tr, "w") as f:
        try:
            subprocess.run(["claude", "-p", PROMPT, "--model", MODEL, "--output-format", "stream-json", "--verbose",
                            "--allowedTools=Bash,Read,Edit,Write,Glob,Grep", "--max-turns", "60"],
                           cwd=d, stdout=f, stderr=subprocess.DEVNULL, env=ENV, timeout=1800)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    wall = round(time.time() - t0, 1)
    code, out = sh("cd server && npx vitest run src/__tests__/lib", d, timeout=600)
    _, touched = sh("git status --porcelain -- server/src/__tests__", d)
    _, diff = sh("git diff --stat", d)
    rec = {"task": task["id"], "arm": arm, "rep": rep, "wall_s": wall, "timed_out": timed_out,
           "tests_pass": code == 0, "tests_touched": bool(touched.strip()), "diff": diff.strip()[-400:]}
    rec["success"] = rec["tests_pass"] and not rec["tests_touched"]
    rec.update(parse(tr))
    if arm == "B":
        try:
            rec["hook_rewrites"] = sum(1 for ln in open(HOOK_LOG, encoding="utf-8") if d.replace("/tmp", "/private/tmp")
                                       in ln or d in ln)
        except OSError:
            rec["hook_rewrites"] = 0
    with lock:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{task['id']} r{rep} {arm}: success={rec['success']} eq={rec['eq_tokens']:,} cost=${rec['cost_usd']} "
              f"turns={rec['turns']} wall={wall}s rewrites={rec.get('hook_rewrites', '-')}", flush=True)
    shutil.rmtree(d, ignore_errors=True)
    return rec


def main():
    tasks_path, out_path = sys.argv[1], sys.argv[2]
    reps = int(sys.argv[sys.argv.index("--repeats") + 1]) if "--repeats" in sys.argv else 3
    pairs = int(sys.argv[sys.argv.index("--pairs") + 1]) if "--pairs" in sys.argv else 2
    tasks = json.load(open(tasks_path, encoding="utf-8"))
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
        tasks = [t for t in tasks if t["id"] in only]
    os.makedirs(ROOT_A, exist_ok=True)
    os.makedirs(ROOT_B, exist_ok=True)
    os.makedirs(os.path.dirname(PILOT), exist_ok=True)
    open(PILOT, "w").write(ROOT_B + "\n")  # 只有 B 组目录在试点名单里
    jobs = [(t, rep) for rep in range(1, reps + 1) for t in tasks]
    try:
        with ThreadPoolExecutor(max_workers=2 * pairs) as ex:
            futs = []
            for t, rep in jobs:  # A、B 成对提交，同时开跑
                futs.append(ex.submit(run_one, t, "A", rep, out_path))
                futs.append(ex.submit(run_one, t, "B", rep, out_path))
            for f in futs:
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    print("run failed:", e, flush=True)
    finally:
        open(PILOT, "w").write("")  # 跑完清空试点名单
    print("done")


if __name__ == "__main__":
    main()
